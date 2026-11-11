# Branch `design/drive-prepare` — automatic drive detection and one-button "Erase and Set Up Drive"

## 1. Summary (for the project owner)
When a drive is plugged in, Sentinel can now tell what kind of drive it is: already set up, empty,
wrong format, holding someone else's files, holding Sentinel recordings, or not usable at all. It
shows a plain sentence for each. For empty or wrongly formatted drives it offers **Erase and Set Up
Drive** (docs/04 calls this "Prepare This Drive"; it was renamed so operators cannot confuse it with
**Prepare Review Drive**). That button erases the drive and sets it up correctly after one
confirmation. For drives holding other files it demands an administrator and the last 4 characters
of the drive's serial number. The computer's own drive, the drive holding the recordings, and any
drive that holds or held Sentinel recordings can never be erased by this tool. **No drive was
actually formatted while building this**: the erase step was tested only by generating, parsing and
checking its commands, not by running them on a disk. **By owner decision the erase button is
switched OFF** (`[drives] erase_enabled = false`) until a supervised test in the real dock has been
passed (§9). Until then Sentinel still detects and describes drives and can safely remove them;
pressing erase only shows "Setting up drives is not enabled on this system yet. Contact technical
support."

## 2. Requirements addressed
| ID | What this branch does | Requirement or design choice |
|---|---|---|
| S2 | Automatic classification READY / BLANK / WRONG_FORMAT / FOREIGN_DATA / SENTINEL_EVIDENCE / UNSUITABLE; one-button erase-and-set-up | Stated (owner) |
| S3 | Never automatic; confirmation bound to the exact disk; stricter authorisation for drives with data; policy `never` possible; Sentinel evidence never erasable | Stated (brief) |
| U4 | `safe_remove()` (flush, eject, confirm the disk is gone, then "SAFE TO REMOVE DRIVE"); operator wording kept distinct from "Prepare Review Drive" | Stated |
| U7 | New operator codes M-001…M-009 with fixed wording | Stated (style); codes are my choice |
| U9 | Windows first: built-in Storage cmdlets, UAC, cfgmgr32; Linux implemented (not executed) | Stated |
| E4, E6 | Drives holding Sentinel recordings (marker, layout, or known to the ledger) are never erased | Stated (preservation / retention); detection rules are my design |
| E5 | Audit events in the ledger: authorised / refused / failed / prepared, with identity and who | Stated |
| C1, C2, C4 (docs/02) | Configurable approved filesystem (default NTFS); identity uses hardware + content identifiers; cache-bypassing read-back test | Risks; handling is my design |

Design choices (all mine, all reversible by configuration or small code changes):
* **Mechanism:** the elevated helper is a separate Windows PowerShell started through UAC
  (`Start-Process -Verb RunAs -Wait -PassThru`). The script is passed with `-EncodedCommand`, so there is no
  script file another user could tamper with. The recorder never runs elevated. Linux uses `pkexec`.
* **Triple check (TOCTOU):** guards run when erasing is requested, again immediately before the
  elevated helper is started, and a third time *inside* the elevated helper right before
  `Clear-Disk`. That third check happens after the UAC prompt is approved, and the person may sit on
  that prompt for minutes. The hardware identity is re-checked again before Initialize-Disk and New-Partition.
* **Identity:** the hardware part is serial (normalised), UniqueId/WWN, model, size and bus. The
  content part is the disk GUID (GPT) **or MBR disk signature**, partition GUIDs, **partition
  offsets+sizes**, volume GUIDs and **filesystem serial numbers**. All of this goes into a SHA-256
  fingerprint. The confirmation carries the fingerprint, not just the disk number. Disk numbers, device nodes
  and drive letters are locators only. If a content identifier the disk should have is missing,
  erasing is refused (`identity_incomplete`).
* **Protected disks by identity:** the OS (`%SystemRoot%`, `/`, `/boot`…), the Python runtime and
  Sentinel code, the spool, the ledger (data dir) and logs are resolved to volume GUIDs
  (`GetVolumePathNameW` + `GetVolumeNameForVolumeMountPointW`) or to Linux `maj:min` (including LVM/LUKS
  stacked devices). Windows IsSystem/IsBoot and page-file flags are also used. **Fail closed:** if any
  of these cannot be resolved or is found on no disk, every drive is UNSUITABLE with M-008. Erasing
  also requires that the configuration is the **installed recorder's** (see §8, 1.3).
* **Sentinel evidence is never erasable** (§8, 1.1).
* **Uncertainty is never "empty":** an unrecognised filesystem, an unmounted volume on Linux, an
  unreadable root, or EFI/recovery/unknown partitions all classify as FOREIGN_DATA (the stricter path).
  Only OS-created root entries (System Volume Information, $RECYCLE.BIN, lost+found…) are ignored.
* **Only dock drives by default:** `allowed_bus_types = ["USB"]`. Minimum size 128 GB.
  Drives without serial and UniqueId are UNSUITABLE.
* **Drive letter after formatting** (`Add-PartitionAccessPath -AssignDriveLetter`). This way Explorer
  cannot pop up "You need to format the disk" in the middle of the setup.
* **Label** is `SENTINEL_<n>`, validated against filesystem label limits.
* **Administrator** (interim answer to D4): the OS account is in BUILTIN\Administrators
  (S-1-5-32-544, also when UAC-filtered) and `--admin` was given. On Linux: root or group `sentinel-admin`.
* **Alert prefix `M-`** ("media"), because `D-` is already the recording database. M-001 empty, M-002
  wrong format, M-003 other files, M-004 cannot be used, M-005 setup failed, M-006 drive changed before
  it could be erased (nothing erased), M-007 could not be safely removed, M-008 drive checks
  unavailable, M-009 holds Sentinel recordings (never erased), M-010 setting up drives is not enabled
  yet. None of these stop recording.
* **`drives` table not added.** Identity is recorded in `events` (`category='drives'`) instead. A
  `drives` table created by the export branch is *read* if present (recognised column names only).

## 3. What changed
New package `src/sentinel/drives/`: `model.py`, `windows_enum.py`, `linux_enum.py`, `protected.py`,
`classify.py`, `policy.py`, `commands.py`, `prepare.py`, `marker.py`, `eject.py`, `cli.py`,
`__main__.py`, `powershell.py`, `errors.py`. Added after review: `known.py` (drives known to the
ledger) and `installed.py` (installed-configuration check).
Changed outside the package:
* `src/sentinel/alerts.py`: M-001…M-010 added. Nothing else changed.
* `config/sentinel.example.toml`: documented `[drives]` section (`erase_enabled = false`).
* `src/sentinel/config.py` (review 1.5, +14/−1 lines): accepts and validates a `[drives]` section
  through `sentinel.drives.policy` and exposes `AppConfig.drives`.
Tests: `tests/unit/test_drives_{classify,prepare,commands,cli_eject,review_fixes}.py` and
`tests/drive_fakes.py`. Fixtures in `tests/fixtures/drives/`:
* `windows_dev_host.json`: REAL, this machine; re-captured with Signature and volume SerialNumber.
* `linux_wsl_lsblk.json`: REAL, WSL2 Fedora VM.
* `linux_usb_constructed.json` and `windows_mbr_usb_constructed.json`: constructed.
Not changed: `platform/windows.py`, `platform/posix.py`, `db.py`, `src/sentinel/export/`.

## 4. Tests run (development host, Windows 11 Home 10.0.26200, Python 3.14, non-elevated)
* `"C:\Users\silva\Documents\Proc 6 Build\.venv\Scripts\python.exe" -m pytest tests/unit -q -p no:cacheprovider`
  → **227 passed** (50 pre-existing + 177 new, 12 of them for the erase gate), 0 failed, 0 skipped
  (after the erase gate was added, 2026-11-10).
* `... -m pytest tests/integration -q -p no:cacheprovider` (run because `config.py` changed)
  → **28 passed, 2 failed** (364 s). The failures were `test_recorder_faults.py::test_short_freeze_is_recorded_as_internal_gap`
  and `::test_corrupted_stream_data_does_not_stop_recording`. **These are timing-sensitive and also
  fail on the unchanged main baseline (b39fe1b) on this machine.**
  * Whole `test_recorder_faults.py` on this branch: 9 passed, 2 failed (the same two).
  * The same file on main in a temporary worktree, run straight after: **8 passed, 3 failed**
    (`test_continuous_recording…`, `test_camera_offline…`, `test_short_freeze…`).
  * In isolation the corrupted-stream test passed 6 of 8 runs on this branch and 6 of 6 on main.
    The freeze test passed when re-run alone.
  * The host was under load from parallel sessions. This branch changes no recorder code; `config.py`
    only adds a validated `drives` field.

  I conclude the failures are environmental, but the integration suite should be re-run on an idle
  machine before merging.
* What the tests cover:
  * The real fixture's system disk is UNSUITABLE, even with its OS flags stripped.
  * The spool disk is excluded by volume GUID even with its drive letter removed.
  * Every class and every UNSUITABLE reason, plus fail-closed protection.
  * Refusal in every guard case: no confirmation, no named person, system disk, spool disk, READY,
    missing admin, wrong serial, policy `never`, swap after confirmation, identity changed between
    guard and execute, files added, disk removed.
  * Helper outcomes, including timeout.
  * Evidence layouts under all three policies.
  * Ledger-known drives, including ones that now look blank.
  * MBR / dock-serial swap.
  * Missing identifiers.
  * Installed-config checks.
  * Wording.
  * `[drives]` in config.py.
  * The generated PowerShell is **parsed by the real PowerShell parser**, and every cmdlet and
    parameter is checked with `Get-Command`. The helper's pure `Same-Set` function is evaluated in
    real PowerShell. The Linux script passes `sh -n`.
* Manual, read-only: `python -m sentinel.drives list --config <tmp config with [drives]>` loads the
  configuration through `config.py` and shows all 7 disks of this machine UNSUITABLE. (During the
  review round the tool-permission system blocked running `prepare` against a real disk number even as a refusal
  check, so that path is covered only by unit tests. In the first round, `prepare --disk 2` was
  refused with "system_disk".)

## 5. NOT executed / not verified
* **No destructive command was run on any disk.** No disposable VHDX was possible either: it needs
  admin rights and New-VHD is not installed.
* The UAC launch, the result/progress file written by the elevated process, and writing the marker
  to a fresh NTFS root as a non-elevated user are all NOT VERIFIED.
* `CM_Request_Device_EjectW`, Linux (pkexec, udisksctl, `O_DIRECT`, findmnt), and the mkfs options
  are NOT EXECUTED.
* Terminating a timed-out elevated helper: `terminate_process` was exercised only on non-helper
  processes (its image-name guard refuses them). Its expected failure against a real elevated
  helper (integrity policy) is NOT VERIFIED.
* The installed-configuration path is `%ProgramData%\Sentinel\config\sentinel.toml`, agreed at
  integration with packaging and the Windows service (`sentinel.paths`; corrected by review
  2026-11-10, this document earlier gave `%ProgramData%\Sentinel\sentinel.toml`, which no release used).
* Real USB docks: the serial/UniqueId seen through the Sabrent dock (C2) and the MBR Signature value
  through a bridge are NOT VERIFIED. Constructed fixtures use invented values.
* Python 3.12 not tested.

## 6. Risks, limitations, open decisions for the owner
* **DECIDED (owner, 2026-11-10):** FOREIGN_DATA erasing is `admin_only`; the approved filesystem is
  NTFS (C1). Both are the defaults. The erase path stays OFF (`erase_enabled = false`) until the §9
  dock test is passed.
* **OPEN (owner, new): recycling old Sentinel drives.** A drive the ledger knows, or one showing any
  Sentinel marker or layout, can never be erased by this tool. Reusing old archive/review drives
  after retention needs a separate documented procedure, e.g. technical support with a retention
  record.
* **Conflict to resolve at merge:**
  * docs/02 C1 says "Drives are never formatted by the application", and docs/01 §2.5 says reformat
    is never offered. Both should be reworded to "only through Erase and Set Up Drive with these guards".
  * `config.py` was changed (small, but other branches may touch the same section list).
  * The installer/packaging branch must install the config at the path in `installed.py`.
* C2: a dock that reports its own serial weakens the serial-tail confirmation. The operator would
  type the dock's serial. Content identifiers now catch look-alike swaps, but two *blank* drives
  of the same model in such a dock remain indistinguishable. Erasing the wrong blank drive loses nothing.
* Detecting evidence on a drive this computer cannot read (e.g. NTFS seen from Linux while unmounted)
  relies on the ledger. A Sentinel drive written by a *different* appliance whose filesystem is
  unreadable here would classify as FOREIGN_DATA. Hence the admin_only default.
* Residual TOCTOU of milliseconds between the in-helper check and `Clear-Disk`.
* The GUI (Phase 4) must show `Classification.message` and collect the confirmation. Under a Windows
  service (session 0), UAC cannot be shown, so erasing must start from the interactive session.

## 7. Recommendation
**Merge after** these steps. The erase path ships switched OFF (owner decision), so merging does not
expose untested erasing; enable it only after the §9 test.
1. The owner decides drive recycling. FOREIGN_DATA policy and C1 were decided on 2026-11-10.
2. The installed config path is agreed with packaging.
3. The docs/01 and docs/02 statements are updated.
4. The integration suite is re-run on an idle machine.

The §9 dock test is required to *enable* erasing, not to merge.

Read-only detection and classification can be merged now.

## 8. Independent QA review findings (round 2)
| # | Finding | What I changed | Tests | Remains |
|---|---|---|---|---|
| 1.1 HIGH | Prior Sentinel evidence drive (marker missing, corrupt, newer, `.partial`, on a non-approved filesystem or second partition; export or archive layout) was FOREIGN_DATA and therefore erasable | New class **SENTINEL_EVIDENCE** (M-009, message "This drive holds Sentinel recordings and will not be erased. Contact technical support."). It is never erasable under any policy and is refused in `check_guards` independently of the classification. Detection: any marker state; `Sentinel/` directory; `Manifests/Export_Manifest.json` or `SHA256SUMS.txt`; `README_FIRST.txt` with Recordings/Originals/Manifests/Reports; or ≥2 of those folders. Also any drive_uuid, volume GUID, filesystem serial, UniqueId or (serial, model, size) known from `drive_prepared` audit events or an export-branch `drives` table. The ledger is re-read before every erase. An unreadable ledger is UNSUITABLE (M-008). READY still wins for a valid single-partition drive. | `test_drives_review_fixes.py`: 8 layouts × 3 policies; marker on second partition; forged confirmation never reaches the helper; ledger-known drive that now looks blank; known volume/filesystem on another dock; `drives` table; READY stays READY; broken ledger | Evidence on an *unreadable* volume written by another appliance is detected only via the ledger (see §6). The layout signatures must be kept in sync with the export branch's final layout. |
| 1.2 HIGH | Post-UAC re-check weak for MBR disks and docks reporting their own serial | Enumeration now captures the MBR disk signature, partition offsets, and Win32_Volume serial numbers (verified non-admin on this host; real fixture re-captured). Identity, fingerprint and the elevated helper compare partition style, disk GUID, MBR signature, partition GUIDs, offset:size layout, volume GUIDs and filesystem serials. Lists are compared as sets, with no culture-dependent sorting. A missing identifier refuses (`identity_incomplete`) at planning, in `check_guards`, and by mismatch inside the helper. The Linux helper also compares PARTUUIDs and filesystem UUIDs. | MBR fixture with two same-model drives behind a bridge serial "000000000000"; swap refused; every identifier present in the helper; 4 missing-identifier cases; Same-Set evaluated in real PowerShell; Linux checks | MBR Signature / volume serials through the real dock NOT VERIFIED |
| 1.3 MED | Protected disks taken from any `--config` | `installed.py`: erasing requires all of the following. (1) The config is the installed path (`%ProgramData%\Sentinel\config\sentinel.toml` on Windows, `/etc/sentinel/sentinel.toml` on Linux). (2) The spool and data folders exist (no "nearest parent" guess). (3) `<data>/sentinel.db` is a Sentinel ledger. (4) `<data>/recorder.lock` exists, i.e. the recorder has run with this configuration. Otherwise M-008, refused. Dry-run still prints the script. | accepted case; 3 missing-item cases; copy/None config refused; CLI refusal with M-008 | It cannot prove the recorder is running *now*. The path choice needs agreement with packaging. |
| 1.4 MED | "Prepare This Drive" collides with "Prepare Review Drive" | Every operator-visible text now says **Erase and Set Up Drive**, "set up", "erase". This covers M-001/M-002/M-003/M-005/M-006, classification messages and CLI messages. S-001 ("Prepare a review drive") is untouched. Internal identifiers (`prepare_drive`, `drive_prepared` events, CLI subcommand `prepare`) are unchanged, for audit continuity and to match the task's CLI spec. | test that no operator text contains "prepar" | GUI wording (Phase 4) must follow |
| 1.5 MED | config.py rejects `[drives]` | `config.py` accepts `drives`, validates it via `load_policy` (DrivePolicyError → ConfigError), and adds `AppConfig.drives`. The CLI uses `load_config` directly. | valid/invalid/absent section; other unknown sections still rejected; example config loads | Merge-conflict risk with other branches editing `config.py` |
| 1.6 LOW | Elevated helper keeps running after timeout | The helper writes step and PID to its result file at every step. On timeout, the runner reads it and tries `terminate_process(pid)`. That function checks the image name first, so a reused PID is never killed. The runner reports `still_running` honestly. `prepare_drive` then raises "Drive setup is taking too long and may still be running. Do not unplug the drive." and audits `erase_started: true`, even if last seen at "verify". On Linux, `Popen` + kill and PermissionError are reported the same way. | runner timeout → still_running; orchestration message and audit; terminate refuses non-helper and nonexistent PIDs | A non-elevated parent is expected to be unable to stop an elevated child (mandatory integrity). This is reported, not solved, and NOT VERIFIED against a real elevated helper. |

## 9. Erase gate (owner decision 2026-11-10): how to enable after the dock test

**What the gate does.** `[drives] erase_enabled` defaults to `false`. While it is false:
* Detection, classification (all six classes and their reasons) and `list`/`classify` work unchanged.
* `safe_remove` / `eject` work unchanged.
* For BLANK / WRONG_FORMAT / FOREIGN_DATA drives the operator sees the first sentence of the normal
  message plus "Setting up drives is not enabled on this system yet. Contact technical support."
  (alert **M-010**). No erase button is offered (`can_prepare = false`, refusal `erase_disabled`).
* `check_guards` refuses first, before any other check. So `prepare_drive` never starts the elevated
  helper and writes no audit event, even with a valid confirmation.
* The CLI `prepare` refuses with M-010 before doing anything else. `prepare --dry-run` still prints the
  script that *would* run, so the administrator can review it before the test.

**Dock test checklist.** Supervised, with an administrator present, on the real appliance with the
real Sabrent dock, using only **scratch drives that hold nothing of value**. Record each result
(pass/fail, screenshots, `list --json` output) in the test log required by E7.
1. Preconditions:
   * The recorder is installed and has run, and its configuration is at the installed path
     `%ProgramData%\Sentinel\config\sentinel.toml` (§8, 1.3).
   * Run every `python -m sentinel.drives` command from an **elevated** Command Prompt ("Run as
     administrator"). With the Windows service installed, the data folder (ledger, `recorder.lock`)
     is readable only by SYSTEM, Administrators and the service. A non-elevated run cannot read the
     ledger: `list` then reports "ledger: ... cannot be read by this account" and blocks erasing
     (fail closed), and `prepare` refuses with M-008 asking for an elevated run (review 2026-11-10).
     The elevated helper is still a separate UAC-started process.
   * `python -m sentinel.drives list --config <installed>` shows the system disk and the
     recording disk as UNSUITABLE. Check that both show the right reasons.
2. Identity through the dock (C2):
   * For each scratch drive, `classify --json` shows a serial, model and size matching the drive's label.
   * Note whether the serial is the disk's or the dock's.
   * Note that the content identifiers are present: `identity_gaps` is empty and the refusal is not
     `identity_incomplete`.
3. Temporarily enable: in the installed config set `[drives] erase_enabled = true`.
4. Run these cases on scratch drives:
   * BLANK drive (new or wiped): erase and set up, approve UAC. Expect "Drive erased and set up:
     SENTINEL_n (ntfs)" with "cache bypassed", then `list` shows READY.
   * WRONG_FORMAT drive (empty exFAT): erase with a single ERASE confirmation, as above.
   * FOREIGN_DATA drive (scratch files): erase without `--admin`, expect refusal; with `--admin` and
     a wrong serial tail, expect refusal; with the correct tail, expect success.
   * **Swap during the UAC prompt:** start erasing drive A. While the UAC prompt is shown, replace A
     with drive B of the same model if available. Approve UAC. Expect M-006 "The drive changed…
     Nothing was erased", and check that B's files are intact.
   * **Decline UAC:** expect "Administrator approval was not given. Nothing was erased."
   * **Evidence protection:** insert a review drive produced by the export feature, and an archive
     drive whose `SENTINEL_DRIVE.json` was deleted or renamed. Both must show M-009, with no erase possible.
   * **Safe removal:** eject a READY drive. Expect "SAFE TO REMOVE DRIVE" and the drive disappears.
     Repeat with a file open on it; expect the "still in use" refusal.
   * **Pull during setup:** unplug a scratch drive during formatting. Expect M-005, an audited
     failure, and the drive classifying as FOREIGN_DATA afterwards (never READY).
   * **Audit:** check the ledger `events` (`category='drives'`) for authorised / refused / failed /
     prepared rows with the right person and disk identity.
   * On an exported/archived drive Sentinel set up, check that a non-elevated user could write
     `SENTINEL_DRIVE.json` (if not, M-005 "could not be written to" occurs and must be fixed first).
5. Decide:
   * If every case passes, leave `erase_enabled = true` and file the test log.
   * If any case fails, set it back to `false` and report the failure.
   * Enabling is a configuration change by an administrator, recorded in the configuration-change
     log (E5). The operator interface has no switch for it.
