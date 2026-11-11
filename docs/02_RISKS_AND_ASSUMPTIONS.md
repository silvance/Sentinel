# Unresolved Assumptions and Highest-Risk Technical Items

Each item must be closed with evidence (test log, vendor document, policy memo) before
deployment. "Owner" is the role expected to close it.

## A. Camera (MAXSUR M961-5-CP-IP) — nothing below has been verified on hardware

| ID | Assumption / unknown | Risk if wrong | How to close | Owner |
|---|---|---|---|---|
| A1 | **RTSP URL path and port are unknown.** No path is assumed in code or config. | Cannot record. | Vendor documentation, or ONVIF `GetStreamUri` against the actual unit. | Field engineer |
| A2 | Main stream actually delivers 2592×1944 @ 30 fps over RTSP (advertised figures may apply to a different stream or require a setting). | Lower evidential quality than expected; bitrate estimates wrong. | `ffprobe` the live stream; record 1 h and measure. | Media engineer |
| A3 | Main stream codec is H.264 or H.265 (update 2026-11-10: H.265 is recorded, fingerprinted, exported and motion-checked end to end in tests/integration/test_hevc_camera.py; review copies are tagged hvc1 and need Microsoft's HEVC Video Extensions or VLC on Windows; decoding H.265 for fingerprints and motion costs more CPU than H.264). Not "H.264+"/"Smart codec" variants. Some cameras default to H.265 or vendor smart-codecs with very long GOPs. | `-c copy` MP4 may not play on Windows without HEVC extension; long GOP makes segments far longer than 5 min. | Inspect with ffprobe; configure camera explicitly to H.264, smart-codec **off**. | Media engineer |
| A4 | I-frame (GOP) interval is short (≤ 2 s). Stream copy can only cut at keyframes. | Segment boundaries drift; longer loss when a segment is interrupted. | Configure camera GOP = 1–2 s; measure with `ffprobe -show_frames`. | Media engineer |
| A5 | RTSP over TCP (interleaved) is supported. Code defaults to TCP to avoid UDP loss. | UDP fallback loses packets silently on busy LANs. | Test `-rtsp_transport tcp` on the unit. | Media engineer |
| A6 | Camera clock / OSD timestamp behaviour, NTP support, and whether RTCP sender reports carry usable wall-clock. | Timestamp corroboration unavailable. Sentinel currently timestamps from the **recording host clock** only. | Test; document OSD config; consider local NTP server on the isolated LAN. | Field engineer |
| A7 | Audio: whether the camera has a microphone and whether audio is in scope. **Audio is excluded by default** (`-an`). | Unlawful collection if enabled outside scope. | Legal/collection-scope determination before `include_audio=true`. | Legal / supervisor |
| A8 | Camera firmware has no outbound "cloud/P2P" features that phone home. | Network exposure, policy violation. | Isolate on a dedicated switch/VLAN with no gateway; disable P2P/cloud in camera UI; packet-capture verify. | Security |
| A9 | Camera supports a **view-only user account**. | Recorder credentials could reconfigure the camera. | Create a dedicated low-privilege user on the camera. | Field engineer |

## B. Recording pipeline

| ID | Item | Risk | Mitigation / status |
|---|---|---|---|
| B1 | MPEG-TS as the original container (see storage model §2). | Some edge cases (H.264 without in-band SPS/PPS) need `-bsf:v dump_extra`. | Integration test with real camera; add bsf if probes show missing extradata. **Open.** |
| B2 | FFmpeg's RTSP client recovers via process restart, not in-process reconnect. Each restart = one gap. | Gaps of `backoff + connect` seconds per outage. | Measured in integration tests against the simulator; real-camera reconnect latency **unmeasured**. |
| B3 | Stall detection relies on `-progress` `out_time` advancing. | If FFmpeg buffers heavily, false stalls or late detection. | `stall_timeout_s` default 15 s; tested with a frozen simulator. |
| B4 | **Credentials appear in FFmpeg's argv**, visible to other local users via `/proc/<pid>/cmdline`. FFmpeg has no option to read an RTSP URL from a file/stdin. | Credential disclosure to local users. | Mount `/proc` with `hidepid=invisible`; dedicated appliance with no other interactive users; camera account view-only (A9). Logs are redacted. **Residual risk accepted only with these controls.** |
| B5 | Wall-clock timestamps are derived from the host clock at FFmpeg launch plus stream time (storage model §4). Accuracy target ±2 s, **not verified on the camera**. | Disputed times. | Host NTP sync to an approved source; clock-step detection flags affected segments; camera OSD as corroboration. |
| B6 | Monotonic clock does not advance during system suspend. | Time derivation error after suspend. | Appliance must have suspend/hibernate disabled (deployment checklist). |
| B7 | 5MP @ 30 fps bitrate unknown (likely 4–12 Mbit/s). 1 TB ≈ 9.5 days at 10 Mbit/s. | Storage sizing. | Measure (A2). Storage monitor reports measured rate. |
| B8 | Python 3.12 is the deployment target; development host runs 3.14. | Version-specific behaviour. | Code avoids >3.12 features; CI on 3.12 required before deployment. **Not yet executed on 3.12.** |

## C. Removable drives (Phase 3)

| ID | Item | Risk | Mitigation |
|---|---|---|---|
| C1 | **Filesystem choice not approved.** Candidates: NTFS (journaled, native on Windows, large files; Linux write via `ntfs-3g` FUSE or in-kernel `ntfs3`) vs exFAT (native everywhere, **no journal** – higher corruption risk if unplugged). Provisional recommendation: **NTFS via ntfs-3g**, pending organisational policy and testing. | Unreadable or corrupted review drives. | Policy decision + Phase 3 write/unplug tests. Drives are formatted **only** through Erase and Set Up Drive with its guards (owner decision 2026-11-10: NTFS; erasing off until the supervised dock test, see docs/branches/design-drive-prepare.md). |
| C2 | Sabrent dock exposes stable USB serial / WWN of the inserted SATA disk. Some bridges report the bridge's serial, not the disk's. | Drive identity ambiguity. | Read `ID_SERIAL`, `ID_WWN`, filesystem UUID + an application-written `SENTINEL_DRIVE_ID` file; test with the actual dock. |
| C3 | USB bridge honours flush/power-off (UASP quirks). | Data loss at removal. | Read-back verification after `sync` with page-cache bypass (`O_DIRECT` or drop-caches via helper); UDisks2 `PowerOff`. |
| C4 | Read-back verification must not be satisfied from the page cache. | False "verified". | Unmount/remount (or `posix_fadvise(DONTNEED)` + `O_DIRECT`) before hashing destination. **Design item for Phase 3.** |

## D. Organisational / legal

* D1 Collection scope (what may be recorded, audio, retention period, who may export).
* D2 Approved software list status of Ubuntu, FFmpeg, PySide6.
* D3 Chain-of-custody procedures for drives (this software produces manifests; it does not replace custody forms).
* D4 Operator identity: OS accounts vs. an application operator ID. Phase 4 decision.

## Highest-risk items (ranked)

1. **A1/A3/A4** — the real stream's URL, codec and GOP. Everything else assumes stream copy of H.264 with short GOPs.
2. **C1/C3/C4** — removable-drive filesystem reliability and *honest* read-back verification.
3. **B4** — RTSP credentials in process arguments.
4. **B5/A6** — defensible timestamps.
5. **B2** — gap length per network outage on the real camera.
