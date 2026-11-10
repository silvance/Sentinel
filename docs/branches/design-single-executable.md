# Branch notes: design/single-executable

## 1. Summary
Sentinel can now be delivered as one folder with one program, `Sentinel.exe`, that anyone can
double-click: the first time, it asks the installing technician a few questions (operators only
see "not set up yet, contact technical support"); afterwards it starts the recorder in the
background if it is not running and shows the recorder's real, measured status in large letters.
FFmpeg is carried inside the folder, so nothing else has to be installed. The build is
repeatable (two clean builds were identical file for file) and was tested by recording from the
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
| `packaging\windows\build.ps1` twice from a deleted venv/build/dist (commit 2439529; later commits change only docs, README and the example config, none of which is bundled) | Both built (13 s); 635/635 file hashes identical |
| `Sentinel.exe about --verify` | "installed files match the build manifest" |
| WSL Fedora 43: `packaging/linux/build.sh --ffmpeg-dir /usr/bin` from a clean clone | Built in 9 s (66 files, 24.4 MB) |
| WSL: built `sentinel` with `PATH=/nonexistent`: `about`, `about --verify`, `launch --once`, 28 s recording, `status`, `stop` | 15 segments FINALIZED with the bundled FFmpeg; SIGTERM stop; status STOPPED |
| WSL: `pytest tests/unit -q` (pytest 8.3.4 from requirements/dev.txt) | **102 passed, 1 skipped** (Windows-only named-event test) |

Measured: output folder 465.9 MB (FFmpeg 444.3 MB of it, Python part 18.2 MiB); startup of
`Sentinel.exe about` 0.32 s warm, 1.29 s first run after build (one 7.4 s outlier, assumed AV
scan); double-click to recorder confirmed 0.74–0.75 s.

## 5. NOT executed / not verified
* Double-clicking `Sentinel.exe` from Explorer and looking at the window (interactive console
  loop, screen clearing, title, "Press Enter to close") — exercised only through `--once` and
  unit tests of the renderer. The interactive `SETUP` prompt path on the C-002 screen was not run
  (setup itself was run with piped answers; the hidden-password `getpass` path was not run).
* Real RTSP camera; credentials file path from setup used by a real recording (unit-tested only;
  the icacls restriction was executed on a temp file).
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
1. **FFmpeg licence (open decision).** The bundled test FFmpeg is a GPL-3.0 build; distributing
   the folder requires providing its complete corresponding source (incl. ~67 compiled-in
   libraries). Recommendation: switch to an LGPL build and re-run the suite with it; alternatives
   in docs/08 §8. Not decided here.
2. **No automatic start** until the Windows service branch lands: after a reboot, someone has to
   open Sentinel (U8). The background recorder runs under the account that started it and the
   credentials file is restricted to the setup account.
3. **Size:** 466 MB, almost entirely the two static FFmpeg executables (222 MB each); an LGPL
   shared build would be much smaller (not measured).
4. The built program contains the build machine's Python (3.14.3); pin the release interpreter.
5. `Sentinel.exe stop` is a technician command reachable by the same account; the Windows
   named event is not accessible to other standard users. Operators are not told about it.
6. The banner after a deliberate stop reads "STOPPED / No problems detected" (existing
   `service.banner` wording) — the GUI branch may want different wording.
7. Pre-existing on `main` (not this branch): `test_hard_kill_restart_recovery_and_graceful_stop`
   currently fails on this host with "disk I/O error" when the test reads the ledger right after
   killing the recorder. Worth a separate look (WAL/shared-memory file after a hard kill).

## 7. Recommendation
**Merge after the owner decides the FFmpeg distribution policy** (the code does not depend on
the choice: any FFmpeg folder can be passed to the build script). The launcher, resolution
rules and build are tested on Windows and partially on Linux; the service and GUI branches plug
in through the two provider lists without changing the packaging approach. Do not distribute
builds containing the current GPL FFmpeg until that decision is made.
