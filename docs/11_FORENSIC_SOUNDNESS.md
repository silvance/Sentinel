# Forensic soundness: Sentinel against NIST and SWGDE guidance (self-assessment, 0.3.29)

Written 2026-11-23. This is a self-assessment by the developer: it maps Sentinel's design to published
guidance and lists what is still open. **It is not a forensic validation, an accreditation or a
certification, and Sentinel does not claim to be forensically validated.**

## 1. What "NIST compliant" can and cannot mean here

There is no NIST compliance programme, certification or test that a video recorder can pass:

* NIST's **Computer Forensics Tool Testing (CFTT)** programme publishes test specifications for tool types
  such as disk imaging and write blockers. It has none for video recorders.
* The **OSAC Registry** (administered by NIST) lists standards for *examiners* who retrieve and examine
  video, for example the Standard Practice for Data Retrieval from Digital CCTV Systems. They describe
  how an examiner should take video out of a system like Sentinel, not how the system must be built.
* **NIST IR 8387** (*Digital Evidence Preservation: Considerations for Evidence Handlers*, 2022) is guidance
  for evidence handlers, not a requirement on a product.
* **FIPS 180-4** defines SHA-256, the hash Sentinel uses. **FIPS 186-5** approves EdDSA (Ed25519), which signs
  Sentinel's update files.

So the claim Sentinel can honestly support is: *"designed to follow NIST and SWGDE guidance on preserving
digital video, with the controls and open items listed in docs/11."* Whether its recordings are accepted
is decided by the receiving agency's procedures and, in the end, by a court. Validation is the agency's
own testing of the tool under its quality system (for example ISO/IEC 17025), and docs/09 is the bench test
it can start from.

## 2. Guidance used

| Reference | What it covers that applies here |
| --- | --- |
| NIST IR 8387 (2022), https://doi.org/10.6028/NIST.IR.8387 | Preserving digital evidence: integrity, documentation, storage, custody |
| SWGDE 17-I-001, *Best Practices for Maintaining the Integrity of Imagery* | Hash verification and other ways to show a video is unchanged |
| SWGDE 17-V-002, *Best Practices for Data Acquisition from Digital Video Recorders* | Native format as best evidence; documented, verified acquisition |
| OSAC Registry: *Standard Practice for Data Retrieval from Digital CCTV Systems* | Retrieval of native data, time offset documentation, working copies |
| FIPS 180-4 / FIPS 186-5 | SHA-256; Ed25519 signatures |
| docs/00 E8 | SWGDE 17-V-002 / 18-F-002 already named as SOP references in the requirements |

The SWGDE and OSAC documents should be read in their current versions before an SOP is written. This
table summarises their scope; it does not quote them.

## 3. Principles and Sentinel's controls

Status: **Met** = designed, implemented and covered by tests in this repository; **Partly** = implemented
with a limit stated; **Open** = not done.

| # | Principle | Sentinel's control | Where | Status |
| --- | --- | --- | --- | --- |
| P1 | Keep the native recording; never re-encode the original | The video stream is copied from the camera unchanged (`-c copy`) into MPEG-TS files | `recorder/ffmpeg_cmd.py`; docs/03 | Met |
| P2 | Hash at the moment of acquisition with a documented algorithm | Each file is sealed with SHA-256 of the file, a hash of its decoded pictures (`content_sha256`, with the method recorded) and a per-picture hash list | `recorder/integrity.py`, `recorder/finalizer.py`; docs/03 | Met |
| P3 | Make tampering with the record evident | Every ledger row is in a SHA-256 hash chain; copies of the chain head are written to archive drives and exports and can be checked with `ledger verify --anchors-from` | `ledgerchain.py`; docs/07 "Ledger tamper evidence" | Partly: the chain has no key, so an administrator could rewrite the ledger and recompute it; the copies of the chain head on drives that have left the computer are what reveals that (docs/07 "honest limits") |
| P4 | Verify every copy against the original | Archive copies are read back and compared with the sealed hash before they count; copies are made read-only | `archive/worker.py` (`VerifyMismatch`) | Met |
| P5 | Keep the original until verified copies exist | Recordings leave the computer only when they have the required number of verified archive copies, are sealed and are not recent; every removal is a ledger entry | `retention/manager.py` | Met |
| P6 | Document time and its accuracy | Times are UTC receipt times; the operating system's synchronisation state, an optional reference-server offset, camera and PC drift, corrections and clock steps are recorded; docs/03 4.5 says how to state time accuracy | `recorder/timebase.py`, clock observations; docs/03 4.4–4.5 | Partly: see G1 |
| P7 | Mark missing or damaged data instead of hiding it | Gaps carry a reason; interrupted, corrupt and clock-anomaly files are flagged; pictures lost to damaged camera data are marked (0.3.29) | `export/manifest.py`, `recorder/finalizer.py` | Met |
| P8 | Exports carry their own verification data | `SHA256SUMS.txt`, `Export_Manifest.json/.csv`, a summary with gaps, the operator and the chain head; generated deterministically | `export/manifest.py`, `export/engine.py`; docs/04 | Met |
| P9 | Record who did what | Exports record the operator; administrative actions (new recording log, drive erase, removals) are ledger entries; the new recording log keeps the earlier log whole | `export/`, `newlog.py`, `drives/` | Met |
| P10 | Restrict who can change or delete evidence | Data folders are limited to SYSTEM, Administrators and the service; operators cannot delete; destructive steps need administrator rights and typed confirmation; drives are never formatted automatically | docs/07, docs/08; `drives/` | Met when the Windows services are installed (see G3) |
| P11 | Record the tool and its version | Each run records the Sentinel and FFmpeg versions and the host | `ledgerchain.py` run fields | Met |
| P12 | Derived products are labelled as derived | Sound converted to AAC is labelled; the export says the picture check does not cover sound | `recorder/audio.py`, `export/manifest.py` | Partly: see G2 |
| P13 | Test the tool before relying on it | 1766 automated tests (2026-11-22, this PC); bench test docs/09; acceptance plan docs/06 | `tests/`, docs/06, docs/09 | Open: see G4 |

## 4. Gaps and what to do about them

| # | Gap | Effect on the evidence | Action | Who |
| --- | --- | --- | --- | --- |
| G1 | **The laptop's clock is not synchronised** (T-002 in the 2026-11-13 diagnostic report). | Times are the laptop's own clock, with no measured offset from true time. They can be stated only as "PC time, offset unknown", which weakens every timestamp. | Point Windows Time at a source the site allows (a local NTP server or GPS time source if the network is closed), or, failing that, record the laptop's offset against a trusted clock at set times and keep the log. Set `[service] time_reference_server` if a reference is reachable so Sentinel measures the offset itself. | Admin, on site |
| G2 | **Sound is converted (G.711 to AAC) and the camera's original sound is not kept.** | The sound is a derived product, not native data. The video is unaffected. | If sound matters as evidence: keep the camera's own sound stream in a separate file, hashed and chained like the video (a code change). Until then, state in the SOP that sound is a converted copy. Sound is off by default. | Developer, if wanted |
| G3 | **The Windows services are not installed on the laptop.** | Sentinel runs under the signed-in user, so that user's account has full control of the recording folders (shown as "WIDER THAN EXPECTED" in the diagnostic report). P10 is not in force. | System › Install / repair service before relying on the recordings. | Admin |
| G4 | **Acceptance testing on the real deployment is not finished** (docs/06: 72 h soak with the real camera, unplug/replug, power cut, clock step, on-screen camera time compared with ledger times at random points). | Without it, there is no record that the system behaves as designed on this hardware. | Run docs/09 and docs/06 item 3 on the laptop and keep the signed results with the deployment record. | Admin |
| G5 | **0.3.18 to 0.3.28 were installed without a signature check** (field installs with INSTALL-FULL-FOLDER.cmd). | Each build was checked against its own manifest, which proves it is complete but not who made it. The software that recorded during that period is still identified per run (P11), and the builds are kept in `releases/`. | Install the next version from a signed update file. Keep `releases/` (with each build's manifest) for as long as recordings made with those versions are kept. | Admin / developer |
| G6 | **Crypto modules are not FIPS 140-3 validated.** SHA-256 comes from Python's `hashlib` (OpenSSL, not in a validated mode); Ed25519 is Sentinel's own implementation, checked against the RFC 8032 test values. | The algorithms are FIPS-approved, but the modules are not validated. This matters only if a policy requires validated modules for integrity hashing. | Ask whether the receiving agency requires FIPS 140-3 validated modules. If so, that is a packaging change (a validated OpenSSL provider). | Agency, then developer |
| G7 | **No chain-of-custody form is generated.** | Exports record the operator, time, hashes and chain head, but not the hand-over from person to person. | Use the agency's own custody form (for the Army, DA Form 4137) with the export's `SHA256SUMS.txt` hash and batch ID written on it. | Admin / agency |

## 5. Suggested wording

For an SOP or a statement about the recordings:

> Video is recorded by Sentinel 0.3.x, which copies the camera's video stream without re-encoding,
> seals each file with SHA-256 hashes at the time of recording, records every file in a hash-chained log,
> and verifies every archive and export copy against those hashes. Times are the recording computer's
> UTC receipt time; their accuracy is stated from the recorded clock observations. Sentinel follows
> NIST IR 8387 and SWGDE guidance on preserving digital video; it has not been forensically validated.

Do not describe Sentinel as "NIST compliant", "NIST certified" or "forensically validated".
