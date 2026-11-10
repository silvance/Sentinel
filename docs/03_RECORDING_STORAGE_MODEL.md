# Recording Storage Model

## 1. Directory layout (local spool, internal SSD)

```
<spool_dir>/
  <camera_id>/
    <run_id>/                    # one directory per FFmpeg process lifetime ("run")
      segments.csv               # FFmpeg segment list: one line per *closed* segment
      seg_000000.ts              # original media, MPEG-TS, H.264 stream copy
      seg_000001.ts
      ...
<data_dir>/
  sentinel.db                    # SQLite ledger (WAL)
  status.json                    # measured status heartbeat (atomic replace)
  recorder.lock                  # single-instance lock
```

`run_id` = `YYYYMMDDTHHMMSSZ-<8 hex>` (UTC launch time + random suffix), so directory
names sort chronologically and never collide. Local file names are **not** reviewer-facing;
the export stage produces date/time-named files.

## 2. Container decision (ADR-001)

**Decision:** record originals as **MPEG-TS** segments; produce **MP4** only as a derivative at export.

| Property | MPEG-TS | MP4 (default) | Fragmented MP4 | Matroska |
|---|---|---|---|---|
| Playable after power loss / kill mid-write | **Yes** – self-synchronising 188-byte packets, no index | **No** – `moov` index written at close; file is unplayable without repair | Mostly (up to last complete fragment) | Mostly (no cues; players vary) |
| Stream copy of H.264 from RTSP | Yes | Yes | Yes | Yes |
| Windows double-click playback | Films & TV/VLC play it; less universal | **Best** | Inconsistent in older players | Needs VLC/codec |
| Overhead | ~2–4 % | ~0.5 % | ~1 % | ~0.5 % |

*Evidence from this repository's experiments (FFmpeg 9.0.1, see Phase 2 report):* a segment
killed with SIGKILL-equivalent mid-write remained decodable up to the last flushed byte
(1.2 s of a 3 s segment). This is the property we need: an interruption loses seconds, not a
whole segment.

**Consequences**
* Original `.ts` is the evidential original; its SHA-256 is recorded at sealing time.
* Reviewer MP4s are produced by `ffmpeg -c copy -movflags +faststart` (remux, no re-encode).
  The MP4 has **its own** SHA-256. Its manifest entry references the source segment IDs and
  their hashes. The MP4 hash is **never** expected to equal the source hash.
* Exports include the original `.ts` files as well as the MP4 derivatives (configurable; default on).

## 3. Segment lifecycle (states)

```
                 file appears                 FFmpeg lists it in segments.csv
   (none) ───────────────────────► ACTIVE ─────────────────────────────────► CLOSED
                                     │                                         │ ffprobe OK + hash + seal
                                     │ FFmpeg exited / killed / stalled /       ▼
                                     │ service crashed, file NOT listed     FINALIZED   (export-eligible)
                                     ▼                                         
                              (validate + hash + seal)                         
                         ┌───────────┴───────────┐                             
                    playable                 not playable / no video          
                         ▼                         ▼                           
                    INTERRUPTED               CORRUPT                       
             (export-eligible, flagged)   (kept, hashed, never auto-deleted,
                                           exported only to a quarantine folder)
```

| State | Meaning | Allowed next |
|---|---|---|
| `ACTIVE` | FFmpeg may still be writing. Never copied, never hashed. | `CLOSED`, `INTERRUPTED`, `CORRUPT` |
| `CLOSED` | FFmpeg reported the segment complete; awaiting validation. | `FINALIZED`, `CORRUPT` |
| `FINALIZED` | Closed normally, ffprobe-valid, hashed, sealed read-only. | (terminal) |
| `INTERRUPTED` | Writer ended abnormally while this segment was open. Playable portion hashed and sealed. **Never presented as continuous.** | (terminal) |
| `CORRUPT` | Unreadable or no decodable video. Hashed and sealed as-is. | (terminal) |

State transitions are enforced in `sentinel.db` (illegal transitions raise).
A `FINALIZED` segment may still carry a **warning** (e.g. internal timestamp gap > `max_internal_gap_s`,
clock-step during recording); warnings are columns, not states.

**Missing intervals** are first-class rows in `recording_gaps`, never inferred later by subtraction.

## 4. Timestamps

### 4.1 Clock source and UTC handling (E1)

All stored timestamps are **UTC, ISO-8601 with `Z`**, microsecond precision. The configured
display timezone (IANA name) is stored in each run and in export manifests.

**Clock source.** Segment times are the time the **recording PC received** the footage,
expressed on the PC's wall clock (UTC), which the operating system keeps synchronised (Windows
Time service / chrony / systemd-timesyncd, to the source the administrator configured — on an
isolated network, a local NTP server; docs/07). They are **not** the camera's capture time and
not the camera's on-screen (OSD) clock; camera-to-PC latency (encoder + network, typically
< 1 s on a LAN) is not corrected. Three clocks are involved:

| Clock | Used for | Weakness |
|---|---|---|
| Camera clock (stream time `t`, from FFmpeg `-progress` and `segments.csv`) | position within a run | free-running crystal: tens of ppm fast or slow (~4 s/day at 50 ppm) |
| PC monotonic clock `m` | intervals, anchoring | never steps; on **Windows not disciplined** by time sync (drifts vs UTC); on Linux slewed with NTP |
| PC wall clock `w` (UTC) | the reported time | disciplined by the OS, but can **step** (operator change, large NTP correction) |

### 4.2 Run anchor and drift-corrected segment times

Per run, at FFmpeg launch the supervisor records `(wall_launch, mono_launch)`. FFmpeg
`-progress` reports output stream time `t` at monotonic time `m` (once per second). Because a
packet at stream time `t` cannot be reported before it was received, `m − t` is an upper bound
on the reception offset; its minimum filters reporting and scheduling jitter. The run anchor is

```
anchor_mono = min over reports before the first segment closes (m − t)
anchor_utc  = wall_launch + (anchor_mono − mono_launch)
```

Phase 2 used `segment time = anchor_utc + stream time` for the whole run. That accumulates the
camera-vs-PC clock drift and, on Windows, the monotonic-vs-wall drift. Since `reception_tracking_v1`
(`recording_runs.time_method`) each segment boundary carries a **correction** `c`:

```
at each segment boundary b (stream time t_b, when FFmpeg closes the segment):
  camera_b = min over reports with t in [t_b − 8 s, t_b + 2 s] of (m − t)  −  anchor_mono
  host_b   = (wall − mono now) − (wall − mono at launch) − (sum of detected clock steps)
  measured = camera_b + host_b
  c_b      = c_prev + clamp(measured − c_prev, ± (max_clock_drift_ppm·1e-6·(t_b − t_prev) + 0.1 s))
utc(t)     = anchor_utc + t + c(t),  c linear between the segment's start and end boundaries
segment.start_utc = utc(stream_start_s)      segment.end_utc = utc(stream_end_s)
```

* **Contiguous.** A segment's start boundary *is* the previous segment's end boundary, and a
  boundary's correction is fixed once (write-once in the ledger), so `start_utc(N+1) ==
  end_utc(N)` exactly when FFmpeg reports no gap. `c = 0` at stream time 0, so the first segment
  starts at the anchor as before.
* **Tracks drift without accumulating error.** `measured` is an absolute measurement against
  reception, not a running sum, so after days the error is still the measurement noise (tens of
  ms), not ppm × elapsed time.
* **Steps are not followed.** `host_b` excludes wall-clock steps (`ClockStepDetector`: a jump
  > `clock_step_tolerance_s` between two 0.5 s samples), which are still recorded as `clock_step`
  events with `clock_anomaly = 1` on the run's segments, as before. A new run re-anchors to the
  (stepped) wall clock. Gradual change (Windows monotonic drift, NTP slewing of the wall clock)
  is followed.
* **Jumps are not followed.** The rate limit (`max_clock_drift_ppm`, default 1000 ppm — ten
  times worse than any working clock) plus a 0.1 s jitter allowance per boundary means a sudden
  change in reception timing (lost real time, a sub-tolerance clock step) is never smeared into
  the times. A boundary where the limit applied gets `time_correction_limited = 1`, the segment
  the warning `time_correction_limited`, the run a counter and one `time_correction_limited`
  event; if the change persists the correction catches up only at the limit rate.
* **Absorbed discontinuities** (real time FFmpeg removed from the timeline, docs/PHASE2_REPORT.md §2) are
  unchanged: the supervisor measures the lost time, ends the run, and the finaliser records a
  `source_discontinuity_absorbed` gap and shifts later footage. The drift tracker is told the
  break (`set_break`), so reports after it are measured on the shifted timeline and the shift is
  never counted as drift. The supervisor's lag baseline already followed slow drift; at 1 %
  drift its excess stays < 0.3 s, far below the 3 s tolerance, so drift causes no false gaps
  (tested).
* **Interrupted segments** (no CSV entry) start at the previous segment's end (or 0 for the
  first); their end correction is measured at the run's last progress report. Segments closed by
  startup recovery after a crash (no in-memory reports) **hold** the run's last measured
  correction (warning `time_correction_held`). Runs recorded before this version have
  `time_method` NULL and keep their single-anchor times.
* Every segment also stores `observed_closed_utc` (PC wall time when the supervisor saw it close)
  as an independent sanity reference (it lags reception by up to one poll interval; for a run's
  first segment by ~1 s more, because FFmpeg buffers input while probing the stream).

Ledger columns (added append-only by `src/sentinel/ledger_timing.py`):
`recording_runs.time_method, time_correction_s, camera_drift_ppm` (camera stream clock vs PC
monotonic, positive = camera fast), `host_clock_drift_ppm` (PC wall vs monotonic, steps
excluded, positive = wall faster), `drift_measured_over_s, time_correction_limited_count`;
`segments.time_correction_start_s, time_correction_end_s, time_correction_stream_s,
time_correction_limited, time_correction_detail` (JSON: measured, camera and PC components,
number of reports). Drift figures are reported once measured over ≥ 60 s.

### 4.3 Alternatives considered

| Alternative | Why not chosen |
|---|---|
| Keep one anchor per run, restart FFmpeg every N hours to re-anchor | Each restart loses a GOP or more of footage and creates a gap; drift still accumulates within N hours. |
| Re-anchor each segment independently (`start = reception estimate`) | Breaks contiguity: boundaries jump by the measurement noise, creating tiny fake gaps/overlaps and making a real gap indistinguishable from noise. |
| Linear regression of `m − t` over the whole run (rate + offset) | Accurate for constant drift, but a regression over days reacts slowly to drift changes (temperature) and one bad interval (a lost-time event) bends the whole line; still needs a jump detector. The local minimum + rate limit is simpler and fails safe. |
| Use wall-clock time of each progress report directly | Follows wall-clock steps and every scheduling delay; no immunity to steps. |
| Use the camera's own timestamps (RTCP sender reports / OSD) | Camera clock facts are unknown (docs/02 A6); RTCP is not exposed by FFmpeg `-c copy` progress; OSD is burned into pixels. Kept as **corroboration** (§4.5), not as the time source. |
| FFmpeg wall-clock timestamps (`-use_wallclock_as_timestamps`, `-copyts`) | Rewrites the evidential stream's timestamps or makes them depend on FFmpeg's internal clock handling; we keep the camera's stream untouched and record time alongside it. |

### 4.4 Clock observations (measured clock offset, E1)

The service writes a `clock_observations` row at start-up, every `clock_observation_interval_s`
(default 900 s) and after a detected clock step. Each row has the PC's UTC and monotonic time,
the raw wall-minus-monotonic offset, the PC's wall-vs-monotonic drift since service start
(steps excluded, ppm), the total of detected steps, the current run's camera drift (ppm) and
applied correction, and what the operating system reports about synchronisation
(`src/sentinel/timesync.py`, read-only commands, no administrator rights):

| OS | Command | Recorded |
|---|---|---|
| Windows | `sc query w32time`, `w32tm /query /status /verbose` (fallback `/status`), registry `W32Time\Parameters` | service state, synchronised (leap indicator / stratum / source), source, last good sync (UTC, from "Time since Last Good Sync Time"), `Phase Offset` as raw text value (sign convention not documented clearly enough to normalise) |
| Linux | `chronyc tracking`, else `timedatectl show` + `show-timesync` | synchronised, source, offset (chrony: local minus NTP time, from the explicit fast/slow wording), frequency error |

On the development PC (Windows 11 Home) the Windows Time service is demand-start and was
stopped: `w32tm` returns "The service has not been started. (0x80070426)". This is recorded as
`synchronized = 0` with reason `windows_time_service_stopped` and the *configured* source
(`time.windows.com,0x9`), flagged as not confirmed. **Deployment must set the Windows Time
service to start automatically and point it at the approved time source** (docs/07). Field
labels in `w32tm` output are localised; on non-English Windows the raw text is kept and
`synchronized` is left unknown rather than guessed. Linux and successful-`w32tm` parsing are
implemented from the documented formats and are **not verified on a live system**.

The diagnostic report includes `clock_observations.csv`, the drift/correction columns in
`runs.csv`/`segments.csv`, and a "Time accuracy" section in `summary.txt`.

### 4.5 How an examiner states time accuracy

A defensible statement has four parts; the ledger and diagnostic report supply each:

1. **Clock source.** "Times are the recording PC's receipt time in UTC, from its system clock
   synchronised to *source* (clock_observations.sync_source); synchronisation was reported
   *yes/no* at *times*." Quote the observations spanning the footage; a gap in synchronisation
   (e.g. `synchronized = 0`) must be stated.
2. **Measured offset.** Where the OS reports it (`os_offset_s`, chrony), state the PC's offset
   from its reference at the observations before and after the footage. On Windows, state the
   last good synchronisation time and, if captured, the w32tm phase offset as reported.
3. **Drift and corrections.** From `recording_runs`: camera clock drift (`camera_drift_ppm`),
   PC monotonic drift (`host_clock_drift_ppm`) and from `segments`: the correction applied
   (`time_correction_end_s`). Any segment with `clock_anomaly = 1` (clock step), warning
   `time_correction_limited` or `time_correction_held`, or an adjacent gap row must be listed
   with its reason.
4. **Residual uncertainty.** Receipt time ≈ capture time + camera-to-PC latency (measure it once
   per installation with step 5 below; an unmeasured estimate for H.264 IP cameras is 0.2-1 s), plus the PC
   clock's offset from UTC (step 2), plus measurement noise (< 0.1 s in tests; ±0.5 s is the
   tested bound at 1 % drift). Express the result as e.g. "within ±1 s of UTC" only when steps
   1-3 support it.

**OSD comparison procedure (real camera; corroboration, not the time source).**

5. *Installation / each drive swap:* hold a reference clock in view of the camera (a phone or
   GPS clock showing seconds, itself checked against an authoritative time source; note its
   offset). Record ≥ 30 s. On review, step frame by frame to a seconds rollover of the reference
   clock; the frame's ledger time is `segment.start_utc + (frame pts − first frame pts)`
   (the in-segment correction is linear and below the drift rate, i.e. negligible within a
   segment). `ledger time − reference time` = capture-to-receipt latency + PC clock error.
6. *Camera OSD:* if the camera burns its own clock into the picture (OSD), note at ≥ 10 points
   spread over the recording (start, each day, either side of every gap and every
   `clock_anomaly` segment) `OSD time − ledger time`, converting the OSD to UTC (state the
   camera's configured timezone/DST). A constant difference is the camera clock's offset plus
   latency; a difference that changes steadily is camera clock drift (compare its slope with
   `camera_drift_ppm`); a sudden change marks a camera clock adjustment (camera NTP) or a PC
   clock step (cross-check `clock_step` events). Record the points and the camera's NTP
   configuration in the case notes.
7. Never "correct" exported footage to the OSD clock; report the comparison alongside the ledger
   times.

## 5. Ledger schema (SQLite)

Tables (see `src/sentinel/db.py` for DDL): `recording_runs`, `segments`, `recording_gaps`,
`events`, `schema_meta`; `clock_observations` and the time-correction columns of §4.2 are added
append-only by `src/sentinel/ledger_timing.py` (own `schema_meta` key `timing_schema_version`).
Phase 3 adds `drives`, `export_batches`, `export_items`.

Key `segments` columns: `segment_id` (UUIDv4), `camera_id`, `run_id`, `seq`, `rel_path`, `state`,
`stream_start_s`, `stream_end_s`, `start_utc`, `end_utc`, `duration_s` (measured), `size_bytes`,
`sha256`, `video_codec`, `width`, `height`, `packet_count`, `keyframe_count`, `max_pts_gap_s`,
`interrupted`, `clock_anomaly`, `validation_detail` (JSON), `sealed_utc`.

Gap rows: `camera_id`, `start_utc`, `end_utc` (NULL while open), `reason`, `prev_segment_id`,
`next_segment_id`, `kind` (`between_runs` | `within_segment`).

Durability: WAL journal, `synchronous=FULL`, every state change in a transaction.

## 6. Retention (Phase 3; nothing deletes data in Phase 2)

* Default: **no automatic deletion.** When storage reaches the critical threshold the
  recorder stops cleanly and reports *Storage full — recording stopped*.
* Optional admin-enabled policy (requires administrator authorization and an audit event):
  delete a sealed segment only if (a) older than `retention_days`, (b) verified present on at
  least `min_verified_copies` export drives, and (c) not `CORRUPT` (quarantine is never auto-deleted).
