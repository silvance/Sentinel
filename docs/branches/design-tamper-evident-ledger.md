# Branch `design/tamper-evident-ledger`

## 1. Summary
The recording database (the "ledger" that lists every recording file, its fingerprint, its
times, every gap and every event) could be edited by anyone with file access without leaving a
trace. This branch makes such edits **detectable**: every recorded change is linked into a
fingerprint chain, copies of the latest fingerprint are saved in several places, and a check
(run automatically every hour, or by hand) reports in plain language where the log was altered
and raises operator alert **V-003 "The recording log has been altered"**. It does not make
tampering impossible; see §6.

## 2. Requirements addressed
| ID | How | Stated requirement or design choice |
|---|---|---|
| E2 Integrity | ledger records of file SHA-256 / content hash / times / state are chained and re-checked; `--check-files` re-hashes files against the chain | requirement; the hash-chain mechanism is **my design choice** |
| E5 Auditability | audit/integrity events, runs, gaps chained; tampering raises V-003 and an event | requirement; mechanism is design choice |
| E3 Continuity | gap rows chained when opened, updated and closed (a deleted or shortened gap is detected) | requirement; mechanism is design choice |
| U7 | V-003 added to `alerts.CATALOG` with fixed plain wording | requirement (style) |
| docs/07 residual risk "Ledger is not tamper-evident" | replaced by an honest statement of what is and is not covered | — |

## 3. What changed
* `src/sentinel/ledgerchain.py` (new): RFC 8785 canonical JSON, chain format 1 (normative spec in
  the docstring), writer helpers used inside ledger transactions, genesis/adoption for existing
  ledgers, anchor log (`data/chain_heads.log`), `current_chain_head()` for the export branch,
  `verify_ledger()`, CLI `python -m sentinel.ledgerchain verify|head --config …`.
* `src/sentinel/db.py`: new separate DDL block `CHAIN_DDL` (table `ledger_chain`) after the existing
  DDL; every write path (`create_run`, `update_run`, `mark_run_clock_anomaly`, `insert_segment`,
  `update_segment`, `transition`, `seal`, `begin_gap`, gap close, `flag_open_gap_clock_anomaly`,
  `add_internal_gap`, events) records chain entries in the same transaction (seal stays ONE
  transaction); write-time discrepancy detection; chain failures isolated by a savepoint so they can
  never block sealing. `SCHEMA_VERSION` unchanged (the change is additive).
* `src/sentinel/recorder/integrity.py`: periodic `verify_chain_once()` (separate read-only snapshot).
* `src/sentinel/service.py`: anchors at start / every 15 min / stop / startup failure; chain head and
  last check in `status.json` (`integrity.ledger_chain`); V-003 measurement.
* `src/sentinel/status.py`, `src/sentinel/alerts.py`: `ledger_altered` → V-003 (critical, recording continues).
* `src/sentinel/config.py`: `[service] chain_anchor_interval_s = 900`, `chain_verify_interval_s = 3600`.
* `src/sentinel/diagnostics.py`: `ledger_chain.json` (head + fresh check) and two summary lines; each
  report also appends an anchor line.
* `tests/unit/test_ledgerchain.py` (new), `tests/integration/test_ledger_chain.py` (new),
  `tests/integration/harness.py` (every `Harness.stop()` now verifies the chain, so every existing
  fault scenario also proves there is no false alarm; opt-out `check_chain=False` for tamper tests).
* `docs/07_SECURITY.md`: design, anchoring, limits, key-less rationale, examiner specification.

## 4. Tests
All commands were run from the worktree root with `"C:\Users\silva\Documents\Proc 6 Build\.venv\Scripts\python.exe"`
(Windows 11, Python 3.14, FFmpeg 9.0.1).

**After the review fixes (§8):**
* `-m pytest tests/unit -q` → **130 passed** (50 existing + 80 in `test_ledgerchain.py`).
* `-m pytest tests/integration/test_ledger_chain.py tests/integration/test_unattended.py -q` →
  **10 passed**.
* Full suite `-m pytest tests -q` → **160 passed, 3 failed**. The three failures were:
  * `test_service_process::test_hard_kill_restart_recovery_and_graceful_stop`: `sqlite3.OperationalError:
    disk I/O error`, from the test's own read-only query right after it kills the recorder;
  * `test_recorder_faults::test_camera_offline_shows_interrupted_and_recovers`: the status showed
    INTERRUPTED before the gap row existed;
  * `test_recorder_faults::test_short_freeze_is_recorded_as_internal_gap`: no gap row appeared.

  None of the three was a chain check. Re-run individually on this branch they pass intermittently.
  **Two of them also failed on an unmodified copy of `main`:** `test_hard_kill…` failed with the same
  disk I/O error (1 of 6, then again at run 3 of a second loop), and `test_camera_offline…` failed
  1 of 5. I believe they are load-dependent flakes (other engineers' suites run on this machine at
  the same time). The short-freeze one was discussed below.

**Before the review fixes:**
* `-m pytest tests/unit -q` → **107 passed** (50 existing + 57 new in `test_ledgerchain.py`).
* `-m pytest tests -q -p no:cacheprovider` (unit + all integration) → **140 passed in 363 s**. This
  run includes the harness change, so every integration scenario (camera loss, stalls, freezes,
  FFmpeg crash, service kill/restart recovery, clock steps, storage full, tamper audit…) also
  verified the chain after stopping, with **no false "altered" finding**.
* An earlier full run (before the harness change) gave **1 failed, 139 passed**:
  `test_recorder_faults.py::test_short_freeze_is_recorded_as_internal_gap` timed out waiting for
  a within-segment gap row. I then ran that one test about 45 times on this branch: **3 failures**,
  all in the first ~12 runs (while other engineers' test suites were also running on this
  machine), none in the last ~30. On a copy of `main` (`git archive main`) it passed **24 of 24**
  runs, interleaved with the branch runs. In the failed runs the database had no within-segment gap
  at all (the freeze was not seen as a timestamp jump). That is media timing, not the chain. I
  found no way the chain could cause it (it adds about 0.2 ms per write), but I have **not proven**
  it is pre-existing flakiness. It needs watching.
* New tests cover: RFC 8785 number vectors (24) and string/key rules; the chain covers every
  change; bookkeeping writes add no entries; a failed seal rolls back its chain entries (one
  transaction); edits to segment `sha256` / `end_utc` / `state` / `size_bytes`, a segment set back
  to ACTIVE, gap and run edits, deleted segment / event / gap rows, reordered events, a row
  inserted by hand, reordered chain entries, an edited chain record (with and without its hash
  recomputed), a deleted chain entry, a cut-off chain tail, an emptied chain, a dropped chain table:
  each is detected **at the expected first position**. A chain rebuilt from scratch passes with no
  anchors (the honest limitation, asserted) and is caught by an earlier anchor in
  `chain_heads.log` or `--expect-head`. Also: the laundering protection, a ledger from before this
  feature (genesis + adoption), a wiped chain restarted with a mark, chain failure not blocking a
  seal, the anchor log format, `--check-files`, V-003 wording, the CLI exit codes, and an
  independent re-implementation of the spec. Integration: the chain grows with real recordings
  and verifies live and after stop (files included), the head is in status/anchor log/diagnostic
  report, a live edit raises V-003 while recording continues, and a byte flipped in a file is found
  by `--check-files`.
* **Cost measured:** chain bookkeeping is 0.13–0.22 ms per seal (median). That is ~20 % of the
  bare ledger transaction (0.64 ms on this fast disk) but **0.25 %** of the real per-segment
  sealing work (80 ms median, with 2 s test segments; 300 s production segments hash far more).
  Verification runs at ~22 000–24 000 entries/s. 30 simulated days (25 922 entries) took 1.16 s,
  with 5.7 MB peak memory (`tracemalloc`).

## 5. NOT executed / not verified
* Linux: not run (no Linux host). The code is platform-neutral (stdlib `sqlite3`, `hashlib`); the
  anchor log's directory fsync uses the existing `Platform.fsync_dir`.
* Export manifests do not yet contain the chain head: the export engine is another branch; it should
  call `sentinel.ledgerchain.current_chain_head(ledger)` and write `{seq, hash, utc}` into
  `Export_Manifest.json`, and `append_anchor(…, reason="export")`.
* Verification against an independent third-party RFC 8785 library was not done (no new
  dependencies); numbers were checked against the RFC 8785 Appendix B vectors and an independent
  re-implementation in the test file.
* The year-scale measurement used a synthetic ledger (105 000 segments, 325 502 entries) built with
  direct SQL and chain appends, not a year of real recording.
* `PASSIVE` checkpoint + `journal_size_limit`: verified to not block seals while a check runs
  (unit test + measurement). WAL growth over days with a reader repeatedly active was not observed
  long-term.
* No HMAC/TPM/DPAPI keying (deliberate; see docs/07).

## 6. Risks, limitations, open decisions
* **Detectable, not preventable.** Anyone who can write the data directory can rebuild a
  self-consistent chain and rewrite the local copies of the head. Only heads that have left the
  machine (review-drive manifest, diagnostic report sent to support, head written on the custody
  form) expose that. Decision for the owner: make "record the chain head at every drive swap" a
  procedure, and have the export branch put it in every manifest.
* **V-003 is permanent for that ledger** (no acknowledgement mechanism). A real tamper should keep
  showing; a false positive (e.g. running older software against the ledger, or a fresh install
  that kept the old `chain_heads.log`) would too. Open decision: an administrator acknowledgement
  that is itself chained.
* Alert code collision: V-003 chosen per the task brief; if another branch also adds V-003, renumber
  at merge (codes must never change meaning once released).
* `ServiceConfig` gained two fields and `db.py` write methods were edited throughout: expect simple
  textual merge conflicts with other branches that touch the same lines; the separate `CHAIN_DDL`
  block avoids schema-text conflicts.
* Uncovered: bookkeeping columns (retry counters, last audit time), segments still being written,
  and columns other branches add (add them to `ledgerchain.COVERED` only with a new chain format).
* Ledger growth ~7 KB per sealed segment (~2 MB/day at 300 s segments). Verification takes ~26 s of
  CPU per hourly check after a year (one core, background thread, short snapshots).
* **Rule-compliant appends after the last off-machine head are undetectable** (review C1,
  remaining part). Examples: invented new rows, false values for fields still empty at that head
  (a content hash not yet computed, the end of an open gap), or a rewritten tail. Going further
  needs a keyed or attested chain (docs/07).
* V-004 is new: if another branch also uses V-004, renumber at merge.

## 8. Review findings (independent QA, wave 2)
* **C1 HIGH, fixed.** Verification now enforces the recorder's rules across each row's entries
  (`ledgerchain.illegal_changes`, matching `db.WRITE_ONCE`). The reviewer's `forge_append.py` is
  now the regression test `test_reviewer_forged_append_with_earlier_anchor_is_detected`, and 13
  more append cases are tested. Remaining by design: rule-compliant appends (see §6).
* **C2 MED, fixed.** Chain write or read faults raise V-004 (system fault), not V-003. The
  affected rows are persisted in `ledger_chain_pending` and re-recorded at the next write
  (`protection_gap` + `chain_repair`), so there is no false discrepancy, including after a
  restart. Rule-breaking changes made across a protection gap are still V-003 (tested). If SQLite
  aborts the transaction, or the savepoint cannot be undone, the whole seal rolls back and is
  retried: nothing is half-written and nothing blocks (2 tests). CLI exit 3 = intact with
  protection gaps.
* **C3 LOW-MED, fixed.** Verification uses short snapshots of at most 2 000 items, with a pause
  between them in the worker. The checkpoint is now `PASSIVE` with `journal_size_limit` 64 MB.
  Year-scale synthetic ledger (325 502 entries): 26 s, 292 snapshots, longest snapshot 0.28 s,
  peak memory 9.9 MB, at most 132 ms per seal + checkpoint while a check ran.

## 7. Recommendation
**Merge after** (a) the owner accepts the limits in §6 (detection, not prevention; the protection
depends on chain heads leaving the machine), and (b) the intermittent
`test_short_freeze_is_recorded_as_internal_gap` failure has been checked on `main` under the same
machine load. It was not reproduced in the last ~30 runs, and I found no way this branch could
cause it. After merging, the export branch should put `current_chain_head()` in every export
manifest; that is what makes the anchoring meaningful. The change is additive: the schema version
is unchanged, the new table is in a separate DDL block, no new dependencies were added, and if the
chain fails it raises an alert rather than stopping recording.
