# Branch `req/review-drive-export` — review-drive export

## 1. Summary
An operator can now export recordings to a review drive with one command (the future
**Prepare Review Drive** button will call the same code). The drive gets a `Recordings` folder
of MP4 videos named by local date, time and time zone that play on double-click, the untouched
original recordings, a plain-language summary page that highlights gaps, and fingerprint
(SHA-256) lists. Every file is read back from the drive and checked before the export is
reported complete; MP4 videos are additionally checked picture by picture against the
originals. Unplugging the drive mid-export, a full drive, or a name clash never produces a
"complete" drive and never overwrites anything; re-inserting the same drive finishes the job.

## 2. Requirements addressed
| ID | What | Stated / design choice |
|---|---|---|
| U3 | Open folder → double-click MP4; README_FIRST.txt; HTML summary with no external resources | Stated |
| U4 | Insert → export → verify → remove only when told (`export_status.json`, final message) | Stated; software eject NOT implemented (see §5) |
| S5 | `Recordings/<YYYY-MM-DD>/<HHMMSS>-<HHMMSS>_<TZ>.mp4`, hourly, contiguous only, gaps start new files; SHA-256 manifests | Stated; exact naming = design choice (seconds precision, abbreviation **and** UTC offset, e.g. `140000-150000_EST_UTC-0500.mp4`, so DST fall-back hour and shared abbreviations are unambiguous) |
| E2 | Hashes of every transferred file, read back from the destination (Windows: `FILE_FLAG_NO_BUFFERING`) | Stated |
| E4 | Originals copied unchanged under `Originals/`; MP4 → source mapping in manifest; MP4 hash never claimed equal to an original's | Stated |
| E7 | Recorded tests for export, drive full, drive removed, conflicts, corrupt input, parallel recording | Stated |
| docs/04 §5 | `drives` table (owned here, per coordinator) | Stated |

Design choices (owner may revisit): an MP4 never spans two recording runs (different FFmpeg runs
may change H.264 parameter sets); a segment with an internal timestamp gap gets an MP4 of its
own (cannot split a segment without re-encoding); planning stops at the first not-yet-sealed
footage; "late" segments (sealed after an export had already passed their time) are included in
the next export; a drive too small for everything gets the oldest footage, a warning, and the next
export continues from there; README_FIRST.txt that already exists with different text is kept
(warning in ledger) rather than failing; exporting to the recording storage volume is refused
(E-005) unless `--allow-same-volume` (tests only); operator id comes from `--operator-id`
(config.py was not changed, to avoid conflicts).

## 3. What changed
* `src/sentinel/export/` (new): `target.py` (ExportTarget, drive identity, SafeRemover protocol,
  ManualRemovalFallback), `fileio.py` (.partial → fsync → no-replace rename; unbuffered read-back),
  `planner.py` (interval, eligibility, contiguous/hourly grouping, gaps, coverage, space),
  `ledger.py` (batch/item state machine), `media.py` (concat remux, decoded per-frame hashes from
  file / pipe / joined originals), `manifest.py` (README, HTML, CSV, JSON, SHA256SUMS),
  `engine.py` (orchestration, error classification, resume), `status.py` (`export_status.json`),
  `__main__.py` (CLI `python -m sentinel.export run|status`).
* `src/sentinel/db.py`: separate `EXPORT_DDL` block (`drives`, `export_batches`, `export_items`)
  appended after the existing DDL + one line in `_migrate`. Additive; `SCHEMA_VERSION` unchanged.
* `src/sentinel/alerts.py`: E-001 drive full, E-002 drive removed, E-003 verification failed,
  E-004 existing different files, E-005 drive cannot be used, E-006 local recording unreadable,
  E-007 software problem. None of them stops recording.
* Tests: `tests/unit/test_export_units.py`, `tests/integration/test_review_export.py`.

### Export_Manifest.json schema (`format: "sentinel-review-export"`, `format_version: 1`)
Top level: `export_id`, `purpose`, `camera_id`, `generated_utc`, `planned_utc`,
`interval{start_utc,end_utc,start_local,end_local,display_timezone}`, `drive{drive_id, …identity}`,
`software{app_version, ffmpeg_version}`, `operator_id`, `verification{read_back_methods, method_notes, rule}`,
`files[]`, `originals[]`, `derivatives[]`, `gaps[]`, `coverage{interval_s, recorded_s, unaccounted[]}`,
`warnings[]`, `documents[]` (README/HTML/CSV with hashes), `notes[]`.
* `files[]` — one self-contained entry per exported file: `path` (relative to drive root, `/`),
  `kind` (original | framehash | mp4 | quarantine | quarantine_framehash), `sha256` (read back),
  `size_bytes`, `segment_ids[]`, `content_sha256` (decoded-frame hash where available),
  `framehash_path`, `start_utc`/`end_utc`, `start_local`/`end_local`, `video_codec`, `width`,
  `height`, `segment_state`, `verified_utc`, `verify_method`, `already_present`.
* `originals[]` — per segment: ids, run/seq, state, `exported_path`, `quarantined`, times (UTC +
  local), `sealed_sha256`, `content_sha256` + `content_hash_method` (+ `content_hash_computed_at_export`),
  `framehash_file`, codec/resolution/pix_fmt, flags, `late`.
* `derivatives[]` — per MP4: `path`, `sha256`, `content_sha256`, `frame_count`, times, `flags`,
  `sources[{segment_id, original_path, original_sealed_sha256}]` in order, `content_reference`,
  `frames_differing_from_segment_lists`, `gap_ids_after` (gaps between this file and the next).
* `gaps[]` — every gap row overlapping the interval: `gap_id`, `kind`, UTC + local times,
  `duration_s`, `reason`, `reason_text`, `open`, `prev/next_segment_id`.
SHA256SUMS.txt (`<hash>  <path>`, LF, no comments) covers every file incl. the JSON. Times: UTC
ISO-8601 with `Z`. The per-frame sidecars use FFmpeg's `framehash` text format (SHA-256).

## 4. Tests run (development host, Windows 11, Python 3.14, FFmpeg 9.0.1)
* `"…\.venv\Scripts\python.exe" -m pytest tests/unit -q` → **84 passed** (50 pre-existing + 34 new).
* `… -m pytest tests/integration/test_review_export.py -q -p no:cacheprovider` → **15 passed**
  (62 s), run twice in a row on the final code, both 15/15.
* Existing integration suites, one command
  (`test_media_hashing.py test_recorder_faults.py test_unattended.py test_service_process.py`) on
  the code before the last planner fix → **29 passed, 1 failed** (361 s). The failure was
  `test_recorder_faults.py::test_short_freeze_is_recorded_as_internal_gap` (timed out waiting for
  the internal gap row; timing-sensitive, other engineers were running FFmpeg tests on the machine;
  this branch does not touch recorder code). Rerun alone → **1 passed**. The last planner fix
  touches only `sentinel/export/planner.py`, which the recorder suites do not import.
* An earlier full run of the export tests found a real bug (a 1-frame final segment followed by
  the stop gap was split into its own MP4); fixed in `planner._joinable`, regression unit test added.

Integration tests: happy path (ffprobe plays every MP4, `moov` before `mdat`, every SHA256SUMS
entry re-hashed, manifest/CSV/ledger consistent, local originals untouched and read-only, second
press = nothing to export); fake ejector → SAFE_TO_REMOVE; drive full before start (E-001);
drive too small → partial export + next drive continues with no hole and no overlap; ENOSPC
mid-copy → FAILED, partials removed, nothing claimed verified, retry on the same drive
re-verifies existing files instead of copying; drive unplugged mid-copy and mid-verify → INTERRUPTED
(E-002), `.partial` left and discarded on resume, nothing re-copied; existing different file →
E-004, untouched; existing identical file → re-verified, not copied; corrupt segment → Quarantine;
damaged copy of an original and of an MP4 → E-003; damaged final frame → joined-decode reference;
missing per-frame lists computed on demand; CLI export in a separate process **twice while the
recorder keeps recording** (one run, no new gap, second interval starts exactly where the first ended).

Found during development: a recording cut off by a camera drop ends in a damaged frame whose
error-concealed picture can differ when the segment is decoded alone versus joined (1 of 6 trials,
FFmpeg 9.0.1; also differs between `-threads` settings). The MP4 (same packets) matched the joined
decode of the originals in 6 of 6 trials. Export therefore falls back to decoding the originals as
one stream and requires an exact match; the affected frame indices are listed in the manifest.

## 5. NOT executed / not verified
* **Software eject/power-off: not implemented.** `ManualRemovalFallback` flushes and tells the
  operator to use Windows "Safely Remove Hardware"; status `COMPLETE_REMOVE_MANUALLY`. Only a fake
  ejector was tested. (`CM_Request_Device_EjectW` may work without admin but could not be verified
  without touching a real drive.)
* No real removable drive, USB unplug, real disk-full, exFAT/FAT32/NTFS-on-USB volume was used:
  drives were temp directories on the internal NTFS disk; full/unplug were injected faults.
* Linux paths (O_DIRECT read-back, `/proc/self/mountinfo` identity, hard-link no-replace rename)
  not executed.
* Disk serial/model/size/bus type are not discovered here (owned by `sentinel.drives`); the CLI
  identifies by volume GUID (+ sub-folder) or `SENTINEL_DRIVE.json` `drive_uuid`.
* No 5MP export was measured. Expected cost from the Phase 2 measurement (decoded hashing ≈ 0.55 s
  per second of 5MP@30 video on this 20-thread host): verifying one day of footage ≈ 13 h of
  decoding, roughly double if per-frame lists are missing. Not measured; likely the dominant
  operational problem.
* Real cache-bypass effect not demonstrated (user space cannot make cache and disk disagree); only
  that the unbuffered path is used and returns exact bytes.

## 6. Risks, limitations, open decisions
1. **Verification time at 5MP** (above). Options: verify MP4s against the decoded originals only
   on a sample, verify by compressed-packet comparison, or accept long exports. Owner decision.
2. Read-back bypasses the OS cache but not the drive's internal cache; a drive that ignores flush
   commands is not detected (documented in the manifest `method_notes`).
3. A sealed original missing from local storage blocks every export (E-006) until support fixes it
   (design choice: never silently skip evidence). A missing CORRUPT file is only a warning.
4. "Late" segments, a missing local CORRUPT file, or clock changes produce warnings in the
   manifest/HTML; "unaccounted time" (no footage, no gap row) is flagged as missing.
5. MP4s are video-only; if audio is ever authorised it exists only in `Originals/`.
6. Merge risks: `db.py` (separate block, low risk); `alerts.py` CATALOG (E-codes); another branch
   adding a different `drives` table would collide on `CREATE TABLE IF NOT EXISTS` — this branch
   owns it per the coordinator.
7. A FAILED batch is not resumed; the next press plans a new batch (existing files re-verified).
   An INTERRUPTED batch on a drive that never returns stays INTERRUPTED (harmless).
8. Open: who may export, whether the operator id must be mandatory, and whether one review drive
   may receive the same footage twice (currently: no, intervals continue from the last complete
   batch on any drive).

## 7. Recommendation
**Merge after** the owner accepts the manual-removal fallback (or a verified eject lands with
`sentinel.drives`) and decides on the 5MP verification-time trade-off (§6.1). The code paths are
tested end to end with real recordings; the hardware-facing parts are not.
