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
| 5 | design/windows-service | 404 passed | none (auto-merged) |
| 6 | design/single-executable | 480 passed | config.py, platform/windows.py, platform/winacl.py (add/add), tests/unit/test_winacl.py (add/add) |
| 6+ | cross-branch items (a)(b)(c) | 493 passed | — |
| 7 | design/tamper-evident-ledger | 575 passed (573 after the merge, +2 integration tests) | db.py, service.py, tests/integration/harness.py, docs/07 |
| 8 | req/operator-gui (322ed90) | 793 passed (4 failed before the test adaptation below; +3 integration tests) | none textually (auto-merged) |

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

## 5. design/windows-service
Auto-merged (config.py, db.py). `[storage] status_dir`, `[service] watchdog_timeout_s` and
`Ledger.write_snapshot` sit beside `[export]`/`[drives]` and the export DDL without overlap. The
ledger snapshot copies the whole database, so it now also contains the export tables (read-only
for operators; no credentials are stored in them).
* **Export status file moved to `status_dir`** (`export/engine.py`, `export/__main__.py`):
  `export_status.json` (what the operator screen shows during/after an export, U4) was written
  to `data_dir`, which the windows-service layout makes service-only. It now goes to
  `cfg.status_dir_path` (which defaults to `data_dir`, so behaviour is unchanged without a
  separate status folder and the export tests are unaffected).
* **Not integrated — recorded as an open architecture item:** under the service layout an
  operator account cannot write the ledger in `data_dir`, so an export started from the
  operator's session (CLI or GUI) cannot run as that account. An export must either run inside
  the service or be requested through a request file the service processes. Nothing of that
  exists yet; the Prepare Review Drive button stays disabled (GUI branch decision, merge 8).
  `drives` erasing likewise must start from the interactive session (UAC), as its notes say.
* The drive-prepare installed-config path (`%ProgramData%\Sentinel\sentinel.toml`) differs from
  the service layout (`…\Sentinel\config\sentinel.toml`); resolved after merge 6 (below).

## 6. design/single-executable
* **`src/sentinel/platform/winacl.py` (add/add — both branches created this module).** Merged
  into one module, keeping every public function of both: windows-service's owner+DACL reader
  and evaluator (`file_sddl`, `read_dacl`, `parse_sddl`, `disallowed_grants`, `effective_allow`,
  `service_sid`, SID constants) and single-executable's private-at-creation helpers and broad
  write check (`private_sddl`, `create_private_dir`, `write_private_file`, `protect_existing`,
  `set_dacl`, `get_sddl`, `broad_write_aces`, `foreign_trustees`, `dacl_aces`). Duplicates were
  collapsed to one definition: `AclError` (both `OSError` subclasses), `current_user_sid` (the
  service version, which upper-cases; SIDs are digits apart from the `S` prefix), the shared
  constants, `_ACE_RE`. **`parse_rights`** existed in both with different results for an unknown
  rights code (single-exe: `0xFFFFFFFF`; service: `FILE_ALL_ACCESS | GENERIC_ALL`). Both are
  "assume everything"; the merged version returns `0xFFFFFFFF`, which satisfies both branches'
  tests (it includes `GENERIC_ALL`). The two private ctypes loaders (`_advapi`/`_kernel` and
  `_api`) were kept as they are rather than rewritten, to avoid changing tested Windows-API code.
* **`tests/unit/test_winacl.py` (add/add):** both test sets kept in one file; single-exe's
  constants renamed `EXE_PROGRAMDATA` / `EXE_ME` (the service file already defines `ME`).
* **`platform/windows.py`:** both new methods kept (`restrict_new_files` docstring/no-op from the
  service branch, `write_access_problems` from single-exe), plus the service branch's
  `secret_file_problems` and `clock_synchronized`.
* **`config.py`:** both imports kept (`drives.policy`, `paths`); single-exe's defaults for an
  omitted `[storage]`/`[tools]` and the relative `credentials_file` resolution sit alongside
  `status_dir`, `export` and `drives`.
* `service.py`, `alerts.py` (C-002, C-003), example config, `platform/base.py`/`posix.py`
  auto-merged. Catalogue: 36 codes, no duplicates.

## Cross-branch items after merge 6 (single-executable x windows-service x drive-prepare x export)
Committed separately after the merge commit. Unit tests afterwards: **493 passed** (480 + 13 new in
`tests/unit/test_integration_cross_branch.py`).

**(a) Service executable in the frozen build.** As the windows-service branch recommended
(option b), the one-folder build now contains a second executable, `SentinelService.exe`
(`packaging/entry_service.py` → `sentinel.winsvc.__main__:main`; with no sub-command `host` is
assumed), sharing `_internal\` with `Sentinel.exe` (`packaging/windows/sentinel.spec`: second
`Analysis`/`EXE` in the same `COLLECT`; `spec_common.SERVICE_*`; `build.py` refuses a build
without it). `winsvc.commands.host_command(frozen=True)` points the service at
`SentinelService.exe` when `install` is run from `Sentinel.exe`; any other frozen executable is
used as it is (keeps the service branch's tests valid). Decision: `console=True` for the service
exe, deviating from "console-less" in the service notes: under the SCM there is no visible
console either way (the source install already runs console `python.exe`), and the technician
sub-commands (`install`, `status`, `set-camera-credentials`) must print in an elevated prompt.
**NOT BUILT**: PyInstaller is not installed in the project venv and installing it needs a
download; the spec was only syntax-checked (`ast.parse`) and is unit-checked for the entries.
Linux packaging unchanged (systemd, no second executable).

**(b) One configuration / secret layout, launcher defers to the service.**
* Layout (`sentinel/paths.py`): Windows `%ProgramData%\Sentinel\config\sentinel.toml` +
  `config\camera.secret`, spool/data/logs as before, plus `%ProgramData%\Sentinel\status`
  (`Layout.status_dir`). This is the service branch's proposal (own config folder that its
  installer re-permissions; the single-exe file name `camera.secret`). The old
  `%ProgramData%\Sentinel\sentinel.toml` is not searched (never released). Linux unchanged.
  `default_storage_dirs()` deliberately does **not** default `status_dir`: a configuration with
  its own `data_dir` but no `status_dir` keeps status beside its data (otherwise e.g. a test
  configuration would write into the machine's status folder).
* Setup (`launcher/setup.py`): writes `status_dir` (and creates it private like the others);
  on Windows the secret's **content** is the service's DPAPI format (machine scope, via
  `winsvc.credstore.protected_file_bytes`), still created private-at-creation with
  `winacl.write_private_file`, read back and decrypted before it replaces the target. Linux keeps
  plain TOML 0600. Test: DPAPI round trip through `credentials.load_credentials`, no plaintext on
  disk, still private.
* `drives/installed.py`: the installed-config path now comes from `sentinel.paths`
  (`…\Sentinel\config\sentinel.toml` on Windows), as drive-prepare asked to agree with packaging.
* Launcher (`launcher/control.py`, new `winsvc/control.py`, `winsvc/scm.py` `start_service` /
  `stop_service`): `CONTROL_PROVIDERS[0]` asks the SCM whether `SentinelRecorder` is installed;
  if so a `ServiceControl` is used (StartService / ControlService; access denied → C-003 with no
  detached fallback; stopped again with exit 2 → C-001; SCM not queryable → fail closed, C-003).
  `cmd_launch` with a service-managed control skips the detached-mode location checks, the
  launcher lock and log in the service-only folders, and shows `cfg.status_path`
  (`status_dir`). Detached mode now also reads `cfg.status_path` (= `data_dir` unless set).
  Real StartService/ControlService: **NOT EXECUTED** (no service can be installed without
  administrator rights); covered with a fake SCM.
* Tests changed because the agreed layout changed (expected values only, behaviour asserted is
  the same): `test_paths_tools.py::test_windows_layout_uses_programdata`,
  `::test_config_lookup_order`, `test_drives_review_fixes.py::test_installed_path_names`.
* Docs: docs/08 §4/§5, `config/sentinel.windows.example.toml` (`camera.secret`), winsvc help text.

**(c) Owner decisions.** `[export] content_check` default `"sampled"` confirmed (was already the
code default; "provisional" wording removed from config.py and the example config). Archive
policy default `"any_drive"`: to be applied when design/archive-drive merges (not merged yet).

**Not integrated here (recorded):** the service host path (`run_recorder_in_host`) does not run
single-exe's `install_check.location_problems`; it has its own stronger program-file ACL check
and the credentials-file DACL check. Adding the location check there is left for the owner's
first real service install, since that path cannot be executed on this machine.

## 7. design/tamper-evident-ledger
* **db.py** (conflict): `EXPORT_DDL` and `CHAIN_DDL` are separate blocks; both kept and both
  applied in `_migrate` (export first, then chain). The reader-retry constructor (`readonly`,
  `retry_seconds`, `_retry_read`) and the chain counters are combined in `Ledger.__init__`;
  `checkpoint()` is now the ledger branch's PASSIVE checkpoint; `write_snapshot` (service) kept.
* **service.py** (conflict): the janitor writes the operator ledger snapshot (service) and the
  periodic chain anchor (ledger); both kept. Startup failure keeps `_terminal = True` (service)
  and `_anchor_chain("startup_failed")` (ledger).
* **tests/integration/harness.py** (conflict): `Harness` keeps both changes: host clock-sync
  pinned to "unknown" (service) and the chain verification after every stop (`check_chain`).
* **docs/07** (conflict): the chain-head custody checkbox stays in the general checklist; the
  Windows deployment checklist follows it unchanged.
* `alerts.py` auto-merged: V-003, V-004 added. Catalogue: **38 codes, no duplicates**.
* **`ledgerchain._ro_connect` now uses the reader-retry rule** (fix/ledger-reader-retry notes
  asked for this): open + a probe query are retried on the same transient errors and budget
  (`db.READ_RETRY_SECONDS`, `db._is_transient`); other errors raise at once. Test:
  `test_chain_reader_retries_a_transient_error_like_ledger_readers`.
* **Status anchor under `status_dir`**: `verify_ledger` read the `status.json` anchor beside the
  ledger only; with the service layout it lives in `status_dir`. New `status_path` parameter,
  passed as `cfg.status_path` by the CLI, diagnostics and the integrity worker (default unchanged).
  Test: `test_chain_verification_reads_the_status_anchor_from_status_dir`.
* **Chain coverage of the write paths other branches added (security-relevant review):**
  * `events` written by export (`ExportLedger.event`) and by drive-prepare (`drive_prepared`,
    refusals, failures) go through `Ledger.add_event` → **chained**.
  * The export tables `drives`, `export_batches`, `export_items` are written directly by
    `sentinel.export.ledger` and are **NOT covered** by chain format 1 (it covers runs, segments,
    gaps, events; adding tables needs a new chain format, as the ledger branch notes say). A
    person with write access to the data folder could alter batch/item state or recorded
    read-back hashes there without a V-003. Mitigation done here: the chained `export_complete`
    audit event now also carries the interval, the SHA-256 of the manifest and of
    `SHA256SUMS.txt` as written to the drive, and the number of verified files, so the outcome
    that matters for custody is chained (the drive's own manifest/SUMS remain the primary
    record). Export start/failure/interruption/conflict events were already chained.
  * The `drives` table is also read by drive-prepare's erase guard (`known.py`): deleting a row
    there could make an evidence drive look unknown. The guard does not depend on it alone
    (marker, layout and `drive_prepared` events are checked too; events are chained), but this
    is a documented residual risk. Owner decision for a later chain format 2: cover
    `export_batches`/`export_items`/`drives`.
  * Not done (recommended by the ledger branch, needs a manifest-format decision): write
    `current_chain_head()` into `Export_Manifest.json` and an `export` anchor line, so the head
    leaves the machine on every review drive.

## 8. req/operator-gui (requested by the coordinator as merge 8)
Auto-merged (only `pyproject.toml` shared). Integration changes:
* **GUI reads `[storage] status_dir` under the Windows service layout** (`gui/window.py`):
  `status.json` from `cfg.status_path`; history from `cfg.ledger_snapshot_path`
  (`status_dir/ledger-snapshot.db`, written by the service janitor every 5 min) when a separate
  status folder is configured, else the live ledger in `data_dir` as before. The snapshot is
  opened `mode=ro&immutable=1` (new `Ledger(..., immutable=True)` for readers;
  `gui/ledger_view.open_ledger` selects it by the snapshot file name
  `config.LEDGER_SNAPSHOT_NAME`), so an operator account never needs to create `-wal`/`-shm`
  files. The 24 h timeline allows the snapshot interval as extra "being recorded now" slack so
  the newest ≤ 5 min are not shown as "no information".
* **Admin PIN file** (`gui/admin_pin.admin_pin_dir`): with a separate `status_dir` it lives in the
  configuration folder (installer: Users read, Administrators change — the service branch's
  rule that the PIN verifier must never be operator-writable); otherwise in `data_dir` as before.
  `--set-admin-pin` then needs an administrator.
* **Prepare Review Drive stays disabled** (GUI branch decision, unchanged), also now that
  `sentinel.export` is installed: the request-file interface (GUI writes a request; the service
  runs the export) is not built.
* **Tests adapted, with reason:** `test_gui_viewmodel.py::test_exports_none_yet_and_known_table`,
  `::test_exports_states_always_shown_in_plain_words`, `::test_exports_unknown_schema_is_reported_not_guessed`
  and `test_gui_widgets.py::test_exports_page_shows_plain_words_not_ledger_states` created a
  reduced copy of the export schema in a fresh ledger; since merge 3 every ledger already has
  the real tables, so `CREATE TABLE` failed and "no export table yet" no longer occurs. They now
  insert into the real tables (all required columns), and the "unknown export schema" case uses
  a database from other software (plain sqlite3). The plain-word outcome assertions are unchanged.
* New tests: `tests/unit/test_gui_status_dir.py` (window uses the status folder and the snapshot;
  snapshot read immutable, no -wal/-shm created, bytes unchanged; PIN folder by layout).
* **Not integrated (recorded):** the GUI's "Save diagnostic report" calls `build_report`, which
  reads the live ledger and logs in service-only folders; under the service layout an operator
  account gets a partial report (the status part only). A service-side report request is needed.
  The GUI is not registered in `launcher.view.VIEW_PROVIDERS` yet (Sentinel.exe still shows the
  console view); packaging PySide6 into the frozen build is not done (not built here).
