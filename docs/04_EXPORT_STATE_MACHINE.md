# Removable Storage: Archive Drive and Review Drives (design — implemented in Phase 3)

Status: design only. Nothing in this document is implemented yet.

> **Implementation note (branch `design/archive-drive`):** §1–3 (archive drive) are implemented in
> `src/sentinel/archive/`; see `docs/branches/design-archive-drive.md`. Differences from the text
> below: archive file names use UTC (`<HHMMSS>Z_<HHMMSS>Z_<seg8>.ts`); the index is rewritten on
> attach, after each hour of copied footage (or an hour of wall time with new copies) and on swap,
> and between rewrites an append-only `Sentinel/<camera>/INDEX/verified_journal.csv` (the index is per camera) documents each verified
> file; at the default 40 MB/s throttle a 24 h backlog of 12 Mbit/s footage (≈130 GB) needs ≈1.4 h
> on this development host (measured to an internal SSD — **no USB platter drive was tested**);
> eject goes through the `SafeRemover` interface (only the manual-removal fallback ships). §6 is
> implemented by `req/review-drive-export`.

## 1. Storage tiers

```
 camera ──► FFmpeg ──► internal SSD "spool"  ──(background archive copy)──►  ARCHIVE drive (3.5" HDD in dock)
                        always the landing zone                              continuous, chunked, verified
                        sized as a buffer, not the archive                   swappable
                              │
                              └──(operator "Prepare Review Drive")────────►  REVIEW drive (any approved HDD)
                                                                              MP4 + originals + manifests, then eject
```

* **Recording never writes to removable media.** The SSD absorbs every outage, slow spin-up,
  swap, or failure of the HDD.
* **Throughput check (to verify on hardware):** 5MP H.264 at 4–12 Mbit/s is 0.5–1.5 MB/s
  (≈ 43–130 GB/day). A 3.5" HDD on USB 3 sustains ≥ 100 MB/s; even USB 2 (~30 MB/s) is ≥ 20×
  the recording rate. The archive copy runs throttled at low I/O priority so it can never starve
  recording; a 24 h backlog drains in well under an hour once a drive is present.
* **SSD buffer sizing:** buffer days = (free SSD bytes − critical threshold) ÷ measured daily
  volume. The status shows both "time until storage requires attention" and "not yet archived: N h".

## 2. Archive copy (continuous)

Per sealed segment, oldest first:
1. Write to `<drive>/Sentinel/<camera>/<YYYY-MM-DD>/<start>_<end>_<seg-id8>.ts.partial` in
   8 MiB chunks; `fsync` every 64 MiB and at the end.
2. Rename to final name; `fsync` the directory.
3. **Read-back verification that cannot be satisfied from cache:** `posix_fadvise(DONTNEED)`
   on the file after `fsync`, then re-read with `O_DIRECT` (fallback: re-read after the page cache
   is dropped for that file). SHA-256 must equal the ledger's sealed hash.
4. Copy the per-frame hash sidecar the same way, if present.
5. Ledger: `archive_copies(segment_id, drive_uuid, path, sha256, verified_utc)`.

The archive drive also carries a rolling `Sentinel/INDEX/` (JSON + CSV + `SHA256SUMS.txt`) and a
gap list, regenerated after each hour of copies, so a drive pulled at any moment is
self-describing up to its last verified segment.

**Local deletion** is never performed by the copy process. The (admin-enabled) retention policy
may delete a local segment only when it is sealed, older than `retention_days`, and verified on
≥ `min_verified_copies` drives, at least one of them not currently inserted (configurable).

## 3. Swapping the archive drive (operator)

Operator presses **Swap Archive Drive** →
1. copy pauses at the next segment boundary (recording continues on the SSD);
2. in-flight file is finished and verified (or its `.partial` is discarded — never claimed);
3. index/manifests written and read back; `sync`; UDisks2 Unmount → PowerOff;
4. screen says which drive to take out and how, plus "Recording continues. Not yet archived: 0 h". The
   deployed Windows copier has no software eject (review 2026-11-12), so it never shows **SAFE TO REMOVE**:
   with one archive drive it says to use Windows' "Safely Remove Hardware" first; with two it names the drive
   that "is ready to be taken out". (SAFE TO REMOVE appears only where a software eject is wired in.)

Inserting the next drive → identification (§5) → copy resumes from the oldest segment not yet
verified on *that* drive, or (policy) not yet verified on *any* archive drive.
Unexpected removal → copy stops, `.partial` files are ignored on the next insert, ledger keeps
only verified copies; operator sees "Archive drive removed without preparing. No recordings were
lost; copying will continue when a drive is inserted."

**Two drives in a two-bay dock (0.3.5):** when the drive in use fills up, its index is written and
copying moves on to the other archive drive by itself. Swap then prepares the *full* drive while
copying continues on the other one; the screen names the drive to take out and says not to use
Windows' "Safely Remove Hardware", which would stop both bays (docs/branches/design-archive-drive.md,
"Two drives").

**Not with the Sabrent EC-HD2B (field result 2026-11-11):** taking one disk out of that dock disconnects
both bays until a disk is inserted again, so the drive left in it is cut off mid-copy and reported as
"removed without preparing". Keep **one archive drive at a time** in that dock and swap it the
single-drive way (Swap, Safely Remove Hardware, take it out, insert the next). The two-drive mode is
only for docks with a separate USB connection per bay, or two single-bay enclosures.
Recommended archive drives: separate external USB drives (SSDs for a laptop that is moved), each with its
own USB connection (docs/09, "Recommended instead"; bench steps U.1-U.4).

**Return to service (0.3.9, owner request 2026-11-12).** A full archive drive goes to the office, which
copies it to its own storage and checks every file against `SHA256SUMS.txt` (docs/10_OFFICE_TRANSFER.md).
Back on the laptop, an administrator (the agent) selects it and presses **Return to service…**, naming the
office destination, who copied it and when, and confirming the check. The archive copier then, between two
files: checks every recorded copy is still on the drive (else nothing happens), records the copies as
TRANSFERRED (audit event `archive_drive_returned`, hash chain), and only then deletes them, the index and the
playlists from the drive. The drive keeps its marker and label and is used again. Transferred recordings
count as archived (not copied again, no A-006) and, for local retention, as copies that left the laptop.
Erasing a drive that held recordings stays refused; this is the way to reuse one.
Review 2026-11-12: the return is idempotent. A return interrupted after the transfer was recorded is finished
when the drive is next seen (or by pressing Return to service again); TRANSFERRED files a return could not
delete (file open, antivirus) are deleted on the next attach and before each later index write, so the next
office check does not report them NOT LISTED. Only TRANSFERRED copies with their recorded size are deleted;
a VERIFIED copy never is. Cancel swap releases only the drive whose swap it cancels and drives inserted at
that moment; drives at the office stay waiting for their return. A swap pressed while the inserted drive is
refused, or whose drive is removed without preparing, is dropped instead of preparing the next drive attached.
One scan that misses the drive in use is not a removal even when another archive drive is inserted.

**Swap while archive copying is stopped (0.3.10, field report 2026-11-12).** With the copier not running
(A-007), Swap used to wait its full 10 minutes in silence while every other Drives-page button answered "Wait
until the current command has finished". Swap now answers at once that nothing is being copied and that the
drive can be taken out with Safely remove…, and writes no swap request for a copier that starts later; it also
stops waiting if the copier stops during a swap. The "still running" message names the command, and a
command that only waits (swap) can be stopped there.

**Drive names (0.3.11, owner decision 2026-11-12).** Drives are named `SSD_<n>` (`[drives] label_prefix`,
`SENTINEL_` before), the permanent name on the drive's sticker; return to service keeps it. The number is one
more than any used before in the set-up and rename records or on a connected drive. An administrator can rename
a Sentinel drive in place (Drives › Rename…, `sentinel.drives rename`): the volume label and the marker's
`label` change (old names kept in `previous_labels`), an audit event `drive_renamed` records old and new name;
identity, recordings and records are unchanged. A name another drive holds (by the records, or on a connected
volume) is refused. The archive index on the drive shows the new name from its next rewrite.

## 4. Automatic drive detection and one-button preparation

On insertion the drive is **classified automatically** (UDisks2/udev: partition table, filesystem
type and label, mount state, size, SMART, whether it holds the OS or spool):

| Classification | Example | Operator sees | Button |
|---|---|---|---|
| READY | Sentinel-prepared, approved filesystem, has `SENTINEL_DRIVE.json` | "Archive drive ready" (copy resumes) | — |
| BLANK | no partition table, or empty partition, no files | "This drive is empty and needs to be prepared." | **Prepare This Drive** |
| WRONG_FORMAT | e.g. exFAT / FAT32 / ext4 / HFS+ when the approved format is NTFS (or vice versa), **with no files on it** | "This drive uses the wrong format (exFAT). It must be prepared before use." | **Prepare This Drive** |
| FOREIGN_DATA | any filesystem containing files, not a Sentinel drive | "This drive contains other files (412 GB). Preparing it will erase them." | **Prepare This Drive** (stricter confirmation, below) |
| UNSUITABLE | too small, read-only, failing SMART, the system disk | Specific reason | none — the system disk is never offered |

**Prepare This Drive** (one press, then a confirmation screen — never automatic):
* Confirmation shows the drive in plain words: model, size, serial, and exactly what will be
  erased ("everything on this 4 TB drive"). The system disk and the spool disk are excluded by
  device identity, not by name, and can never reach this screen.
* BLANK / WRONG_FORMAT (nothing to lose): a single **Erase and Prepare** confirmation.
* FOREIGN_DATA: requires **administrator authorization** (default; see open decision below) and
  typing the last 4 characters of the drive serial shown on screen.
* Docks that hide the serial (field report 2026-11-10: the Sabrent EC-HD2B reports
  `000000000000000000` for every disk). A placeholder serial (one character repeated, a counting
  run, or a filler word) counts as hidden: the list shows "hidden by the dock", the operator types
  `ERASE` instead of the serial's last characters (typing `0000` would prove nothing), and erasing
  is refused while another connected disk cannot be told apart from it: same serial, model and
  size *and* the same partition and volume IDs, i.e. two empty identical disks (`lookalike`). A new
  disk next to the current archive drive is fine (field report 2026-11-11).
* In such a dock the serial and UniqueId identify the dock or bay, not the disk, so they are not
  used to recognise drives Sentinel has used before (field report 2026-11-11: a brand-new disk was
  taken for SENTINEL_1 and could not be set up). Such drives are recognised by their contents only
  (marker, volume GUIDs, filesystem serials); consequence: a Sentinel drive reformatted outside
  Sentinel is not recognised in such a dock.
* USB bridges can also fill in the disk's UniqueId (field report 2026-11-12: a brand-new, empty Samsung
  T7 with a real serial reported `5000000000000001`, as the disks in the Sabrent dock had, and was refused
  as "holds Sentinel recordings", reason `known_hardware`). Since 0.3.8 a placeholder UniqueId (one
  character repeated, or an NAA type 5/6 ID without a company ID) is never used to recognise a drive, and
  the ledger no longer keeps UniqueIds recorded behind a placeholder serial. A drive with a real serial
  is still recognised by its serial, model and size. The refusal now says why a drive counts as holding
  recordings.
* After erasing, the drive is found again by its hardware identity **and** the GUID of the partition
  the elevated helper has just created (0.3.6). Before, two disks in one such dock matched the same
  hardware identity, so setup erased and formatted the right disk but then stopped with "Two
  connected drives look identical" (M-005) without writing its marker (field report 2026-11-11).
  Such a half-set-up disk shows as blank; Erase and Set Up again fixes it, and the label number it
  was given is reused because it was never recorded as prepared. The helper also checks that the disk
  is empty (RAW) after clearing before it initialises it.
* Action: GPT + one partition, approved filesystem (provisionally NTFS — risk C1), label
  `SSD_<n>` (`SENTINEL_<n>` before 0.3.11), then `SENTINEL_DRIVE.json` (`drive_uuid`, label, prepared_utc, operator/admin id),
  a write/read-back test file, and an `audit` event recording drive identity and who pressed it.
* Optional "thorough check": full surface read test with progress (hours for 4 TB).

**Open decision for the project owner:** who may press *Prepare This Drive* for a FOREIGN_DATA
drive — (a) administrator only (recommended: protects against wiping a drive that holds other
evidence), (b) operator with typed-serial confirmation, or (c) never (always handled off-site).

## 5. Drive identity

Recorded in `drives`: `ID_WWN` or disk serial via udev (note: some USB bridges report their own
serial — risk C2), model, size, partition and filesystem UUIDs, plus `drive_uuid` from
`SENTINEL_DRIVE.json`. Device nodes (`/dev/sdX`) are diagnostics only.

## 6. Review-drive export batches

```
 IDLE ─ inserted ─► CHECKING ─┬─ unsuitable ─► REJECTED ("This drive cannot be used: <reason>")
                              └─ ok ─► READY ("Press Prepare Review Drive")
 READY ─ operator ─► PLANNING  (eligible segments + gaps for the interval; batch rows PLANNED)
 PLANNING ─► COPYING     originals + MP4 derivatives (remux -c copy +faststart) via .partial/rename
 COPYING  ─► VERIFYING   read-back SHA-256 (cache-bypassing) of every file; MP4 content hash must
                         equal the original's content hash (decoded-frame SHA-256)
 VERIFYING ─► MANIFEST   Export_Manifest.json, SHA256SUMS.txt, Export_Summary.html (+ read-back)
 MANIFEST ─► EJECTING    sync; Unmount; PowerOff
 EJECTING ─► COMPLETE    "SAFE TO REMOVE DRIVE"
 any ─ error ─► FAILED_ATTENTION ("Error E-0xx: … Do not remove the drive. Contact technical support…")
 any ─ drive vanished ─► INTERRUPTED (batch kept; unverified items retried on next insert)
```

Rules: eligible = `FINALIZED` or `INTERRUPTED` (flagged), not already verified on this drive;
`CORRUPT` go to `Quarantine/`. The batch interval starts where the previous complete batch ended,
so successive exports cannot leave holes; gaps in the interval are listed in the manifest.
Existing destination files are never overwritten (same hash → re-verify and record; different
hash → explicit conflict). Local sources are never deleted by export. Reviewer MP4s are grouped
only from contiguous segments (no gap row between them, same codec parameters); a gap always
starts a new file and is named in the manifest.
