# Sentinel — single-camera recorder and evidence export appliance

Status: **Phase 2 prototype (recording engine).** Not forensically validated. Not yet run on
Linux or against the real camera. See [docs/PHASE2_REPORT.md](docs/PHASE2_REPORT.md).

## What works today
* Continuous recording from one camera with FFmpeg (`-c copy`, no re-encoding) into ~5-minute
  MPEG-TS segments on local storage; audio excluded unless explicitly enabled.
* Every segment validated (ffprobe), SHA-256 hashed, sealed read-only and recorded in a SQLite
  ledger; a low-priority worker adds a container-independent decoded-frame hash and per-frame
  hashes; a rolling audit re-verifies stored originals.
* Explicit gap records for every interruption, including footage FFmpeg silently drops from its
  timeline; no interrupted recording is ever presented as continuous.
* Self-recovery for camera loss, stream freezes, FFmpeg crashes, service crashes, full storage
  and clock changes; retries forever; plain-language operator alerts with fixed error codes
  that escalate to "Recording stopped … Contact technical support and provide diagnostic report",
  plus an automatic diagnostic report.

## Also built (merged 2026-11-10; see docs/INTEGRATION.md and docs/branches/)
Review-drive export, continuous archive drive with swap workflow, drive detection with a guarded
erase-and-set-up tool (switched off until a supervised dock test), Windows service with
DPAPI-protected credentials, operator status screen (light/dark), tamper-evident ledger,
multi-day time accuracy, local retention (on in deployed configurations: archived footage on a
removed archive drive is cleared from this computer when it runs low), and a
click-to-run packaging build. Since then (2026-11-10): a recording schedule (pause/resume at set days and
times, P-001), keep-awake and power settings for an unattended laptop (H-001), and a "Lock this computer"
button (docs/branches/schedule-and-unattended.md). Full suite on the merged code: 1003 passed (Windows 11,
FFmpeg 9.0.1, simulated camera). Release preparation (branch `release-prep`): the operator screen in
`Sentinel.exe`; set up in advance without the camera (`setup --camera-later`, then `set-camera` on site;
P-002 until then); review export and drive tools reachable from `Sentinel.exe` (`export`, `drives`);
third-party licences and the LGPL source offer in the build (`licences/`, `packaging/fetch_sources.py`);
a printable bench-test, soak and dry-run checklist ([docs/09](docs/09_BENCH_TEST.md)); motion-triggered
recording outside set hours and motion markers for reviewers in the export summary and timeline
(schedule action `motion`, `[motion]`, `Sentinel.exe motion-test`;
[docs/branches/motion-recording.md](docs/branches/motion-recording.md)).

## Not yet done
Anything on the real camera, real removable drives, an administrator service install, Linux
(except a WSL build), or Python 3.12; the operator screen cannot yet start
an export (needs a request interface to the service; a technician can run `Sentinel.exe export run`).
The repository has no LICENSE file yet (pyproject.toml says Apache-2.0; the owner must confirm).
The sources and notices of the libraries linked into the bundled FFmpeg are not archived yet, so a
build must not leave the organisation until docs/08 §8.7 is done (review 2026-11-10). The operator screen is bundled in the build
(2026-11-10) but has not been looked at on a real display from the built program yet.

## Click-to-run program (branch design/single-executable)
`Sentinel.exe` (one-folder PyInstaller build with FFmpeg bundled next to it) sets itself up on
first start (technician questions), starts the recorder in the background and shows its
status. Build: `packaging\windows\build.ps1 -FfmpegDir <dir>` (Linux: `packaging/linux/build.sh`).
From source the same launcher is `python -m sentinel.launcher`. See
[docs/08](docs/08_PACKAGING.md). Owner decision: releases bundle an **LGPL** FFmpeg; the build
script refuses GPL/nonfree builds (docs/08 §8). The LGPL build in use: BtbN FFmpeg 9.0.2 (docs/08 §8.6).

## Developer quick start (Windows or Linux)
Requires Python ≥ 3.12 and FFmpeg/FFprobe ≥ 6.0 on PATH.

```bash
python -m venv .venv
```
```bash
.venv/Scripts/python -m pip install -r requirements/dev.txt -r requirements/runtime.txt
```
```bash
.venv/Scripts/python -m pytest
```

Try it with the simulated camera (two terminals, from the repository root, `PYTHONPATH=src`):

```bash
python -m sentinel.sim.camera --port 8554
```
```bash
python -m sentinel.service run --config dev.toml
```

where `dev.toml` uses `url = "tcp://127.0.0.1:8554"`, `input_format = "mpegts"` and
`development_source = true` (see `tests/integration/test_service_process.py` for a complete
example). `python -m sentinel.service status --config dev.toml` shows the operator banner.

## Documentation
| | |
|---|---|
| [01 Architecture](docs/01_ARCHITECTURE.md) | components, IPC, repository layout, dependencies |
| [02 Risks & assumptions](docs/02_RISKS_AND_ASSUMPTIONS.md) | everything unverified, ranked |
| [03 Recording storage model](docs/03_RECORDING_STORAGE_MODEL.md) | container choice, segment states, timestamps, ledger |
| [04 Removable storage](docs/04_EXPORT_STATE_MACHINE.md) | archive drive, review drives, drive preparation (Phase 3) |
| [05 Failure analysis](docs/05_FAILURE_ANALYSIS.md) | each failure: behaviour, operator message, recovery, test |
| [06 Verification & traceability](docs/06_VERIFICATION.md) | test results and requirements matrix |
| [07 Security](docs/07_SECURITY.md) | controls, residual risks, deployment checklist |
| [08 Packaging](docs/08_PACKAGING.md) | click-to-run build, tool/config resolution, FFmpeg licensing |
