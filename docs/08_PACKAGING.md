# 08 — Packaging: one click-to-run program (U9)

Status: implemented on branch `design/single-executable`; Windows build **built and tested on the
development host**; Linux build **built and tested under WSL (Fedora 43)**, not on a real Linux
desktop. Nothing here claims forensic validation.

Requirement U9 (owner): *"a single executable. You can toss all dependencies in a folder
alongside it, but it needs to be incredibly user friendly in the 'just click this one program'
area."* Fully Windows-compatible; Linux needs a very simple executable option.

## 1. What the operator gets

```
Sentinel\                      copy this whole folder (e.g. to C:\Program Files\Sentinel)
    Sentinel.exe               <- the one program anyone starts (double-click)
    READ-ME-FIRST.txt          plain-language instructions
    _internal\                 Python runtime + Sentinel code (PyInstaller one-folder)
    ffmpeg\ffmpeg.exe          bundled FFmpeg / FFprobe, used before anything on PATH
    ffmpeg\ffprobe.exe
    ffmpeg\LICENSE.txt ...     FFmpeg licence text, build readme, FFMPEG-LICENCE-NOTE.txt
    BUILD-MANIFEST.json        what was built, from which commit, with SHA-256 of every file
    SHA256SUMS.txt             the same hashes in `sha256sum -c` format
```

Double-clicking `Sentinel.exe`:

1. **Not set up yet** → the screen shows `Error C-002: This computer has not been set up for
   recording yet. Setup must be done by the installing technician. Contact technical support.`
   Only someone who types `SETUP` continues to the technician questions (§4).
2. **Set up, recorder not running** → starts the recorder as a background process with no
   window (§5) and waits until the recorder's own status heartbeat confirms it.
3. **Always** → shows the recorder's *measured* status as the large banner already used by the
   `status` command (`RECORDING NORMALLY`, or the error code and fixed message), refreshed every
   2 s, with "Recording continues in the background when this window is closed."

The launcher never stops recording, and closing its window has no effect on the recorder (U6).

## 2. Packaging options compared (design choice)

| Option | Startup | Antivirus / SmartScreen | Reproducible / offline | Code signing | Fit with a Windows service | Verdict |
|---|---|---|---|---|---|---|
| **PyInstaller one-folder** (chosen) | Measured 0.32 s warm, 1.3 s first run after build | No self-extraction, no UPX; ordinary exe + DLLs. Lowest false-positive profile of the PyInstaller modes | Two clean builds were bit-identical (§7); builds offline from a wheel folder | Sign `Sentinel.exe` (and optionally every PE in `_internal\`) in place | Same exe can be the service host; one process, real PID | **Chosen** |
| PyInstaller one-file | Unpacks ~20 MB (plus 444 MB FFmpeg if bundled inside) to `%TEMP%` on **every** start | Self-extracting bootloaders are a well-known AV heuristic trigger; temp-dir writes of executables | Same as above | Signing covers the outer file only; extracted DLLs unsigned | Two processes (bootloader parent + child): signals, job objects, PIDs and the single-instance lock become indirect; temp folder cleanup on hard kill | Rejected |
| Nuitka standalone/one-file | Faster Python start (irrelevant: the recorder is I/O bound and runs for days) | Compiled binaries have also drawn AV false positives; one-file mode has the same extraction issue | Needs a C compiler toolchain (MSVC or a downloaded MinGW) on the build machine; longer, compiler-dependent builds | Fine | Fine in standalone mode | Rejected: heavier toolchain, no benefit for this workload |
| Embeddable Python zip + native launcher | Fast | Clean (signed python.exe from python.org) | Very transparent | Needs our own signed launcher stub (C) | Fine | Viable fallback; rejected for now because it needs a compiled launcher and hand-maintained dependency copying that PyInstaller already does |
| MSI/installer only | — | — | — | — | — | Complementary, not an alternative: an installer can later wrap the one-folder output (Program Files + service registration). Not built here. |

Why the folder is acceptable: U9 explicitly allows dependencies "in a folder alongside it". The
operator still sees and starts exactly one program.

## 3. Resolution rules (`src/sentinel/paths.py`, unit-tested in `tests/unit/test_paths_tools.py`)

**FFmpeg / FFprobe.** `[tools] ffmpeg/ffprobe` in the configuration always win. When not set, a
*frozen* build uses `<app>\ffmpeg\ffmpeg.exe` / `ffprobe.exe` (`<app>/ffmpeg/ffmpeg` on Linux) if
the file exists; otherwise the bare name and the OS searches PATH (the behaviour of a source
checkout, which never looks for a bundled copy).

**Configuration file**, first that exists:
1. `--config FILE` (returned even if missing, so the named file is reported as missing);
2. `%SENTINEL_CONFIG%` / `$SENTINEL_CONFIG` (same rule);
3. `<app>\sentinel.toml` — frozen builds only: a folder a technician pre-configured ("portable");
4. per-machine file (table below);
5. Linux without root only: the per-user file.

**Default locations** (used by first-run setup, and when `[storage]` keys are omitted):

| Platform | Configuration and camera credentials | Recordings (spool), ledger + status (data), logs |
|---|---|---|
| Windows | `%ProgramData%\Sentinel\sentinel.toml`, `...\camera.secret` | `%ProgramData%\Sentinel\spool`, `\data`, `\logs` |
| Linux as root (systemd) | `/etc/sentinel/sentinel.toml`, `/etc/sentinel/camera.secret` | `/var/lib/sentinel/spool`, `/var/lib/sentinel/data`, `/var/log/sentinel` |
| Linux as a user | `$XDG_CONFIG_HOME/sentinel/` (default `~/.config/sentinel/`) | `$XDG_DATA_HOME/sentinel/spool|data|logs` (default `~/.local/share/sentinel/`) |

`Sentinel.exe about` prints the resolved folders, tools and configuration file. First-run setup
always writes the storage folders explicitly into the configuration, so the evidence location
is visible in the file and in diagnostic reports.

Edit to `config.py`: only `load_config_dict` changed — omitted `[tools]`/`[storage]` keys are
filled from the rules above; the `[storage]` section is no longer mandatory. All validation is
unchanged.

## 4. First-run setup (technician)

`Sentinel.exe setup` (or `SETUP` on the C-002 screen) asks only what the installing technician
knows: camera IP address or host name; the camera's stream path *from its documentation* (never
guessed — docs/02 risk A1); the stream port (default 554); the camera account user name and
password (hidden input); a camera label; the display time zone (IANA name, validated); the folder
for recordings (default `%ProgramData%\Sentinel`). Each answer is validated and re-asked when
wrong; the complete configuration is then validated by `sentinel.config` before anything is
written. Everything else keeps the tested defaults (5-minute segments, audio excluded, …).

* An existing configuration is never overwritten silently: `setup --force` moves it to
  `sentinel.toml.<UTC>.bak` first (the recorder also audits configuration changes).
* Credentials go to `camera.secret` next to the configuration, never into it. On Windows the
  file's ACL is reduced with `icacls` to the setup account, SYSTEM and Administrators (inheritance
  removed — verified by `tests/unit/test_launcher.py`); on Linux it is `0600`. **Interim:** the
  recorder then has to run under that same account (run setup while logged in as the account
  that will use Sentinel). The Windows-service branch replaces this file with DPAPI storage.
* `setup --simulated-camera tcp://127.0.0.1:8554` (hidden, developer only) writes a
  `development_source = true` configuration; every status then shows Z-001.

## 5. Recorder process, single instance, stop (`src/sentinel/launcher/control.py`, `stopsignal.py`)

* The launcher runs `Sentinel.exe recorder run --config <file>` as a separate process:
  Windows `CREATE_NO_WINDOW | CREATE_NEW_PROCESS_GROUP | CREATE_BREAKAWAY_FROM_JOB` (retried
  without breakaway when a job forbids it), all standard handles to NUL, working directory =
  data folder; Linux `start_new_session=True`. A hidden console (not `DETACHED_PROCESS`) is used
  on purpose: FFmpeg/FFprobe children inherit it and never flash console windows.
* **Single instance** is the recorder's existing instance lock (`data\recorder.lock`); a second
  recorder exits with code 3. The launcher probes that lock before starting, never while a
  recorder may be starting (a probe holds the lock for an instant), and serialises concurrent
  launchers with `data\launcher.lock`. Start is confirmed by a status heartbeat newer than the
  spawn; an early exit maps to C-001 (configuration/tools) or C-003 (anything else).
* **Graceful stop** (technician: `Sentinel.exe stop`): Windows — a named event
  `Local\SentinelRecorderStop-<hash of data folder>` created by the recorder with its token's
  default security (same account, SYSTEM, Administrators); it exists only while a recorder
  listens, so a stop can never be left armed. Linux — SIGTERM to the PID in `recorder.lock`, sent
  only while that lock is actually held (a free lock means a stale PID). Both end in the
  existing graceful path: the open segment is closed and sealed as FINALIZED.
* **Limitations of this interim mode** (replaced by the service branch): no start at boot or
  logon — after a restart someone must open Sentinel (U8 risk); runs under the account that
  opened it; nobody restarts it if the recorder process itself exits (the recorder already
  retries the camera forever internally).

### Plug-in points for the other branches (no packaging change needed)
* **Windows service** (design/windows-service): append a provider to
  `sentinel.launcher.control.CONTROL_PROVIDERS` returning a control whose `is_running/start/stop`
  use the Service Control Manager when the service is installed. The service host runs the same
  `Sentinel.exe` (e.g. a new `service` sub-command); `service.run_recorder(config, before_run=…)`
  lets it attach the SCM stop callback exactly as the launcher attaches its stop channel.
* **Operator GUI** (req/operator-gui): append a provider to `sentinel.launcher.view.VIEW_PROVIDERS`
  returning an object with `run(status_path, camera_name, started_after, once, start_problem)`.
  PyInstaller follows the import and its PySide6 hooks collect Qt automatically; expect the
  folder to grow by roughly the size of the Qt modules used. If the GUI must not show a console,
  build a second, windowed EXE in the same `COLLECT` (spec change only, same approach).

## 6. Building (Windows)

```powershell
powershell -ExecutionPolicy Bypass -File packaging\windows\build.ps1 -FfmpegDir <folder with ffmpeg.exe and ffprobe.exe>
```

* Creates `.venv-build` and installs `requirements/build.txt` with `--require-hashes --no-deps`
  (PyInstaller 6.22.3, hooks-contrib 2026.10, altgraph, packaging, setuptools, pefile,
  pywin32-ctypes, tzdata 2025.2). Nothing is installed system-wide.
* `packaging/build.py` refuses an uncommitted source tree (unless `--allow-dirty`, which the
  manifest records), inspects the given FFmpeg (`-version`: version and configure flags →
  licence class; `--enable-nonfree` builds are refused), runs PyInstaller with
  `packaging/windows/sentinel.spec` (`PYTHONHASHSEED=0`, `SOURCE_DATE_EPOCH` = commit time), copies
  FFmpeg/FFprobe unchanged (copy verified by SHA-256) plus licence/readme files, and writes
  `BUILD-MANIFEST.json` and `SHA256SUMS.txt`.
* **FFmpeg is never downloaded** by the build. The test build used the copy installed on the
  development host (gyan.dev 9.0.1 full build, WinGet).
* Outputs: `dist\Sentinel\` and `build\` (both git-ignored). Never commit them.
* **Offline build:** once, `pip download --no-deps -r requirements/build.txt -d wheels\`
  (Windows) and with `--platform manylinux2014_x86_64 --only-binary=:all:` for Linux; then install
  with `--no-index --find-links wheels\ --require-hashes`. The base Python (python.org installer)
  and FFmpeg must also be kept in the offline kit.
* `Sentinel.exe about --verify` re-hashes every installed file against the manifest (15.5 s on
  the dev host, dominated by the 444 MB of FFmpeg).
* Base interpreter: the shipped program contains the build machine's Python (3.14.3 here). For a
  release, pin it (e.g. a specific 3.12.x/3.14.x installer, recorded in the manifest's `python`).
  `pip` inside the build venv is whatever `venv` provides (25.3 here); it does not affect output.

### Code signing and antivirus (later)
Not implemented. Recommended: Authenticode-sign `Sentinel.exe` (and, for stricter environments,
all `.exe/.dll/.pyd` in `_internal\`) with `signtool sign /fd sha256 /tr <timestamp-url> /td sha256`
**inside** `build.py` after PyInstaller and before the manifest is written (otherwise the hashes
change). FFmpeg binaries from a third party should not be re-signed by us; keep their hashes in
the manifest. Unsigned builds copied from a USB stick do not carry Mark-of-the-Web, but a ZIP
downloaded from the internet does, and Windows SmartScreen will then warn on first start.
UPX is disabled on purpose (packed executables are a common false-positive trigger).

## 7. Measured results (development host, 2026-11-09)

| Measurement | Result |
|---|---|
| Build time (PyInstaller + copy + manifest) | 13 s; 27 s including a fresh hash-checked venv install |
| Output folder | 635 files, 465.9 MB (444.4 MiB): FFmpeg 444.3 MB (`ffmpeg.exe` 222.2 MB, `ffprobe.exe` 222.0 MB), Python runtime + code 18.2 MiB in `_internal` (605 tzdata files), `Sentinel.exe` 2.4 MiB |
| Reproducibility | Two builds from a clean venv, same commit: all 635 file hashes identical (`SHA256SUMS.txt` equal; `BUILD-MANIFEST.json` differs only by its build timestamp) |
| Startup (`Sentinel.exe about`) | 0.32 s warm (5 runs 0.31–0.39 s); 1.29 s on the first run after a fresh build; one outlier of 7.4 s observed once (assumed antivirus scan; not investigated) |
| Launcher: double-click → recorder confirmed by heartbeat | 0.74–0.75 s (`launch --once` wall time); recorder process start → first status 0.5 s |
| Frozen recorder, 30 s, simulated camera, PATH without any FFmpeg | 15 segments FINALIZED, 14 content-hashed (the last one is hashed on next start, existing behaviour); every FFmpeg command line starts with the bundled `dist\Sentinel\ffmpeg\ffmpeg.exe`; graceful stop via `Sentinel.exe stop`; final segment FINALIZED; status `STOPPED`. Separate first-run-setup run (same build, PATH without FFmpeg): `stop` took 2.2 s |
| Bundled FFmpeg | `9.0.1-full_build-www.gyan.dev`; `ffmpeg.exe` SHA-256 `57c56e369d5b4873b4d93fc1a1d833cb7cd8bc9325c14b05c34ce60b22842d8a`; `ffprobe.exe` SHA-256 `afe05347caaabe479b3c4eae71992b6ec1e11c57266a1d665deb0f9fe9847208`; licence class GPL-3.0-or-later |

## 8. FFmpeg licensing (decision for the owner — not decided here; not legal advice)

**Facts about the test build.** The bundled gyan.dev "full" build is configured with
`--enable-gpl --enable-version3` and statically links GPL libraries such as libx264, libx265 and
libxvid. Its licence is therefore **GPL version 3** (its README also states "License: GPL v3"). Sentinel runs it as a separate program over
the command line and does not link to it.

**What distributing that folder implies (GPL-3.0):** whoever gives the folder to someone else
must (a) pass on the GPL licence text (shipped as `ffmpeg\LICENSE.txt`), (b) provide the
*complete corresponding source* for that exact FFmpeg build and every library compiled into it —
either with the copy or through a written offer / equivalent access, kept available — and (c)
impose no further restrictions on those binaries. Running FFmpeg as a separate process is
commonly treated as "aggregation", which would not extend the GPL to Sentinel's own code, but
that is a legal judgement for counsel. Distribution only inside one agency may or may not count
as "conveying" — also for counsel.

**Options:**
1. **Recommended technically: an LGPL FFmpeg build** (configured without `--enable-gpl` and
   `--enable-nonfree`; e.g. the "lgpl" Windows builds published by BtbN, or our own build).
   Sentinel only needs stream copy, the MPEG-TS/segment/MP4 muxers, demuxers/RTSP and FFmpeg's own
   (LGPL) H.264/HEVC decoders for validation and content hashing — no GPL encoder. Obligations
   are lighter (licence text, source of the LGPL build, allow replacement — trivially true for a
   separate `ffmpeg.exe`). A "shared" build would also be much smaller than 444 MB (not measured).
   **Before switching, re-run the full test suite with that exact build** (`[tools]` or bundle it),
   because decoder availability and version affect validation and content hashes.
2. Keep a GPL build and adopt a compliance procedure: archive the complete corresponding source
   with every release and include a written offer. Note that the gyan.dev README only points to
   the FFmpeg source commit (`bf1b838f2a`); the sources of the statically linked third-party
   libraries (67 `--enable-lib*` flags, plus others such as gnutls, zlib, SDL2) would have to be collected separately, which makes this option laborious.
3. Do not distribute FFmpeg: the installing technician installs an approved FFmpeg and points
   `[tools]` at it. Weakest for U9 ("just click this one program").

Separately from copyright: H.264/HEVC are patent-encumbered formats; whether decoding them in
this deployment needs a patent licence is outside engineering scope.

The build manifest records the licence class derived from the configure flags of whatever was
bundled, so a release can be checked against the chosen policy.

## 9. Linux

**Recipe (implemented):** PyInstaller one-folder with `packaging/linux/sentinel.spec`, built
with `packaging/linux/build.sh --ffmpeg-dir <dir>` on the oldest distribution to be supported
(the output needs that glibc or newer). Output `dist/Sentinel/` with `sentinel` (the program),
`sentinel.desktop` (template) and `install-desktop-entry.sh`, which writes a per-user menu
entry (`~/.local/share/applications/sentinel.desktop`, `Terminal=true`) and a desktop copy —
"double-click Sentinel". For unattended use the systemd unit's `ExecStart` becomes
`/opt/sentinel/sentinel recorder run --config /etc/sentinel/sentinel.toml`.

* Bundle a **statically linked** Linux FFmpeg. A distribution FFmpeg is dynamically linked
  (Fedora's has 314 shared-library dependencies) and only works where those libraries exist;
  this recipe does not collect them.
* tzdata is not bundled on Linux: Python uses the system time-zone database.
* **AppImage (option, not implemented):** wrap the same `dist/Sentinel/` as `AppDir/usr/lib/sentinel`
  with an `AppRun` that `exec`s `sentinel`, the `.desktop` file and an icon, then run
  `appimagetool`. Gives a single file, but: AppImages mount via FUSE (not always available), and
  `<app>` would be the read-only mount, so a portable `sentinel.toml` next to it is impossible and
  the bundled FFmpeg path changes per run (harmless: it is resolved at start). Prefer the folder
  until a target distribution is chosen.

**Executed under WSL (Fedora 43, Python 3.14.5, glibc 2.42, no admin):** build from a clean clone
in 9 s (66 files, 24.4 MB with Fedora's 0.6 MB dynamically linked FFmpeg 7.1.4, GPL-3.0 class);
`about` 0.09–0.10 s; `about --verify` passed; with `PATH=/nonexistent` the frozen recorder used
the bundled FFmpeg and sealed 15 segments in ~30 s from a local H.264 test source (Fedora's
FFmpeg lacks libx264, so the source was `ffmpeg … -c:v libopenh264 … tcp://127.0.0.1:18555?listen=1`
instead of `sentinel.sim.camera`); `sentinel stop` stopped it gracefully via SIGTERM (final
segment FINALIZED, status STOPPED). Unit suite on Linux: 102 passed, 1 skipped (Windows-only).
**Not executed on Linux:** systemd, a desktop session / `.desktop` double-click,
`install-desktop-entry.sh`, a static FFmpeg build, the integration suite, Python 3.12.
