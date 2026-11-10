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
| Windows: credentials file DACL checked at startup — only SYSTEM, Administrators, the process's own account and `NT SERVICE\SentinelRecorder` may read/change/re-permission it; startup refused (C-001) otherwise. Read from the security descriptor as language-neutral SDDL, not localised `icacls` text | `platform/windows.py`, `platform/winacl.py`, `service.py` | `test_real_file_dacl_and_secret_file_problems` (real DACL, real `icacls` grant), SDDL parser tests on captured strings |
| Windows: camera credentials protected with DPAPI (`CryptProtectData`, machine scope, application entropy) via ctypes; written by `python -m sentinel.winsvc set-camera-credentials` (getpass, never echoed or logged); ACL restricted *before* the secret is written; read back and compared before replacing the file. Plain TOML still accepted (Linux; Windows fallback with a logged warning) | `winsvc/dpapi.py`, `winsvc/credstore.py`, `credentials.py` | `tests/unit/test_dpapi_credentials.py` (real DPAPI round trips, user **and machine** scope, without admin) |
| Files created 0640 / directories 0750 (umask 027) | `platform/posix.py`, systemd `UMask=0027` | **not executed** (Linux) |
| Windows equivalent of the umask: installer gives spool/data/log an inheritable ACL without inheritance from the parent — SYSTEM + Administrators full, service account modify, nobody else; the configuration folder: Administrators change, service + Users read; the credentials file: Administrators full, service read. Language-neutral `*S-1-…` SIDs. Refuses to re-permission drive roots and system/profile folders | `winsvc/commands.py` | generated commands unit-tested; `icacls` grants/reset **executed on a temp folder** (`test_generated_icacls_commands_execute_and_produce_the_intended_acl`); `/setowner` and the real install **not executed** (admin) |
| Windows service runs as the virtual account `NT SERVICE\SentinelRecorder` (no password, no admin rights, own SID), with only `SeChangeNotifyPrivilege`; no network listener is added | `winsvc/commands.py` | **not executed** (admin) |
| Sealed originals read-only; rolling audit re-hashes them and alerts V-001 on change | `finalizer.py`, `integrity.py` | `test_audit_detects_modified_original` |
| Integrity fields write-once in the ledger | `db.py` | `test_sealed_integrity_fields_are_immutable` |
| Audit events: service start/stop, configuration change (config file SHA-256), recovery, integrity failures | `service.py`, `db.events` | integration tests |
| FFmpeg ≥ 6.0 enforced | `service.py` | — (dev host has 9.0.1) |
| Single instance (no double recording) | `platform.acquire_instance_lock` | process test |

## Residual risks (accepted only with listed controls)

* **B4 — RTSP credentials in FFmpeg's command line.** FFmpeg cannot read the URL from a file.
  Controls: systemd `ProtectProc=invisible` (or `/proc` mounted `hidepid=invisible`), no other
  interactive users on the appliance, a view-only camera account. **Windows has no equivalent of
  `ProtectProc`:** FFmpeg's command line (with the camera password) is readable by any process that
  can open the FFmpeg process with query access. Administrators can always do so; whether a standard
  user can open a process owned by the service's virtual account depends on that process's security
  descriptor and was **not measured** (it needs an installed service). DPAPI protects the file at
  rest only. Controls: no other interactive users, operators as standard (non-admin) users, view-only
  camera account. Not measured on this host.
* **Windows DPAPI machine scope.** Any process on the machine that can *read* the protected file
  can decrypt it; the file ACL is the real barrier. DPAPI does not protect against administrators,
  or against extraction of the machine DPAPI keys from an unencrypted disk (use BitLocker).
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

### Windows deployment (draft; see docs/branches/design-windows-service.md)

- [ ] Program (Python or the PyInstaller **one-folder** build, Sentinel, FFmpeg) under `C:\Program Files\Sentinel` — not in a personal user folder (the service account cannot read those; `install` refuses).
- [ ] Configuration in `C:\ProgramData\Sentinel\config\` with full `[tools]` paths; data/spool/logs in dedicated folders (example: `config/sentinel.windows.example.toml`).
- [ ] Elevated Command Prompt: `python -m sentinel.winsvc set-camera-credentials --config …`, then `install --config … --dry-run`, review, then `install --config …`.
- [ ] `python -m sentinel.winsvc status --config …` shows RUNNING, folders "OK", credentials "DPAPI-protected; permissions: OK", time synchronisation "yes".
- [ ] Windows Time service set to Automatic with an approved NTP source (`w32tm /config …`); otherwise T-002 is shown permanently.
- [ ] Operators are standard users (not administrators); BitLocker per policy; sleep/hibernate disabled; power-on after power loss.
- [ ] Reboot test: recording resumes with nobody logged in; kill test (`taskkill /F` on the service process) shows a restart within ~5 s.
