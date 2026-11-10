# Branch `design/time-accuracy` — drift-corrected segment times and clock observations

## 1. Summary
Recording times used to be worked out from a single starting point per recording run, so a
camera clock or PC clock that runs slightly fast or slow made the times drift (about 4 s per
day at a typical 50 ppm). Each 5-minute segment now has its time checked against when the
footage actually arrived and is nudged back — slowly, never in jumps, and with segments still
joining up exactly. The recorder also writes a "clock observation" every 15 minutes (is the PC
clock synchronised, to what, how far off) so an examiner can state how accurate the times are.

## 2. Requirements addressed
| ID | Item | Stated requirement or design choice |
|---|---|---|
| E1 | Documented clock source, UTC handling (docs/03 §4.1, §4.5) | Stated requirement |
| E1 | Measured clock offset: `clock_observations` table, OS sync status/source/offset, camera and PC drift in ppm | Stated requirement; *what* is measured and how is my design |
| E1 / U8 | Drift-corrected segment times for multi-day runs (docs/PHASE2_REPORT §5 limitation) | Design choice (the scheme, rate limit, default 1000 ppm, 0.1 s jitter allowance, 8 s/2 s window) |
| E3 | Contiguity kept; absorbed-discontinuity gaps still recorded; drift never creates false gaps | Stated (E3); preserving existing behaviour |
| E7 | Recorded test results for drift scenarios | Stated |

## 3. What changed
* `src/sentinel/recorder/timebase.py` — `DriftTracker` (rate-limited per-boundary correction,
  `set_break` for absorbed discontinuities), `TimeKnot`, `interpolate_correction`;
  `ClockStepDetector` gained `drift_s` / `drift_ppm` (gradual wall-vs-monotonic drift, steps
  excluded); `RunTimebase.iso_for/epoch_for` take a correction.
* `src/sentinel/recorder/supervisor.py` — feeds progress reports to the tracker, fixes a knot
  when each segment closes (and at run end for interrupted segments), persists per-run drift
  and per-segment corrections, one `time_correction_limited` event per run; the absorbed
  lost-time (`timeline_shift_s`) is now measured from reception offsets either side of the
  break (`measure_jump`), the old lag-vs-baseline figure is the fallback.
* `src/sentinel/recorder/finalizer.py` — segment times = anchor + stream time + interpolated
  correction; legacy runs (no `time_method`) unchanged; segments without a measured correction
  in a tracked run hold the last one (`time_correction_held` warning). Gap/shift logic untouched.
* `src/sentinel/ledger_timing.py` (new) — separate DDL block (`clock_observations`) and
  `ALTER TABLE ADD COLUMN` helper for run/segment columns, own `schema_meta` key; `db.py` only
  gained one import, one call in `_migrate` and `| TIMING_WRITE_ONCE`.
* `src/sentinel/timesync.py` (new) — read-only OS time-sync queries/parsers: Windows `sc query
  w32time`, `w32tm /query /status [/verbose]`, registry `W32Time\Parameters`; Linux `chronyc
  tracking`, `timedatectl show` / `show-timesync`. `platform/*.py` not touched.
* `src/sentinel/service.py` — clock observations at start-up, every
  `clock_observation_interval_s` (900 s) and after a clock step, in a background thread; drift
  and last observation in `status.json`.
* `src/sentinel/diagnostics.py` — `clock_observations.csv`, drift/correction columns in
  `runs.csv`/`segments.csv` (only columns that exist), "Time accuracy" section in `summary.txt`.
* `src/sentinel/config.py`, `config/sentinel.example.toml` — `[recording] max_clock_drift_ppm`
  (1000), `[service] clock_observation_interval_s` (900).
* `src/sentinel/sim/camera.py` — `clock_rate` (and `--clock-rate`): `settb=1/90000,setpts=PTS*rate`
  with `-fps_mode passthrough`, frames still paced in real time by `-re`.
* Tests: `tests/unit/test_time_accuracy_units.py` (new), `tests/integration/test_time_accuracy.py`
  (new), `tests/integration/harness.py` (`DriftingClock`).
* Docs: `docs/03` §4 rewritten (4.1 clock source, 4.2 scheme, 4.3 alternatives, 4.4 clock
  observations, 4.5 examiner statement + OSD procedure) and §5; `docs/05` row 30; `docs/06`;
  `docs/07` Windows Time checklist item.

### Measurements made for design decisions (FFmpeg 9.0.1, this PC)
| Question | Result |
|---|---|
| What makes simulated stream time run fast/slow vs wall time? | Encoder → pipe → receiver `-progress`, 32.5 s: `-re` 1.00065; `-readrate 0.99` 0.98891; `-readrate 1.01` 1.00816; `-re` + `setpts=PTS*1.01` 1.01101. `setpts` chosen (exact scaling; `-readrate` pacing was off by ~0.1 %). |
| Windows Time on this PC | Service `w32time` STOPPED, DEMAND_START; `w32tm /query /status` → "The following error occurred: The service has not been started. (0x80070426)", exit 0x80070426; registry NtpServer `time.windows.com,0x9`, Type NTP. Used as a unit-test fixture. |
| Reference error of `observed_closed_utc` | No drift (rate 1.0): segment 0 −1.19 s (FFmpeg probe backlog writes segment 0 late), others −0.18..−0.41 s with 0.2 s poll. Hence the drift tests use a 50 ms poll and skip segment 0 in the reception comparison. |

## 4. Tests run (Windows 11, Python 3.14.3, FFmpeg 9.0.1)
Baseline before changes: `pytest tests/unit tests/integration/test_recorder_faults.py
tests/integration/test_unattended.py` → **68 passed**.

Interpreter: `"C:\Users\silva\Documents\Proc 6 Build\.venv\Scripts\python.exe" -m pytest ... -q -p no:cacheprovider`

| # | Command (from worktree root) | Result |
|---|---|---|
| 1 | `tests/unit tests/integration` (first commit) | 98 passed, **3 failed**: `test_short_freeze_is_recorded_as_internal_gap`, `test_corrupted_stream_data_does_not_stop_recording`, `test_hard_kill_restart_recovery_and_graceful_stop` (sqlite "disk I/O error" on a read-only open right after the hard kill) |
| 2 | `tests/unit` + `test_recorder_faults.py test_unattended.py test_time_accuracy.py test_media_hashing.py` | 99 passed, 1 failed (`test_short_freeze_…`) |
| 3 | `tests/unit test_recorder_faults.py test_unattended.py test_time_accuracy.py` | 89 passed, 1 failed: my new `test_absorbed_freeze_is_still_a_gap_under_drift` (run's correction 0.086 s vs expected ≈0.39 s). Root cause: the existing lost-time estimate (lag − slow baseline) overstated the absorbed gap by the baseline's lag behind drift. **Fixed** (`DriftTracker.measure_jump`, commit dfafcc1). |
| 4 | after the fix: `tests/unit`; `test_time_accuracy.py test_unattended.py -k "absorbed or freeze"` | **70 passed**; **2 passed** |
| 5 | **final**: `tests/unit tests/integration` | **102 passed, 1 failed** (`test_short_freeze_is_recorded_as_internal_gap`: "timed out after 30s waiting for internal gap row") |

**Pre-existing flakiness (not caused by this branch), measured:** I exported the baseline commit
b39fe1b with `git archive` to a scratch directory and ran it with the same interpreter:
* `test_recorder_faults.py`, whole file, run twice on each tree, alternating:
  branch: 1 failed (`camera_offline…`) / 1 failed (`corrupted_stream…`);
  **baseline: 1 failed (`short_freeze…`) / 1 failed (`camera_offline…`)**. These are timing-sensitive
  tests failing at random on both trees (other agents' test runs were probably loading this PC).
* `-k short_freeze` on its own: branch 6/6 passed, baseline 6/6 passed.
* `test_service_process.py`: **baseline 1/1 failed with the same "disk I/O error"**; branch failed
  3/3 in one series and passed in the final run (#5). It is intermittent and pre-existing (the
  read-only ledger open right after a hard kill), and should be looked at separately.

New tests (all passed in runs 2 and 5, and run 4 for the freeze test):
* `tests/integration/test_time_accuracy.py`
  * `test_slow_camera_and_drifting_pc_clock_stay_on_reception_time`: camera 1 % slow + PC wall
    0.5 % fast vs monotonic, 150 s, 5 s segments. All segment ends (after segment 0) within
    ±0.5 s of observed reception; the old single-anchor end error < −1.5 s and growing; one run,
    no `source_discontinuity_absorbed` gap, no restarts, segments contiguous to 2 ms with equal
    boundary corrections; `camera_drift_ppm` ≈ −10 000 ± 1500, `host_clock_drift_ppm` ≈ 5000 ± 300.
  * `test_fast_camera_stays_on_reception_time`: camera 1 % fast, 100 s; same checks, old error > +0.8 s.
    (Measured in the first attempt: new errors −0.05..−0.28 s; segment 0 −1.09 s, which is a
    reference artefact, see §3.)
  * `test_absorbed_freeze_is_still_a_gap_under_drift`: 12 s freeze under 1 % slow camera →
    exactly one `source_discontinuity_absorbed` gap of 12 ± 2 s at the right times, no
    rate-limited corrections (break not smeared into drift), times monotonic.
* `tests/unit/test_time_accuracy_units.py` (20 tests): 3-day synthetic drift at ±50–80 ppm
  (old error ≈ 13 s, tracked < 0.1 s), 1 % drift with matching / production limits, a 12 s jump
  not followed (per-knot change ≤ limit), break handling, unbiased jump measurement, wall-clock
  step flagged not followed, contiguity/interpolation, ledger migration of an old schema
  (append-only, idempotent), finaliser legacy/held corrections, diagnostics contents,
  w32tm/sc/chronyc/timedatectl parsing incl. the real "service has not been started" output.

## 4a. Review findings (independent QA review of @4af7b3b) — status
Regression tests: `tests/unit/test_time_review_findings.py` (the reviewer's `drift_probe.py`
ported as `_reviewer_probe`, and the `seg0_probe.py` shape) and
`test_first_video_pts_gives_one_constant_offset_per_run` (real FFmpeg).

| # | Finding | Status |
|---|---|---|
| 1 HIGH | Stall at a boundary moved times (+0.400 s with a false limited flag; 0.35 s unflagged) | **Fixed.** Estimator replaced: Theil-Sen line through per-bucket minima of `m − t` over the last hour (bucket 30 s; t/10 ≥ 2 s early in a run), evaluated at the boundary; non-drift allowance cut from 0.1 s to 20 ms per boundary. Reviewer's probe now: ≤ 1 ms error and no flag for 0.35 s, 3 s and 60 s stalls (tests: `test_stall_at_a_boundary_never_moves_it[3.0/0.35]`, `test_stall_of_a_whole_minute_is_not_followed`). I did not add an explicit "two consecutive knots agree" rule: the median-based fit over an hour already requires most buckets to agree. |
| 2 HIGH | Late wall read (loop latency) entered host drift (+0.25 s) | **Fixed.** `read_clock_pair` (mono, wall, mono; spread ≤ 2 ms) in the supervisor loop, run start and service; `ClockStepDetector.drift_s` = median of the last 15 tight samples. Probe: ≤ 1 ms (`test_late_wall_clock_reads_do_not_become_drift`, `test_loose_clock_pairs_are_not_used_for_drift`). |
| 3 MED | Docs overclaimed; per-frame formula wrong after a break | **Fixed** in docs/03 §4.2/§4.5: measured bounds stated with their source, "spread over following boundaries (bounded, flagged)" instead of "never smeared", frame formula adds `timeline_shift_s` after the break, within-segment correction bound stated. |
| 4 MED | No measured offset on Windows | **Fixed, with a condition.** SNTP query (stdlib, read-only) to `[service] time_reference_server` → `clock_observations.ref_offset_s/ref_delay_s/ref_detail`, shown in the diagnostics summary. **No default server**: if it is not configured, the recorder does not meet E1's "measured offset" and docs/03 §4.4 says so. Tested only against a local fake SNTP responder (offset, bad replies, unreachable, end-to-end into the ledger). `w32tm /stripchart` syntax checked against `w32tm /?` and documented as the manual alternative. |
| 5 MED | w32tm "synchronized" ignored Last Sync Error and sync age | **Fixed.** True only with Last Sync Error 0 and last good sync ≤ `max_sync_age_s` (default 2 × reported poll interval); otherwise False/None with `sync_reason` (`test_w32tm_week_old_or_failing_sync_is_not_synchronized`). Field names unchanged, compatible with the windows-service branch. |
| 6 MED (pre-existing) | Seg-0 lead-in hid a timestamp jump | **Fixed.** Segment 1's first PTS (new `probe.first_video_pts`, reads one packet) calibrates the run's constant PTS offset; measured lead-in used, rest goes to the boundary-gap path; no segment 1 → `lead_in_unverified` warning (three unit tests incl. the reviewer's shape; the constant offset verified with real FFmpeg). |
| 7 LOW | Fast wall-clock slew entered host_s | **Fixed.** Host and camera components are limited separately; a host change beyond drift sets `host_limited`, marks the run `clock_anomaly`, records a `clock_slew` event and raises T-001 (`test_fast_wall_clock_slew_is_flagged_as_clock_anomaly_not_followed`). A persisting change is still caught up at the limit rate (documented). |
| 8 LOW (pre-existing) | Race between segment close and break detection | **Fixed.** Pending segments are timed only after a progress report newer than their end has passed the lost-time check (`segment_ready`); `observed_closed_utc` is now stamped when the close is *seen*, not when it is processed (the hold otherwise distorted it by up to 1 s; caught by the drift integration tests). |
| 9 LOW | Observation thread could write after ledger close | **Fixed.** `_closing` event checked before writing; join 30 s (`test_clock_observation_is_not_written_once_stopping`). |

**Tests after the review fixes (commit 1fd31f5, this PC):**
* `tests/unit tests/integration/test_time_accuracy.py` → **95 passed** (91 unit + 4 integration).
  The first attempt failed 3 drift tests: segment ends drifted −0.1 → −1.08 s in a sawtooth.
  Cause: the new finding-8 hold delayed processing by up to one progress interval, and
  `observed_closed_utc` was stamped at processing time. The segment times were right; the
  reference was wrong. Fixed by stamping when the close is seen; then 95 passed.
* `tests/integration/test_recorder_faults.py tests/integration/test_unattended.py` → **18 passed**.
* `test_service_process.py` / `test_media_hashing.py` were not re-run after the review fixes.

## 5. NOT executed / not verified
* **Linux:** `chronyc tracking`, `timedatectl show`/`show-timesync` parsing is from documented
  formats only (unit-tested with hand-written fixtures); never run on Linux.
* **Successful `w32tm` output** (service running, synchronised): parsed from the documented
  format; on this PC the service is stopped so only the error path ran for real. The sign of
  w32tm's `Phase Offset` is kept raw, not normalised. Non-English Windows labels are not parsed
  (kept raw, `synchronized` unknown).
* **Real camera / RTSP:** drift only emulated with the simulator (MPEG-TS over TCP). Real camera
  drift rate, RTSP timestamp behaviour, and capture-to-receipt latency are unmeasured; the OSD
  procedure (docs/03 §4.5) is written but not performed.
* **Multi-day runs:** only simulated (unit test: 3 days of synthetic reports at ±50-80 ppm);
  the longest real run in tests is 150 s at exaggerated 1 % drift.
* **Real Windows monotonic drift:** emulated with `DriftingClock` (wall 0.5 % fast vs
  monotonic); the real QPC-vs-UTC drift of the deployment PC is unmeasured (the clock
  observations will measure it).
* Python 3.12 not run (no 3.13+ features used knowingly).

## 6. Risks, limitations, open decisions
* **Reference time is the PC clock.** If the PC's time service is not running (as on this
  development PC) the times are only as good as the free-running PC clock; this is now *recorded*
  (`synchronized = 0`) but not alerted by this branch — T-002 depends on
  `Platform.clock_synchronized()`, which another branch is implementing for Windows. Suggest
  wiring `timesync.query_windows()` behind it, or alerting from the clock observations.
* **Rate limit default (1000 ppm)** is my choice: a camera worse than that (broken crystal) gets
  flagged segments and lagging correction rather than followed. With very short segments the
  0.1 s jitter allowance per boundary dominates the limit (e.g. 2 s test segments allow
  0.1 s + 2 ms per boundary).
* **Sub-tolerance wall-clock steps** (< 2 s) are treated as drift but rate-limited, so they are
  smeared in over several segments and flagged, not followed instantly.
* **Crash recovery:** segments closed by startup recovery hold the last measured correction
  (flagged `time_correction_held`); the error is at most the drift since the last measured
  boundary.
* **Schema:** new columns are append-only via `ledger_timing.py`; other branches adding columns
  to `db.py` DDL will not conflict, but if another branch also edits `Ledger._migrate` there is a
  one-line merge.
* Accuracy figures in docs/03 §4.5 (latency 0.2-1 s) are estimates to be replaced by the OSD /
  reference-clock measurement on the real camera.

## 7. Recommendation
**Merge after** (a) a reviewer reads `timebase.DriftTracker` and the finaliser change (they are
on the evidential time path) and (b) the shared-test flakiness is acknowledged: the remaining
failures occur at the same rate on the untouched baseline. The scheme fixes the known
multi-day drift limitation and keeps the existing gap and step guarantees, with tests showing
it. It also records E1's measured-offset evidence. Before deployment, configure and start the
Windows Time service (docs/07) and do the reference-clock/OSD measurement on the real camera
(docs/03 §4.5).
