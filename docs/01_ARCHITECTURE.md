# Sentinel — Architecture (Phase 1)

Status: **Draft for review.** Applies to Phase 2 prototype code in `src/sentinel/`.
Nothing in this document claims forensic validation.

## 1. Design goals, in priority order

1. **Never lose footage silently.** Every second the camera is not recorded must appear
   as an explicit gap record with a reason.
2. **Evidence integrity.** Original segments are written once, hashed (SHA-256), sealed
   read-only, and never modified. Derivatives (MP4) carry provenance back to originals.
3. **Recoverability.** Any crash, kill, reboot or power loss leaves the system in a state
   that the next start can reconcile without human help.
4. **Measured status.** The operator sees what was *measured* (stream time advancing,
   segments sealed, bytes free), never "process exists = green".
5. **Simplicity for operators.** Three routine buttons. Everything else is admin-only.

## 2. Component overview

```
                 ┌────────────────────────────── recording host (Ubuntu) ───────────────────────────────┐
  IP camera      │                                                                                       │
  (RTSP/H.264) ──┼─► FFmpeg (child process, -c copy, segment muxer, MPEG-TS)                             │
                 │        │ stdout: -progress key=value          writes: spool/<cam>/<run>/seg_NNNNNN.ts   │
                 │        │ stderr: warnings (redacted)          writes: spool/<cam>/<run>/segments.csv    │
                 │        ▼                                                                              │
                 │  ┌──────────────────── sentinel-recorder (systemd service, user "sentinel") ─────────┐ │
                 │  │ Supervisor ── launches/monitors FFmpeg, stall detection, retry/backoff,          │ │
                 │  │               clock-step detection, run & gap bookkeeping                        │ │
                 │  │ Finalizer  ── ffprobe validation → fsync → SHA-256 → read-only seal (fast)       │ │
                 │  │ IntegrityWorker ── deferred decoded-frame hash + rolling re-verification audit   │ │
                 │  │ Janitor/Watchdog ── retry stuck work, restart workers, systemd watchdog ping     │ │
                 │  │ Recovery   ── on start: reconcile spool directory against ledger                 │ │
                 │  │ StorageMonitor ── free space, measured bitrate, time-to-full                     │ │
                 │  │ StatusWriter ── atomic status.json heartbeat                                     │ │
                 │  └───────────────┬───────────────────────────────────────────────────────────────────┘ │
                 │                  │ SQLite ledger (WAL)   status.json (atomic replace)                   │
                 │                  ▼                                                                    │
                 │  ┌──────── sentinel-export (Phase 3, privileged helper) ────────┐                      │
                 │  │ drive identification · copy · read-back SHA-256 · manifests  │──► SATA drive (dock) │
                 │  │ remux to MP4 (derivative) · flush · unmount · power-off      │                      │
                 │  └──────────────────────────────────────────────────────────────┘                      │
                 │  ┌──────── Operator GUI (Phase 4, PySide6, read-mostly) ─────────┐                      │
                 │  │ reads status.json + ledger (read-only); requests exports via  │                      │
                 │  │ a request queue; never starts/stops recording                 │                      │
                 │  └──────────────────────────────────────────────────────────────┘                      │
                 └───────────────────────────────────────────────────────────────────────────────────────┘
```

### 2.1 Recording engine (`sentinel.recorder`) — implemented in Phase 2
* **FFmpeg does the recording.** Python never touches media bytes on the write path.
* `ffmpeg_cmd.py` builds argv: `-rtsp_transport tcp`, I/O timeout, `-map 0:v:0 -an`
  (audio excluded unless explicitly enabled), `-c copy`, segment muxer into MPEG-TS,
  CSV segment list, `-progress pipe:1`.
* `supervisor.py` owns one FFmpeg child at a time. It:
  * refuses to launch when storage is below the critical threshold;
  * creates a *run* record (one per FFmpeg process lifetime);
  * parses progress; derives a time anchor (§4 of the storage model);
  * declares a **stall** if stream time stops advancing for `stall_timeout_s`, even if
    the process is alive; stops FFmpeg (graceful `q`, then kill);
  * records the run's end reason, opens a gap, and retries with exponential backoff.
  * measures stream time against monotonic time; if stream time falls persistently behind
    (footage FFmpeg removed from its timeline), records the lost interval and restarts;
  * retries forever; repeated *internal* errors end the process so systemd restarts it.
* `finalizer.py` validates closed segments with ffprobe, fsyncs, hashes (SHA-256) and seals them;
  the seal, its gap rows and the closing of the preceding outage gap are one ledger transaction.
* `integrity.py` (low CPU priority) adds the decoded-frame content hash and per-frame hash list,
  and continuously re-verifies sealed originals (alert V-001 on any change).
* `recovery.py` reconciles the spool on every start (crash/power-loss path); a janitor in
  `service.py` retries stuck segments every minute and restarts dead worker threads.
* `alerts.py` is the operator vocabulary: fixed error codes and messages, escalation to
  "Recording stopped … Automatic recovery unsuccessful … provide diagnostic report";
  `diagnostics.py` writes the support report (no video, no credentials).

### 2.2 Storage manager (`sentinel.storage`) — monitoring in Phase 2, retention in Phase 3
* Measures free space on the spool filesystem and the observed recording bitrate,
  and estimates time until the critical threshold.
* **Retention is not implemented in Phase 2. Nothing is ever deleted automatically.**
  Phase 3 adds an *admin-enabled* retention policy that may only delete segments that
  are sealed, older than the retention window, and verified on ≥ N export drives.

### 2.3 Export manager (`sentinel.export`) — Phase 3
See `04_EXPORT_STATE_MACHINE.md`. Runs as a separate process so a crashing export can
never stop recording. Mount/unmount/power-off via UDisks2 over D-Bus (Linux), behind
the `DriveManager` interface.

### 2.4 Operator GUI (`sentinel.gui`) — Phase 4
* Reads `status.json` (heartbeat-checked; stale ⇒ "System error: recorder not responding").
* Reads the ledger with a read-only SQLite connection.
* Requests exports by writing a request record the export service picks up (no direct
  privileged operations from the GUI process). Closing the GUI affects nothing.

### 2.5 Administrator interface — Phase 4/5
Separate window, separate OS group (`sentinel-admin`), polkit-gated actions for anything
destructive (retention changes, deleting quarantined data). Erasing a drive is offered only as **Erase and Set Up Drive** (`sentinel.drives`), with the guards in docs/branches/design-drive-prepare.md: never automatic, never for system/recording/Sentinel-evidence drives, and switched off by default (`[drives] erase_enabled = false`).

## 3. Process & IPC model

| Channel | Writer | Readers | Why |
|---|---|---|---|
| `status.json` (atomic rename) | recorder | GUI, admin, export | Crash-safe, trivially cross-platform, no listening socket. Contains a heartbeat; readers treat stale files as failure. |
| SQLite ledger (`sentinel.db`, WAL, `synchronous=FULL`) | recorder (segments/runs/gaps), export (Phase 3) | GUI (read-only) | Durable, transactional, one file, stdlib. |
| Export request queue (table in ledger) | GUI | export service | Keeps privileges in the service, not the GUI. |
| journald / rotating log file | all | admin | Diagnostics; credentials redacted at the logging-filter level. |

No TCP/HTTP listener exists anywhere in the design. Nothing listens on the network.

## 4. Repository structure

```
README.md
pyproject.toml                  # package metadata, pinned tool config
requirements/
  runtime.txt                   # Phase 2: empty (stdlib only)
  dev.txt                       # pinned test tooling
config/sentinel.example.toml    # no real addresses or credentials
docs/                           # architecture, risks, storage model, export SM, failures, verification, security
packaging/systemd/              # unit file draft (Phase 6 finalises)
src/sentinel/
  __init__.py                   # version
  config.py                     # schema + validation (TOML, stdlib tomllib)
  credentials.py                # secret loading, URL assembly, redaction
  db.py                         # ledger schema, migrations, state transitions
  models.py                     # enums: segment states, run end reasons, status levels
  hashing.py                    # streaming SHA-256
  clock.py                      # wall/monotonic clock abstraction (testable)
  logging_setup.py              # handlers + redaction filter
  status.py                     # measured status model + atomic writer
  alerts.py                     # operator error codes, messages, escalation
  diagnostics.py                # support report (ZIP, no video, no credentials)
  service.py                    # entry point: `python -m sentinel.service run|status|diagnostics`
  media/probe.py                # ffprobe wrapper and validation rules
  media/videohash.py            # decoded-frame (container-independent) content hashing
  recorder/ffmpeg_cmd.py        # argv builder (pure)
  recorder/progress.py          # -progress parser
  recorder/segment_list.py      # incremental CSV segment-list reader
  recorder/supervisor.py        # FFmpeg lifecycle, stall detection, backoff
  recorder/finalizer.py         # validate → hash → seal (one ledger transaction)
  recorder/integrity.py         # deferred content hashing + rolling integrity audit
  recorder/recovery.py          # startup reconciliation
  recorder/timebase.py          # run time anchor + clock-step detection
  storage/monitor.py            # free space, bitrate, time-to-full
  platform/base.py              # interfaces
  platform/posix.py             # Linux implementation
  platform/windows.py           # Windows implementation (dev + future service)
  sim/camera.py                 # simulated camera with fault injection
tests/unit/                     # pure-Python tests, no FFmpeg needed
tests/integration/              # real FFmpeg + simulated camera
```

## 5. Dependencies and justification

| Dependency | Phase | Why | Alternatives rejected |
|---|---|---|---|
| Python ≥ 3.12 | 2 | Ubuntu 24.04 LTS ships 3.12; stdlib has `tomllib`, `sqlite3`, `hashlib`. | — |
| tzdata (Windows only, pinned) | 2 | Windows has no system IANA timezone database; Ubuntu does. | — |
| FFmpeg/FFprobe (distro package) | 2 | Mature RTSP client, stream copy, segmenting; distro builds get security updates and are installable offline from a local mirror. | GStreamer (more moving parts), OpenCV (re-encodes, not a recorder). |
| SQLite (stdlib) | 2 | Transactional ledger, zero admin. | Flat JSON files (no atomic multi-row updates). |
| pytest (dev only, pinned) | 2 | Test runner. | unittest (more boilerplate). |
| PySide6 (pinned) | 4 | Required by brief; LGPL. | — |
| UDisks2 via `gdbus`/D-Bus | 3 | Native, unprivileged-with-polkit mount/unmount/power-off. | Raw `mount`/`umount` as root (wider privilege). |

Phase 2 has **no third-party runtime Python dependencies on Linux** (only `tzdata` on Windows)
to minimise supply-chain risk.
Configuration validation is hand-written dataclasses instead of pydantic for the same reason.

## 6. Platform abstraction

`sentinel.platform.base.Platform` defines: single-instance lock, child-process lifetime
binding (child dies with service), file fsync, directory fsync, read-only sealing,
secret-file permission checks, and (Phase 3) `DriveManager`. Linux and Windows
implementations exist; Windows is used for development today and is the seam for a
future Windows service. Linux-only behaviour (systemd, UDisks2, `PR_SET_PDEATHSIG`)
has **not been executed** in this development environment (see Phase 2 report).
