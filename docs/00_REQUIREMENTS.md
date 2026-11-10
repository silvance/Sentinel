# Consolidated Requirements

Sources: the original project brief, and the project owner's requirements discussion
("Camera Doc", 2026-11-09), plus owner instructions given during development. IDs are used by
branches and the decision report. **Must** = stated requirement; **Owner** = added by the
project owner during development; **Open** = decision still needed.

## Users and workflow
| ID | Requirement | Source |
|---|---|---|
| U1 | Reviewers have very low technical proficiency (struggle with AXIOM Portable Case). Design for the least proficient person who will touch it. | Doc |
| U2 | No live viewing. Recording runs continuously and unattended; reviewers receive a swapped hard drive (Sabrent dock) and review elsewhere. | Doc |
| U3 | Reviewer experience: connect drive → open folder → double-click an MP4 in a familiar player. No proprietary NVR software or database. | Brief, Doc |
| U4 | Operator flow: insert drive → press **Prepare Review Drive** → wait for verification → remove only after **Safe to Remove**. | Brief, Doc |
| U5 | Administrator role is separate from operators/reviewers; configuration and diagnostics hidden from them. | Brief, Doc |
| U6 | Recording is independent of any viewer/GUI; closing or misusing the UI cannot interrupt collection. | Brief, Doc |
| U7 | Plain-language error codes; unrecoverable failures say e.g. "Recording stopped. Error R-003: Camera connection lost. Automatic recovery unsuccessful. Contact technical support and provide diagnostic report." Make it obvious. | Owner |
| U8 | Must run for days unattended and recover from common errors without anyone checking. | Owner |
| U9 | Fully Windows-compatible; delivered as a single click-to-run executable (dependencies may sit in a folder beside it). Linux also needs a very simple executable option. | Owner |

## Recording
| ID | Requirement | Source |
|---|---|---|
| R1 | Continuous recording (not motion/event-only). | Brief, Doc |
| R2 | Do not reduce resolution/frame rate by default: 2592×1944 (4:3) up to 30 fps if the camera delivers it. | Doc |
| R3 | Record to internal storage; removable drives are a delivery medium, not the recording destination. | Brief, Doc |
| R4 | FFmpeg stream copy, segmenting, interruption-tolerant intermediate format, MP4 for review. | Brief, Doc |
| R5 | Audio excluded unless the authorization explicitly permits it (5MP model does not advertise audio anyway). | Brief, Doc |
| R6 | Camera facts not to be assumed: RTSP path, ONVIF support on the 5MP variant, lens FOV, stream combinations, GOP, firmware/security features, NDAA compliance. | Doc |
| R7 | Camera hardening: change default admin credentials, least-privilege account for the recorder, static/DHCP-reserved address, NTP, disable cloud/P2P/UPnP. | Doc |

## Storage and drives
| ID | Requirement | Source |
|---|---|---|
| S1 | Onboard buffer + chunked transfer to a 3.5" platter drive that may be slower or absent; drives swappable. | Owner |
| S2 | Detect unformatted / wrongly formatted drives automatically; offer a one-button tool to prepare (format) them correctly. | Owner |
| S3 | Never automatically format; destructive operations need explicit authorization. | Brief |
| S4 | Storage sizing from measured bitrate: GB/day ≈ Mbit/s × 10.8. | Doc |
| S5 | Reviewer drive layout by date/hour with timezone in names; hourly files only from contiguous footage; SHA-256 manifest. | Brief, Doc |
| S6 | Drive swap interval (daily / weekly / on demand). | **Open** |

## Evidence integrity (acceptance criteria)
| ID | Requirement | Source |
|---|---|---|
| E1 | Time accuracy: documented clock source, UTC handling, **measured clock offset**. | Doc |
| E2 | Integrity: hashes of preserved recordings and documented transfers; copies verified by reading back. | Brief, Doc |
| E3 | Continuity: detectable gaps and interruptions with logs. | Brief, Doc |
| E4 | Preservation: originals retained separately from working copies; derivatives traceable to originals. | Brief, Doc |
| E5 | Access control: restricted users, authenticated access, auditability. | Brief, Doc |
| E6 | Retention: documented duration and controls against premature deletion. | Brief, Doc |
| E7 | Validation: recorded test results for restart, playback, export and storage failures. | Brief, Doc |
| E8 | Authorization: collection scope, minimization, network approval. SWGDE 17-V-002 / 18-F-002 as SOP references. | Doc |

## Explicitly rejected approaches (from the discussion)
* Frigate/ZoneMinder/Shinobi as delivered: built around NVR databases, motion/AI features and web
  UIs (Frigate also exposes an unauthenticated internal API port) — poor fit for removable,
  self-describing evidence drives and low-skill reviewers.
* Motion-based retention as the default; reducing to 1080p/10–15 fps by default; Docker.
* Recording directly to the removable drive.
