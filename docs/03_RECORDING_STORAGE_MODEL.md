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

All stored timestamps are **UTC, ISO-8601 with `Z`**, microsecond precision. The configured
display timezone (IANA name) is stored in each run and in export manifests.

Per run, at FFmpeg launch the supervisor records `(wall_launch, mono_launch)`. FFmpeg
`-progress` reports output stream time `t` at monotonic time `m`. Because a packet at stream
time `t` cannot have been received before the first packet arrived plus `t`, `m − t` is an
upper bound on the monotonic time of first-packet receipt; the run anchor is

```
anchor_mono = min over reports (m − t)
anchor_utc  = wall_launch + (anchor_mono − mono_launch)
segment.start_utc = anchor_utc + segment.stream_start_s     (from segments.csv)
segment.end_utc   = anchor_utc + segment.stream_end_s
```

* This is the time the **recording host received** the first packet, not the camera's capture
  time. Camera-to-host latency (typically < 1 s on a LAN) is not corrected.
* Using the monotonic clock makes derived times immune to a wall-clock step **during** a run.
* **Clock-step detection:** every status tick compares `wall − wall_launch` with
  `mono − mono_launch`. A difference > `clock_step_tolerance_s` (default 2 s) records an event
  and sets `clock_anomaly=1` on segments of that run. A new run re-anchors to the (stepped) wall clock.
* Interrupted segments have no CSV entry; their start is the previous segment's end in the
  same run (or 0 for the first), and their duration is ffprobe-measured.
* Every segment also stores `observed_closed_utc` (host wall time when the supervisor saw it
  close) as an independent sanity reference.

## 5. Ledger schema (SQLite)

Tables (see `src/sentinel/db.py` for DDL): `recording_runs`, `segments`, `recording_gaps`,
`events`, `schema_meta`. Phase 3 adds `drives`, `export_batches`, `export_items`.

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
