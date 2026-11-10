# Bench test, soak and dry run (release candidate)

Purpose: show, on the hardware and camera that will be deployed, that Sentinel records, recovers
and reports as designed before any laptop is placed. Automated tests use a simulated camera and
cannot vouch for the real camera, the laptop's power behaviour, real drives or real operators.

Print this page, tick each box, write results in the right-hand column, and file it with the
deployment record. Any **FAIL** stops deployment until it is understood. This is an engineering
acceptance test; it does not make the system forensically validated.

Conventions: run technician commands in a Command Prompt **opened with "Run as administrator"** in
`C:\Program Files\Sentinel`. `CFG` means `C:\ProgramData\Sentinel\config\sentinel.toml`.

## 0. Record what is being tested

| Item | Value |
|---|---|
| Date / tester | |
| Laptop make, model, serial | |
| Windows edition and build (`winver`) | |
| Camera make, model, firmware | |
| Camera stream path and codec (H.264 expected) | |
| Sentinel build: `Sentinel.exe about` → version, git commit | |
| Archive drives (make, size, serial) | |
| Review drive (make, size, serial) | |

## 1. Install (stage 2)

| # | Step | Expected | Result |
|---|---|---|---|
| 1.1 | Copy the whole `Sentinel` folder to `C:\Program Files\Sentinel`. Run `Sentinel.exe about --verify`. | "installed files match the build manifest"; FFmpeg licence "OK: LGPL-3.0-or-later …". | |
| 1.2 | Check `licences\` exists with `SOURCE-OFFER.txt`, `LGPL-3.0.txt`, `GPL-3.0.txt`. | Present. | |
| 1.3 | **Camera-later path** (if testing advance setup): `Sentinel.exe setup --camera-later`, answer the questions. Then `Sentinel.exe check-config --config CFG`. | "NOTE: no camera added yet"; "configuration OK". | |
| 1.4 | Otherwise: double-click `Sentinel.exe`, type `SETUP`, answer the questions (camera IP, stream path from the camera's documentation, view-only camera account). | "Configuration written". | |
| 1.5 | `SentinelService.exe install --config CFG --dry-run`, read the plan, then run it without `--dry-run` (add `--time-server <host>` if a local time server exists). | Every step OK; both services (recorder, archive) started. | |
| 1.6 | `net localgroup "Sentinel Operators" <operator account> /add` for each operator account. | Command completed successfully. | |
| 1.7 | `Sentinel.exe admin-pin` and set the screen's administrator PIN. | PIN saved. | |
| 1.8 | If 1.3 was used: `Sentinel.exe set-camera` and answer the camera questions. | "The camera answered: h264 video, …"; "The recorder is running with the camera". | |
| 1.9 | `SentinelService.exe status --config CFG`. | Service RUNNING; folders OK; credentials "DPAPI-protected; permissions: OK"; time synchronisation "yes" (or noted why not). | |

## 2. The operator screen

| # | Step | Expected | Result |
|---|---|---|---|
| 2.1 | Sign in as an **operator** (standard user) and double-click `Sentinel.exe`. | The Sentinel screen opens maximised; the black console window disappears. "Recording normally" within about a minute. | |
| 2.2 | Before step 1.8 (camera-later only): screen headline. | "Not recording: camera not added yet" (amber), P-002; no alarm. | |
| 2.3 | Switch dark/light appearance. | Readable in both. | |
| 2.4 | Close the screen; wait 2 minutes; open it again. | Recording continued (no gap in the timeline). | |
| 2.5 | Press **Lock this computer**. Wait 10 minutes. Sign back in. | Windows lock screen appeared; status still "Recording normally"; no gap in those 10 minutes. | |

## 3. Failure drills (each must recover by itself)

Note the clock time of each action; afterwards check the timeline/ledger shows a gap of the right
length and reason, and nothing else.

| # | Action | Expected while failed | Expected after | Result |
|---|---|---|---|---|
| 3.1 | Unplug the camera's network cable for 5 minutes. | "Recording interrupted", R-002/R-003, escalating to "Automatic recovery unsuccessful" after ~3 minutes. | Recording resumes within ~1 minute of reconnecting; one gap of ~5 minutes. | |
| 3.2 | Power the camera off and on. | As 3.1. | As 3.1. | |
| 3.3 | Unplug the laptop's power for 10 minutes (battery). | Recording continues. | No gap. | |
| 3.4 | Restart Windows while recording; **do not sign in**. | — | After boot, recording resumes with nobody signed in (check later: gap ≈ restart time, reason recorded). | |
| 3.5 | In an admin prompt: `taskkill /F /IM SentinelService.exe`. | — | Service restarts within ~5 s; the screen may briefly show X-001 "not responding"; short gap recorded. | |
| 3.6 | Close the lid (on mains power) for 10 minutes. | Recording continues (install set "lid: do nothing"). | No gap. | |
| 3.7 | Change the power plan so the laptop sleeps after 15 minutes, wait for the next check (≤10 min). | H-001 "This computer is set to go to sleep…". | Restore the setting; H-001 clears. | |
| 3.8 | Fill the recording disk to below the warning level with a large dummy file (then delete it). | S-001 at warning; S-002 "Recording stopped" at critical. | Recording resumes after deleting the file. | |

## 4. Recording schedule (only if schedules will be used)

| # | Step | Expected | Result |
|---|---|---|---|
| 4.1 | Add a `[schedule]` date period pausing for 10 minutes starting in 5 minutes; restart the service; `Sentinel.exe schedule --days 2`. | The plan lists the pause. | |
| 4.2 | During the pause. | "Paused by schedule", P-001 naming the resume time; no R-alerts. | |
| 4.3 | After the pause. | Resumes on its own; one gap, reason "paused by the recording schedule (planned)". | |
| 4.4 | Remove the test period; restart the service. | — | |

## 4a. Motion-triggered recording (only if the schedule uses "motion")

| # | Step | Expected | Result |
|---|---|---|---|
| 4a.1 | `Sentinel.exe motion-test --seconds 120` in daylight: stand still out of view, then walk through. | Empty scene never MOTION; walking through always MOTION. Settings noted. | |
| 4a.2 | Same at night / lights off (IR). | As 4a.1; note the IR switch (it may trigger once). | |
| 4a.3 | Set a 30-minute `[[schedule.dates]]` motion period starting now; restart the service; stay out of view 5 min. | "Watching for motion", P-003; nothing recorded. | |
| 4a.4 | Walk through once, note the clock time; leave. | Recording starts; check the first frame: how many seconds after you entered? Record it. | |
| 4a.5 | Walk through again before `keep_recording_s` ends. | Same recording continues; stops `keep_recording_s` after your last movement. | |
| 4a.6 | Unplug the camera for 1 minute during the motion period. | W-001 and/or R-alerts; after reconnecting, back to watching. | |

## 5. Archive drive and retention

| # | Step | Expected | Result |
|---|---|---|---|
| 5.1 | `Sentinel.exe drives list --config CFG`; identify the archive drive **by serial number**. | Only the external drive is offered; internal drives UNSUITABLE. | |
| 5.2 | `Sentinel.exe drives prepare --config CFG --disk <n> --who <name> --admin` and confirm with the serial's last characters. | Drive prepared; audit entry. | |
| 5.3 | `Sentinel.exe archive designate --config CFG --drive <letter>:\ --admin-id <name>`. | Designated. | |
| 5.4 | Wait 30 minutes; `Sentinel.exe archive status --config CFG`. | Copying, then up to date; copies verified. | |
| 5.5 | Swap as an agent would (screen / `archive swap`), insert drive 2. | "SAFE TO REMOVE" before removal; drive 2 starts copying. | |
| 5.6 | On another PC, open recordings from drive 1. | Files play; the drive's manifest verifies. | |
| 5.7 | Retention (optional, long): run with a small recording disk until it is low. | Only recordings verified on a **removed** drive and older than 24 h are deleted; ledger records each deletion. | |

## 6. Review export and verification

| # | Step | Expected | Result |
|---|---|---|---|
| 6.1 | Insert the review drive; `Sentinel.exe export run --config CFG --target <letter>:\ --operator-id <name>`. | Export completes and verifies. | |
| 6.2 | On a **different** computer, check the drive's hashes against its manifest. | All match. | |
| 6.3 | Pick 3 moments; compare the time shown in the video with a known clock photographed in view of the camera. | Within ±1 s (record the actual difference). | |
| 6.4 | `Sentinel.exe diagnostics`. | Report written; recording-log chain check OK. | |

## 7. Unattended soak (stage 3)

5–7 days untouched, placed like the real site (closet, heat, locked screen), including at least
one archive-drive swap.

| # | Check at the end | Expected | Result |
|---|---|---|---|
| 7.1 | Status | "Recording normally"; no unexplained alerts in the history. | |
| 7.2 | Gaps | Every gap has a reason you can explain (drills, swaps, planned pauses). | |
| 7.3 | Recording-log chain (`Sentinel.exe diagnostics`) | Chain check OK; head written on the custody form. | |
| 7.4 | Disk space and temperature | Within limits; no S- or H-alerts. | |
| 7.5 | Time drift (6.3 again) | Within ±1 s. | |

## 8. Operator dry run

Give a probationary agent only `READ-ME-FIRST.txt` and watch, without helping:

| # | Task | Notes (where they hesitated, what they misread) |
|---|---|---|
| 8.1 | Tell whether it is recording. | |
| 8.2 | Swap the archive drive. | |
| 8.3 | Lock the computer and leave it. | |
| 8.4 | Report an error code (stage one with 3.1). | |

Every hesitation is a change request for the screen or the read-me.

## 9. Sign-off

| | Name | Date | Signature |
|---|---|---|---|
| Tester | | | |
| Technical lead | | | |
| Evidence/legal review of hashing and export (docs/07) | | | |
