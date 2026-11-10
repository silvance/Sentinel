# Security Design and Deployment Checklist

Nothing here asserts that the camera, workstation or software is approved for any government
network. Approval is the deploying organisation's decision.

## Implemented (Phase 2)

| Control | Where | Verified by |
|---|---|---|
| No network listener, no telemetry, no cloud calls | whole codebase; production config accepts only `rtsp`/`rtsps` client URLs | review; `test_invalid_config_rejected` |
| Test sources (tcp/file, `input_format`) only with `development_source = true`, which raises permanent alert Z-001 | `config.py`, `alerts.py` | unit + integration tests |
| Credentials only in a separate file; never in the main config URL, path or query | `config.py`, `credentials.py` | unit tests |
| Credentials redacted from every log line, traceback, ledger text, status file and diagnostic report | `credentials.redact`, `logging_setup` | `test_credentials_never_leak` (real FFmpeg error output) |
| Credentials file must be 0600 and owned by the service account (Linux; startup refused otherwise) | `platform/posix.py`, `service.py` | **not executed** (Linux) |
| Files created 0640 / directories 0750 (umask 027) | `platform/posix.py`, systemd `UMask=0027` | **not executed** (Linux) |
| Sealed originals read-only; rolling audit re-hashes them and alerts V-001 on change | `finalizer.py`, `integrity.py` | `test_audit_detects_modified_original` |
| Integrity fields write-once in the ledger | `db.py` | `test_sealed_integrity_fields_are_immutable` |
| Audit events: service start/stop, configuration change (config file SHA-256), recovery, integrity failures | `service.py`, `db.events` | integration tests |
| FFmpeg ≥ 6.0 enforced | `service.py` | — (dev host has 9.0.1) |
| Single instance (no double recording) | `platform.acquire_instance_lock` | process test |

## Residual risks (accepted only with listed controls)

* **B4 — RTSP credentials in FFmpeg's command line.** FFmpeg cannot read the URL from a file.
  Controls: systemd `ProtectProc=invisible` (or `/proc` mounted `hidepid=invisible`), no other
  interactive users on the appliance, a view-only camera account.
* **Same-account tampering.** The recorder account can lift the read-only bit on its own files.
  The rolling audit detects changes; stronger separation (a separate owner via a privileged
  sealing helper, or filesystem immutability) is a Phase 6 option.
* **Ledger is not tamper-evident.** A hash-chained event log and signed export manifests are
  recommended for Phase 3 (see docs/04).
* **Supply chain.** Python dependencies are version-pinned but not yet hash-pinned; FFmpeg comes
  from the distribution. Phase 6: `--require-hashes` requirements and a recorded FFmpeg build hash.

## Deployment checklist (draft)

- [ ] Camera on an isolated switch/VLAN with no default gateway; recorder has no Internet route.
- [ ] Camera: cloud/P2P/UPnP disabled; firmware version recorded; view-only RTSP account created.
- [ ] Camera RTSP URL verified (vendor docs or ONVIF GetStreamUri); codec H.264, GOP ≤ 2 s, smart codec off.
- [ ] Audio inclusion decided and documented by the responsible authority (default: excluded).
- [ ] Ubuntu LTS, full-disk encryption per policy, automatic updates disabled; updates applied from an approved offline mirror.
- [ ] Dedicated `sentinel` service account (no login shell); operators and administrators in separate groups.
- [ ] `/etc/sentinel/camera.secret` 0600 owned by `sentinel`; `/etc/sentinel/sentinel.toml` root:sentinel 0640.
- [ ] systemd unit installed as shipped (`ProtectProc=invisible`, `UMask=0027`, watchdog, `Restart=always`).
- [ ] NTP from an approved local source (or documented manual time procedure); `timedatectl` shows synchronised.
- [ ] Suspend/hibernate disabled; BIOS set to power on after power loss; UPS recommended.
- [ ] SSH disabled or restricted to administrators; no remote desktop.
- [ ] Removable-drive filesystem approved (risk C1); drive preparation authority decided (docs/04 §4).
- [ ] Acceptance tests (docs/06) run on the deployment hardware with the real camera; results filed.
