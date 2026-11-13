# Office transfer: moving a full archive drive to office storage

Sentinel 0.3.9 (0.3.12: see "Safeguards" below). Two parts: the **office** copies the archive drive (an external SSD such as `SSD_1`)
to its larger drive and checks the copy; the **agent** then returns the SSD to service on the laptop, which
records where the recordings went and empties the SSD. No Sentinel software is needed at the office.

Never delete, rename, reformat or "clean up" anything on the SSD at the office. Sentinel does that itself,
and only after the copy has been checked. A Sentinel SSD reformatted elsewhere can no longer be used.

## At the office

1. **The SSD must have been taken out with "Swap archive drive"** on the laptop (so its index lists every
   file). If it was simply unplugged, first take it back to the laptop:
   * **Only this SSD in the laptop** (one-bay dock such as the Sabrent, or one SSD at a time): if another SSD
     is in use, press Swap archive drive and take that one out when the screen says so. Plug this SSD in,
     wait until the Drives page shows it **In use** (or **Full**), press Swap archive drive, and take it out
     when the screen says so. Put the other SSD back; if the Drives page says it is back from the office,
     press **Cancel swap** (it was only taken out to make room).
   * **Two SSDs connected at the same time** (two separate USB drives): plug this SSD in next to the other
     one. Swap archive drive prepares the SSD that is *in use*, which is not this one, so: press Swap archive
     drive once and **take nothing out**; wait until the Drives page shows this SSD **In use** (or **Full**);
     press **Cancel swap**; then press Swap archive drive again. Take out only the SSD the screen names, and
     only when it names this SSD (for example "SSD_1 is ready to be taken out"). Never take out an SSD the
     screen has not named.
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
2. Open the Drives page; the SSD appears by itself. Select it (it shows as "archive drive", named SSD_1).
3. Press **Return to service…** and fill in from the transfer sheet: where it was copied, who copied it, the
   date. Tick the box that the office checked every file and all matched. Press **Delete and return to service**.
   Windows asks for the administrator password.
4. The log under the list says, for example: "SSD_1 was returned to service: 1432 file(s) (1840.2 GB)
   recorded as moved to Office archive drive 3 and deleted from the drive." The SSD is then empty and is
   used for copying again; it keeps its name.

If it says recordings are "missing or changed", nothing was deleted: contact technical support.

If it says some files "could not be deleted now", close any video from the SSD that is open (a player,
Explorer's preview). The SSD is still returned to service; Sentinel deletes those files by itself when it next
uses the SSD, and again before it prepares the SSD for its next swap, so the next office check is not refused.

If the laptop lost power or Sentinel was stopped during a return, the return is finished by itself when the
SSD is plugged in again (pressing Return to service again also finishes it). Only files already recorded as
moved to the office are deleted.

## Safeguards (0.3.12, review 2026-11-12)

* A drive taken out with **Swap archive drive** is not copied to again when it is plugged back in. The
  Drives page says it is back from the office and waits to be returned to service; copying goes on to
  another drive, or the recordings wait on the laptop. (To keep using the drive without an office copy,
  press **Cancel swap** while it is plugged in.) So the office copy always holds everything Return to service
  deletes. With a one-bay dock, the drive taken out only to make room for the returned one (step 1 above) is
  used again as usual once the returned drive is back in service.
* **Cancel swap** releases only the drive whose swap it cancels and drives plugged in at that moment. A drive
  that is at the office stays "waiting to be returned" (review 2026-11-12: Cancel swap used to release every
  such drive).
* Return to service can be repeated safely: it finishes a return that was interrupted (power cut, Sentinel
  stopped) and deletes files a return could not delete. It only ever deletes copies recorded as moved to the
  office; a recording copied to the drive since then is never deleted.
* A Swap pressed while the inserted drive cannot be used ("The inserted drive cannot be used as an archive
  drive") prepares nothing, and is not kept for the next drive plugged in.
* Return to service refuses, and deletes nothing, when the selected drive is the one Sentinel is copying to
  now, was not taken out with Swap since it was set up or last returned, was prepared for removal but never
  taken out, or had recordings copied to it after its swap. It also refuses when the drive at that letter is not the one selected (select it again).
* With two drives, pressing Swap again while one drive is waiting to be taken out prepares nothing else:
  take that drive out first.

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
