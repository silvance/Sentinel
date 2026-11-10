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
| **PyInstaller one-folder** (chosen) | Measured 0.32 s warm, 1.3 s first run after build | No self-extraction, no UPX; ordinary exe + DLLs. Lowest false-positive profile of the PyInstaller modes | Two builds on one machine: all bundled files identical except BUILD-MANIFEST.json timestamps (§7); builds offline from a wheel folder | Sign `Sentinel.exe` (and optionally every PE in `_internal\`) in place | Same exe can be the service host; one process, real PID | **Chosen** |
| PyInstaller one-file | Unpacks ~20 MB (plus 444 MB FFmpeg if bundled inside) to `%TEMP%` on **every** start | Self-extracting bootloaders are a well-known AV heuristic trigger; temp-dir writes of executables | Same as above | Signing covers the outer file only; extracted DLLs unsigned | Two processes (bootloader parent + child): signals, job objects, PIDs and the single-instance lock become indirect; temp folder cleanup on hard kill | Rejected |
| Nuitka standalone/one-file | Faster Python start (irrelevant: the recorder is I/O bound and runs for days) | Compiled binaries have also drawn AV false positives; one-file mode has the same extraction issue | Needs a C compiler toolchain (MSVC or a downloaded MinGW) on the build machine; longer, compiler-dependent builds | Fine | Fine in standalone mode | Rejected: heavier toolchain, no benefit for this workload |
| Embeddable Python zip + native launcher | Fast | Clean (signed python.exe from python.org) | Very transparent | Needs our own signed launcher stub (C) | Fine | Viable fallback; rejected for now because it needs a compiled launcher and hand-maintained dependency copying that PyInstaller already does |
| MSI/installer only | — | — | — | — | — | Complementary, not an alternative: an installer can later wrap the one-folder output (Program Files + service registration). Not built here. |

Why the folder is acceptable: U9 explicitly allows dependencies "in a folder alongside it". The
operator still sees and starts exactly one program.

## 3. Resolution rules (`src/sentinel/paths.py`, unit-tested in `tests/unit/test_paths_tools.py`)

**FFmpeg / FFprobe.** `[tools] ffmpeg/ffprobe` in the configuration always win. When not set, a
*frozen* build **always** uses `<app>\ffmpeg\ffmpeg.exe` / `ffprobe.exe` (`<app>/ffmpeg/ffmpeg` on
Linux); if that file is missing the recorder and the launcher refuse to start (C-001) — there is
no fallback to PATH. A source checkout searches only the *absolute* directories on PATH itself
(never the current directory or relative PATH entries, unlike `shutil.which` on Windows). Every
tool path handed to the recorder is absolute (configured relative paths are resolved against
the configuration folder). The recorder changes its working directory to
`%SystemRoot%\System32` (`/` on Linux) before recording, and the launcher starts it there, so
neither it nor FFmpeg ever runs from a user-writable folder.

**Configuration file**, first that exists:
1. `--config FILE` (returned even if missing, so the named file is reported as missing);
2. per-machine file (table below) — nothing a user can set or create outranks it;
3. `<app>\sentinel.toml` — frozen builds only: a folder a technician pre-configured ("portable");
4. `%SENTINEL_CONFIG%` / `$SENTINEL_CONFIG` (development convenience; skipped if missing);
5. Linux without root only: the per-user file.

**Refusal of changeable locations** (`src/sentinel/install_check.py`, run by
`service.run_recorder`, by the launcher before it starts anything, and reported by
`check-config`): the configuration file and folder, the credentials file and folder, the FFmpeg
programs and their folder, and the spool/data/log folders (or, if not created yet, their nearest
existing parent) must not be changeable by Everyone, Authenticated Users, Users, Interactive,
Guests, Anonymous or Power Users (Windows: evaluated from the DACL read with
`GetNamedSecurityInfoW`; an unreadable ACL counts as a problem) — on Linux: not world-writable,
not group-writable unless group root, owned by root or the recorder's account. `%ProgramData%`
itself fails this check (standard users may create files and folders in it), so a hand-written
configuration whose folders are plain sub-folders of `%ProgramData%` is refused until they are
protected (run setup, or restrict them). Consequence for technicians: an FFmpeg configured in a
folder like `C:\ffmpeg\bin` (which inherits "Authenticated Users: Modify" from `C:\`) is refused.

**Default locations** (used by first-run setup, and when `[storage]` keys are omitted):

| Platform | Configuration and camera credentials | Recordings (spool), ledger + status (data), logs |
|---|---|---|
| Windows | `%ProgramData%\Sentinel\config\sentinel.toml`, `...\config\camera.secret` (DPAPI content; integration: same layout as the Windows service) | `%ProgramData%\Sentinel\spool`, `\data`, `\logs`, `\status` (status_dir) |
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
* Folders: before anything is written, setup creates the configuration folder and the
  spool/data/log folders **private from creation** (`CreateDirectoryW` with a protected DACL:
  setup account, SYSTEM, Administrators, inherited by everything the recorder later creates;
  Linux `0750`). An existing folder is re-protected only if it is empty or holds nothing but
  Sentinel's own files; otherwise setup stops rather than re-permission someone else's data.
* Credentials go to `camera.secret` next to the configuration, never into it. Windows: the file
  is created by `CreateFileW` with an explicit protected DACL (setup account, SYSTEM,
  Administrators) under a temporary name, so it is never readable by others even for an instant;
  its DACL is read back and verified, then it is renamed over the target (a rename keeps the
  file's own DACL) and verified again. Linux: `O_CREAT|O_EXCL` with mode `0600`, verified, renamed.
  **Any failure aborts setup and deletes the plaintext**; the configuration is written only after
  the secret succeeded. **Interim:** the recorder has to run under the setup account (run setup
  while logged in as the account that will use Sentinel). On Windows the file's content is
  DPAPI-protected (machine scope, integration); installing the service grants its account access.
* `setup --simulated-camera tcp://127.0.0.1:8554` (hidden, developer only) writes a
  `development_source = true` configuration; every status then shows Z-001.

## 5. Recorder process, single instance, stop (`src/sentinel/launcher/control.py`, `stopsignal.py`)

* The launcher runs `Sentinel.exe recorder run --config <file>` as a separate process:
  Windows `CREATE_NO_WINDOW | CREATE_NEW_PROCESS_GROUP | CREATE_BREAKAWAY_FROM_JOB` (retried
  without breakaway when a job forbids it), all standard handles to NUL, working directory =
  data folder; Linux `start_new_session=True`. A hidden console (not `DETACHED_PROCESS`) is used
  on purpose: FFmpeg/FFprobe children inherit it and never flash console windows.
* **Single instance** is the recorder's existing instance lock (`data\recorder.lock`); a second
  recorder exits with code 3. The launcher's "is it running?" check never touches that lock (a
  probe that takes the lock, even briefly, could make a starting recorder exit as "already
  running"): on Windows it checks whether the recorder's stop event exists; on Linux whether the
  PID in `recorder.lock` is alive and its `/proc/<pid>/cmdline` is a recorder run (PID reuse
  ruled out; without `/proc` only liveness). A recorder started without the launcher host
  (`python -m sentinel.service run`) is not seen; starting another is still safe (it exits on the
  lock, reported as running). Concurrent launchers are serialised with `data\launcher.lock`. Start is confirmed by a status heartbeat newer than the
  spawn; an early exit maps to C-001 (configuration/tools) or C-003 (anything else).
* **Graceful stop** (technician: `Sentinel.exe stop`): Windows — a named event
  `Local\SentinelRecorderStop-<hash of data folder>` created by the recorder with its token's
  default security (same account, SYSTEM, Administrators); it exists only while a recorder
  listens, so a stop can never be left armed. Linux — SIGTERM to the PID in `recorder.lock`, sent
  only after the PID check above. Both end in the
  existing graceful path: the open segment is closed and sealed as FINALIZED.
* **Interim background mode is NOT suitable for unattended deployment.** The recorder is a
  process of the logged-on user's session: it **stops when that user logs off** (and at every
  restart), it **depends on that user being logged on**, it does not start by itself at boot or
  logon, and nobody restarts it if the process exits (the recorder already retries the camera
  forever internally). Unattended operation (U8) requires the Windows service (branch
  design/windows-service) or, on Linux, the systemd unit.

### Plug-in points for the other branches (no packaging change needed)
* **Windows service** (design/windows-service) — **done at integration** (docs/INTEGRATION.md):
  * `sentinel.winsvc.control.service_control_provider` is the first entry of
    `CONTROL_PROVIDERS`. It returns a Service-Control-Manager control whenever the service is
    **installed, in any state**: `is_running` = RUNNING/START_PENDING, `start` = StartService
    (access denied → C-003, never a fallback; a service that stops again with exit 2 → C-001),
    `stop` = ControlService. If the SCM cannot be asked, it fails closed (C-003, nothing started).
    While the service is installed the launcher only shows the status from `[storage] status_dir`;
    it does not check or write the service-only data/spool/log folders.
  * Setup writes the service's layout: `%ProgramData%\Sentinel\config\sentinel.toml` and
    `config\camera.secret` with **DPAPI (machine scope) content** on Windows (the same bytes as
    `set-camera-credentials`; the format is detected by content), plus `status_dir`.
  * The service host is a **second executable in the same one-folder build**,
    `SentinelService.exe` (entry `packaging/entry_service.py` → `sentinel.winsvc.__main__:main`,
    `host` assumed when no sub-command is given), as the service branch recommended; the SCM runs
    `"SentinelService.exe" host --config <cfg>`. Added to `packaging/windows/sentinel.spec` and
    checked by `build.py`. **NOT BUILT at integration** (PyInstaller is not installed in the
    project venv; installing it would need a download).
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
* **FFmpeg is never downloaded** by the build. The test builds used the copy installed on the
  development host (gyan.dev 9.0.1 full build, WinGet, GPL — the latest one built with
  `-AllowGplInternalUse`). The release build must bundle an LGPL FFmpeg (§8).
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
| Reproducibility | Two builds on one machine (fresh hash-checked venv each, commit 2439529): all bundled files identical (635/635 hashes in `SHA256SUMS.txt`); only `BUILD-MANIFEST.json` differs, by its build timestamp. Not tested across machines. |
| Startup (`Sentinel.exe about`) | 0.32 s warm (5 runs 0.31–0.39 s); 1.29 s on the first run after a fresh build; one outlier of 7.4 s observed once (assumed antivirus scan; not investigated) |
| Launcher: double-click → recorder confirmed by heartbeat | 0.74–0.75 s (`launch --once` wall time); recorder process start → first status 0.5 s |
| Frozen recorder, 30 s, simulated camera, PATH without any FFmpeg | 15 segments FINALIZED, 14 content-hashed (the last one is hashed on next start, existing behaviour); every FFmpeg command line starts with the bundled `dist\Sentinel\ffmpeg\ffmpeg.exe`; graceful stop via `Sentinel.exe stop`; final segment FINALIZED; status `STOPPED`. Separate first-run-setup run (same build, PATH without FFmpeg): `stop` took 2.2 s |
| Re-test after the QA review fixes (commit d26c67e, rebuilt) | Frozen 30 s run again: 15 segments FINALIZED with the bundled FFmpeg, PATH without FFmpeg, graceful stop; `launch --once` 1.45 s. Setup with credentials under a stand-in `%ProgramData%`: folder, `spool` and `camera.secret` show only the setup account, SYSTEM and Administrators (`icacls`), no `(I)` entries. Hidden `ffprobe.exe` → `launch` C-001 (exit 2), `check-config` "FFprobe not found (<bundled path>)". Configuration in a folder granting Users Modify → C-001, "configuration file/folder … can be changed by Users". WSL rebuild: 7 segments in 12 s, stop via verified PID; Linux unit suite 112 passed, 2 skipped. |
| Bundled FFmpeg (test builds only; GPL, now accepted only with `--allow-gpl-internal-use`) | `9.0.1-full_build-www.gyan.dev`; `ffmpeg.exe` SHA-256 `57c56e369d5b4873b4d93fc1a1d833cb7cd8bc9325c14b05c34ce60b22842d8a`; `ffprobe.exe` SHA-256 `afe05347caaabe479b3c4eae71992b6ec1e11c57266a1d665deb0f9fe9847208`; licence class GPL-3.0-or-later |

## 8. FFmpeg licensing — owner decision: ship an LGPL FFmpeg (not legal advice)

**Decision (owner, 2026-11-10): Sentinel is distributed with an LGPL build of FFmpeg.** The build
script enforces it (§8.3). No LGPL build has been obtained or tested yet: downloading one needs
the owner's explicit approval, which is being requested separately. **Until an LGPL build is
bundled and the test suite has passed with it (§8.5), no build may be distributed.**

### 8.1 Why the current test build is not acceptable
The FFmpeg used for all tests so far (gyan.dev 9.0.1 "full", WinGet) is configured with
`--enable-gpl --enable-version3` and statically links GPL-only libraries (libx264, libx265,
libxvid, libvidstab, librubberband, frei0r, libcdio, libdavs2, libxavs2); `ffmpeg -L` reports
GPL version 3, and its README states "License: GPL v3". Distributing it would require providing
the complete corresponding source of FFmpeg *and* of every library compiled in (the README only
points to the FFmpeg commit `bf1b838f2a`; 67 `--enable-lib*` flags). Linux distribution packages
are also GPL (Fedora's `ffmpeg-free` 7.1.4 observed with `--enable-gpl`; Debian/Ubuntu packages are, to our knowledge, likewise configured with `--enable-gpl` — not checked here) and are dynamically linked.

### 8.2 What Sentinel needs from FFmpeg — all of it is LGPL code
Sentinel never encodes video (stream copy only). Everything it uses is FFmpeg's own code under
the LGPL; none of it requires `--enable-gpl` or `--enable-nonfree`:

| Use | FFmpeg components | Licence |
|---|---|---|
| Camera input | `rtsp` demuxer (+ RTP depacketisers), `tcp`/`udp` protocols; `rtsps` additionally needs a TLS library (GnuTLS LGPL-2.1+, or OpenSSL 3 / Schannel on Windows) | LGPL |
| H.264 / H.265 stream handling | `h264`/`hevc` parsers, `h264_mp4toannexb`/`hevc_mp4toannexb` bitstream filters | LGPL |
| Recording | `segment` and `mpegts` muxers, `file`/`pipe` protocols | LGPL |
| Validation (ffprobe) and content hash | `mpegts` demuxer, native `h264`/`hevc` **decoders**, `null`, `hash`/`framehash` muxers, SHA-256 from libavutil, libswscale for pixel-format conversion | LGPL |
| MP4 review copies (Phase 3) | `mp4` (mov) muxer and `mov` demuxer | LGPL |

The native `h264`/`hevc` decoders matter: content hashes (`videohash.py`) were validated with
FFmpeg's own decoders; builds that replace them (e.g. with libopenh264) are refused by the
component check. Only the *simulated camera* (a development tool, never shipped) needs the GPL
encoder libx264; the test suite therefore keeps a GPL FFmpeg on the developer's PATH for the
simulator while the recorder under test uses the LGPL candidate (§8.5).

### 8.3 What the build script enforces (`packaging/build.py`, tests `tests/unit/test_build_licence.py`)
* The licence is read from two independent sources and must agree: `ffmpeg -L` and
  `ffprobe -L` (the licence string FFmpeg's `configure` compiles in: "GNU Lesser General Public
  License … version 2.1/3", "GNU General Public License … version 2/3", or "nonfree parts
  compiled in") and the `configuration:` line of `ffmpeg -version` (`--enable-nonfree` ⇒ nonfree;
  `--enable-gpl`, `--enable-postproc` or any GPL-only library such as `--enable-libx264` ⇒ GPL;
  `--enable-version3` selects v3). Disagreement or an unrecognised `-L` text ⇒ refused.
* **Nonfree: always refused. GPL: refused** with a message pointing here, unless the builder
  passes `--allow-gpl-internal-use` (`-AllowGplInternalUse` in `build.ps1`). Then
  `BUILD-MANIFEST.json` starts with `"ffmpeg_license_status": "GPL FFMPEG - INTERNAL USE ONLY …
  must NOT be distributed"`, `ffmpeg\FFMPEG-LICENCE-NOTE.txt` says so, and `Sentinel about` prints
  it inside a row of `!` characters with "This folder must not be given to anyone outside the
  organisation."
* The components in §8.2 must be present (`-demuxers`, `-muxers`, `-decoders`, `-protocols`,
  `-bsfs`), else refused.
* `build.py --ffmpeg-dir <dir> --inspect-only` (or `build.ps1 -InspectOnly`) checks a candidate
  without building and prints the verdict.

Test basis: captured `-version`, `-L` and component lists of the local GPL build
(`tests/fixtures/ffmpeg/`); the LGPL and nonfree `-L` texts in the tests are synthetic, written
from FFmpeg's wording, because no such build is installed here — they must be confirmed against
the first real LGPL build (§8.5 step 3).

### 8.4 Which LGPL build to obtain (for the owner's approval)
Use a static build (no separate DLLs) of an FFmpeg **release** branch (7.1 or newer; the code is
tested on 9.0.1 and requires ≥ 6.0), 64-bit, LGPL variant. Options, in order of preference:

1. **Build it ourselves from the official release tarball** — `https://ffmpeg.org/releases/ffmpeg-<version>.tar.xz`
   with its `.asc` signature, verified with `gpg --verify` against the FFmpeg release signing
   key published on ffmpeg.org. Configure **without** `--enable-gpl` and `--enable-nonfree`
   (optionally `--enable-version3`), e.g.
   `--disable-shared --enable-static --disable-doc --disable-ffplay --enable-gnutls` (TLS only if
   `rtsps` is needed) and no external codec libraries. Most auditable: we hold exactly the source
   we distribute. Costs a cross-compile toolchain (MSYS2/MinGW or a Linux cross build).
2. **BtbN "FFmpeg-Builds"** (GitHub, `github.com/BtbN/FFmpeg-Builds/releases`): automated builds
   published in GPL and LGPL variants for `win64` and `linux64`; choose an asset whose name
   contains `win64-lgpl` (Windows) / `linux64-lgpl` (Linux) for a **release** branch (`n<version>`),
   not `master`/`latest` nightlies, and not the `-shared` variant unless DLLs are wanted. The
   release page lists checksums; record the asset URL and SHA-256. (Asset naming as published
   at the time of writing — confirm on the page.)
3. Not acceptable: gyan.dev "full" (GPL, §8.1) and, as published at the time of writing, gyan.dev
   "essentials" (also includes libx264/libx265 ⇒ GPL — confirm with `--inspect-only`); Linux
   distribution packages (GPL, dynamically linked).

### 8.5 How to verify a candidate and what must be re-run
1. Record where it came from (URL, release tag), its SHA-256, and for option 1 the verified
   signature and the exact `configure` command.
2. `packaging\windows\build.ps1 -FfmpegDir <dir> -InspectOnly` → must print
   `"license_status": "OK: LGPL-…"`; any refusal ends the evaluation.
3. Look at `ffmpeg -L` yourself: it must say "GNU Lesser General Public License"; compare with the
   synthetic text in `tests/unit/test_build_licence.py` and, if FFmpeg's wording differs, update
   the detector and add the real output under `tests/fixtures/ffmpeg/`.
4. **Re-run the whole test suite with the candidate as the recorder's FFmpeg**:
   `SENTINEL_TEST_TOOLS_DIR=<dir>` makes every test configuration use it, while the simulated camera
   keeps using the FFmpeg on PATH: `python -m pytest tests -q -p no:cacheprovider`. Content hashes
   carry the FFmpeg version in their method string, so expect no comparison against hashes made
   with the old build.
5. Build with it (no override flag), then run the frozen smoke test
   (`SENTINEL_FROZEN_EXE=dist\Sentinel\Sentinel.exe SENTINEL_LAUNCHER_RUN_S=30 pytest tests/integration/test_launcher_process.py -s`),
   `Sentinel about --verify`, and record the results and new folder size in the branch notes.
6. Ship the FFmpeg licence text (copied automatically if present next to the binaries) and keep
   the matching source archive (option 1: our tarball; option 2: the tagged source the build used)
   with each release, so the LGPL source offer can be honoured.

Separately from copyright: H.264/HEVC are patent-encumbered formats; whether decoding them in
this deployment needs a patent licence is outside engineering scope.
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
