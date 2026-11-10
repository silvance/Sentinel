# Verification Plan and Requirements Traceability

Results below are from runs executed in this repository on the development host
(Windows 11, Python 3.14.3, FFmpeg 9.0.1, simulated camera over TCP/MPEG-TS). **Nothing here
has been executed on Linux, on Python 3.12, against an RTSP server, or against the MAXSUR
camera.** "Result" is copied from the run log in docs/PHASE2_REPORT.md; DESIGN means designed
but not demonstrated.

## Acceptance plan (outstanding before deployment)
1. Linux CI on Ubuntu 24.04 / Python 3.12 / distribution FFmpeg 6.1: full suite.
2. RTSP-level tests with an RTSP server (e.g. MediaMTX, offline binary) feeding the recorder.
3. Real camera: 72 h soak with scripted unplug/replug, freeze, reboot, power cut (UPS-less), clock
   step; compare footage OSD time with ledger times at 10 random points; measure bitrate and CPU.
4. 5MP@30 fps throughput on the deployment PC: sealing latency, content-hash backlog over 24 h.
5. Phase 3+: removable-drive tests with disposable drives in the Sabrent dock.

## Traceability matrix (Phase 2 scope)

| Req (brief §) | Requirement | Test(s) | Result |
|---|---|---|---|
| 3 | FFmpeg records; `-c copy`; no transcoding | `test_argv_stream_copy_no_audio_by_default` | see report |
| 3 | ~5-minute segments; actual boundaries tracked | `test_continuous_recording_…`, `test_first_segment_time_matches_wall_clock` | see report |
| 3 | Finalized originals never modified | `test_sealed_integrity_fields_are_immutable`, `assert_sealed_integrity` (read-only + hash), `test_audit_detects_modified_original` | see report |
| 3 | RTSP disconnect handling with retry/backoff | `test_disconnect_reconnect_records_gap`, `test_camera_offline_…`, `test_backoff_grows_and_resets` | see report (TCP, not RTSP) |
| 3 | Resume after restart | `test_hard_kill_restart_recovery_and_graceful_stop` | see report |
| 3 | Detect stalled recording while FFmpeg runs | `test_stream_freeze_detected_as_stall`, `test_status_recording_requires_measured_progress` | see report |
| 3 | Storage exhaustion / write failure | `test_storage_warning_then_full_then_recovery`, `test_storage_full_message`, `test_storage_read_failure_is_critical_error` | see report |
| 3 | Accurate, documented timestamps | `test_first_segment_time_matches_wall_clock`, `test_timebase_*`, docs/03 §4 | see report |
| 3 | Interruption-tolerant container | `test_probe_truncated_segment_is_playable_but_shorter`, `test_ffmpeg_crash_…` | see report |
| 3 | Distinguish finalized / active / interrupted / corrupt / missing | `test_legal_transitions`, `test_illegal_transitions_rejected`, crash/corrupt/gap tests | see report |
| 3 | Never present interrupted as continuous | gap tests incl. `test_freeze_absorbed_by_ffmpeg_is_recorded_as_gap`, `test_short_freeze_is_recorded_as_internal_gap` | see report |
| 6 | Per-segment SHA-256, duration, size, validation, interruption flags | `test_continuous_recording_…`, `test_ffmpeg_crash_…` | see report |
| 6 | MP4 derivative provenance (not equal file hash) | `test_content_hash_survives_remux_but_file_hash_does_not`, `test_content_hash_detects_changed_pictures` | see report |
| 8 | No credentials in logs | `test_credentials_never_leak`, `test_redact_*`, `test_logging_filter_and_formatter_redact` | see report |
| 8 | Local-only; no listeners in production | `test_invalid_config_rejected` | see report |
| 8 | Audit log of configuration changes | `_audit_start` (config SHA-256 per start) | DESIGN (no dedicated test) |
| 9 | Clock change | `test_wall_clock_step_flags_run_and_segments`, `test_clock_step_detector_*` | see report |
| 9 | Corrupt segment / invalid timestamps | `test_probe_unusable_files`, `test_corrupted_stream_data_…`, `test_camera_reboot_with_timestamp_reset`, `test_pts_wraparound_is_not_a_gap` | see report |
| 9 | Status from measurements | `test_status_*` | see report |
| added | Operator error codes + escalation + diagnostic report | `test_outage_alert_escalates_…`, `test_camera_never_reachable_is_r001`, `test_storage_full_message` | see report |
| added | Unattended self-healing | `test_transient_validation_failure_…`, `test_internal_error_…` | see report |
| 5, 7 | Removable drives, export, manifests, reviewer layout | — | Phase 3 |
| 4 | Operator GUI | — | Phase 4 |
| 11 | Packaging, single executable | — | Phase 6 |
