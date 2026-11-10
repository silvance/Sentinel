# Branch `design/archive-drive` — continuous archive drive (built on `req/review-drive-export`)

## 1. Summary
Recordings still always land on the internal disk first. A new background program copies every
finished recording, oldest first, to an attached archive drive (for example a 3.5" disk in a
dock), reads each copy back from the drive and checks its fingerprint before counting it as
archived. If the drive is slow, missing or full, recordings simply wait on the internal disk and
the status shows "Not yet archived: N h"; nothing is ever deleted. An operator can press "Swap"
(today: a command) to make the drive safe to remove; the next drive continues automatically.
The drive always carries its own index of what is on it.

## 2. Requirements addressed
| ID | What | Stated / design choice |
|---|---|---|
| S1 | Onboard buffer + chunked copy to a slow/absent platter drive; drives swappable | Stated. **Mechanism is a design choice** (below) |
| R3 | Recording never writes to the removable drive (the archive worker only reads the spool) | Stated |
| E2 | Every archived file read back bypassing the OS cache and compared with the SHA-256 recorded at sealing; recorded in `archive_copies` only after that | Stated |
| E3 | Gap list on every drive (`gaps.csv`, `gaps[]`), plus footage in the drive's span that is on another drive / not yet archived | Stated (format is a design choice) |
| E4/E6 | Originals copied unchanged; local recordings never modified or deleted (no retention implemented) | Stated |
| E7 | Tests for drive absent/full/removed/swap, recording undisturbed | Stated |
| U6/U7 | Archive runs independently of the recorder; plain-language A-0xx alerts shown in the recorder's status | Stated; codes are a design choice |

**Design choices (owner may revisit):**
* **Separate low-priority process** `python -m sentinel.archive run` (the worker class can also run as
  a thread, used by tests). Why: a failing USB disk can block a system call for minutes — only the
  archive process stalls, never the recorder, its watchdog or its shutdown; Windows background mode
  (CPU + I/O + memory priority, `PROCESS_MODE_BACKGROUND_BEGIN`) and Linux `nice`/`ioprio` apply
  cleanly per process; no GIL contention with the recorder's threads; same model as the export.
  The two processes share the SQLite ledger (WAL).
* **Copy mechanism:** per file, 8 MiB chunks through a byte-rate throttle (default 40 MB/s ≈ 27×
  a 12 Mbit/s camera), `.partial` with fsync every 64 MiB, final fsync, no-replace rename (reused
  `TargetIO.write_new`, which also refuses bytes that do not hash to the sealed value), then
  uncached read-back (reused `open_unbuffered`, 8 MiB reads, throttled at 2× the rate) and
  comparison with the sealed hash; then mark read-only, insert `archive_copies`, append a journal
  line. Read-back failure → the bad file is renamed `*.unverified-<UTC>` (kept, never claimed,
  never deleted) and copied again, up to `verify_attempts` (3), then alert A-006 and skip.
* **Order:** oldest first by ledger insertion order (never wall-clock text). Per segment: media
  file, then its per-frame sidecar; sidecars that the deferred content hasher produces later are
  picked up in a later pass. CORRUPT segments go to `Sentinel/<camera>/Quarantine/`.
* **Layout:** `Sentinel/<camera>/<YYYY-MM-DD>/<HHMMSS>Z_<HHMMSS>Z_<seg8>.ts` (+ `.framehash.txt`),
  **UTC** names (no DST ambiguity; the reviewer-friendly local names are the review export's job),
  `<seg8>` = first 8 hex digits of the segment id (unique even for two segments in the same second).
* **Self-describing drive:** `Sentinel/INDEX/archive_index.json` (`format: sentinel-archive-index`,
  v1: copies with UTC + local times, flags, verify method; gaps; `not_on_this_drive[]`; coverage),
  `archive_index.csv`, `gaps.csv`, `SHA256SUMS.txt` (`sha256sum -c` from the drive root), and
  `README_ARCHIVE.txt`. Built **only** from verified `archive_copies` rows. Each file is written as
  `.partial`, fsynced, read back uncached, then atomically replaced (SHA256SUMS last). Rewritten on
  attach, after each `index_every_s` (1 h) of copied footage, after 1 h of wall time with new copies,
  and on swap. Because copies are only ever added, an older index is always a correct subset; between
  rewrites `verified_journal.csv` (one fsynced line per verified file; torn last line ignored) documents
  the newest copies — so a drive pulled at any moment documents everything verified on it (index ∪ journal,
  checked by every test). Every generation is recorded in `archive_indexes`.
* **Swap:** `request_swap()` / `python -m sentinel.archive swap` (control file in `data_dir/archive_control/`)
  → the file in flight is finished and verified if that takes ≤ `swap_max_wait_s` (120 s), otherwise
  abandoned (its `.partial` discarded) → stray partials removed → index written and read back →
  `SafeRemover.flush` + `eject` (the export branch's protocol; only `ManualRemovalFallback` ships, so
  the status is REMOVE_MANUALLY with "Safely Remove Hardware" wording; a real ejector from
  `sentinel.drives` makes it SAFE_TO_REMOVE) → the swapped drive is never written again until it has
  been removed or the operator runs `resume`.
* **Unexpected removal:** the failing copy is not recorded; the `.partial` stays on the drive and is
  deleted on the next attach (only `*.partial` files are ever deleted, and only on the archive drive);
  a final file renamed but not yet verified is re-verified on return (`already_present=1`).
* **Resume policy** `resume_policy = this_drive` (default, as specified: a new drive gets everything
  from the oldest segment not verified on *that* drive) or `any_drive` (continue with what is on no
  archive drive yet). "Not yet archived: N h" always means *not verified on any archive drive*.
* **Drive acceptance:** `ConfiguredPathLocator` watches `[archive] drive_path`; with
  `require_drive_marker = true` (default) only a drive with `SENTINEL_DRIVE.json` (`drive_uuid`,
  written by the drive-preparation branch) is used, else A-005 and nothing is written. The archive
  folder on the recording volume itself is refused. Identity/`drives` row reused from the export.
* **Index cadence is not "after every copy"**: the journal covers the gap, so a large index is not
  rewritten every few minutes onto a platter drive.

## 3. What changed
* New `src/sentinel/archive/`: `worker.py` (loop, attach/detach, copy+verify, swap, index, status),
  `io.py` (`ArchiveIO(TargetIO)`, `Throttle`, atomic index replace, journal append, partial cleanup),
  `ledger.py` (`ArchiveLedger`: pending/backlog queries, `record_verified` refuses a hash ≠ sealed),
  `index.py`, `layout.py`, `status.py` (`archive_status.json`, `Condition`→alerts, `recorder_view`),
  `locator.py`, `priority.py`, `bench.py` (synthetic sealed segments + benchmark), `__main__.py`
  (`run | swap | resume | status | bench`).
* `src/sentinel/db.py`: separate `ARCHIVE_DDL` (`archive_copies`, `archive_indexes`), one line in
  `_migrate`; **and** a retry around `PRAGMA journal_mode=WAL` — found by the tests: recorder and
  archive opening a *new* ledger at the same moment failed with "database is locked" (SQLite does not
  apply `busy_timeout` to the WAL switch). This would have hit a first boot.
* `src/sentinel/config.py`: optional `[archive]` section (`ArchiveConfig`, validated; default disabled).
* `src/sentinel/export/fileio.py`: `TargetIO.write_new(..., fsync_every=None)` + `_fsync` hook
  (backward compatible; export behaviour unchanged).
* `src/sentinel/alerts.py`: A-001 no drive, A-002 drive full, A-003 removed without preparing,
  A-004 backlog too long (critical), A-005 drive cannot be used, A-006 files could not be verified,
  A-007 archive copying not running, A-008 local recording unreadable. None stops recording.
  Escalation: A-001 WARNING (NOTICE right after a proper swap); A-002 WARNING → CRITICAL when the
  backlog exceeds `backlog_alert_hours` (24 h), which also raises A-004 CRITICAL.
* `src/sentinel/service.py` / `status.py`: when `[archive] enabled`, the recorder's `status.json` gets
  an `archive` section (state, drive, message incl. "Not yet archived: N h") and the A-0xx alerts
  (missing/stale archive status ⇒ A-007). Additive field; overall recording status unaffected.
* `config/sentinel.example.toml` `[archive]` example; `packaging/systemd/sentinel-archive.service`
  (draft, NOT EXECUTED); `docs/04` implementation note.
* Tests: `tests/archive_helpers.py`, `tests/unit/test_archive_units.py` (29),
  `tests/integration/test_archive_drive.py` (3).

## 4. Tests (Windows 11, Python 3.14, FFmpeg 9.0.1, i7-12700K, 96 GB RAM, internal NVMe SSD)
* `"…\.venv\Scripts\python.exe" -m pytest tests/unit -q -p no:cacheprovider` → **113 passed**
  (84 pre-existing + 29 new).
* `… -m pytest tests/integration/test_archive_drive.py -q -p no:cacheprovider -s` → **3 passed**.
* Full integration suite: see §4a.

Unit tests cover: backlog drains oldest first, sidecars, periodic fsync, local spool byte-identical
and read-only afterwards; CORRUPT → Quarantine; drive absent → A-001 → inserted → copies; unplug
mid-write (partial left, nothing claimed, A-003, resume discards partial and copies again); unplug
during read-back (renamed file not claimed, re-verified on return); EIO with drive present (partial
discarded, retried); full before copy (A-002, nothing written); ENOSPC mid-copy then a new drive
(this_drive restarts from the oldest); backlog over threshold → A-004 + A-002 critical; corrupted
writes → moved aside ×2, A-006, nothing claimed; tampered local file → A-008, not archived; existing
identical file re-verified; unprepared drive refused (A-005, nothing written); swap with fake ejector
for both policies (swapped drive not written again while still inserted; A-001 NOTICE after removal);
`any_drive` index lists segments held on another drive; manual-removal fallback + `resume`; swap
mid-copy finishing vs abandoning the file in flight; thread start/stop; hourly index cadence + journal;
torn journal line; alert rules and catalogue wording; stale status → A-007; throttle; config validation;
ledger refuses an unverified hash. Every test that touches a drive runs `check_drive`: every claimed
file exists with the sealed hash, the index lists only verified copies, SHA256SUMS = index,
index ∪ journal = all verified copies (and = index after a swap).

**Recording undisturbed (measured, `test_recording_unaffected_while_archive_drains_large_backlog`):**
simulated camera 1280×720 @ 30 fps, 4 Mbit/s target, 2 s segments; archive and spool on the **same**
internal SSD (worse than production). Sampled the recorder's own measurements every 50 ms:

| Window | samples | stream lag min..max (s) | max time since FFmpeg progress (s) |
|---|---|---|---|
| baseline, recording only (8 s) | 159 | 0.000 .. 0.031 | 1.008 |
| archiving 1.2 GB at 40 MB/s throttle (45.5 s) | 904 | 0.000 .. 0.031 | 1.025 |
| archiving 4 GB unthrottled (9.0 s, ≈ 444 MB/s) | 178 | 0.000 .. 0.032 | 1.018 |

One recording run, no gap row, all 40 real segments contiguous and FINALIZED, max seal latency
0.25 s; all sealed segments (synthetic + real) verified on the drive, index complete. (FFmpeg reports
progress about once a second, hence ~1.0 s.) An earlier run of the same test with a 0.7 GB
unthrottled phase (2.2 s) gave the same picture (lag −0.015..0.036 s, max progress interval 0.97 s).

**Throughput / CPU** (`python -m sentinel.archive bench`, real worker code path, to a folder on the
internal NVMe SSD; source files freshly written, so likely served from the OS cache; read-back uncached):

| Run | data | end-to-end | copy | verify | CPU | CPU per GB |
|---|---|---|---|---|---|---|
| unthrottled | 16 × 256 MB | 328.7 MB/s | 499.3 MB/s | 1016 MB/s | 66.6 % of one core | 2.03 s |
| default 40 MB/s | 8 × 256 MB | 26.4 MB/s | 39.7 MB/s | 79.1 MB/s | 5.6 % of one core | 2.14 s |

Expected margin vs a 4–12 Mbit/s camera (0.5–1.5 MB/s): at the default throttle 26.4 MB/s end to end
= **17.6× (12 Mbit/s) to 52.7× (4 Mbit/s)**; 24 h of 12 Mbit/s footage (≈130 GB) drains in ≈1.4 h;
steady state costs ≈0.3 % of one core. A USB 3 3.5" disk typically sustains 100–200 MB/s sequential, so
the throttle, not the disk, should be the limit; over USB 2 (~30 MB/s) expect roughly 15 MB/s end to end
(≈10×). **A real USB platter drive was NOT tested; these expectations are not measurements.**

### 4a. Full integration suite
* `… -m pytest tests/integration -q -p no:cacheprovider` (all files, one run) → **46 passed, 2 failed**
  (556 s): `test_recorder_faults.py::test_short_freeze_is_recorded_as_internal_gap` and
  `::test_corrupted_stream_data_does_not_stop_recording` (timing-sensitive recorder fault tests; the
  archive is disabled in them — `[archive]` defaults to off).
* Rerun `test_recorder_faults.py` alone → **10 passed, 1 failed** (freeze test); the freeze test alone →
  **1 passed**.
* **Same file on the unmodified base `req/review-drive-export`** (temporary worktree, same machine) →
  **10 passed, 1 failed** (the same freeze test). So the failure is pre-existing flakiness (also noted
  in the export branch's notes), not caused by this branch.
* Final rerun of `test_archive_drive.py` after the last code change → **3 passed** (133 s); metrics
  consistent with §4 (lag −0.031..0.003 s, max progress interval 1.024 s while archiving).

## 5. NOT executed / not verified
* **No real USB 3.5" platter drive, no real unplug, no real disk-full, no exFAT/NTFS-on-USB volume.**
  Drives were temporary folders on the internal NVMe SSD; unplug/full/EIO/corruption were injected.
  Platter seek behaviour (index rewrite + journal fsync interleaved with large writes), spin-up delays,
  USB bridge caches and drive sleep were not exercised.
* **Software eject not implemented here** (by design: `sentinel.drives` owns it). Only the
  `ManualRemovalFallback` and a fake ejector were run.
* `ConfiguredPathLocator` + Windows volume identity was exercised against a temp folder with a
  `SENTINEL_DRIVE.json`; detection of a *different* disk appearing at the same drive letter was only
  tested with fake identities.
* Linux: `ioprio_set`/`nice`, O_DIRECT read-back, hard-link no-replace rename, the systemd unit —
  NOT EXECUTED. Windows background mode was applied (return value checked; the status shows
  `windows-process-background-mode`), but its effect was not isolated from the throttle.
* The cache-bypass of read-back cannot be proven from user space (see export notes); the drive's own
  cache is not bypassed.
* No GUI button: "Swap Archive Drive" is the `swap` CLI / control file; a GUI branch must call it.
* Throughput was measured with warm source files (96 GB RAM); a cold backlog read from the spool SSD
  was not measured separately.

## 6. Risks, limitations, open decisions for the owner
1. **Resume policy (owner decision).** Default `this_drive` (as specified) means a fresh drive first
   receives the *whole* history still on the internal disk, oldest first, while new footage waits —
   with weeks of history this can take many hours and trigger A-004. If drives are meant as
   successive volumes (swap when full / weekly), `any_drive` is the natural choice. Recommend deciding
   with S6 (swap interval).
2. Once a file has been verified on a drive, the ledger keeps counting it even if the drive is later
   damaged or files are deleted elsewhere; on re-attach missing files are detected (A-006) but not
   re-copied automatically (needs support). Retention (local deletion) must not rely on a single
   archive copy — it is deliberately not implemented (E6).
3. A-006 / A-005 alerts clear when that drive is removed (the event stays in the ledger).
4. Index cadence: up to 1 h of copies are documented only in the journal until the next rewrite/swap.
5. Archive and review drives are different drives; the archive worker refuses only the recording
   volume itself, not a review drive inserted at the archive location (require the marker, and in
   future a role in `SENTINEL_DRIVE.json`, to prevent mix-ups).
6. Merge risks: `config.py` (new section), `db.py` (new DDL block + WAL retry), `alerts.py` (A-codes,
   no collision with R/S/V/T/C/D/X/Z/E/M), `service.py` (status hook), `fileio.py` (optional parameter).
   Depends on `req/review-drive-export` (target, fileio, drives table) — must merge after it.
7. Default throttle 40 MB/s is a judgement: unthrottled archiving did not disturb the simulated
   recording on this host, but a slower spool disk in the appliance might differ. Tune after
   hardware measurement.

## 7. Recommendation
**Merge after** `req/review-drive-export`, once the owner picks the resume policy (§6.1), and with a
hardware test on the real dock + 3.5" drive (sustained throughput, unplug mid-copy, full drive, swap)
before operational use. The logic is covered end to end with real recordings; the hardware-facing
parts are not.
