# Bench test, soak and dry run (release candidate)

Purpose: show, on the hardware and camera that will be deployed, that Sentinel records, recovers
and reports as designed before any laptop is placed. Automated tests use a simulated camera and
cannot vouch for the real camera, the laptop's power behaviour, real drives or real operators.

Print this page, tick each box, write results in the right-hand column, and file it with the
deployment record. Any **FAIL** stops deployment until it is understood. This is an engineering
acceptance test; it does not make the system forensically validated.

Conventions: technician tasks are done in the Sentinel window after **Unlock settings…** (Windows
administrator prompt). Where a command is given, run it in a Command Prompt **opened with "Run as
administrator"** in `C:\Program Files\Sentinel`. `CFG` means `C:\ProgramData\Sentinel\config\sentinel.toml`.

## 0. Record what is being tested

| Item | Value |
|---|---|
| Date / tester | |
| Laptop make, model, serial | |
| Windows edition and build (`winver`) | |
| Camera make, model, firmware | |
| Camera stream path and codec (H.264 or H.265) | |
| Sentinel build: `Sentinel.exe about` → version, git commit | |
| Archive drives (make, size, serial) | |
| Review drive (make, size, serial) | |

## 1. Install (stage 2)

| # | Step | Expected | Result |
|---|---|---|---|
| 1.1 | Copy the whole `Sentinel` folder to `C:\Program Files\Sentinel`. Run `Sentinel.exe about --verify`. | "installed files match the build manifest"; FFmpeg licence "OK: LGPL-3.0-or-later …". | |
| 1.2 | Check `licences\` exists with `SOURCE-OFFER.txt`, `LGPL-3.0.txt`, `GPL-3.0.txt`, `THIRD-PARTY-NOTICES.txt` and the `third-party\` folder. | Present; `about` shows no "Licence notices incomplete" warning (otherwise the folder must not leave the organisation, docs/08 §8.7). | |
| 1.3 | Double-click `Sentinel.exe`. The window opens on "Set up this computer"; press **Continue as administrator…** and approve the Windows prompt. | Window reopens with the setup form editable. | |
| 1.4 | Fill in the camera (IP, stream path from the camera documentation, view-only account) and press **Test connection**; or tick **Add the camera later** (camera-later path). Choose the time zone (and, optionally, the organisation using the recorder; leave it empty to name none); press **Set up this computer**. | "The camera answered: h264 video, …" or "hevc video, …" (if tested); "This computer is set up"; the full window (Status … System) appears. | |
| 1.5 | System › Service: enter the time server if one exists, press **Install / repair service…**, read the plan in the output, confirm. | Every step OK; both services (recorder, archive) started. | |
| 1.6 | `net localgroup "Sentinel Operators" <operator account> /add` for each operator account (Command Prompt as administrator). | Command completed successfully. | |
| 1.7 | Sign in as an operator and open Sentinel. | Window title ends "[read-only]"; settings fields greyed; **Unlock settings…** shown. | |
| 1.8 | If the camera was added later: Settings › Camera, fill in the camera, **Test connection**, **Save and restart recorder**. | "The camera answered: …"; "Saved. The recorder was restarted with the new settings." | |
| 1.9 | System › Service › **Check service** (or `SentinelService.exe status --config CFG`). | Service RUNNING; folders OK; credentials "DPAPI-protected; permissions: OK"; time synchronisation "yes" (or noted why not). | |

## 2. The operator screen

| # | Step | Expected | Result |
|---|---|---|---|
| 2.1 | Sign in as an **operator** (standard user) and double-click `Sentinel.exe`. | The Sentinel screen opens maximised; the black console window disappears. "Recording normally" within about a minute. | |
| 2.2 | Before step 1.8 (camera-later only): Status headline. | "Not recording: camera not added yet" (amber), P-002; no alarm. | |
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

Prerequisite for 5.2–5.6: erasing drives is **off by default** (`[drives] erase_enabled = false`,
owner decision) and is switched on only by the supervised dock test in
docs/branches/design-drive-prepare.md §9, which an administrator runs on scratch drives first. If
that test has not been passed on this laptop and dock, do it now (it ends with `erase_enabled`
left on, or set back to `false` on any failure), or mark 5.2–5.6 "not run: erase disabled" — that
is not a FAIL, but the laptop cannot be deployed with archive drives until it is done. Never switch
`erase_enabled` on just for this checklist.

**Known issue in builds before 2026-11-10 (commit 973f3b8 and earlier): docks that hide the serial.**
The Sabrent EC-HD2B dual-bay dock (and other "toaster" docks with a cheap USB-SATA bridge) reports
`000000000000000000` as the serial of every disk, so Sentinel never sees the disk's real serial.
Those builds still ask for "the serial's last 4 characters", and the only answer they accept is
`0000`. That confirms nothing about which disk is in the dock. With such a build, keep exactly
one drive in the dock and check the model and size on screen before typing `0000`. Later builds
show "hidden by the dock" in the Serial column, ask for `ERASE` instead, and refuse to erase while
another connected drive that could be erased also hides its serial and has the same model and size
(or is an identical empty disk); a set-up archive drive next to it is fine. Also in those builds:
after ticking "Allow 'Erase and set up'"
in Settings › Storage, press **Refresh** on the Drives page before erasing, or the page still
answers "erase_disabled".

**Sabrent EC-HD2B: one archive drive at a time.** Taking one disk out of this dual-bay dock
disconnects both bays until a disk is inserted again (field result 2026-11-11, step 5.5a). Keep only
the archive drive in use in the dock; keep the next one on the shelf. To swap: **Swap and safely remove**,
wait for the page to say what to do; if it shows **Safely remove SSD_…**, press it (with one disk in the
dock it does no harm), take the drive out, insert the next one. Erasing and setting up a new drive is
done the same way: take the archive drive out first, or set new drives up on another day.

**Recommended instead: separate external USB drives (2026-11-11).** Each external drive has its own
USB-SATA bridge, so taking one out affects only that drive, Safely Remove Hardware ejects only that
drive, and Windows notices the removal at once. Most report their real serial number, so Sentinel can
tell them apart by hardware and the erase confirmation uses the serial again. External SSDs suit a
laptop that is moved (no moving parts); 2.5" portable hard drives are cheaper per TB. Not USB flash
sticks. Next trial: two 2 TB external SSDs. Before relying on them:

| # | Step | Expected | Result |
|---|---|---|---|
| U.1 | Plug in one SSD and open Drives (the list updates by itself). | Shown as USB with a **real serial** in the Serial column (not zeros, not "hidden by the dock"). If it shows "hidden by the dock", it is used like the dock (one at a time). If the drive already holds files it is "contains other files": erasing needs an administrator and the serial's last characters; first make sure nothing on it is needed. A drive locked with BitLocker To Go or vendor encryption is not usable until that is removed. | |
| U.2 | **Erase and set up…** that SSD; then the second one. | Each becomes SSD_n (from 0.3.11; SENTINEL_n before), set up, ready. Write the name on a sticker on the drive. | |
| U.3 | With one SSD as archive drive: steps 5.4 to 5.6 (wait for the first full hour, folders, Swap with Safely Remove, playlists). | As in 5.4 to 5.6. | |
| U.4 | Both SSDs plugged in (two USB ports): step 5.5a, then take out only the full one (Swap first). | The other SSD stays connected and copying carries on; no "removed without preparing". Only then use two drives at once. | |

**Return to service (0.3.9).** Rehearse the office transfer (docs/10_OFFICE_TRANSFER.md) with an SSD that holds a few hours of copies:

| # | Step | Expected | Result |
|---|---|---|---|
| R.1 | Swap the SSD out; on another PC copy everything to a new folder and run the check in docs/10 step 5. | Last line `N files checked, 0 problems`, N > 0. | |
| R.2 | Rename one copied `.ts` in that folder and run the check again; rename it back. | A `PROBLEM` and a `NOT LISTED` line; then 0 problems again. | |
| R.3 | Plug the SSD into the laptop; Drives › select it › **Return to service…**; fill in, tick, confirm. | Log: "SSD_n was returned to service: N file(s) … deleted from the drive." The SSD holds only `SENTINEL_DRIVE.json`, `Sentinel/README_ARCHIVE.txt` and an empty index. No A-006; "Not yet archived" does not jump up. | |
| R.4 | Leave it plugged in for an hour of recording. | New recordings are copied to it as usual. | |

| # | Step | Expected | Result |
|---|---|---|---|
| 5.1 | Drives page (tick **Show all drives**); identify the archive drive **by serial number** (if the dock hides it: by model and size, with only that drive in the dock). | Only the external drive is offered; internal drives UNSUITABLE. | |
| 5.2 | Select it › **Erase and set up…**, enter your name and the serial's last characters (or `ERASE` when the list says the dock hides the serial). | With the dock test passed (`erase_enabled = true`): drive prepared; audit entry. Otherwise: refused with M-010 "Nothing was erased" — record it and skip to section 6. | |
| 5.3 | Select it › **Use as archive drive…** (needs the drive prepared in 5.2). | Designated. | |
| 5.4 | Wait until the first recording file is finished (files end on the hour; with 15- or 30-minute files at the next quarter or half hour), then Drives › **Archive details**. | Copying, then up to date; copies verified. On the drive: `Sentinel\<camera>\<date>\` holds only `.ts` files, `Fingerprints\` their `.framehash.txt`, `Playlists\` one `.m3u` per day. | |
| 5.5 | Swap as an agent would (screen / `archive swap`), insert drive 2. Then `Sentinel.exe ledger head` (docs/07 checklist). | The page says the drive is ready to be taken out; drive 2 starts copying; chain head written on the custody form. | |
| 5.5a | Two drives (0.3.5): put an archive drive in each bay of the dock. Fill the first one or use a small one, or watch it the next time it fills. | When it is full: Drives page shows "Full (swap it)" for it and "In use" for the other; copying continues without A-003. Press **Swap and safely remove**: the page says the full drive is ready to be taken out. Take out **only that disk** (do not use Safely Remove Hardware). Record whether the other drive stays connected: if Archive details then shows "removed without preparing" for the drive in use, the dock resets both bays when one disk is taken out; note it, copying must still carry on by itself within a minute. | **Sabrent EC-HD2B, 2026-11-11, 0.3.7: both bays drop.** The removed disk stayed listed for about a minute, then neither disk was listed; the other disk came back only when a disk was inserted. Decision: one archive drive at a time in this dock (see the note above this table); skip 5.5a with this dock. |
| 5.6 | On another PC, open recordings from drive 1, and open a day's playlist (`Sentinel\<camera>\Playlists\<date>.m3u`) in VLC and in Windows Media Player. | Files play; the playlist plays that local day back to back; the drive's manifest verifies. | |
| 5.6a | Back on the recorder, as administrator: System › Check and history › **Check the log against a drive…** › drive 1 (or `Sentinel.exe ledger verify --anchors-from <drive>:\`). | "N saved fingerprint(s) found"; intact (exit 0). | |
| 5.7 | Retention (optional, long): run with a small recording disk until it is low. | Only recordings verified on a **removed** drive and older than 24 h are deleted; ledger records each deletion. | |

## 6. Review export and verification

| # | Step | Expected | Result |
|---|---|---|---|
| 6.1 | Insert the review drive; Export page › pick the drive, enter your name › **Export new recordings**. | Export completes and verifies. | |
| 6.2 | On a **different** computer, check the drive's hashes against its manifest. | All match. | |
| 6.3 | Pick 3 moments; compare the time shown in the video with a known clock photographed in view of the camera. | Within ±1 s (record the actual difference). | |
| 6.4 | File › **Save diagnostic report…** (or `Sentinel.exe diagnostics`). | Report written; recording-log chain check OK. | |

## 7. Unattended soak (stage 3)

5–7 days untouched, placed like the real site (closet, heat, locked screen), including at least
one archive-drive swap.

| # | Check at the end | Expected | Result |
|---|---|---|---|
| 7.1 | Status | "Recording normally"; no unexplained alerts in the history. | |
| 7.2 | Gaps | Every gap has a reason you can explain (drills, swaps, planned pauses). | |
| 7.3 | Recording-log chain: `Sentinel.exe ledger verify --expect-head <head from the custody form>`, then `Sentinel.exe ledger head` | Exit status 0, "intact"; the new head written on the custody form. | |
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
