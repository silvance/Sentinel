# Branch `design/drive-prepare` — automatic drive detection and one-button preparation

## 1. Summary (for the project owner)
When a drive is plugged in, Sentinel can now tell what kind of drive it is — already prepared,
empty, wrong format, holding someone else's files, or not usable at all — and shows a plain
sentence for each. For empty or wrongly formatted drives it offers **Prepare This Drive**, which
erases the drive and sets it up correctly after one confirmation; for drives holding other files it
demands an administrator and the last 4 characters of the drive's serial number. The computer's
own drive and the drive holding the recordings can never be selected. **No drive was actually
formatted while building this**: the erase step was tested only by generating and checking its
commands, not by running them on a disk.

## 2. Requirements addressed
| ID | What this branch does | Requirement or design choice |
|---|---|---|
| S2 | Automatic classification READY / BLANK / WRONG_FORMAT / FOREIGN_DATA / UNSUITABLE; one-button prepare | Stated (owner) |
| S3 | Never automatic; confirmation bound to the exact disk; stricter authorisation for drives with data; policy `never` possible | Stated (brief) |
| U4 | `safe_remove()` (flush, eject, confirm the disk is gone, then "SAFE TO REMOVE DRIVE") | Stated |
| U7 | New operator codes M-001…M-008 with fixed wording | Stated (style); codes are my choice |
| U9 | Windows first: built-in Storage cmdlets, UAC, cfgmgr32; Linux implemented (not executed) | Stated |
| E5 | Audit events in the ledger: authorised / refused / failed / prepared, with identity and who | Stated |
| C1, C2, C4 (docs/02) | Configurable approved filesystem (default NTFS); identity uses serial+UniqueId+model+size+GUIDs; cache-bypassing read-back test | Risks; handling is my design |

Design choices (all mine, all reversible by configuration or small code changes):
* **Mechanism:** elevated helper = a separate Windows PowerShell started through UAC
  (`Start-Process -Verb RunAs -Wait -PassThru`), script passed with `-EncodedCommand` so there is no
  script file another user could tamper with. The recorder never runs elevated. Linux: `pkexec`.
* **Triple check (TOCTOU):** guards run when Prepare is requested, again immediately before the
  elevated helper is started, and a third time *inside* the elevated helper right before
  `Clear-Disk` (after the UAC prompt was approved — the person may sit on that prompt for minutes).
  It re-checks again before Initialize-Disk and New-Partition.
* **Identity:** serial (normalised), UniqueId/WWN, model, size, bus, disk GUID, partition GUIDs,
  volume GUIDs → SHA-256 fingerprint. The confirmation carries the fingerprint, not just the disk
  number. Disk numbers, device nodes and drive letters are locators only.
* **Protected disks by identity:** the OS (`%SystemRoot%`, `/`, `/boot`…), the Python runtime and
  Sentinel code, the spool, the ledger (data dir) and logs are resolved to volume GUIDs
  (`GetVolumePathNameW` + `GetVolumeNameForVolumeMountPointW`) or Linux `maj:min` (including LVM/LUKS
  stacked devices), plus Windows IsSystem/IsBoot and page-file flags. **Fail closed:** if any of these
  cannot be resolved or is found on no disk (network share, Storage Spaces, SUBST…), every drive is
  UNSUITABLE with M-008.
* **Uncertainty is never "empty":** unrecognised filesystem (e.g. ext4/HFS+ seen from Windows),
  unmounted volume on Linux, unreadable root, EFI/recovery/unknown partitions → FOREIGN_DATA (the
  stricter path). Only OS-created root entries (System Volume Information, $RECYCLE.BIN, lost+found…)
  are ignored.
* **Only dock drives by default:** `allowed_bus_types = ["USB"]`; internal SATA/NVMe disks are
  UNSUITABLE unless an administrator lists their bus. Minimum size 128 GB (≈1 day at 12 Mbit/s).
* **Drives without serial and UniqueId are UNSUITABLE** (cannot be re-identified safely).
* **Drive letter after formatting** (`Add-PartitionAccessPath -AssignDriveLetter`) instead of
  `New-Partition -AssignDriveLetter`, so Explorer cannot pop up "You need to format the disk" during
  preparation. Sentinel itself uses the volume GUID path.
* **Label** `SENTINEL_<n>`, n = one more than any number in the ledger or on attached drives;
  validated against filesystem label limits (exFAT 11 characters → n ≤ 99).
* **Administrator** (interim answer to D4): the OS account is a member of BUILTIN\Administrators
  (SID S-1-5-32-544, also when UAC-filtered) and the CLI was given `--admin`; Linux: root or group
  `sentinel-admin`. Real application roles are Phase 4.
* **Alert prefix `M-`** ("media"): `D-` is already the recording database (D-001). M-001 empty,
  M-002 wrong format, M-003 other files, M-004 cannot be used, M-005 preparation failed, M-006 drive
  changed before preparing (nothing erased), M-007 could not be safely removed, M-008 drive checks
  unavailable. None stop recording (R3: recording never writes to removable drives).
* **`drives` table not added:** identity is recorded in `events` (`category='drives'`,
  `severity='audit'`) to avoid colliding with the export branch's ledger changes. The export branch
  should add the `drives` table of docs/04 §5 and can backfill from these events.

## 3. What changed
New package `src/sentinel/drives/`:
* `model.py` — DiskInfo/PartitionInfo/VolumeInfo, DriveIdentity + fingerprint, normalisation.
* `windows_enum.py` — read-only PowerShell enumeration script and parser; volume-GUID lookup by path.
* `linux_enum.py` — `lsblk --json` parser and runner; `maj:min` lookup by path (findmnt fallback).
* `protected.py` — protected roles → identities; fail-closed completeness check.
* `classify.py` — classification and operator messages.
* `policy.py` — `[drives]` policy (approved_filesystem, foreign_data_authorization, allowed_bus_types,
  min_size_gb, label_prefix, serial_confirm_chars) with validation.
* `commands.py` — generated Windows PowerShell and Linux sh scripts, with independent guards.
* `prepare.py` — guards, confirmation, orchestration, UAC/pkexec runners, audit events, admin check.
* `marker.py` — `SENTINEL_DRIVE.json`, root inspection, uncached write/read-back test.
* `eject.py` — `safe_remove()` (Windows cfgmgr32; Linux udisksctl).
* `cli.py`, `__main__.py` — `python -m sentinel.drives list|classify|prepare|eject|snapshot`.
* `powershell.py`, `errors.py` — helpers, structured errors carrying M- codes.

Changed: `src/sentinel/alerts.py` (M-001…M-008 added; nothing else changed).
Tests: `tests/unit/test_drives_{classify,prepare,commands,cli_eject}.py`, `tests/drive_fakes.py`,
fixtures `tests/fixtures/drives/windows_dev_host.json` (REAL, this machine),
`linux_wsl_lsblk.json` (REAL, WSL2 Fedora VM), `linux_usb_constructed.json` (constructed).
Not changed: `platform/windows.py`, `platform/posix.py`, `config.py`, `db.py`, `src/sentinel/export/`.

## 4. Tests run (development host, Windows 11 Home 10.0.26200, Python 3.14, non-elevated)
* `"C:\Users\silva\Documents\Proc 6 Build\.venv\Scripts\python.exe" -m pytest tests/unit -q -p no:cacheprovider`
  → **163 passed** (50 pre-existing + 113 new), 0 failed, 0 skipped.
* `... -m pytest tests/integration/test_unattended.py -q -p no:cacheprovider` (alert catalogue
  consumer) → **7 passed** in 121 s. Other integration files were not run: this branch changes no
  recorder code, only adds alert-catalogue entries.
* What the new tests cover: the system disk of the real fixture is UNSUITABLE (also with its OS
  flags stripped — volume identity alone suffices); spool disk excluded by volume GUID with the drive
  letter removed; every class and UNSUITABLE reason; fail-closed protection; all three FOREIGN_DATA
  policies; refusal without confirmation, without a named person, for system disk, spool disk, READY
  drive, missing admin, wrong serial (5 variants), policy `never`; disk swapped after confirmation;
  **identity changed between the guard and the execute step** (helper never started); files added
  after confirmation; disk removed; helper exit codes (identity mismatch, protected, UAC declined,
  failure before/after erasing); wrong layout after helper (no marker written); marker never
  overwritten; end-to-end success with a fake helper and a temp directory as the "new volume"
  (marker, write test, audit, next label). The generated PowerShell is **parsed by the real
  PowerShell parser** and every cmdlet and every named parameter is checked with `Get-Command` on this
  machine (no execution). Generated Linux script passes `sh -n` (Git for Windows sh). CLI: refuses
  non-interactively without `--yes` *and* the correct `--serial-tail`; ERASE must be typed; EOF is not
  consent; `--admin` verified against the OS; dry-run prints the script. Safe removal logic with a fake
  device tree (two-bay dock refused, veto reported, never "safe" while the disk is still present).
* Manual, read-only, on this machine: `python -m sentinel.drives list --config <tmp>` (all 7 disks
  UNSUITABLE: disk 2 = system disk with page file; others internal), `prepare --disk 2 … --yes
  --serial-tail 0001 --admin` → refused "system_disk", exit 2; `prepare --disk 0 --dry-run` → refused.
* Verified on this machine (non-elevated, read-only/non-destructive): Get-Disk/Get-Partition/Get-Volume/
  Win32_Volume/Win32_DiskDrive return full data; volume-GUID lookup; cfgmgr32 bindings (device chain
  of all 7 disks); `Write-VolumeCache` by letter and by volume GUID path on C: (flush only) works
  without admin; unbuffered (`FILE_FLAG_NO_BUFFERING`) re-read of a temp file; parameters of
  Clear-Disk, Initialize-Disk (PartitionStyle Unknown/MBR/GPT), New-Partition (no -Confirm),
  Format-Volume (FileSystem FAT/FAT32/exFAT/NTFS/ReFS), Add-PartitionAccessPath, Start-Process.

## 5. NOT executed / not verified
* **No destructive command was run on any disk** (Clear-Disk, Initialize-Disk, New-Partition,
  Format-Volume, Add-PartitionAccessPath, wipefs, sfdisk, mkfs). A disposable VHDX test was not
  possible: creating/attaching one needs administrator rights (diskpart / Mount-DiskImage) and
  Hyper-V's New-VHD is not installed; the session is not elevated.
* The UAC launch (`WindowsUacRunner.run`) was not executed; that Start-Process RunAs returns the
  elevated exit code and that the elevated process can write the result file to the user's temp/data
  directory (including over-the-shoulder elevation by a different admin account) is NOT VERIFIED.
* That a non-elevated user can write `SENTINEL_DRIVE.json` to the root of a freshly formatted NTFS
  volume (default root ACL) is NOT VERIFIED. If not, the result is M-005 "could not be written to".
* `CM_Request_Device_EjectW` (actual eject) NOT EXECUTED — no removable disk attached; whether it
  needs admin is NOT VERIFIED (expected not, as for Explorer's Safely Remove Hardware).
* Linux: everything NOT EXECUTED on a Linux appliance (lsblk parsing tested on fixtures only;
  pkexec runner, udisksctl mount/unmount/power-off, `O_DIRECT` read-back, findmnt fallback).
  mkfs.ntfs/mkfs.exfat/mkfs.ext4 options NOT VERIFIED (tools absent in the available VM); wipefs and
  sfdisk options checked with `--help` (util-linux 2.41.4).
* Real USB docks: serial/UniqueId reported through the Sabrent dock (C2), BusType value, removable
  flag and device-tree shape are NOT VERIFIED. The constructed Linux fixture and the synthetic USB
  disks in tests are invented values.
* Health: only Windows' HealthStatus is used; SMART (`Get-StorageReliabilityCounter`, smartctl) needs
  admin/root and is not read. The optional "thorough check" surface scan (docs/04 §4) is not built.
* Python 3.12 not tested (dev host has 3.14); code avoids 3.13+ features as far as I know.

## 6. Risks, limitations, open decisions for the owner
* **OPEN (owner): who may prepare a FOREIGN_DATA drive** — `admin_only` (default, recommended),
  `operator_with_serial`, or `never`. Configurable in `[drives] foreign_data_authorization`.
* **OPEN (C1): approved filesystem** — default NTFS, configurable (`ntfs`/`exfat`/`ext4`).
* **Conflict to resolve at merge:** docs/02 C1 says "Drives are never formatted by the application";
  the owner's S2 request supersedes it. docs/02 and docs/01 §2.5 ("reformat is never offered") should
  be updated to "only through Prepare This Drive with the guards above".
* **`config.py` integration needed:** `sentinel.config` rejects unknown sections, so the drives CLI
  strips `[drives]` before loading; `load_config` (service) would reject a file containing it until
  `drives` is added to the allowed sections. I did not edit `config.py` to avoid conflicts.
* C2: a dock that reports its own serial makes the serial-tail confirmation weaker (the operator
  would type the dock's serial). Identity still includes model, size and GUIDs. Test with the dock.
* Residual TOCTOU: between the final in-helper check and `Clear-Disk` there are milliseconds; files
  added to a confirmed FOREIGN drive in the seconds between the last Python check and the helper are
  not re-detected (partition GUIDs are). Acceptable in my judgement; noted.
* A failure after erasing started leaves a partly prepared drive (M-005, audited); it will then
  classify as FOREIGN_DATA (unreadable/raw partition) and needs the stricter path to re-prepare.
* Two identical-looking drives connected at once → preparation stops with "ambiguous".
* The operator GUI (Phase 4) must show `Classification.message`, `describe()`, and collect the
  confirmation; only the CLI exists now. Under a Windows *service* (session 0) UAC cannot be shown —
  preparation must be started from the interactive session (GUI/CLI), not from the service.

## 7. Recommendation
**Merge after** (1) the owner decides the FOREIGN_DATA policy and C1 (defaults are safe meanwhile),
(2) `config.py` accepts `[drives]`, and (3) one supervised hardware test on a scratch drive in the
real Sabrent dock with an administrator present: `prepare` on a BLANK and a FOREIGN_DATA drive,
`eject`, and swap-during-UAC-prompt (expect M-006). Until (3) the destructive path should be treated
as untested. The read-only detection and classification can be merged now.
