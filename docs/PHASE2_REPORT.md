# Phase 1–2 Report: Architecture and Recording Engine

Date: 2026-11-09 · Version 0.2.0.dev0 · Host: Windows 11, Python 3.14.3, FFmpeg 9.0.1 (gyan.dev full build)

## 1. What was implemented
* Phase 1 documents: architecture (01), risks & unresolved assumptions (02), storage model and
  container decision (03), removable storage / drive preparation design (04, Phase 3), failure
  analysis (05), verification & traceability (06), security & deployment checklist (07).
* Phase 2 recording engine (`src/sentinel/`): FFmpeg supervisor, segment sealing, deferred
  content hashing and integrity audit, startup recovery, ledger, measured status, operator alert
  catalogue with escalation, automatic diagnostic reports, storage monitoring, platform layer
  (Windows implemented and exercised; Linux implemented, **not executed**), simulated camera with
  fault injection, systemd unit draft.

## 2. Independent review
Two reviewers, independent of the implementer, read the code and ran their own experiments:
* **Evidence-integrity reviewer**: 12 findings. Critical: a 10–15 s source freeze was silently
  removed by FFmpeg's MPEG-TS discontinuity correction, leaving no gap and shifting later
  timestamps (reproduced). High: crash mid-seal could leave a segment permanently unsealed;
  finaliser failures left segments stuck while status went green; sealing was not atomic.
* **Security/honesty reviewer**: 13 findings. High: decoded-frame hashing ran slower than real
  time for 5MP@30 fps *and* delayed sealing (measured). Medium: no umask; secrets-file check only
  warned; redaction gaps (raw `@` in passwords, credentials in URL path/query); development inputs
  (TCP/UDP listeners, lavfi) accepted in production; docs citing files that did not exist; dead code.

| Finding | Resolution |
|---|---|
| Freeze absorbed by FFmpeg (critical) | Stream time is measured against monotonic time; a sudden persistent excess records a `source_discontinuity_absorbed` gap at the measured position and restarts the run. Closed segments are held while drift is suspected. |
| Content hash too slow / delayed sealing | Sealing now = probe + file SHA-256 + read-only (fast). Content hash moved to a low-priority worker; single decode pass (aggregate derived from per-frame hashes). |
| Non-atomic seal; stuck segments; green status | One ledger transaction per seal; janitor retries every minute (5 attempts); V-001 alert when overdue or given up. |
| Read-only sidecar blocks retry | Sidecar written as `.partial` then replaced; read-only cleared first. |
| Interrupted first segment mistimed by lead-in | Interrupted times now derived from the run's final measured stream time (or the previous segment's listed end). |
| Corrupt seals counted as healthy | Only good seals feed the "recordings are being saved" check; V-002 for corrupt. |
| Clock changes between runs undetected | Service-level step detection flags open gaps; ordering uses insertion order, not wall-clock text. |
| Supervisor crash reported as clean stop | Exception path writes ERROR (R-006) and a diagnostic report. |
| Unbounded `proc.wait()` | 30 s bound, logged as critical. |
| umask / secrets / redaction / dev inputs / FFmpeg version / `prctl` / regex `$` | Fixed (see docs/07); Linux paths not executed. |
| Missing docs; dead code | Docs written; dead code removed. |

## 3. Measured evidence used in design decisions
All measured on the development host; commands are reproducible with the repository and FFmpeg 9.0.1.

| Question | Measurement | Consequence |
|---|---|---|
| Is FFmpeg's CSV segment list written incrementally? | Yes: after 7.5 s of a 3 s-segment recording, 2 entries present; hard kill leaves the open segment unlisted. | CSV entry = "closed by FFmpeg"; unlisted file at run end = interrupted. |
| Is a killed MPEG-TS segment usable? | Truncated segment probed playable (1.2 s of a 3 s segment) before flushing; with flushing, data up to the kill. | MPEG-TS chosen (ADR-001). |
| Does `-flush_packets 1` reach the segment's inner muxer? | No: sizes grew in 256 KiB steps. `-segment_format_options flush_packets=1` wrote continuously. | The latter is used. |
| Does a graceful `q` list the final segment? | Yes. | Graceful stop gives a FINALIZED last segment. |
| Are compressed-packet hashes container-independent? | No: TS and its `-c copy` MP4 differ, also after `h264_mp4toannexb`. | Not used for provenance. |
| Are decoded-frame hashes container-independent? | Yes: aggregate and all 45 per-frame hashes identical for TS and MP4. | Content hash = SHA-256 over per-frame decoded SHA-256 list. |
| Single-pass content hash cost at 2592×1944@30 | 5.48 s for 10 s of video (1.83× real time) on this 20-thread host; the reviewer measured the earlier two-hash design at 21.1 s per 20 s. | Deferred, low-priority; slower PCs may build a backlog (reported, not hidden). |
| File SHA-256 and ffprobe cost (same clip) | 0.01 s and 0.11 s per 10 s of 5MP video. | Done at seal time. |
| PTS wrap (33-bit, ~26.5 h) inside a segment | ffprobe unwraps; 4.0 s duration, no gap reported. | No special handling; regression test added. |
| Python `zoneinfo` on Windows | No system tz database: even "UTC" fails. | `tzdata` pinned for Windows only. |

## 4. Tests actually run
Final baseline (after all review fixes), development host, 2026-11-09:

| Suite | Command | Result |
|---|---|---|
| Unit (config, credentials/redaction, argv, parsers, time base, clock-step detector, backoff, storage, status/alerts, ledger, hashing) | `pytest tests/unit` | **50 passed** |
| Media & hashing (probe, truncation, corruption, timestamp gap, PTS wrap, remux content hash, tamper) | `pytest tests/integration/test_media_hashing.py` | **10 passed** |
| Recorder faults (continuous, wall-clock accuracy, disconnect, offline, stall, short freeze, FFmpeg crash, camera reboot, stream corruption, storage, clock step) | `pytest tests/integration/test_recorder_faults.py` | **11 passed** |
| Unattended (absorbed 12 s freeze, alert escalation + wording + report, never-reachable, storage-full message, janitor retry, internal-error retry, tamper audit) | `pytest tests/integration/test_unattended.py` | **7 passed** |
| Real process (hard kill + restart recovery, FFmpeg dies with service, single instance, graceful signal stop, CLI banner; credential leak check incl. real FFmpeg error output and diagnostic ZIP) | `pytest tests/integration/test_service_process.py` | **2 passed** |
| **Total** | | **80 passed, 0 failed** |

Failures found and fixed during development (not hidden): first-segment timestamps fell back to
file times after reconnect bursts; FFmpeg's I/O buffer lost seconds on hard kill; a clock-anomaly
flag could be overwritten by a race; janitor excluded retryable segments; a held-segment race lost
the absorbed-freeze gap; drift detection reacted to transient lag spikes; recovery queued work
across runs out of order. Each is covered by a test above.

## 5. Known limitations
* **Not executed:** Linux (systemd, `PDEATHSIG`, permissions, `timedatectl`, watchdog), Python 3.12,
  RTSP (all recorder tests used TCP/MPEG-TS from the simulator), the MAXSUR camera, real power
  loss, real disk-full (simulated by thresholds), 5MP end-to-end recording (only hashing was benchmarked).
* **Slow clock drift:** segment times are derived from one anchor per run. Camera-vs-host clock
  drift (tens of ppm) is not corrected within a run: ~4 s per day at 50 ppm. Sudden steps are
  detected and flagged; gradual drift is ignored by step detection by design. A per-segment
  re-anchoring scheme is planned (see §7).
* Credentials file is plaintext (0600 on Linux); Windows ACL check not implemented — Windows
  should use DPAPI encryption (planned with the Windows service).
* Windows service / auto-start is not implemented yet; the recorder runs as a console process.
* Same-account tampering of sealed files is detected (audit) but not prevented.
* No GUI; operator messages exist only in `status.json` and the `status` command banner.

## 6. Remaining risks (ranked)
1. Real camera unknowns: RTSP URL, codec (H.264 vs H.265/smart codec), GOP length, reconnect behaviour (docs/02 A1–A5).
2. Removable-drive reliability and honest read-back verification (Phase 3; docs/02 C1–C4).
3. Timestamp drift over multi-day runs (see §5).
4. 5MP CPU load on the deployment PC (content-hash backlog).
5. RTSP credentials in FFmpeg argv (mitigated by `ProtectProc=invisible` on Linux; Windows equivalent: run the service under a dedicated account; other local users cannot read another account's process command line without admin rights — to verify).

## 7. Recommended next phase
**Phase 3 — removable storage**, Windows-first: drive detection and classification, one-button
*Prepare This Drive* (decision on authority pending, docs/04 §4), continuous archive copy with
cache-bypassing read-back verification, swap workflow, review-drive export with MP4 derivatives
and manifests. In parallel (small): Windows background service + DPAPI credentials, per-segment
time re-anchoring, and a Linux CI run of the existing suite.
