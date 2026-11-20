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
* Free space on the recording storage < floor: `[retention] floor_free_gb`, or, when none is set, the
  built-in level of section 9 (150 GB; until 0.3.25 it was `[storage] warning_free_gb`). Removal stops at
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

## 9. Where removal starts (owner decision 2026-11-20, 0.3.26)

**Asked (owner, with the laptop's drive at about 480 GB free):** "What I think it needs is a small change
where if no archive drive is attached, it automatically deletes all recordings that have been successfully
archived instead of waiting until only 10GBs are left. That'll give the agent a few days to offload the
recordings and put the drives back into service."

**What the rule did.** Removal started when free space fell below the storage warning level (50, in the
unit of `[storage]`; "Free up to 10 GB" on the Storage page was the margin, and read as the trigger), freed
10 at a time, oldest first, and went on freeing as recording needed room. So with both drives out the
computer already kept recording until every archived recording was gone: removing them all at once would
have added no days. It would have ended the computer's second copy at the moment the drives leave, for as
long as they are carried and copied at the office, and it would have needed a delay so that a swap with one
drive unplugged for a minute does not trigger it.

**What was wrong with the rule** (read from the code on 2026-11-20, then shown at small scale by
`test_a_full_computer_does_not_live_on_the_storage_warning_line`): the level WAS the warning level. A computer
whose drive had filled up lived on that line. Free space had to touch the warning level before anything was
removed, so the status showed S-001 "Recording storage is low ... Contact technical support" until the next
pass (up to a minute, about every 10 GB of recording, 5 to 6 hours at 4 Mbit/s), and the time left was
computed from the free space alone: "about 1 day", for ever.

**Decided (owner, same day, offered both): "Start at 150 GB free".**

* **The level.** `[retention] floor_free_gb` is used when it is set (0 or more), as before. When it is not
  set, `retention.manager.default_floor_bytes`: 150 GB as the window shows sizes (150 x 10^9 bytes), at most
  40 % of the recording drive, and at least 10 above the storage warning level (same unit as the warning
  level). A drive of 375 GB or more: 150 GB. 256 GB drive: 102 GB. 128 GB drive: the warning level plus 10 (64 GB). If
  the size of the drive cannot be read when the recorder starts (the folder may not exist yet), the warning
  level plus 10 is used until it can. An installed recorder whose settings do not set a level (every setup so far) gets
  the new level when it is updated; nothing has to be entered.
* **Unchanged:** which recordings may be removed (sealed, older than `keep_recent_hours`, verified on an
  archive drive that has left the machine, hashes equal), oldest first, only up to level + margin, every
  removal in the recording log. They only go sooner.
* **Units.** The file's storage levels are multiples of 1024^3 bytes; the window shows every size in GB of
  10^9 bytes. The new field on the Storage page is in the window's GB and converts when it saves (150 is
  written as 139.7). The built-in level is defined in the window's GB, so Status hovers at "150 to 161 GB
  free" (the margin is 10 x 1024^3).
* **Settings > Storage:** "Start deleting when free space is below" shows the level in force, with the size
  of the recording drive, the space free now, how long the entered room lasts at the measured recording
  rate, and whether it is the built-in level. A changed value is written to the file; an untouched one
  writes nothing. Refused: a level not above the storage warning level, and a level as large as the whole
  drive (it could never be reached, and would delete every archived recording as soon as its drive is
  taken out: the rule the owner decided against). "Free up to" under Advanced is now "Each time, free".
* **Time left.** Each fifth pass, and at once after a removal, the manager counts what the rule would allow
  removing now (`count_reclaimable`, the same eligibility test as for removing; nothing is changed) and the
  status adds it: `storage.seconds_until_attention` = (free - critical + removable) / rate, with
  `storage.reclaimable_bytes` beside it. Status > Details says "... (counting 300 GB of archived recordings
  that are deleted as space is needed)" and has a new line "Kept on this computer: recordings since
  2026-11-12; archived recordings are deleted when less than 150 GB is free".

**What follows from 150 GB on a 480 GB drive at 4 Mbit/s (43 GB a day; less with motion-only hours).**
The computer keeps up to about 330 GB, 7 to 8 days, as its own copy. Recordings on the drive in use cannot be
removed (it has not been taken out), so if no drive is taken out for about 7.6 days, free space falls below
the level with nothing to remove and L-002 ("... could not be removed ... Swap the archive drive") shows, with
a little over 3 days left before recording would stop. Under the old level it showed with about one day left. With
both drives out, the recordings waiting to be archived cannot be removed either: the same 7.6 days until
L-002 and about 11 until recording stops.

**Tests:** `tests/unit/test_retention_start_level.py` (the level for several drive sizes, set and unset; the
recorder settling it once the drive can be read; the full computer under the old and the new level; what
could be removed is counted with the rule and not on every pass; the time left in the status), and in
`tests/unit/test_gui_app.py` the Storage page field (shown, saved in the file's unit, untouched = unset,
refusals) and the Status details. `tests/unit/test_retention_units.py` is unchanged apart from its helper
taking storage settings.

Full suite on the second development machine, 2026-11-20: 1715 passed, 2 skipped, none failed.

**Not tried:** on a drive that is really full (free space is simulated, as in section 6); on the laptop;
over days. The warning on the old level was not watched on a real recorder, only derived and simulated.
