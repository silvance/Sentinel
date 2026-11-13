# Branch `req/review-drive-export` — review-drive export

## 1. Summary
An operator can now export recordings to a review drive with one command (the future
**Prepare Review Drive** button will call the same code). The drive gets a `Recordings` folder
of MP4 videos named by local date, time and time zone that play on double-click, the untouched
original recordings, a plain-language summary page that highlights gaps, and fingerprint
(SHA-256) lists. Every file is read back from the drive and its fingerprint checked before the
export is reported complete. The MP4 videos are also compared picture by picture with the
originals: fully, by sample, or later. How thoroughly is a setting the owner still has to choose
(provisionally "sample"). Unplugging the drive mid-export, a full drive or a name clash never
produces a "complete" drive and never overwrites anything; pressing the button again with the
same drive finishes the job.

## 2. Requirements addressed
| ID | What | Stated / design choice |
|---|---|---|
| U3 | Open folder → double-click MP4; README_FIRST.txt; HTML summary with no external resources | Stated |
| U4 | Insert → export → verify → remove only when told (`export_status.json`, final message) | Stated; software eject NOT implemented (see §5) |
| S5 | `Recordings/<YYYY-MM-DD>/<HHMMSS>-<HHMMSS>_<TZ>.mp4`, hourly, contiguous only, gaps start new files; SHA-256 manifests | Stated; exact naming = design choice (seconds precision; abbreviation **and** UTC offset, e.g. `140000-150000_EST_UTC-0500.mp4`; both times written in the start's offset, so the DST fall-back hour is unambiguous) |
| E2 | Hashes of every transferred file, read back from the destination (Windows: `FILE_FLAG_NO_BUFFERING`) | Stated |
| E4 | Originals copied unchanged under `Originals/`; MP4 → source mapping in manifest; MP4 hash never claimed equal to an original's | Stated |
| E7 | Recorded tests for export, drive full, drive removed, conflicts, corrupt input, parallel recording | Stated |
| docs/04 §5 | `drives` table (owned here, per coordinator) | Stated |

Design choices (owner may revisit):
* An MP4 never spans two recording runs, because different FFmpeg runs may change H.264 parameter sets.
* A segment with an internal timestamp gap gets an MP4 of its own; a segment cannot be split without re-encoding.
* Planning stops at the first footage that is not yet sealed.
* "Late" segments, sealed after an export had already passed their time, are included in the next export.
* A drive too small for everything gets the oldest footage and a warning; the next export continues from there.
* A README_FIRST.txt that already exists with different text is kept, with a warning in the ledger, rather than failing the export.
* Exporting onto the volume of the recording storage, ledger, logs or system disk is refused (E-005). The check fails closed. `--allow-same-volume` bypasses it for development and tests only.
* A FAILED batch is retried on the same drive while no later export has moved past it.
* The operator id comes from `--operator-id`.

## 3. What changed
* `src/sentinel/export/` (new):
  * `target.py`: ExportTarget, drive identity, the protected-volume check, the SafeRemover protocol and ManualRemovalFallback.
  * `fileio.py`: `.partial` → fsync → rename without replacing; unbuffered read-back.
  * `planner.py`: the export interval, eligibility, contiguous/hourly grouping, gaps, coverage, the space check and the segment snapshot.
  * `ledger.py`: the batch/item state machine; retries while the ledger is locked; FAILED-batch retry.
  * `contentcheck.py`: how sample spans are chosen and the damaged-final-frame rule.
  * `media.py`: concat remux, per-frame hashes (from a file or a pipe), span decode, packet timestamps, decoder-error check.
  * `manifest.py`: README, HTML, CSV, JSON, SHA256SUMS.
  * `engine.py`: orchestration, error classification, resume, `verify-content`.
  * `status.py`: `export_status.json`.
  * `__main__.py`: the CLI `python -m sentinel.export run|verify-content|status`.
* `src/sentinel/db.py`: a separate `EXPORT_DDL` block (`drives`, `export_batches`, `export_items`) appended after the existing DDL, plus one line in `_migrate`. It only adds tables, so `SCHEMA_VERSION` is unchanged. Columns added after commit 8daf276 are added to existing ledgers with `ALTER TABLE` by `ExportLedger`.
* `src/sentinel/config.py`: a new optional `[export]` section (`content_check`, `sample_edge_frames`, `sample_spans`, `sample_span_frames`). `config/sentinel.example.toml` documents it.
* `src/sentinel/alerts.py`: E-001 drive full, E-002 drive removed, E-003 verification failed, E-004 existing different files, E-005 drive cannot be used, E-006 local recording unreadable or changed, E-007 software problem. None of them stops recording.
* Tests: `tests/unit/test_export_units.py`, `tests/integration/test_review_export.py`.

### Verification levels (`[export] content_check`)
**Always done, and required for COMPLETE / SAFE TO REMOVE:** every file is read back from the drive with the OS cache bypassed, and its SHA-256 compared with a value known before the read:
* originals and sidecars: the hash recorded when the recording was sealed;
* MP4s: the hash of the file as FFmpeg wrote it, taken by a normal read straight after FFmpeg closed it and before the rename;
* documents: the hash of the generated bytes.

Before remuxing or decoding, the local originals are re-hashed against their sealed SHA-256.

The picture check of each MP4 is configurable. The level each MP4 actually got is recorded in `derivatives[].content_check` and in the HTML.
* `full`: every frame decoded from the read-back bytes must equal the sources' per-frame lists. Missing lists are computed on demand.
* `sampled`: the frame count (demux only) must equal the sources' total. Then the first and last `sample_edge_frames` frames, plus `sample_spans` spans of `sample_span_frames` frames, are decoded from the file on the drive. Span positions are pseudo-random, seeded by export id and file. Each span is reached by an accurate seek with `-copyts -enc_time_base demux`, mapped by exact container timestamps, and compared by position. If a source's per-frame list is missing, that MP4 is recorded as `deferred`, with the reason "not yet available", rather than spending hours decoding.
* `deferred`: no picture check. The manifest says "content not yet verified".

`python -m sentinel.export verify-content --config … --target …` later runs the full check on a reinserted drive. It byte-checks again, can use the drive's own Originals if the local ones are gone, records the result in the ledger, and writes `Manifests/<export>/Content_Verification_<UTC>.json`. Manifests already on the drive are never rewritten, so the HTML still says "not yet checked" and the supplementary file is the record. (Review 2026-11-12) The results are entered in the ledger only after that file has been written and read back, so an interruption leaves the videos to be checked again rather than marked checked without proof. A failed check never changes the video's state: it is recorded as `content_check` `mismatch` (pictures or bytes differ, or the video is missing) or `error` (could not be checked: decoder, tool or source problem), and the video is checked again on the next run, which therefore never says that every video was already checked.

Damaged final frames: a difference is accepted only for the last ≤ 2 frames of a source segment that is INTERRUPTED, or that FFmpeg reports decoding errors for. Accepted frames are listed as `unverifiable_damaged_frames`. Any other difference fails the export (E-003). The earlier unbounded "joined decode" fallback was removed.

**Provisional default: `sampled`.** This is an open decision for the owner.
* Measured 5MP@30 costs (below): full ≈ 13 h of decoding per day of footage; sampled ≈ seconds per hourly file plus the I/O; deferred = I/O only.
* Byte verification already proves that every file on the drive is bit-identical to what was written, and that the originals equal what was sealed.
* The remux is a stream copy whose correctness is a property of FFmpeg. The frame count plus positional samples catch missing, duplicated or reordered segments and systematic remux faults.
* `full` remains available, and `verify-content` can upgrade any drive later.

### Measured timings (this host, FFmpeg 9.0.1, simulated camera, drive = temp directory on internal NVMe)
| Footage | deferred | sampled | full |
|---|---|---|---|
| 640×480@15, 120 s, 12 × 10 s segments, 13 MB, 1 MP4 | 0.86 s (0.007 s/s) | 1.69 s (0.014 s/s) | 2.95 s (0.025 s/s) |
| **2592×1944@30**, 60 s, 6 segments, 31 MB (4 Mbit/s), 1 MP4 | 0.88 s (0.015 s/s) | 4.21 s | 32.75 s (**0.547 s/s**) |

Extrapolation (estimate, NOT measured):
* **Full** at 5MP@30: ≈ 0.55 s of decoding per second of video, so ≈ 13 h per day of footage on this 20-thread host, longer on a slower PC. Roughly double that if per-frame lists are missing.
* **Sampled** decodes a fixed ≈ 300 frames per hourly MP4 (spans plus keyframe lead-in), about 5–10 s each, so ≈ 2–4 min of decoding per day. Real exports will instead be limited by drive I/O. A day of 5MP at 4–12 Mbit/s is 43–130 GB. On a USB HDD at ~100–150 MB/s that is about 5–25 min to write, the same again for the uncached read-back, and up to the same again for the MP4 write-time hash. The originals are written as well as the MP4s, which doubles the volume. Expect roughly 0.5–2 h per day of footage for `sampled` or `deferred`; this is not measured on a real USB drive.

### Export_Manifest.json schema (`format: "sentinel-review-export"`, `format_version: 1`)
Top-level fields:
* `export_id`, `purpose`, `camera_id`, `generated_utc`, `planned_utc`, `operator_id`;
* `interval{start_utc, end_utc, start_local, end_local, display_timezone}`;
* `drive{drive_id, …identity}`;
* `software{app_version, ffmpeg_version}`;
* `verification{read_back_methods, method_notes, rule, content_check_configured}`;
* `files[]`, `originals[]`, `derivatives[]`, `gaps[]`;
* `coverage{interval_s, recorded_s, unaccounted[]}`, `warnings[]`;
* `documents[]` (README, HTML and CSV with their hashes), `notes[]`.

The arrays:
* `files[]`: one self-contained entry per exported file:
  * `path` (relative to the drive root, `/` separators) and `kind` (original | framehash | mp4 | quarantine | quarantine_framehash);
  * `sha256` (read back) and `size_bytes`;
  * `segment_ids[]`, `content_sha256` (decoded-frame hash where available), `framehash_path`;
  * `start_utc`/`end_utc`, `start_local`/`end_local`;
  * `video_codec`, `width`, `height`, `segment_state`;
  * `verified_utc`, `verify_method`, `already_present`, `content_check` (MP4 only).
* `originals[]`: per segment:
  * ids, run/seq, state, `exported_path`, `quarantined`;
  * times (UTC and local), `sealed_sha256`;
  * `content_sha256` and `content_hash_method` (plus `content_hash_computed_at_export`);
  * `framehash_file`, codec/resolution/pix_fmt, flags, `late`.
* `derivatives[]`: per MP4:
  * `path`, `sha256`, `written_sha256`, `byte_verification`;
  * `content_sha256` (full level only), `frame_count`, times, `flags`;
  * `sources[{segment_id, original_path, original_sealed_sha256}]` in order;
  * `content_check{level, spans, frames_checked | reason, unverifiable_damaged_frames, checked_utc, description}`;
  * `gap_ids_after` (gaps between this file and the next).
* `gaps[]`: every gap row overlapping the interval: `gap_id`, `kind`, UTC and local times, `duration_s`, `reason`, `reason_text`, `open`, `prev/next_segment_id`.

`Content_Verification_*.json` (`format: "sentinel-content-verification"`, `format_version: 1`) contains `export_id`, `drive_id` and `videos[{path, previous_level, result, read_back_sha256, frame_count, content_sha256, unverifiable_damaged_frames, checked_utc}]`. `result` is `OK`, `FAILED` (with `outcome: "mismatch"` and `error`) or `NOT_CHECKED` (with `outcome: "error"` and `error`).

SHA256SUMS.txt (`<hash>  <path>`, LF line endings, no comments) covers every file, including the JSON. All stored times are UTC ISO-8601 with `Z`. The per-frame sidecars use FFmpeg's `framehash` text format (SHA-256).

## 4. Tests run (development host, Windows 11, Python 3.14, FFmpeg 9.0.1)
After the review fixes:
* `"…\.venv\Scripts\python.exe" -m pytest tests/unit -q` → **90 passed**.
* `… -m pytest tests/integration/test_review_export.py -q -p no:cacheprovider` → **26 passed** (86 s).
* Recorder integration suites (`test_media_hashing`, `test_recorder_faults`, `test_unattended`, `test_service_process`) after the review fixes → **29 passed, 1 failed** (318 s). The failure was the timing-sensitive `test_recorder_faults.py::test_camera_offline_shows_interrupted_and_recovers`, while other engineers' FFmpeg tests were running; rerun alone → **1 passed**. Before the review fixes the same suites also gave 29 passed / 1 failed, with a different timing test (`test_short_freeze_is_recorded_as_internal_gap`) that likewise passed alone. This branch does not change recorder code; `config.py` only gains the optional `[export]` section.

What the integration tests cover:
* Happy path in `sampled` mode: ffprobe plays every MP4; `moov` comes before `mdat`; every SHA256SUMS entry is re-hashed; manifest, CSV and ledger agree; written hash equals read-back hash; local originals are untouched and read-only; a second press finds nothing to export.
* `full` mode, and `deferred` followed by `verify-content`.
* With per-frame lists missing: `sampled` records `deferred`; `full` computes the lists on demand.
* Damaged final frame of an INTERRUPTED segment is accepted and listed (full and sampled).
* These fail with E-003: a difference at the end of an intact segment, a 3-frame tail, a middle frame, a wrong picture at a sampled position.
* A fake ejector gives SAFE_TO_REMOVE.
* Volume refusal names all four protected locations; the check fails closed when stat raises.
* Drive full before start gives E-001.
* A drive too small gets a partial export, and the next drive continues with no hole and no overlap.
* ENOSPC mid-copy gives FAILED; retrying on the same drive resumes the same batch and copies nothing twice.
* Drive unplugged mid-copy (while the display timezone is also changed) and mid-verify gives INTERRUPTED with E-002, and resume completes. The batch keeps its own timezone.
* Resume re-hashes already-verified files and detects a file altered while the drive was elsewhere (E-003).
* Resume uses the segment snapshot, not ledger rows changed after planning.
* The export survives a ledger write-locked longer than SQLite's busy timeout.
* An existing different file gives E-004 and is left untouched; an existing identical file is re-verified, not copied.
* A corrupt segment goes to Quarantine.
* A damaged copy of an original, and of an MP4, gives E-003.
* CLI export in a separate process, **twice while the recorder keeps recording**: one run, no new gap, and the second interval starts exactly where the first ended.

## 5. Review findings (independent QA, after 8daf276)
| # | Finding | Status |
|---|---|---|
| E1 HIGH | ~13 h verification at 5MP, no fast mode; a FAILED batch re-decodes everything | **Fixed.** `[export] content_check` = full / sampled / deferred; byte verification of every file always required (MP4 write-time hash vs uncached read-back); level per MP4 in manifest + HTML; `verify-content` command; FAILED batches are retried on the same drive keeping verified items, and a file verified earlier on the drive is re-hashed only (its picture result carried over). Default `sampled` is provisional — **owner decision**. Real timings measured (§3). |
| E2 MED-HIGH | Export onto system / data / log volume allowed; stat errors failed open | **Fixed.** Spool, ledger (data), log and system-disk volumes refused (E-005); any stat failure → refused. |
| E3 MED | Resume trusted VERIFIED items | **Fixed.** Re-hashed uncached on resume (no decode); mismatch / missing → E-003. |
| E4 MED | Joint-decode fallback unbounded, used unverified local originals | **Fixed.** Fallback removed; local originals re-hashed vs sealed SHA-256 before remux and before any decode; differences accepted only in the last ≤ 2 frames of INTERRUPTED / decoder-error segments, else E-003. |
| E5 LOW-MED | MANIFEST resume could hit permanent E-007 (manifest rebuilt from live rows) | **Fixed.** Segment metadata snapshot stored in the batch plan at planning time; engine and manifest use it. |
| E6 LOW | zero-frame MP4 could pass; current-config timezone used on resume; DST name ambiguity; "database is locked" → FAILED | **Fixed.** Frame count must be > 0 and equal the sources' total; each per-frame list must match the ledger's frame count; the batch's display timezone is used; end time is written in the start's UTC offset; all export-ledger operations retry on locked/busy (8 attempts, back-off up to 8 s). |

Remaining after the review:
* The MP4 "write-time" hash comes from a normal, possibly cached, read just after FFmpeg closed the file. It proves that what the file system held then is what is on the drive now. It does not check FFmpeg's own write path; the picture check covers that.
* `sampled` assumes the camera stream can be seeked accurately: decoding restarts at a keyframe, and that keyframe is IDR / closed-GOP. This is true for the simulator; not verified on the real camera (docs/02 A-items).
* `verify-content` cannot amend the HTML summary already on the drive. A failure is recorded in the MP4 item's `content_check` (`mismatch` / `error`; the item stays VERIFIED and is checked again next time) and reports E-003. Items an earlier version moved to FAILED this way are reported on every run ("did not match … on an earlier picture check").
* (Review 2026-11-12) A retried MP4 keeps its planned start/end times and notes in the manifest: a verification failure is stored under `detail.failure` instead of replacing the detail, and a detail an earlier version replaced is rebuilt from the batch plan. The export documents' inputs that are not in the batch row (drive identity, organisation, recording-log head) are frozen in `export_batches.document_inputs` with `manifest_generated_utc`; a document an earlier attempt wrote completely is kept (and listed with its own hash) instead of failing with E-007 on every attempt.
* A retried FAILED batch is resumed only while it is the latest export and nothing has completed past its start. Otherwise a new batch is planned, and files already verified on that drive are re-hashed rather than decoded again.

## 6. NOT executed / not verified
* **Software eject/power-off: not implemented.** `ManualRemovalFallback` flushes and tells the operator to use Windows "Safely Remove Hardware"; the status is `COMPLETE_REMOVE_MANUALLY`. Only a fake ejector was tested.
* No real removable drive, USB unplug, real disk-full or exFAT/FAT32/USB volume was used. The drives were temp directories on the internal NTFS disk; full and unplug were injected faults. Because those temp directories share the system volume, the volume refusal had to be bypassed with `allow_same_volume` in tests.
* The Linux paths (O_DIRECT read-back, `/proc/self/mountinfo` identity, hard-link no-replace rename) were not executed.
* Disk serial/model/size/bus type are not discovered here (owned by `sentinel.drives`). The CLI identifies a drive by volume GUID (+ sub-folder) or by the `drive_uuid` in `SENTINEL_DRIVE.json`.
* The 5MP timing used simulated content at 4 Mbit/s on an internal NVMe disk; real camera content and a real USB HDD were not measured.
* The real effect of the cache bypass was not demonstrated: user space cannot make the cache and the disk disagree. The tests only show that the unbuffered path is used and returns the exact bytes.

## 7. Risks, limitations, open decisions
1. **Open (owner): the default `content_check`** (provisional `sampled`), and whether a drive exported `deferred` may be handed to reviewers before `verify-content` has run.
2. Read-back bypasses the OS cache but not the drive's internal cache. A drive that ignores flush commands is not detected (documented in the manifest `method_notes`).
3. A sealed original missing from local storage, or changed since sealing, blocks the export with E-006 until support fixes it. This is a design choice: never silently skip evidence. A missing CORRUPT file is only a warning.
4. "Late" segments, a missing local CORRUPT file and clock changes produce warnings; "unaccounted time" (no footage and no gap row) is flagged as missing.
5. MP4s are video-only; if audio is ever authorised it exists only in `Originals/`.
6. Merge risks:
   * `db.py`: separate block, low risk.
   * `config.py`: new `[export]` section; it conflicts only if another branch also adds `[export]`.
   * `alerts.py`: the CATALOG E-codes.
   * `drives` table: owned here, per the coordinator.
7. Open: who may export, whether the operator id must be mandatory, and whether one review drive may receive the same footage twice. Currently it may not: intervals continue from the last complete batch on any drive.

## 8. Recommendation
**Merge after** the owner chooses the `content_check` default (§7.1) and accepts the manual-removal fallback (or a verified eject lands with `sentinel.drives`). The code paths are tested end to end with real recordings; the hardware-facing parts are not.
