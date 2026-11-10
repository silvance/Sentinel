# Branch `design/local-retention` — removing archived recordings from this computer (built on `design/archive-drive` @ `5c976bc`)

## 1. Summary
Today nothing ever deletes recordings on the recording computer, so its internal disk fills up in
about 8–23 days (1 TB at 4–12 Mbit/s). Drives are swapped every 3–5 days, longer over holidays.
This branch adds an **optional** rule, off by default, that an administrator can turn on: when the
internal disk runs low, the oldest recordings that have already been copied, checked, and taken away
on an archive drive are removed from the computer — only as many as needed. Every removal is
recorded. The status shows "Oldest footage kept on this computer: <date>", warns when the disk will
fill before the next expected swap, and warns when the inserted archive drive is nearly full.

## 2. Requirements addressed
| ID | What | Stated / design choice |
|---|---|---|
| E6 | Documented retention with controls against premature deletion | Stated; the rule is a design choice, **default OFF** |
| S1 / S4 | Internal disk is a buffer; sizing from measured rate | Stated; horizon alert (L-001) and drive capacity alert (A-009) are design choices |
| E2 / E4 | Only verified archive copies count; segment metadata and hashes kept forever | Stated |
| E7 | Tests for every refusal case | Stated |
| Owner | Swap every 3–5 days (longer over holidays) | Owner decision (input); `expected_swap_days = 5` |

## 3. The rule (all must hold)
* `[retention] enabled = true` (administrator) **and** `[archive] enabled = true` (config refuses otherwise).
* Free space on the recording storage < floor (default `[storage] warning_free_gb`). Removal stops at
  floor + `margin_gb` (default 10 GB) — never more than needed — and at `max_deletions_per_pass`.
* Segment FINALIZED or INTERRUPTED (**never CORRUPT**, never unsealed), sealed ≥ `keep_recent_hours`
  (default 24 h) ago, not already removed.
* ≥ `min_verified_copies` (default 1) **VERIFIED** archive copies (LOST copies never count) whose hash
  equals the sealed hash, of the original **and** of its per-frame sidecar if it has one.
* `require_removed_drive = true` (default): only copies on archive drives that have **left the
  machine** count — the drive's presence sessions (new table `archive_drive_sessions`, maintained by
  the archive worker on every scan) are all closed. Unknown presence = still present (fail-safe); a
  present-but-unreadable drive keeps sessions open.
* Oldest first (ledger insertion order); ineligible segments are skipped, never forced.

Each removal: one transaction re-checks the archive record and drive state, inserts a
`retention_deletions` row (DELETING, with the copies relied on) and sets `segments.local_deleted_utc`
→ files deleted (original + sidecar, spool only) → row DONE + `audit` event `retention_deleted`. A crash
in between is completed on the next pass. Runs in its own thread in the recorder service (every 60 s;
restarted by the janitor if it dies).

**Interactions:** the integrity audit, the content hasher and startup recovery skip removed segments
(never reported missing); the review export skips them and names them in the manifest warnings
("kept only on archive drives") instead of failing with E-006.

## 4. What changed
* `src/sentinel/retention/manager.py` (new): `RetentionManager`, `oldest_kept_text`.
* `db.py`: `RETENTION_DDL` (`retention_deletions`, `archive_drive_sessions`), additive column
  `segments.local_deleted_utc` (ALTER TABLE if missing); `segments_for_audit` and
  `segments_needing_content_hash` skip removed segments. `recorder/recovery.py`: skip removed.
* `export/planner.py`: removed segments excluded + `footage_only_on_archive_drive` warning.
* `archive/ledger.py` + `worker.py`: presence sessions; `recording_rate()`; capacity check → **A-009**
  "The archive drive will be full within the next few days" (`[archive] capacity_warning_days = 5`).
* `config.py`: `[retention]` (`RetentionConfig`), `[archive] capacity_warning_days`.
* `alerts.py`: **L-001** storage will be full before the next expected swap (retention OFF, measured
  rate, `expected_swap_days`), **L-002** retention ON but cannot free enough space; A-009.
* `service.py` / `status.py`: `retention` status section (`oldest_kept_utc`, `text`, last pass).
* Tests: `tests/unit/test_retention_units.py` (14).

## 5. Tests (Windows 11, Python 3.14)
* `"…\.venv\Scripts\python.exe" -m pytest tests/unit -q -p no:cacheprovider` → **152 passed**
  (138 from `design/archive-drive` + 14 new).
* Integration: see §5a.

Unit tests: disabled by default (and config refuses retention without archiving); removes oldest
archived until the floor, then stops; margin respected; never unarchived / CORRUPT / LOST / recent;
attached drive does not count by default (option `require_removed_drive=false` tested);
`min_verified_copies=2` needs two removed drives; sessions (swap/removal close, unreadable keeps open);
crash between decision and deletion completed; integrity audit + content hasher + recovery do not
report removed segments; export planner skips them with a warning; oldest-kept text; L-001/L-002 in
the recorder status; A-009 capacity alert; only spool files deleted (archive drive byte-identical).

### 5a. Integration
* `… -m pytest tests/integration -q -p no:cacheprovider` (all files, one run) → **56 passed, 3 failed**
  (521 s): `test_recorder_faults.py::test_camera_offline_shows_interrupted_and_recovers`,
  `::test_short_freeze_is_recorded_as_internal_gap`, `test_service_process.py::test_hard_kill_restart_recovery_and_graceful_stop`.
  `test_archive_drive.py` (3) and `test_review_export.py` (26) all passed in that run.
* Reruns: camera-offline + freeze + hard-kill together → camera-offline **passed**, freeze failed
  (known timing-flaky, also fails on the export base), hard-kill failed with `sqlite3.OperationalError:
  disk I/O error` in the **test's read-only ledger open right after killing the recorder** (a hot WAL
  that needs recovery cannot be opened with `mode=ro`).
* Hard-kill test repeated, same machine: this branch **2 passed / 1 failed** of 3; `design/archive-drive`
  @ `5c976bc` **1 passed / 2 failed** of 3; export base `94c778c` **failed** 1 of 1. So it is a
  pre-existing intermittent failure of the test's read-only access after a hard kill, not caused by
  retention. (A read-only reader after a crash can hit the same error — worth a fix in `db.Ledger`
  read-only mode by whoever owns it: retry, or open read-write without migrating.)

## 6. NOT executed / not verified
* Never run against a real full disk; free space was simulated (fake disk usage).
* Long-run behaviour (weeks, thousands of segments per pass) not measured; each pass is bounded by
  `max_deletions_per_pass` and stops at floor + margin.
* Linux: file deletion on ext4 and the systemd unit NOT EXECUTED.

## 7. Decision for the owner (required before enabling)
**Decided 2026-11-10 (owner): ON at deployment.** The code default stays OFF; first-run setup
(`sentinel.launcher.setup.build_config`) and `config/sentinel.example.toml` write
`[archive] enabled = true` and `[retention] enabled = true` with the recommended settings below
(`expected_swap_days = 5` for the 3–5 day swap cadence). Because retention only counts copies the
archive process made, every deployment path now runs it: the Windows service install registers
`SentinelArchive` next to `SentinelRecorder` when `[archive]` is enabled, and the click-to-run
launcher starts `Sentinel.exe archive run` in the background when no service runs it
(`tests/unit/test_deployment_retention.py`). Original question, kept for the record:

Enable `[retention]`? Recommended settings if yes: `enabled = true`, `min_verified_copies = 1`
(2 if a second archive drive set is rotated), `require_removed_drive = true` (the copy relied on has
physically left the machine), `keep_recent_hours = 24`, floor = storage warning threshold. Without
it, the internal disk fills after ~8–23 days and recording stops (S-002); with a 3–5 day swap cadence
L-001 warns ahead of that. Risks: a removed archive drive that is later lost or damaged is then the
only copy (mitigate with `min_verified_copies = 2`); deletion is irreversible.

## 8. Recommendation
**Merge after** `design/archive-drive` (merged). Retention ON in deployed configurations since the owner decision (§7). The
code paths are covered by unit tests with real file deletion on temp folders; no real disk-full.
