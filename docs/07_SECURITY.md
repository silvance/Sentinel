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
| Files created 0640 / directories 0750 (umask 027) | `platform/posix.py`, systemd `UMask=0027` | **not executed** (Linux) |
| Sealed originals read-only; rolling audit re-hashes them and alerts V-001 on change | `finalizer.py`, `integrity.py` | `test_audit_detects_modified_original` |
| Integrity fields write-once in the ledger | `db.py` | `test_sealed_integrity_fields_are_immutable` |
| Ledger tamper evidence: SHA-256 hash chain over runs, sealed segments, gaps and events; head anchored to `chain_heads.log`, `status.json`, diagnostic reports; periodic verification raises V-003 (see below) | `ledgerchain.py`, `db.py`, `recorder/integrity.py` | `tests/unit/test_ledgerchain.py`, `tests/integration/test_ledger_chain.py` |
| Audit events: service start/stop, configuration change (config file SHA-256), recovery, integrity failures | `service.py`, `db.events` | integration tests |
| FFmpeg ≥ 6.0 enforced | `service.py` | — (dev host has 9.0.1) |
| Single instance (no double recording) | `platform.acquire_instance_lock` | process test |

## Residual risks (accepted only with listed controls)

* **B4 — RTSP credentials in FFmpeg's command line.** FFmpeg cannot read the URL from a file.
  Controls: systemd `ProtectProc=invisible` (or `/proc` mounted `hidepid=invisible`), no other
  interactive users on the appliance, a view-only camera account.
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

If a chain entry cannot be written (e.g. the table was dropped while running) the evidence change
is still committed (a savepoint isolates the chain write), the failure is counted and alert V-003
is raised. The chain must never be a reason to lose a recording.

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
(read-only, safe while recording; exit 0 = intact, 1 = altered, 2 = cannot check) and, every
`chain_verify_interval_s` (default 1 h, first run ~90 s after start) in the integrity worker:
1. recompute every entry hash and link; first missing / reordered / edited entry is reported;
2. compare each covered row with its latest entry (edited, deleted, un-sealed, or never-logged rows);
3. compare with every saved head found (`chain_heads.log`, `status.json`, `--expect-head`);
4. `--check-files`: re-hash every sealed recording file against the SHA-256 in the chain
   (the periodic check skips this — the rolling audit already compares files with their rows,
   and rows are compared with the chain).
The report names the first log position that can no longer be confirmed, in plain language,
e.g. *"First problem at log entry 57: The recording segment CAM01/…/seg_000003.ts was changed
after log entry 57 recorded it: end_utc was '…' and is now '…'."* A failure raises the operator
alert **V-003 "The recording log has been altered"** (critical, recording continues) and an
`ledger_chain_altered` event (once per distinct first problem).

Measured cost (Windows 11 dev host, this branch's tests): chain bookkeeping ~0.13–0.2 ms per seal
(median), i.e. ~0.2 % of the real per-segment sealing work with 2 s test segments (75 ms median;
production 300 s segments hash far more data, so the share is smaller still). Verification runs at
~22 000 entries/s (measured: 25 922 entries = 30 simulated days of 300 s segments, each sealed,
content-hashed and audited once, in 1.16 s; peak memory 5.7 MB). A year is therefore ~0.32 M
entries, ~15 s per hourly check and ~70 MB peak memory, in a background thread on a separate
read-only connection (it never holds the recorder's ledger lock). Storage: each segment's three
snapshots add ~7 KB to the ledger (~2 MB/day at 300 s segments), negligible next to the video.

### Ledgers created before this feature
On first start with this software the chain begins with a `genesis` entry recording how many rows
already existed; those rows are **adopted** (one `adopted` entry each). From then on they are
protected like new rows, but **changes made to them before the genesis time cannot be detected**,
and the verify report says so. If the chain is found empty although an earlier chain existed
(schema marker or anchor log present), the new genesis is marked `previous_chain_missing` and
verification reports it as a failure: wiping the chain cannot silently restart it.

### What this does NOT protect against (honest limits)
* **An attacker with full control before the first anchor leaves the machine.** With write access
  to the data directory they can edit rows, recompute a perfect chain, and rewrite
  `chain_heads.log`, `status.json` and local reports. Verification will then pass. Only a head
  held elsewhere (review drive, support, paper) exposes it. Ship heads off the machine routinely.
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
  `discrepancy` (table/key null; data = `{table, key, expected_entry, expected, found}`).
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
