# Branch notes: design/single-executable

## 1. Summary
Sentinel can now be delivered as one folder with one program, `Sentinel.exe`, that anyone can
double-click: the first time, it asks the installing technician a few questions (operators only
see "not set up yet, contact technical support"); afterwards it starts the recorder in the
background if it is not running and shows the recorder's real, measured status in large letters.
FFmpeg is carried inside the folder, so nothing else has to be installed. The build is
repeatable (two clean builds on one machine produced identical program files; only the build
manifest's timestamp differed) and was tested by recording from the
simulated camera with no other FFmpeg on the computer; a Linux version was built and tested the
same way under WSL.

## 2. Requirements addressed
| ID | How | Stated requirement or design choice |
|---|---|---|
| U9 | One-folder PyInstaller build, `Sentinel.exe`, bundled FFmpeg, Linux one-folder + `.desktop` launcher | Requirement; packaging tool and layout are **my design choice** (docs/08 §2) |
| U1, U7 | Operator screens show only the existing banner and fixed codes; new codes C-002 (not set up) and C-003 (recorder could not be started) | Requirement (codes/wording are my choice, in `alerts.py` CATALOG) |
| U5 | Setup questions and stop/diagnostic commands are technician-only (C-002 screen requires typing SETUP) | Requirement; mechanism is my choice |
| U6 | Launcher never stops recording; closing the window has no effect (recorder is a separate windowless process) | Requirement |
| U8 | Single-instance lock reused; graceful stop path reused. **Not met by this branch alone:** no start at boot (service branch) | Requirement (partially) |
| R6 | Stream path is asked from the camera documentation, never guessed | Requirement |
| E2/E4 | Build manifest with SHA-256 of every shipped file, `about --verify` | Design choice |
| — | Bundled-tool / per-machine default resolution, config lookup order | Task item 1; rules are my design choice |

## 3. What changed
* `src/sentinel/paths.py` (new): install layout, bundled-tool resolution, per-machine/per-user
  defaults, configuration lookup order.
* `src/sentinel/config.py`: `load_config_dict` fills omitted `[tools]`/`[storage]` keys from
  `paths`; `[storage]` section no longer mandatory. Validation unchanged.
* `src/sentinel/service.py`: `run_recorder(config, before_run=None)` factored out of `_cmd_run`
  (behaviour identical) so a host can attach a stop channel; start log line names the FFmpeg used.
* `src/sentinel/alerts.py`: C-002, C-003.
* `src/sentinel/launcher/` (new): `cli.py` (Sentinel.exe commands), `setup.py` (first-run setup,
  TOML writer, credentials file with icacls/0600), `control.py` (background process control,
  `CONTROL_PROVIDERS` plug-in list), `stopsignal.py` (Windows named event / POSIX SIGTERM),
  `view.py` (console status view, `VIEW_PROVIDERS` plug-in list).
* `packaging/`: `build.py` (build driver, FFmpeg inspection, licence class, manifest),
  `spec_common.py`, `entry_sentinel.py`, `windows/sentinel.spec`, `windows/build.ps1`,
  `windows/READ-ME-FIRST.txt`, `linux/sentinel.spec`, `linux/build.sh`, `linux/sentinel.desktop`,
  `linux/install-desktop-entry.sh`, `linux/READ-ME-FIRST.txt`.
* `requirements/build.txt` (new, hash-pinned PyInstaller 6.22.3 and deps); `.gitignore`
  (build venvs).
* Tests: `tests/unit/test_paths_tools.py`, `tests/unit/test_launcher.py`,
  `tests/integration/test_launcher_process.py` (source by default; the built program with
  `SENTINEL_FROZEN_EXE`).
* Docs: `docs/08_PACKAGING.md` (new), README, `config/sentinel.example.toml` comment.
* QA review fixes: `src/sentinel/platform/winacl.py` (new: private DACLs at creation, SDDL
  evaluation), `src/sentinel/install_check.py` (new: refuse changeable locations),
  `Platform.write_access_problems` (Windows/POSIX), absolute tool resolution and lookup order in
  `paths.py`/`config.py` (relative `credentials_file` now resolved against the configuration
  folder), safe working directory and location check in `service.run_recorder` and
  `_cmd_check`, non-intrusive `recorder_alive` in `stopsignal.py`; tests
  `tests/unit/test_winacl.py` and new cases in `test_launcher.py` / `test_paths_tools.py`.

## 4. Tests run (development host, Windows 11, Python 3.14.3, FFmpeg 9.0.1)
| Command | Result |
|---|---|
| `.venv\Scripts\python.exe -m pytest tests/unit -q` (baseline before changes) | 50 passed |
| `.venv\Scripts\python.exe -m pytest tests/unit -q -p no:cacheprovider` | **103 passed** |
| `.venv\Scripts\python.exe -m pytest tests -q -p no:cacheprovider` (unit + all integration, from source) | **132 passed, 2 failed** (358 s). Failures: (a) `test_recorder_faults.py::test_short_freeze_is_recorded_as_internal_gap` — passed when re-run alone (timing-sensitive; other agents were loading the machine); (b) `test_service_process.py::test_hard_kill_restart_recovery_and_graceful_stop` — `sqlite3.OperationalError: disk I/O error` reading the ledger right after the hard kill; **fails identically on an unmodified copy of `main`** (`git archive main`, same venv), so it is pre-existing on this host today, not caused by this branch. Not investigated further (outside scope). |
| Re-run of the two failures on this branch | freeze test **passed**; hard-kill test failed again (same error as on `main`) |
| `pytest tests/integration/test_launcher_process.py -s` (from source) | **1 passed** (11.5 s, 5 segments sealed) |
| `SENTINEL_FROZEN_EXE=dist\Sentinel\Sentinel.exe SENTINEL_LAUNCHER_RUN_S=30 pytest tests/integration/test_launcher_process.py -s` (built program, PATH stripped of every FFmpeg directory) | **1 passed** (32.4 s): 15 segments FINALIZED, every FFmpeg command line used the bundled `dist\Sentinel\ffmpeg\ffmpeg.exe`, graceful stop, final segment FINALIZED, status STOPPED |
| Manual, built program, PATH without FFmpeg: `launch --once` (no config → C-002), `setup --simulated-camera …` with piped answers, `check-config`, `launch --once`, `status`, second `launch` (no second recorder), `stop` | All as expected; `stop` 2.2 s; 1 segment FINALIZED (20.3 s) |
| `packaging\windows\build.ps1` twice from a deleted venv/build/dist on this one machine (commit 2439529) | Both built (13 s); all bundled files identical (635/635 hashes) except `BUILD-MANIFEST.json`, which differs by its build timestamp. Not repeated after the review fixes; not tested across machines. |
| `Sentinel.exe about --verify` | "installed files match the build manifest" |
| WSL Fedora 43: `packaging/linux/build.sh --ffmpeg-dir /usr/bin` from a clean clone | Built in 9 s (66 files, 24.4 MB) |
| WSL: built `sentinel` with `PATH=/nonexistent`: `about`, `about --verify`, `launch --once`, 28 s recording, `status`, `stop` | 15 segments FINALIZED with the bundled FFmpeg; SIGTERM stop; status STOPPED |
| WSL: `pytest tests/unit -q` (pytest 8.3.4 from requirements/dev.txt) | **102 passed, 1 skipped** (Windows-only named-event test) |
| **After the QA review fixes (commit d26c67e):** `pytest tests/unit -q -p no:cacheprovider` | **114 passed** |
| After fixes: `pytest tests/integration/test_launcher_process.py tests/integration/test_service_process.py::test_credentials_never_leak` | **2 passed** |
| After fixes: `pytest tests/integration/test_service_process.py` | **2 passed** — including the hard-kill test that had failed twice on this branch and twice on unmodified `main` earlier the same day; it is intermittent on this host, not deterministic |
| After fixes: rebuild + frozen smoke test (`SENTINEL_FROZEN_EXE`, 30 s, PATH without FFmpeg) | **1 passed** (32.3 s), 15 segments FINALIZED, bundled FFmpeg, `launch --once` 1.45 s |
| After fixes, built program, manual: setup with credentials under a stand-in `%ProgramData%`; `icacls` on the folder, `spool`, `camera.secret`; hidden bundled `ffprobe.exe`; configuration in a folder granting Users Modify | Only setup account/SYSTEM/Administrators, no inherited entries; missing ffprobe → C-001 exit 2 and "FFprobe not found (<bundled path>)"; Users-writable folder → C-001 exit 2, "configuration file/folder … can be changed by Users"; `about --verify` OK afterwards |
| After fixes, WSL Fedora 43: clean clone, build, frozen `launch`/12 s/`stop`, Linux unit suite | Built; 7 segments FINALIZED, bundled FFmpeg, stop via verified PID; **112 passed, 2 skipped** (Windows-only) |
| Full `pytest tests` after the fixes | **NOT re-run** (only unit + the integration files touched by the change) |
| **After the LGPL-policy change (commit d0bdd43):** `pytest tests/unit -q -p no:cacheprovider` | **126 passed** (12 new in `test_build_licence.py`) |
| After the policy change: `SENTINEL_TEST_TOOLS_DIR=<gyan bin> pytest tests/integration/test_launcher_process.py tests/integration/test_service_process.py::test_credentials_never_leak` (checks the new test hook) | **2 passed** |
| `build.ps1 -FfmpegDir <gyan bin> -InspectOnly` | Refused (exit 1): "this FFmpeg is GPL-3.0-or-later (flags: --enable-frei0r --enable-gpl … --enable-libx264 …). Sentinel ships an LGPL FFmpeg only …" |
| `build.ps1 -FfmpegDir <gyan bin> -AllowGplInternalUse`, then `Sentinel.exe about` | Built (13 s); manifest starts with `ffmpeg_license_status: GPL FFMPEG - INTERNAL USE ONLY …`; `about` shows it between rows of `!` with "must not be given to anyone outside the organisation"; required components all present |

Measured: output folder 465.9 MB (FFmpeg 444.3 MB of it, Python part 18.2 MiB); startup of
`Sentinel.exe about` 0.32 s warm, 1.29 s first run after build (one 7.4 s outlier, assumed AV
scan); double-click to recorder confirmed 0.74–0.75 s.

## 5. NOT executed / not verified
* Double-clicking `Sentinel.exe` from Explorer and looking at the window (interactive console
  loop, screen clearing, title, "Press Enter to close") — exercised only through `--once` and
  unit tests of the renderer. The interactive `SETUP` prompt path on the C-002 screen was not run
  (setup itself was run with piped answers; the hidden-password `getpass` path was not run).
* Real RTSP camera; credentials file path from setup used by a real recording (the private
  creation was executed on temp folders and in the built program under a stand-in `%ProgramData%`;
  the real `C:\ProgramData` was never written).
* Another Windows account starting Sentinel after setup (expected to fail to read the restricted
  credentials file → C-001; documented, not tested).
* Code signing, SmartScreen/Mark-of-the-Web behaviour, antivirus products other than the
  machine's default.
* Running from `C:\Program Files` (read-only for standard users) — expected fine (nothing is
  written to the program folder), not tested.
* An LGPL FFmpeg build (recommended option) — not obtained, not tested.
* Linux: systemd unit with the frozen program, desktop session `.desktop` double-click,
  `install-desktop-entry.sh`, a static FFmpeg build, the integration suite (Fedora's FFmpeg has
  no libx264 for the simulator), Python 3.12. AppImage: recipe described only.
* Python 3.12 anywhere (only 3.14.3 on Windows and 3.14.5 on WSL).

## 6. Risks, limitations, open decisions for the owner
1. **FFmpeg licence — decided: LGPL (owner, 2026-11-10).** `packaging/build.py` now refuses GPL
   and nonfree FFmpeg (override `--allow-gpl-internal-use`, recorded in the manifest and shown by
   `Sentinel about`). **No LGPL build has been obtained yet** (download awaits the owner's
   approval); everything was tested with the local GPL build only. Before any distribution:
   obtain an LGPL build (docs/08 §8.4), verify it and re-run the whole suite with it (§8.5). The
   LGPL/nonfree `-L` texts used in the detector tests are synthetic until then.
2. **Interim background mode is not suitable for unattended deployment:** the recorder stops when
   the user logs off and at every restart, depends on that user being logged on, and does not
   start by itself (U8). The Windows service branch is required for unattended use. The
   credentials file and data folders are private to the setup account.
3. **Size:** 466 MB, almost entirely the two static FFmpeg executables (222 MB each); an LGPL
   shared build would be much smaller (not measured).
4. The built program contains the build machine's Python (3.14.3); pin the release interpreter.
5. `Sentinel.exe stop` is a technician command reachable by the same account; the Windows
   named event is not accessible to other standard users. Operators are not told about it.
6. The banner after a deliberate stop reads "STOPPED / No problems detected" (existing
   `service.banner` wording) — the GUI branch may want different wording.
7. Intermittent, also on `main`: `test_hard_kill_restart_recovery_and_graceful_stop` failed
   ("disk I/O error" reading the ledger right after the hard kill) twice on this branch and twice
   on unmodified `main`, then passed after the review fixes. Worth a separate look (WAL/shm
   files after a hard kill).
8. New refusal rules can stop a hand-made installation: configurations, credentials, FFmpeg or
   data folders that standard users can change are refused (C-001), e.g. folders made directly
   under `%ProgramData%` without setup, or FFmpeg in `C:\ffmpeg\bin`. Intended, but technicians
   must know (docs/08 §3).

## 7. QA review findings
| # | Finding | Status |
|---|---|---|
| 4.1 HIGH | Secret written with inherited `%ProgramData%` ACL, then icacls'd; failure only a warning | **Fixed.** `CreateFileW` with a protected DACL under a temp name, DACL read back and verified, renamed, verified again (Linux `O_EXCL` 0600). Any failure aborts setup and deletes the plaintext; configuration written only afterwards. icacls/whoami removed. Tests: `test_secret_creation_is_private_from_the_start`, `test_secret_never_left_behind_when_it_cannot_be_made_private`, `test_write_setup_creates_config_and_private_secret`. |
| 4.3 MED | Evidence/data folders inherit Users RX + create | **Fixed for setup-created installs.** Configuration and spool/data/log folders created private (`CreateDirectoryW` + protected DACL; Linux 0750); existing folders re-protected only if empty or Sentinel-owned. Recorder and launcher refuse folders others can change. **Remaining:** read access for operators other than the setup account is not granted (interim mode is single-account); the windows-service branch owns operator read access to status. |
| 4.4 MED | Silent PATH fallback; relative tools; user-writable cwd; config precedence; configs in non-admin-writable places | **Fixed.** Frozen build always uses the bundled location, missing ⇒ C-001; all tool paths absolute; own PATH search skips cwd/relative entries; launcher starts the recorder in `System32` (or `/`) and `run_recorder` changes directory there too; machine config outranks portable file and `$SENTINEL_CONFIG`; `install_check.location_problems` refuses configuration, credentials, tools and data folders writable by broad groups (recorder, launcher, `check-config`). Tests: `test_paths_tools.py`, `test_winacl.py`, `test_install_check_flags_broadly_writable_locations`, `test_launcher_refuses_insecure_configuration`. **Remaining:** Windows check covers well-known broad groups only, not arbitrary other accounts or groups granted write. Linux: group-writable allowed only for group root. |
| 4.5 MED | `is_running` takes the lock | **Fixed.** Windows: stop-event existence; Linux: PID from `recorder.lock` verified alive and recorder via `/proc/<pid>/cmdline`. Test: `test_is_running_never_touches_the_instance_lock`. How the launcher must defer to an installed service is in docs/08 §5 and `control.py`. **Remaining:** a recorder started without the launcher host is not seen; harmless (lock). Without `/proc`, PID reuse is not ruled out. |
| 4.2 | Interim mode limits | **Documented** (docs/08 §5, `control.py`, risks above). |
| 4.6 LOW | Reproducibility wording | **Fixed** in docs/08 and these notes. |

## 8. Owner decision: LGPL FFmpeg
* Enforced in `packaging/build.py`: licence from `ffmpeg -L`/`ffprobe -L` cross-checked with the
  configure flags (incl. GPL-only libraries); disagreement/unknown ⇒ refused; nonfree ⇒ always
  refused; GPL ⇒ refused unless `--allow-gpl-internal-use`. Required native components checked.
  `--inspect-only` vets a candidate. Tests use captured outputs of the local GPL build
  (`tests/fixtures/ffmpeg/`) and synthetic LGPL/nonfree lines.
* docs/08 §8 lists what Sentinel needs (all FFmpeg-native, LGPL), which LGPL build to obtain
  (own build from the signed ffmpeg.org release tarball, or a BtbN `win64-lgpl`/`linux64-lgpl`
  release build), how to verify it and that the whole suite must be re-run with it
  (`SENTINEL_TEST_TOOLS_DIR`, new test hook in `tests/conftest.py`).
* **Not done:** no LGPL FFmpeg downloaded (needs the owner's approval); nothing tested with one.

## 9. Recommendation
**Merge** the code (the policy is enforced and nothing GPL can be built by accident); **do not
distribute any build until an LGPL FFmpeg has been obtained, bundled and the whole test suite
re-run with it** (docs/08 §8.5). The launcher, resolution
rules and build are tested on Windows and partially on Linux; the service and GUI branches plug
in through the two provider lists without changing the packaging approach. 
