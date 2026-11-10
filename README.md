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

## Not yet built
Removable-drive archive/export (Phase 3, design in [docs/04](docs/04_EXPORT_STATE_MACHINE.md)),
operator GUI (Phase 4), packaging as a single click-to-run program (Phase 6).

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
