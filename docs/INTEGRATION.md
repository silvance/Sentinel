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
| 4 | design/drive-prepare | 277 passed (1 failed before the fix below) | alerts.py, config.py, example config |

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

## 4. design/drive-prepare
* **alerts.py** (conflict): both sides added a block after X-001; kept both (E-001…E-007, then
  M-001…M-010). Catalogue: 34 codes, no duplicates.
* **config.py** (conflict): the known-section list now accepts both `export` and `drives`;
  `AppConfig` keeps both fields (`export: ExportConfig`, `drives: DrivePolicy`).
* **config/sentinel.example.toml** (conflict): both `[export]` and `[drives]` sections kept. The
  merged example config loads (`content_check = "sampled"`, `erase_enabled = false`).
* **Cross-branch bug fixed — drives known from export were partly invisible to the erase guard**
  (`src/sentinel/drives/known.py`). drive-prepare reads the export branch's `drives` table to
  refuse erasing drives that received exports. The export branch stores `volume_guid` as the
  bare `{GUID}` and the filesystem serial only inside the `identity` JSON as 8 hex digits
  (`volume_serial`), while drive-prepare compares `\\?\Volume{GUID}\` and decimal serials. On
  Windows the export side records no disk serial, so a review drive without a
  `SENTINEL_DRIVE.json` marker would have been matched by nothing. Fix: volume ids are
  normalised to `{guid}` on both sides, and `identity.volume_serial` is read and converted to the
  decimal form. Security-relevant (evidence drives must never be erasable), so it was fixed in
  code rather than documented. Test: `test_export_drives_row_matches_by_volume_guid_and_volume_serial`.
* **Evidence layout signature** (`src/sentinel/drives/marker.py`): drive-prepare looked for
  `Export_Manifest.json` / `SHA256SUMS.txt` directly in `Manifests/`; the export branch writes
  them in `Manifests/Export_<stamp>_<id8>/`. Now one level down is checked too (a full review
  drive was already caught by the README + folders rule; this covers a partial one). New layout
  case `only_export_manifest_subfolder` in the parametrised evidence test (×3 policies).
* **Test changed, with reason**: `test_known_from_export_branch_drives_table` created a stand-in
  `drives` table; after the merge every ledger already has the real table (`db.EXPORT_DDL`), so
  `CREATE TABLE drives` failed. The test now inserts the same row into the real table. What it
  asserts (a serial in the export table makes the drive SENTINEL_EVIDENCE) is unchanged.
* **Docs** (requested in the drive-prepare notes §6): docs/01 §2.5 and docs/02 C1 no longer say
  drives are never formatted; they say "only through Erase and Set Up Drive with its guards,
  switched off by default".
* Not done here (still open per the branch notes): the installed-config path agreement with
  packaging (handled after merge 6, below).
