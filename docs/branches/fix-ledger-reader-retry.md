# fix/ledger-reader-retry

## 1. Summary
A bug fix to the baseline, not a feature. Right after the recorder process is killed (crash or
power-loss-like stop), anything that *reads* the recording database — the operator screen,
diagnostic reports, test tools — could fail for about a second with "disk I/O error". Readers
now retry for up to 5 seconds before giving up. Recording itself was never affected.

## 2. Requirements
U8 (recover from common errors unattended), U6 (GUI independent of recorder). Stated requirement;
the retry approach is the orchestrator's design choice.

## 3. What changed
* `src/sentinel/db.py`: read-only `Ledger` retries transient SQLite errors ("disk I/O error",
  "database is locked", "unable to open database file") with backoff up to `READ_RETRY_SECONDS`
  (5 s), reopening the connection between attempts. Writers are unchanged.
* `tests/unit/test_ledger_reader_retry.py`: 3 tests (retry succeeds, gives up after deadline,
  non-transient errors not retried).

## 4. Evidence and tests
* Reproduction (real service hard-killed by the test harness, ledger read immediately after):
  first access failed with `disk I/O error`, every access 1 s later succeeded. Strict read-only vs
  read-write+query_only made no difference (either one failed at random), so the fix is a retry,
  not a connection-mode change. A plain SQLite writer killed the same way did NOT reproduce it
  (8/8 clean) — it is specific to the recorder process (likely handle release timing on Windows).
* `tests/integration/test_service_process.py` on unmodified `main` under tonight's load:
  **0/2 runs fully passed** (`disk I/O error` in the test's ledger read after the kill). It had
  passed earlier in the evening, so it is timing-dependent.
* Same test on this branch: **3/3 runs passed (2 passed each)**.
* `tests/unit`: **53 passed**.

## 5. Not verified
Root cause inside Windows is inferred, not proven. Not tested on Linux.

## 6. Risks
A reader may now block up to 5 s in the worst case; the GUI already treats a failed read as
"history temporarily unavailable".

## Review findings (independent QA) and resolution
* R1 (retry slept while holding the ledger lock; a GUI thread could freeze ~5-10 s): FIXED. The
  lock is held only for one attempt and released while sleeping; `Ledger(..., retry_seconds=0)`
  lets a UI thread fail fast and show "temporarily unavailable". Tests:
  `test_retry_does_not_hold_lock_while_sleeping`, `test_zero_retry_budget_fails_fast_...`.
* R2: query() after close() silently reopened: FIXED (raises). The reopen-inside-query path was
  untested: FIXED (`test_query_reopens_after_transient_failure_mid_session`). Remaining, accepted:
  "unable to open database file" / "disk I/O error" also cover permanent faults; they now surface
  after the retry budget instead of immediately. The chain branch's own read-only connection
  (`ledgerchain._ro_connect`) does not use this retry — integrate when both merge.
* Unit tests after fixes: 56 passed.

## 7. Recommendation
**Merge.** Small, contained, and it fixes a real intermittent failure in the baseline.
