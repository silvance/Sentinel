# Failure Analysis

For each condition: expected behaviour, what the operator sees (alert codes in
`src/sentinel/alerts.py`), what is written to the ledger/log, and how the system recovers.
"Test" names the automated test that exercises it (see docs/06 for results); **none** means
the behaviour is designed but not yet demonstrated.

Escalation rule for all outages: the operator first sees a NOTICE ("Recording interrupted …
Reconnecting automatically (attempt n)"). If recording has not resumed after `escalate_after_s`
(default 180 s) it becomes CRITICAL: "Recording stopped. Error R-00x: … Automatic recovery
unsuccessful. Contact technical support and provide diagnostic report." A diagnostic report is
written automatically when any alert first becomes critical. Retrying never stops.

| # | Condition | Expected behaviour | Operator sees | Ledger / log | Recovery | Test |
|---|---|---|---|---|---|---|
| 1 | Camera disconnected | FFmpeg exits (EOF / I/O timeout) → run ends; open segment sealed as INTERRUPTED (or FINALIZED if FFmpeg listed it); gap opened at last footage | R-003 notice → critical after 180 s; link "disconnected" | `run_end_*` event, gap row (reason, attempts), redacted FFmpeg stderr tail in run | Retries with backoff (1→30 s) forever | `test_disconnect_reconnect_records_gap`, `test_camera_offline_shows_interrupted_and_recovers`, `test_outage_alert_escalates_…` |
| 2 | Camera reconnects | New run, new time anchor; first sealed segment closes the gap | Alert clears; RECORDING | `gap_closed` event with duration | Automatic | same as 1 |
| 3 | Camera never reachable | Connect timeout per attempt; one gap with attempt counter; no empty run folders; retry events throttled | R-001 notice → critical | one gap row, `attempts` counter | Automatic when reachable | `test_camera_never_reachable_is_r001` |
| 4 | RTSP stream freezes (connection open, no data) ≥ stall timeout | Stall detected from stream time not advancing (even though FFmpeg is alive); FFmpeg stopped; gap | R-002 | `run_end_stall`, gap reason `stall` | Automatic restart | `test_stream_freeze_detected_as_stall` |
| 5 | Short freeze (< 10 s) | FFmpeg keeps timestamps; probe finds PTS jump; within-segment gap row | none (footage continues) | gap kind `within_segment` | n/a | `test_short_freeze_is_recorded_as_internal_gap` |
| 6 | Freeze 10–15 s that FFmpeg *removes* from its timeline (MPEG-TS discontinuity correction) | Supervisor measures stream time falling behind monotonic time; records `source_discontinuity_absorbed` gap at the right place, shifts later footage to true time, restarts on a fresh anchor | none (brief) | `timeline_discontinuity` event, run `timeline_break_stream_s`/`timeline_shift_s` | Automatic | `test_freeze_absorbed_by_ffmpeg_is_recorded_as_gap` |
| 7 | FFmpeg crashes / killed | Run ends `ffmpeg_error`/`ffmpeg_killed`; open segment INTERRUPTED (playable up to the last packet — packets are flushed per write) | R-003 notice (brief) | interrupted segment, gap | Automatic restart | `test_ffmpeg_crash_marks_segment_interrupted_and_restarts` |
| 8 | Recording service crashes / killed | FFmpeg dies with it (Windows job object; Linux PDEATHSIG + systemd cgroup). On restart: recovery closes the run as `service_interrupted`, seals listed segments, seals the open one as INTERRUPTED (time from file mtime), opens/closes the gap | X-001 while down (GUI); then normal | `startup_recovery` event | systemd `Restart=always`; recovery on start | `test_hard_kill_restart_recovery_and_graceful_stop` |
| 9 | Recorder hangs (deadlock) | Watchdog ping comes only from the recording loop → systemd kills and restarts it | X-001 (stale heartbeat) | systemd journal | systemd `WatchdogSec=60` | **none** (Linux only; not executed) |
| 10 | Internal software error | Run closed as `internal_error`, retried; 5 in a row → process exits → restarted by systemd | R-006 if exiting | `internal_error` events | Automatic | `test_internal_error_is_retried_without_stopping_service` |
| 11 | Reboot | Service starts at boot (`WantedBy=multi-user.target`); recovery as in 8 | normal after connect | `service_start`, recovery events | Automatic | recovery logic: test 8; boot itself: **none** |
| 12 | Power loss during recording | As 8, plus: data in the OS page cache may be lost. Segment files are flushed per packet to the OS; ledger commits are `synchronous=FULL`. The last few seconds may be missing; the open segment is sealed INTERRUPTED | as 8 | as 8 | as 8 | approximated by hard kill (test 8); true power cut: **none** |
| 13 | Internal storage nearly full | Recording continues | S-001 warning | storage level in status | Prepare review/archive drive; admin frees space | `test_storage_warning_then_full_then_recovery` |
| 14 | Internal storage full (critical threshold) | Recording stopped *cleanly* before the disk is actually full (last segment FINALIZED); no new FFmpeg while critical | S-002 "Recording stopped…" | `run_end_storage_critical`, gap | Resumes automatically when space is freed | `test_storage_warning_then_full_then_recovery`, `test_storage_full_message` |
| 15 | Storage unreadable | Treated as critical | S-003 | log error | Admin | unit: `test_storage_read_failure_is_critical_error` |
| 16 | Write failure / disk error | FFmpeg exits non-zero; "No space left" classified as storage; other errors as `ffmpeg_error` | S-002 or R-003 | stderr tail in run | Retry | partially (storage path) |
| 17 | Removable drive full / disconnects during copy or verify | Phase 3 (docs/04) | — | — | — | **none** (Phase 3) |
| 18 | Corrupted segment | Probe fails → CORRUPT, hashed and sealed as-is, a `corrupt_segment` gap covers its interval | V-002 warning | `segment_corrupt` event | n/a (kept for examination) | `test_probe_unusable_files`, `test_corrupted_stream_data_does_not_stop_recording` |
| 19 | Invalid / backwards timestamps (camera reboot) | Ledger times stay monotonic; segments remain sealed | none | warnings on affected segments | n/a | `test_camera_reboot_with_timestamp_reset` |
| 20 | 26.5 h MPEG-TS PTS wrap | Not reported as a gap | none | none | n/a | `test_pts_wraparound_is_not_a_gap` |
| 21 | Operator closes GUI during recording / export | No effect: the GUI does not own the recorder (Phase 4 GUI; Phase 3 export runs in the service) | — | — | — | by design; GUI test Phase 4 |
| 22 | System clock stepped during a run | Derived times stay on the monotonic timeline; run and its segments flagged `clock_anomaly` | T-001 warning (24 h) | `clock_step` event | n/a | `test_wall_clock_step_flags_run_and_segments` |
| 23 | Clock stepped between runs / backwards | Service-level check flags the open gap; negative gap durations flagged | T-001 | `clock_step_service` event | n/a | **none** (service-level path) |
| 24 | Clock not synchronised (Linux `timedatectl`) | Recording continues | T-002 warning | — | Admin fixes NTP | **none** (Linux only) |
| 25 | Sealing fails transiently (e.g. ffprobe timeout) | Segment stays unsealed; janitor retries every minute up to 5 attempts; status warns if overdue, critical after 5 failures | V-001 warning/critical | `finalize_failed` events, `finalize_attempts` | Automatic, then support | `test_transient_validation_failure_is_retried_by_janitor` |
| 26 | Stored original modified or deleted | Rolling audit re-hashes sealed segments | V-001 critical | `audit_hash_mismatch` / `audit_missing` / `audit_not_read_only` | Support / investigation | `test_audit_detects_modified_original` |
| 27 | Second recorder instance started | Refuses (exit code 3) | — | log | n/a | `test_hard_kill_restart_recovery_and_graceful_stop` |
| 28 | Credentials leak via logs/ledger/status/report | Redaction filter on all log output; ledger stores only redacted text | — | — | — | `test_credentials_never_leak` + unit tests |
| 29 | Content hashing cannot keep up (slow CPU, 5MP) | Originals are already sealed with file SHA-256; content hashing backlog grows and is reported to administrators | none to operator | `integrity.content_hash_backlog` in status | Runs when CPU allows; Phase 3 export computes any missing hashes it needs | **none** (measured throughput only, see report) |
| 30 | Wall-clock drift on Windows during multi-day runs; camera clock drift | Windows' monotonic clock is not NTP-disciplined and camera clocks drift tens of ppm. Segment times are corrected per segment boundary toward measured reception time on the wall clock (rate-limited; steps excluded), docs/03 §4.2. Drift rates per run and periodic clock observations are recorded | none (faster-than-plausible changes: segment warning + `time_correction_limited` event) | run `camera_drift_ppm`/`host_clock_drift_ppm`, segment `time_correction_*`, `clock_observations` | n/a | `test_slow_camera_and_drifting_pc_clock_stay_on_reception_time`, `test_fast_camera_stays_on_reception_time`, `test_three_day_drift_is_tracked_without_accumulating` |
