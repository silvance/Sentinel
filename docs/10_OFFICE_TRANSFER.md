# Office transfer: moving a full archive drive to office storage

Sentinel 0.3.9. Two parts: the **office** copies the archive drive (an external SSD such as `SSD_1`)
to its larger drive and checks the copy; the **agent** then returns the SSD to service on the laptop, which
records where the recordings went and empties the SSD. No Sentinel software is needed at the office.

Never delete, rename, reformat or "clean up" anything on the SSD at the office. Sentinel does that itself,
and only after the copy has been checked. A Sentinel SSD reformatted elsewhere can no longer be used.

## At the office

1. **The SSD must have been taken out with "Swap archive drive"** on the laptop (so its index lists every
   file). If it was simply unplugged, plug it back into the laptop, wait a minute, press Swap archive drive,
   and take it out when the screen says so.
2. Plug the SSD into the office computer. It shows as `SSD_1` (or `SSD_2`, …), the name on its sticker.
3. On the office drive, make a new folder named after the SSD and today's date, for example
   `D:\Sentinel transfers\SSD_1_2026-11-26`. Use a new folder every time; never copy into an older one.
4. Open the SSD, select **everything** (Ctrl+A), copy it (Ctrl+C) and paste it into the new folder (Ctrl+V).
   Wait until Windows has finished.
5. **Check the copy.** In the new folder, click the address bar, type `powershell` and press Enter. Paste
   this into the window that opens and press Enter (it only reads):

   ```powershell
   $n=0; $bad=0; $listed=@{}; Get-ChildItem -Recurse -Filter SHA256SUMS.txt | ForEach-Object { foreach ($line in Get-Content -LiteralPath $_.FullName) { if (-not $line) { continue }; $hash, $rel = $line -split '  ', 2; $n++; $f = Join-Path (Get-Location) $rel; $listed[(Resolve-Path -LiteralPath $f -ErrorAction SilentlyContinue).Path] = 1; if (-not (Test-Path -LiteralPath $f) -or (Get-FileHash -LiteralPath $f -Algorithm SHA256).Hash -ne $hash) { $bad++; "PROBLEM: $rel" } } }; Get-ChildItem -Recurse -File -Include *.ts,*.framehash.txt | Where-Object { -not $listed[$_.FullName] } | ForEach-Object { $bad++; "NOT LISTED: $($_.FullName)" }; "$n files checked, $bad problems"
   ```

   It takes a while for a full drive (it reads every file). The last line must say **`0 problems`** and a
   number of files greater than 0. Any `PROBLEM` or `NOT LISTED` line, or no files checked: do not return
   the SSD to service; contact technical support and keep both the SSD and the copy as they are.
6. Write down on the transfer sheet: the SSD's name, the office drive's name or number, the folder, the
   date, who copied it, and the last line of the check (e.g. `1432 files checked, 0 problems`).
7. Eject the SSD (Safely Remove Hardware) and give it back to the agent **unchanged**, with the sheet.

## On the laptop (agent)

1. Plug the returned SSD in. (With the Sabrent dock: Swap archive drive first and take the drive in use
   out; only one drive at a time in that dock.)
2. Drives page › **Refresh list**. Select the SSD (it shows as "archive drive SSD_1").
3. Press **Return to service…** and fill in from the transfer sheet: where it was copied, who copied it, the
   date. Tick the box that the office checked every file and all matched. Press **Delete and return to service**.
   Windows asks for the administrator password.
4. The log under the list says, for example: "SSD_1 was returned to service: 1432 file(s) (1840.2 GB)
   recorded as moved to Office archive drive 3 and deleted from the drive." The SSD is then empty and is
   used for copying again; it keeps its name.

If it says recordings are "missing or changed", nothing was deleted: contact technical support.

## Drive names (0.3.11)

Each archive SSD has a permanent name: `SSD_1`, `SSD_2`, … Write it on a sticker on the drive. The name
stays through every office transfer and return to service. A new SSD gets the next free number when it is set
up (Drives › **Erase and set up…**).

To give a drive a different name (for example SSDs set up before 0.3.11 as `SENTINEL_3`, `SENTINEL_4`): plug it
in, Drives › select it › **Rename…**, type the name (e.g. `SSD_1`) and confirm (administrator). Nothing on
the drive is erased; its recordings, history and identity stay, and the rename is recorded. A name another
drive already has is refused.

## What Sentinel records

* An audit event `archive_drive_returned` in the tamper-evident log: the drive, the office destination,
  who copied it and when, that the check was confirmed, the administrator, and the number of files.
* Each copy on the SSD changes from VERIFIED to TRANSFERRED with the time and destination. Transferred
  recordings still count as archived (they are not copied again and raise no A-006). The local retention
  rule (if enabled) treats them as copies that have left the laptop.
* Then, and only then, the copies, the index and the playlists are deleted from the SSD.

Sentinel does not see the office copy itself: that it exists and was checked rests on step 5 and on the
person who confirms it. Keep the transfer sheets with the office drive.
