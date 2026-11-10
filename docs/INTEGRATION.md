# Integration log — branch `integration`

Branch `integration` was created from `main` (b39fe1b) and the overnight branches were merged
with `git merge --no-ff`, one merge commit per branch, in the order approved by the project
owner. This file records every non-trivial conflict resolution and every cross-branch change
made during integration (branch, files, decision, why). Unit-test counts are after each merge,
with `"…\.venv\Scripts\python.exe" -m pytest tests/unit -q -p no:cacheprovider`.

After every merge the alert catalogue was checked for duplicate codes with a source scan
(`AlertDef("X-nnn"` occurrences; the CATALOG is a dict comprehension, so a duplicate would
silently replace the earlier entry instead of failing).

| # | Branch | Unit tests after merge | Conflicts |
|---|---|---|---|
| 1 | fix/ledger-reader-retry | 56 passed | none |
| 2 | fix/test-short-freeze | 56 passed (test-only change in an integration file) | none |
| 3 | req/review-drive-export | 96 passed | none (auto-merged) |

## 1. fix/ledger-reader-retry
Clean merge. Note carried forward: `ledgerchain._ro_connect` (tamper-evident-ledger branch) does
not use this retry; handled at merge 7 (below).

## 2. fix/test-short-freeze
Clean merge (test-only).

## 3. req/review-drive-export
Auto-merged. `db.py`: the export DDL block (`drives`, `export_batches`, `export_items`) sits
after the baseline DDL and is applied in `_migrate`. `alerts.py`: E-001…E-007 added; the merge
reproduced a missing space in the `Z-001` entry from that branch (`AlertDef("Z-001","A …`),
restored to the file's style (whitespace only). Catalogue: 24 codes, no duplicates.
