# Security Design and Deployment Checklist

Nothing here asserts that the camera, workstation or software is approved for any government
network. Approval is the deploying organisation's decision.

## Implemented (Phase 2)

| Control | Where | Verified by |
|---|---|---|
| No network listener, no telemetry, no cloud calls | whole codebase; production config accepts only `rtsp`/`rtsps` client URLs | review; `test_invalid_config_rejected` |
| Test sources (tcp/file, `input_format`) only with `development_source = true`, which raises permanent alert Z-001 | `config.py`, `alerts.py` | unit + integration tests |
| Credentials only in a separate file; never in the main config URL, path or query | `config.py`, `credentials.py` | unit tests |
| Credentials redacted from every log line, traceback, ledger text, status file and diagnostic report | `credentials.redact`, `logging_setup` | `test_credentials_never_leak` (real FFmpeg error output) |
| Credentials file must be 0600 and owned by the service account (Linux; startup refused otherwise) | `platform/posix.py`, `service.py` | **not executed** (Linux) |
| Windows: credentials file DACL checked at startup — only SYSTEM, Administrators, the process's own account and `NT SERVICE\SentinelRecorder` may read/change/re-permission it; startup refused (C-001) otherwise. Read from the security descriptor as language-neutral SDDL, not localised `icacls` text | `platform/windows.py`, `platform/winacl.py`, `service.py` | `test_real_file_dacl_and_secret_file_problems` (real DACL, real `icacls` grant), SDDL parser tests on captured strings |
| Windows: camera credentials protected with DPAPI (`CryptProtectData`, machine scope, application entropy) via ctypes; written by `python -m sentinel.winsvc set-camera-credentials` (getpass, never echoed or logged); ACL restricted *before* the secret is written; read back and compared before replacing the file. Plain TOML still accepted (Linux; Windows fallback with a logged warning) | `winsvc/dpapi.py`, `winsvc/credstore.py`, `credentials.py` | `tests/unit/test_dpapi_credentials.py` (real DPAPI round trips, user **and machine** scope, without admin) |
| Files created 0640 / directories 0750 (umask 027) | `platform/posix.py`, systemd `UMask=0027` | **not executed** (Linux) |
| Windows equivalent of the umask: installer gives spool/data/log an inheritable ACL without inheritance from the parent — SYSTEM + Administrators full, service account modify, nobody else; the configuration folder: Administrators change, service + Users read; the credentials file: Administrators full, service read. Language-neutral `*S-1-…` SIDs. Refuses to re-permission drive roots and system/profile folders | `winsvc/commands.py` | generated commands unit-tested; `icacls` grants/reset **executed on a temp folder** (`test_generated_icacls_commands_execute_and_produce_the_intended_acl`); `/setowner` and the real install **not executed** (admin) |
| Windows service runs as the virtual account `NT SERVICE\SentinelRecorder` (no password, no admin rights, own SID), with only `SeChangeNotifyPrivilege`; no network listener is added | `winsvc/commands.py` | **not executed** (admin) |
| Operators can **read, not write**, the recorder status: `[storage] status_dir` (required for the Windows install) holds `status.json` and `ledger-snapshot.db`; ACL SYSTEM/Administrators full, service modify, local group **"Sentinel Operators"** read (owner may choose `--status-readers users`). The live ledger stays service-only: a WAL-mode SQLite reader needs write access to `-shm`, so the GUI reads a self-contained copy refreshed every 5 min (online backup API, rollback journal, atomic replace; open with `mode=ro&immutable=1`) | `config.py`, `db.write_snapshot`, `service.py` janitor, `winsvc/commands.py` | snapshot + status folder ACL tests (`tests/unit/test_review_fixes.py`, real `icacls` on a temp folder); group creation **not executed** (admin) |
| Service never runs code non-administrators can change: `install` refuses if any file/folder of the interpreter, PYTHONPATH, Sentinel package, FFmpeg folders, or the configuration (it names the FFmpeg path) grants write/delete/re-permission rights to anyone but SYSTEM, Administrators, TrustedInstaller, or is owned by someone else; every service start re-checks the top-level paths (C-001 otherwise) | `winsvc/codeacl.py`, `winsvc/__main__.py` | DACL fixtures (captured Program Files, generated `C:\<folder>` default with Authenticated Users Modify), real scan |
| Sealed originals read-only; rolling audit re-hashes them and alerts V-001 on change | `finalizer.py`, `integrity.py` | `test_audit_detects_modified_original` |
| Integrity fields write-once in the ledger | `db.py` | `test_sealed_integrity_fields_are_immutable` |
| Ledger tamper evidence: SHA-256 hash chain over runs, sealed segments, gaps and events; head anchored to `chain_heads.log`, `status.json`, diagnostic reports; periodic verification raises V-003 (see below) | `ledgerchain.py`, `db.py`, `recorder/integrity.py` | `tests/unit/test_ledgerchain.py`, `tests/integration/test_ledger_chain.py` |
| Audit events: service start/stop, configuration change (config file SHA-256), recovery, integrity failures | `service.py`, `db.events` | integration tests |
| FFmpeg ≥ 6.0 enforced | `service.py` | — (dev host has 9.0.1) |
| Single instance (no double recording) | `platform.acquire_instance_lock` | process test |

## Residual risks (accepted only with listed controls)

* **B4 — RTSP credentials in FFmpeg's command line.** FFmpeg cannot read the URL from a file.
  Controls: systemd `ProtectProc=invisible` (or `/proc` mounted `hidepid=invisible`), no other
  interactive users on the appliance, a view-only camera account. **Windows has no equivalent of
  `ProtectProc`:** FFmpeg's command line (with the camera password) is readable by any process that
  can open the FFmpeg process with query access. Administrators can always do so; whether a standard
  user can open a process owned by the service's virtual account depends on that process's security
  descriptor and was **not measured** (it needs an installed service). DPAPI protects the file at
  rest only. Controls: no other interactive users, operators as standard (non-admin) users, view-only
  camera account. Not measured on this host.
* **Windows DPAPI machine scope.** Any process on the machine that can *read* the protected file
  can decrypt it; the file ACL is the real barrier. DPAPI does not protect against administrators,
  or against extraction of the machine DPAPI keys from an unencrypted disk (use BitLocker).
* **Same-account tampering.** The recorder account can lift the read-only bit on its own files.
  The rolling audit detects changes; stronger separation (a separate owner via a privileged
  sealing helper, or filesystem immutability) is a Phase 6 option.
* **Ledger tamper evidence is key-less and anchored locally by default.** The ledger is now
  hash-chained (next section): edits are detectable, not impossible. Someone who controls the
  machine can rebuild a self-consistent chain; that is exposed only by a chain head copied off
  the machine earlier. Signed export manifests remain recommended (docs/04).
* **Supply chain.** Python dependencies are version-pinned but not yet hash-pinned; FFmpeg comes
  from the distribution. Phase 6: `--require-hashes` requirements and a recorded FFmpeg build hash.

## Ledger tamper evidence (hash chain)

Requirement E2/E5 (integrity, auditability). Before this, anyone with file access could edit
`sentinel.db` (a segment's SHA-256, its times, its state, a gap, an event) and nothing would show
it. Now the edit is **detectable**; it is not **prevented**.

### What is chained
Every change the recorder makes to a covered row appends an entry to table `ledger_chain`
**in the same SQLite transaction as the change** (sealing a segment is still exactly one
transaction: state + file SHA-256 + times + its gap rows + closing the outage gap + events +
their chain entries). Covered rows and columns (`ledgerchain.COVERED`):

| Table | Covered | Not covered (bookkeeping) |
|---|---|---|
| `recording_runs` | identity, launch, anchor, timeline shift, end time/reason/exit code, clock anomaly, versions, redacted command; `last_stream_time_s` once the run has ended | `gap_recorded`; `last_stream_time_s` while the run is still recording |
| `segments` | once sealed (FINALIZED/INTERRUPTED/CORRUPT): path, state, stream and UTC times, time source, size, file SHA-256, content hash, frame-hash sidecar hash, codec facts, warnings, validation detail, sealed time, audit status | retry counters, last error, `last_audit_utc`, `updated_utc`; segments still being written (ACTIVE/CLOSED) |
| `recording_gaps` | every column, when opened, updated (retry count) and closed | — |
| `events` | every column (service start/stop, config change, integrity, audit, errors) | — |

Entry `n` holds a canonical JSON record (snapshot of the covered columns) and
`h_n = SHA-256(h_{n-1} || record_n)`. Before every write the recorder also compares the row with
its latest entry; if they differ, the row was changed outside the recorder, and a permanent
`discrepancy` entry plus a `ledger_discrepancy` event are written first. A later legitimate update
therefore cannot "launder" an edit by snapshotting the edited values.

If a chain entry cannot be written (an internal fault, e.g. a disk error) the evidence change is
still committed (a savepoint isolates the chain write). The affected rows are remembered in
`ledger_chain_pending`, and operator alert **V-004 "Part of the recording log could not be
protected against changes"** (warning) is raised. This is a system fault, not V-003. At the next
ledger write (also after a restart) each pending row gets a `protection_gap` entry (when and why
the entry failed) and a `chain_repair` snapshot. Its next update is therefore not mistaken for an
outside change. If SQLite has aborted the whole transaction, or the savepoint cannot be undone, the
complete transaction is rolled back (`ChainTransactionAborted`). Nothing half-written is committed,
and the finaliser retries the seal as it does any other failed seal. The chain must never be a
reason to lose or corrupt a recording.

### Anchoring (copies of the chain head)
The head `(sequence, hash, UTC)` is copied to:
* `data/chain_heads.log` — append-only text, fsynced; at service start, every
  `chain_anchor_interval_s` (default 15 min, only if the head moved), at service stop, at each
  diagnostic report;
* `status.json` → `integrity.ledger_chain` (rewritten every few seconds);
* every diagnostic report (`ledger_chain.json`, `summary.txt`, covered by the report's MANIFEST);
* export manifests: `sentinel.ledgerchain.current_chain_head(ledger)` is the function the export
  branch should call and write into `Export_Manifest.json` (not done on this branch).
* by hand: `python -m sentinel.ledgerchain head --config …` prints a head an administrator can
  write down or photograph at each drive swap.

What anchoring buys: once a head has been copied, any later rewrite of the log up to that entry
changes that entry's hash, so the copy no longer matches. To hide an edit an attacker must find
and rewrite **every** copy made after the edited entry. Copies on this machine (`chain_heads.log`,
`status.json`, reports in `data/diagnostics`) are reachable by the same attacker and only raise the
effort; copies that have **left the machine** (a review drive's manifest, a report e-mailed to
support, a head written in the custody log) cannot be rewritten by them and are what makes a
rewrite detectable. Rolling the database back to an older copy is exposed the same way: a saved
head with a higher sequence number shows the end of the log was removed.

### Verification
`python -m sentinel.ledgerchain verify --config sentinel.toml [--check-files] [--expect-head SEQ:HASH]`
checks the log. It is read-only and safe while recording. Exit status: 0 = intact, 1 = altered,
2 = cannot check, 3 = intact with protection gaps. The integrity worker also runs it every
`chain_verify_interval_s` (default 1 h; the first run is about 90 s after start):
1. recompute every entry hash and link; first missing / reordered / edited entry is reported;
2. **recorder rules across each row's entries** (review finding C1, below): a later entry may only
   make changes the recorder makes. Rejected:
   * sealed segments: any change to state or to a write-once column (`db.WRITE_ONCE`: hashes, size,
     path, times, duration; these may only go from empty to a value), and any change to the other
     covered columns. The exceptions are `audit_status`, and `clock_anomaly` going from 0 to 1.
   * events: changed at all;
   * gaps: any change once closed, or a change to their start, reason, kind or detail, or the retry
     count going down;
   * runs: any change to their identity or launch columns, write-once anchor and timeline values
     that are changed, or end columns changed after the run ended;
   * `genesis` / `adopted` entries anywhere except the start of the log, and unknown kinds;
3. compare each covered row with its latest entry (edited, deleted, un-sealed, or never-logged rows);
4. compare with every saved head found (`chain_heads.log`, `status.json`, `--expect-head`);
5. `--check-files`: re-hash every sealed recording file against the SHA-256 in the chain
   (the periodic check skips this — the rolling audit already compares files with their rows,
   and rows are compared with the chain).
It works in many short read snapshots (at most 2 000 entries, keys or rows each, with a short
pause between them), not in one long snapshot, so memory stays bounded. The chain is append-only.
It is verified up to the head seen at the start, and each row is compared with its entries inside
one snapshot, so results are consistent while the recorder keeps writing.
The report names the first log position that can no longer be confirmed, in plain language,
e.g. *"First problem at log entry 57: The recording segment CAM01/…/seg_000003.ts was changed
after log entry 57 recorded it: end_utc was '…' and is now '…'."* A failure raises the operator
alert **V-003 "The recording log has been altered"** (critical, recording continues) and an
`ledger_chain_altered` event (once per distinct first problem).

Measured cost (Windows 11 dev host, this branch's tests): chain bookkeeping ~0.13–0.2 ms per seal
(median), i.e. ~0.2 % of the real per-segment sealing work with 2 s test segments (75 ms median;
production 300 s segments hash far more data, so the share is smaller still).

Verification was measured on a synthetic ledger the size of one year: 105 000 segments of 300 s,
each sealed, content-hashed and audited, plus events. That is 325 502 entries and 746 MB.
* One full check took **26 s** in 292 short snapshots. The longest single snapshot was **0.28 s**.
* Peak Python memory was **9.9 MB** (measured with `tracemalloc`, which made that run 80 s long).
* While a check ran, 435 seals with a checkpoint every 20 took at most **132 ms** each.

The check runs in a background thread on a separate read-only connection, so it never holds the
recorder's ledger lock. The ledger checkpoint is now `PASSIVE`, which never waits for readers, so
it no longer holds the ledger lock while a reader keeps a snapshot open; the WAL is limited with
`journal_size_limit = 64 MB`. Each segment's three snapshots add ~7 KB to the ledger (~2 MB/day at
300 s segments), which is negligible next to the video.

### Ledgers created before this feature
On first start with this software the chain begins with a `genesis` entry recording how many rows
already existed; those rows are **adopted** (one `adopted` entry each). From then on they are
protected like new rows, but **changes made to them before the genesis time cannot be detected**,
and the verify report says so. If the chain is found empty although an earlier chain existed
(schema marker or anchor log present), the new genesis is marked `previous_chain_missing` and
verification reports it as a failure: wiping the chain cannot silently restart it.

### What this does NOT protect against (honest limits)
* **Anything after the last head that left the machine.** The chain has no key. Anyone with write
  access to the data directory can write well-formed entries, and can rewrite `chain_heads.log`,
  `status.json` and local reports. Let *A* be the last chain head copied off the machine (review
  drive, report sent to support, custody form):
  * **Detected:** any change to history up to *A*. That covers rewriting or reordering entries up
    to *A*, cutting the log back below *A*, deleting rows, and appending entries that break the
    recorder rules above. Once a segment's hash, size, times and state, an event, a closed gap or
    an ended run were in the log at *A*, they cannot be changed afterwards without detection,
    whether by editing old entries or by appending new ones.
  * **Not detected:**
    * entries after *A* rewritten or removed (cutting back to *A* and writing a new tail);
    * invented new rows: events, gaps, runs, and even sealed segments together with matching
      invented files;
    * values the recorder itself fills in later, entered falsely. Examples: a content hash not yet
      computed at *A* (asserted by `test_rule_compliant_appends_are_the_documented_limit`), the
      end of a gap or run still open at *A*, an audit status, a clock-anomaly flag.
  * **No off-machine head at all:** the attacker can rebuild the whole chain consistently, and
    verification passes.
  * Going further needs entries the attacker cannot produce. Options are a signature or HMAC key
    the recorder account cannot use (TPM-held, see below), an external append-only witness, or
    copying heads off the machine much more often. Ship heads off the machine routinely.
* **A compromised recorder.** Whoever runs code as the recorder can write false data through the
  normal path; the chain then faithfully records the false data. The chain proves the log was not
  changed *after* writing, not that what was written is true.
* **Prevention.** Nothing stops deletion of the whole data directory or the database; that is
  detectable only by absence (missing drive exports, gaps in custody records).
* **Trusted time.** The UTC in each entry is the host clock (docs/03, E1); it is not a timestamp
  from an independent authority.
* **Uncovered data.** Bookkeeping columns listed above, segments while still being written
  (seconds to minutes before sealing), and any column added by later schema changes until the
  chain format lists it.
* **Downgrades.** Older software writes rows without chain entries; verification then reports
  them (correctly) as unlogged. A fresh installation must archive the old `chain_heads.log`
  together with the old database, or its saved heads will (correctly) not match the new log.
* There is no acknowledgement mechanism: once V-003 is raised it stays raised for that ledger.
* `ledger_chain_pending` is not chained. Someone who writes a pending marker for a row can make a
  later *rule-compliant* change to it be reported as a protection gap (V-004) rather than an
  alteration (V-003). Rule-breaking changes are still V-003, and V-004 still calls support.

### Review findings (independent QA, wave 2)
| ID | Finding | Status |
|---|---|---|
| C1 HIGH | An edit to a sealed segment's `sha256`/`size_bytes`, plus one well-formed appended `segment_updated` entry, passed verification even with an earlier off-machine anchor, and was later anchored. | **Fixed:** recorder rules are enforced across each row's entries (finding `illegal_change`/`illegal_entry`). The reviewer's script is a regression test (`test_reviewer_forged_append_with_earlier_anchor_is_detected`), and 13 more append cases are tested. **Remaining (by design, documented above):** rule-compliant appends after the last off-machine anchor. These need a keyed or attested chain. |
| C2 MED | Internal chain faults raised V-003 and left rows permanently "unlogged". The next normal update was then reported as a change made outside the software. | **Fixed:** new V-004 (system-fault wording); pending rows are persisted and re-recorded with `protection_gap` + `chain_repair`; verify reports them as protection gaps (CLI exit 3), and still flags rule-breaking changes across the gap. An aborted transaction, or a savepoint that cannot be undone, rolls the whole seal back and it is retried (tested; nothing half-written, no blocking). |
| C3 LOW-MED | The hourly verify held one read snapshot for 15–25 s at year scale while the hourly `wal_checkpoint(TRUNCATE)` waited under the ledger lock; peak memory ~100 MB. | **Fixed:** verification uses short snapshots (longest 0.28 s at year scale); the checkpoint is `PASSIVE` plus `journal_size_limit`; peak memory is 9.9 MB; seal latency while verifying is at most 132 ms (measured above; unit test with a concurrent writer). |

### Why key-less (no HMAC / signature key) on this branch
* A key-less SHA-256 chain can be verified by anyone, including an examiner with no Sentinel
  secrets, from the specification below. That is the property courts and reviewers need most.
* An **HMAC keyed on this machine** (e.g. a key protected with Windows DPAPI, which another branch
  uses for camera credentials) does not stop the main threat: an attacker running as the recorder
  account can ask DPAPI to unprotect the key and recompute valid HMACs. It *would* stop an attacker
  who only has the disk offline (DPAPI keys are not usable without the account's credentials), at
  the cost of: verification needs the key (examiners cannot verify independently), key loss makes
  the log unverifiable, Linux has no DPAPI equivalent (would need a TPM or kernel keyring), and a
  cross-branch dependency. **Not added; recommended as an optional second column later**, keeping
  the key-less chain for examiners.
* **Signatures with a TPM-held key** (or a hardware token) are the strong option: the key never
  leaves the TPM, so even the recorder account cannot forge old entries offline. It needs either a
  new dependency (`cryptography`) or platform APIs (Windows CNG/TPM via ctypes, `tpm2-tools`),
  TPM availability on the deployment hardware, and administrator rights to provision — none of
  which this branch can verify. Deferred to Phase 6 together with signed export manifests.
* The cheapest real improvement is operational, not cryptographic: put the chain head in every
  export manifest and in the drive custody form.

### Chain format 1 — specification for independent verification
Normative text: the docstring of `src/sentinel/ledgerchain.py`; summarised here.
* Table `ledger_chain(seq INTEGER PRIMARY KEY, kind, ref_table, ref_key, record, prev_hash,
  entry_hash)`. Only `record`, `prev_hash`, `entry_hash` are evidence; the other columns are
  look-up copies of values inside `record`.
* `seq` starts at 0 and increases by 1. Entry 0 has `kind = "genesis"` and `prev_hash` = 64 ASCII
  `0`; for n > 0, `prev_hash_n = entry_hash_{n-1}`.
* `entry_hash_n = lowercase hex SHA-256( ASCII bytes of prev_hash_n (64 hex chars) followed by the
  UTF-8 bytes of record_n )`. Example: `printf '%s%s' "$prev" "$record" | sha256sum`.
* `record` = canonical JSON of `{"v":1,"seq":n,"utc":"<ISO-8601 Z>","kind":"…","table":"<table>"|null,
  "key":"<primary key as string>"|null,"data":{…}|null}`.
* Canonical JSON = **RFC 8785 (JCS)**: no whitespace; keys sorted (by UTF-16 code units; all keys
  are ASCII); strings escaped exactly as RFC 8785; numbers in ECMAScript shortest round-trip form
  (`2.0`→`2`, `0.5`, `1e-7`, `1e+21`, `-0`→`0`). Deviations, unreachable from Sentinel's own writes:
  integers beyond 2^53 are exact decimal digits; NaN/±Infinity are the strings `"NaN"`,
  `"Infinity"`, `"-Infinity"`; BLOBs are the string `x'<hex>'`.
* `data` of a row entry = the covered columns (table above / `ledgerchain.COVERED`) with SQLite
  values (NULL→null, INTEGER→integer, REAL→number, TEXT→string — JSON-in-TEXT columns are hashed as
  the stored text). The current row must equal `data` of the latest entry with that table/key.
* Kinds: `genesis`, `adopted`, `run_started`, `run_updated`, `run_ended`, `segment_sealed`,
  `segment_updated`, `gap_opened`, `gap_recorded`, `gap_updated`, `gap_closed`, `event`,
  `chain_repair`, `discrepancy` (table/key null; data = `{table, key, expected_entry, expected, found}`),
  `protection_gap` (table/key null; data = `{table, key, failed_utc, error}`).
* Between two consecutive entries of the same row, only the recorder's own transitions are
  allowed (Verification step 2; normative code `ledgerchain.illegal_changes`). `genesis` only at
  0; `adopted` only directly after genesis.
* Anchor log line: `<anchored UTC> seq=<n> hash=<64 hex> entry_utc=<UTC> reason=<word>`.
`tests/unit/test_ledgerchain.py::test_an_examiner_can_verify_from_the_written_specification`
re-implements this with only `sqlite3`, `hashlib` and `json` and agrees with the recorder.

## Deployment checklist (draft)

- [ ] Camera on an isolated switch/VLAN with no default gateway; recorder has no Internet route.
- [ ] Camera: cloud/P2P/UPnP disabled; firmware version recorded; view-only RTSP account created.
- [ ] Camera RTSP URL verified (vendor docs or ONVIF GetStreamUri); codec H.264, GOP ≤ 2 s, smart codec off.
- [ ] Audio inclusion decided and documented by the responsible authority (default: excluded).
- [ ] Ubuntu LTS, full-disk encryption per policy, automatic updates disabled; updates applied from an approved offline mirror.
- [ ] Dedicated `sentinel` service account (no login shell); operators and administrators in separate groups.
- [ ] `/etc/sentinel/camera.secret` 0600 owned by `sentinel`; `/etc/sentinel/sentinel.toml` root:sentinel 0640.
- [ ] systemd unit installed as shipped (`ProtectProc=invisible`, `UMask=0027`, watchdog, `Restart=always`).
- [ ] NTP from an approved local source (or documented manual time procedure); `timedatectl` shows synchronised.
- [ ] Suspend/hibernate disabled; BIOS set to power on after power loss; UPS recommended.
- [ ] SSH disabled or restricted to administrators; no remote desktop.
- [ ] Removable-drive filesystem approved (risk C1); drive preparation authority decided (docs/04 §4).
- [ ] Acceptance tests (docs/06) run on the deployment hardware with the real camera; results filed.
- [ ] Recording-log chain head (`python -m sentinel.ledgerchain head`) recorded on the custody form at every drive swap; `ledgerchain verify` run before any evidence hand-over.

### Windows deployment (draft; see docs/branches/design-windows-service.md)

- [ ] Program (Python or the PyInstaller **one-folder** build, Sentinel, FFmpeg) under `C:\Program Files\Sentinel` — not in a personal user folder (the service account cannot read those; `install` refuses).
- [ ] Configuration in `C:\ProgramData\Sentinel\config\` with full `[tools]` paths; data/spool/logs and a separate `status_dir` in dedicated folders (example: `config/sentinel.windows.example.toml`). Never under a folder created directly in `C:\` without fixing its permissions (Authenticated Users: Modify).
- [ ] Status readers (owner decision: local group `Sentinel Operators`, the install default): add each operator account with `net localgroup "Sentinel Operators" <account> /add`. `--status-readers users` exists only as an explicit alternative.
- [ ] Elevated Command Prompt: `python -m sentinel.winsvc set-camera-credentials --config …`, then `install --config … --dry-run`, review, then `install --config …`.
- [ ] `python -m sentinel.winsvc status --config …` shows RUNNING, folders "OK", credentials "DPAPI-protected; permissions: OK", time synchronisation "yes".
- [ ] Time (owner decision): a small local time server on the camera network; `install --time-server <host>` sets the Windows Time service to start automatically, starts it, and points it at that server (`sc.exe config w32time start= auto`, `sc.exe start w32time`, `w32tm /config /manualpeerlist:<host>,0x8 /syncfromflags:manual /update`, `w32tm /resync /nowait`). Without `--time-server` the service is still set to automatic but keeps the Windows default source (no Internet on the appliance ⇒ T-002 until a server is configured). Afterwards `winsvc status` must show time synchronisation "yes".
- [ ] Operators are standard users (not administrators); BitLocker per policy; sleep/hibernate disabled; power-on after power loss.
- [ ] Reboot test: recording resumes with nobody logged in; kill test (`taskkill /F` on the service process) shows a restart within ~5 s.
