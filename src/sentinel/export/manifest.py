"""Export documents: README_FIRST.txt, Export_Summary.html, Export_Manifest.json/.csv, SHA256SUMS.txt.

All documents are generated deterministically from the ledger (the generation time is frozen in
the batch row), so regenerating them after an interruption gives byte-identical files and never
conflicts with copies already written. Nothing here reads or writes the drive.
"""

from __future__ import annotations

import csv
import hashlib
import html
import io
import json
from dataclasses import dataclass
from typing import Any
from zoneinfo import ZoneInfo

from ..clock import parse_iso_utc
from . import layout

FORMAT = "sentinel-review-export"
FORMAT_VERSION = 1

GAP_REASONS = {
    "graceful_stop": "Recording was stopped (the system was shut down or restarted)",
    "source_ended": "The camera stopped sending video",
    "ffmpeg_error": "Camera connection lost",
    "ffmpeg_killed": "The recording program stopped unexpectedly",
    "stall": "Camera video stopped (frozen)",
    "connect_timeout": "Could not connect to the camera",
    "storage_critical": "Recording storage was full",
    "service_interrupted": "The recording software stopped unexpectedly",
    "launch_failed": "The recording program could not start",
    "timeline_discontinuity": "Video from the camera was lost",
    "internal_error": "Recording software problem",
    "scheduled_pause": "Recording was paused by the recording schedule (planned)",
    "timestamp_discontinuity": "The camera video paused inside a recording file",
    "timestamp_discontinuity_at_boundary": "The camera video paused between two recording files",
    "source_discontinuity_absorbed": "The camera video paused",
    "corrupt_segment": "Damaged recording (kept separately in the Quarantine folder)",
    "segment_file_missing": "A recording file is missing",
}

FLAG_TEXT = {
    "interrupted": "ends abruptly (recording was interrupted)",
    "clock_anomaly": "system clock was changed while recording; times may be inaccurate",
    "internal_gap": "contains a pause in the video (see gaps)",
    "time_from_file_mtime": "times estimated from the file date",
    "damaged_frames": "contains damaged picture(s) where the recording was cut off",
}

CONTENT_TEXT = {
    "full": "every frame of this MP4, decoded from the bytes read back from the drive, equals the concatenated "
            "per-frame SHA-256 lists of its source segments (except any unverifiable_damaged_frames)",
    "sampled": "the MP4's frame count equals its sources' total, and the frames in 'spans' (decoded from the "
               "file on the drive) equal the sources' per-frame SHA-256 at the same positions; other frames "
               "were NOT decoded",
    "deferred": "content not yet verified: the pictures of this MP4 have not been compared with the originals "
                "(see 'reason'; a later Content_Verification file in this folder may complete it). The file's "
                "bytes were verified.",
}
HTML_LEVEL = {"full": "all frames checked", "sampled": "sample of frames checked",
              "deferred": "pictures not yet checked"}

VERIFY_NOTES = {
    "windows-unbuffered(FILE_FLAG_NO_BUFFERING)":
        "read back from the drive bypassing the Windows file cache (the drive's own internal cache "
        "cannot be bypassed by software)",
    "linux-o_direct(after fadvise-dontneed)":
        "read back from the drive with O_DIRECT, bypassing the Linux page cache",
    "posix-fadvise-dontneed+buffered":
        "read back after asking the OS to drop its cache for the file (not guaranteed)",
    "buffered-cache-not-bypassed":
        "read back through the operating-system cache: proves the file system holds the right data, "
        "not that the drive's surface does",
}

README_TEXT = """\
SENTINEL REVIEW DRIVE - READ THIS FIRST
=======================================

TO WATCH THE VIDEO
  1. Open the folder "Recordings".
  2. Open the folder for the day you want (named year-month-day, for example 2026-11-09).
  3. Double-click a video file. It opens in the normal Windows video player
     (Films & TV / Media Player) or in VLC.

  Each video file name is its local start time and end time (hours, minutes, seconds)
  followed by the time zone. Example:
      140000-150000_EST_UTC-0500.mp4
  means 14:00:00 to 15:00:00 Eastern Standard Time, which is 5 hours behind UTC.
  A video file never runs across a gap: if recording stopped, a new file starts when it resumed.

TO SEE WHAT IS ON THIS DRIVE, AND ANY GAPS
  Open the folder "Reports" and double-click the Export_Summary file. It opens in any web
  browser and lists every video, every period with no recording (gaps, shown in red) and any
  warnings. Each export to this drive adds its own summary.

WHAT THE OTHER FOLDERS ARE
  Originals   The recordings exactly as the recorder saved them (.ts files). These are the
              original evidence files. The MP4 videos were made from them without changing
              the pictures, only the file format.
  Quarantine  Recording files that were found to be damaged. Kept unchanged for examination.
              (This folder only exists if there were damaged files.)
  Manifests   Lists of every file with its SHA-256 fingerprint (hash), in machine-readable
              (Export_Manifest.json), spreadsheet (Export_Manifest.csv) and plain-text
              (SHA256SUMS.txt) form. A Content_Verification file, if present, records a later
              picture-by-picture check of the videos.

HOW TO CHECK THAT A FILE IS UNCHANGED (WINDOWS)
  1. Click Start, type PowerShell, and open "Windows PowerShell".
  2. Type the following, replacing the path with the file you want to check, then press Enter:
       Get-FileHash -Algorithm SHA256 -LiteralPath "E:\\Recordings\\2026-11-09\\140000-150000_EST_UTC-0500.mp4"
     (Use your drive letter instead of E:. Tip: type Get-FileHash -Algorithm SHA256 -LiteralPath
     and a space, then drag the file into the PowerShell window.)
  3. Compare the long "Hash" value with the value for that file in Manifests\\...\\SHA256SUMS.txt.
     Capital and small letters do not matter. If every character matches, the file is unchanged.
  Alternative in Command Prompt:  certutil -hashfile "E:\\path\\to\\file" SHA256

  An MP4 video and the original .ts file it was made from have DIFFERENT fingerprints; this is
  expected, because the file format differs. Each file is checked against its own fingerprint.

Do not save, rename or edit files on this drive. Copy files elsewhere if you need to work on them.
"""


@dataclass(frozen=True)
class Documents:
    readme: bytes
    html: bytes
    csv: bytes
    manifest: bytes
    sums: bytes


def sha256_bytes(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


def _jl(v: str | None) -> Any:
    if not v:
        return None
    try:
        return json.loads(v)
    except ValueError:
        return v


class ManifestBuilder:
    def __init__(self, batch: dict[str, Any], items: list[dict[str, Any]], segments: dict[str, dict[str, Any]],
                 drive: dict[str, Any], tz: ZoneInfo) -> None:
        self.batch = batch
        self.items = [i for i in items if i["kind"] != "document"]
        self.segments = segments
        self.drive = drive
        self.tz = tz
        self.plan = _jl(batch.get("plan")) or {}

    # ------------------------------------------------------------------ helpers
    def _local(self, iso: str | None) -> str | None:
        return layout.local_text(parse_iso_utc(iso), self.tz) if iso else None

    def _orig_path(self, seg_id: str) -> str | None:
        for i in self.items:
            if i["segment_id"] == seg_id and i["kind"] in ("original", "quarantine"):
                return i["dest_rel_path"]
        return None

    def _sidecar_of(self, seg_id: str) -> str | None:
        return next((x["dest_rel_path"] for x in self.items if x["segment_id"] == seg_id
                     and x["kind"] in ("framehash", "quarantine_framehash")), None)

    def _file_entries(self) -> list[dict[str, Any]]:
        """One self-contained entry per file (usable by an offline tool without Sentinel)."""
        out = []
        for i in self.items:
            d = _jl(i["detail"]) or {}
            seg = self.segments.get(i["segment_id"] or "")
            sids = [i["segment_id"]] if i["segment_id"] else (_jl(i["sources"]) or [])
            first = self.segments.get(sids[0]) if sids else None
            start = seg["start_utc"] if seg else d.get("start_utc")
            end = seg["end_utc"] if seg else d.get("end_utc")
            content = None
            if i["kind"] == "mp4":
                content = i["content_sha256"]
            elif i["kind"] in ("original", "quarantine") and seg:
                content = seg["content_sha256"] or self._computed().get(seg["segment_id"], {}).get("content_sha256")
            out.append({
                "path": i["dest_rel_path"], "kind": i["kind"], "sha256": i["sha256"], "size_bytes": i["size_bytes"],
                "segment_ids": sids, "content_sha256": content,
                "framehash_path": self._sidecar_of(seg["segment_id"]) if seg and i["kind"] in ("original", "quarantine")
                else None,
                "start_utc": start, "end_utc": end, "start_local": self._local(start), "end_local": self._local(end),
                "video_codec": first["video_codec"] if first else None, "width": first["width"] if first else None,
                "height": first["height"] if first else None,
                "segment_state": seg["state"] if seg else None,
                "verified_utc": i["verified_utc"], "verify_method": i["verify_method"],
                "already_present": bool(i["already_present"]),
                "content_check": _level_of(i, d) if i["kind"] == "mp4" else None})
        return sorted(out, key=lambda e: e["path"])

    def _computed(self) -> dict[str, dict[str, Any]]:
        computed: dict[str, dict[str, Any]] = {}
        for i in self.items:
            if i["kind"] == "mp4":
                computed.update((_jl(i["detail"]) or {}).get("source_content", {}) or {})
        return computed

    def _originals(self) -> list[dict[str, Any]]:
        out = []
        computed = self._computed()
        for i in self.items:
            if i["kind"] not in ("original", "quarantine"):
                continue
            s = self.segments[i["segment_id"]]
            fh = self._sidecar_of(s["segment_id"])
            c = computed.get(s["segment_id"], {})
            out.append({
                "segment_id": s["segment_id"], "run_id": s["run_id"], "seq": s["seq"], "state": s["state"],
                "exported_path": i["dest_rel_path"], "quarantined": i["kind"] == "quarantine",
                "start_utc": s["start_utc"], "end_utc": s["end_utc"], "start_local": self._local(s["start_utc"]),
                "end_local": self._local(s["end_utc"]), "duration_s": s["duration_s"], "time_source": s["time_source"],
                "sealed_sha256": s["sha256"], "size_bytes": s["size_bytes"], "sealed_utc": s["sealed_utc"],
                "video_codec": s["video_codec"], "width": s["width"], "height": s["height"], "pix_fmt": s["pix_fmt"],
                "content_sha256": s["content_sha256"] or c.get("content_sha256"),
                "content_hash_method": s["content_hash_method"] or c.get("method"),
                "content_hash_computed_at_export": bool(not s["content_sha256"] and c),
                "framehash_file": fh, "interrupted": bool(s["interrupted"]), "clock_anomaly": bool(s["clock_anomaly"]),
                "warnings": _jl(s["warnings"]) or [], "late": bool((_jl(i["detail"]) or {}).get("late")),
            })
        return sorted(out, key=lambda e: (e["start_utc"] or "", e["exported_path"]))

    def _derivatives(self) -> list[dict[str, Any]]:
        out = []
        for i in self.items:
            if i["kind"] != "mp4":
                continue
            d = _jl(i["detail"]) or {}
            srcs = _jl(i["sources"]) or []
            out.append({
                "path": i["dest_rel_path"], "sha256": i["sha256"], "size_bytes": i["size_bytes"],
                "content_sha256": i["content_sha256"], "frame_count": i["frame_count"],
                "start_utc": d.get("start_utc"), "end_utc": d.get("end_utc"),
                "start_local": self._local(d.get("start_utc")), "end_local": self._local(d.get("end_utc")),
                "flags": d.get("flags", []),
                "sources": [{"segment_id": sid, "original_path": self._orig_path(sid),
                             "original_sealed_sha256": self.segments[sid]["sha256"]} for sid in srcs],
                "made_by": "FFmpeg concat demuxer, stream copy (-c copy), -movflags +faststart; no re-encoding",
                "byte_verification": "SHA-256 of the file as written by FFmpeg equals the SHA-256 read back from "
                                     "the drive (" + (i["verify_method"] or "") + ")",
                "written_sha256": i.get("written_sha256"),
                "content_check": dict(_content_of(i, d), description=CONTENT_TEXT.get(_level_of(i, d), "")),
                "note": "This file's SHA-256 differs from its sources' SHA-256 by design (different container).",
            })
        out.sort(key=lambda e: (e["start_utc"] or "", e["path"]))
        # Gaps between consecutive reviewer files (the reason a new file was started).
        gaps = [g for g in self.plan.get("gaps", []) if g.get("start_utc")]
        for n, a in enumerate(out):
            nxt = out[n + 1]["start_utc"] if n + 1 < len(out) else None
            a_end = parse_iso_utc(a["end_utc"]) - 0.5 if a["end_utc"] else None
            a["gap_ids_after"] = [g["gap_id"] for g in gaps if a_end is not None
                                  and parse_iso_utc(g["start_utc"]) >= a_end
                                  and (nxt is None or g["start_utc"] < nxt)]
        return out

    def _gaps(self) -> list[dict[str, Any]]:
        out = []
        for g in self.plan.get("gaps", []):
            out.append(dict(g, start_local=self._local(g.get("start_utc")), end_local=self._local(g.get("end_utc")),
                            reason_text=GAP_REASONS.get(g.get("reason") or "", f"Recording interrupted ({g.get('reason')})")))
        return out

    # ------------------------------------------------------------------ documents
    def readme(self) -> bytes:
        return README_TEXT.replace("\n", "\r\n").encode("utf-8")

    def csv_bytes(self) -> bytes:
        buf = io.StringIO()
        w = csv.writer(buf, lineterminator="\r\n")
        w.writerow(["path", "kind", "sha256", "size_bytes", "segment_ids", "start_utc", "end_utc", "start_local",
                    "end_local", "segment_state", "verified_utc", "verify_method", "already_present",
                    "content_sha256"])
        for i in sorted(self.items, key=lambda x: x["dest_rel_path"]):
            d = _jl(i["detail"]) or {}
            seg = self.segments.get(i["segment_id"] or "")
            start = seg["start_utc"] if seg else d.get("start_utc")
            end = seg["end_utc"] if seg else d.get("end_utc")
            sids = [i["segment_id"]] if i["segment_id"] else (_jl(i["sources"]) or [])
            w.writerow([i["dest_rel_path"], i["kind"], i["sha256"], i["size_bytes"], ";".join(sids), start, end,
                        self._local(start), self._local(end), seg["state"] if seg else "", i["verified_utc"],
                        i["verify_method"], int(i["already_present"]),
                        i["content_sha256"] or (seg["content_sha256"] if seg else "") or ""])
        return ("\ufeff" + buf.getvalue()).encode("utf-8")  # BOM so Excel opens UTF-8 correctly

    def manifest(self, documents: list[dict[str, Any]]) -> bytes:
        b = self.batch
        methods = sorted({i["verify_method"] for i in self.items if i["verify_method"]})
        doc = {
            "format": FORMAT, "format_version": FORMAT_VERSION,
            "export_id": b["batch_id"], "purpose": b["purpose"], "camera_id": b["camera_id"],
            "generated_utc": b["manifest_generated_utc"], "planned_utc": b["planned_utc"],
            "interval": {"start_utc": b["interval_start_utc"], "end_utc": b["interval_end_utc"],
                         "start_local": self._local(b["interval_start_utc"]),
                         "end_local": self._local(b["interval_end_utc"]),
                         "display_timezone": b["display_timezone"]},
            "drive": {"drive_id": b["drive_id"], **self.drive},
            "software": {"application": "Sentinel", "app_version": b["app_version"],
                         "ffmpeg_version": b["ffmpeg_version"]},
            "operator_id": b["operator_id"],
            "verification": {"file_hash": "SHA-256", "read_back_methods": methods,
                             "method_notes": {m: VERIFY_NOTES.get(m, "") for m in methods},
                             "rule": "every file listed was written as .partial, flushed, renamed, then read back "
                                     "from the drive and its SHA-256 compared with a value known beforehand "
                                     "(originals: the hash recorded when the recording was sealed; MP4s: the hash "
                                     "of the file as FFmpeg wrote it; documents: the hash of the generated text). "
                                     "MP4 pictures were additionally compared as stated per file in "
                                     "derivatives[].content_check (full | sampled | deferred).",
                             "content_check_configured": (self.plan.get("content_check") or {}).get("level", "full")},
            "files": self._file_entries(),
            "originals": self._originals(),
            "derivatives": self._derivatives(),
            "gaps": self._gaps(),
            "coverage": self.plan.get("coverage", {}),
            "warnings": self.plan.get("warnings", []),
            "documents": documents,
            "notes": ["All stored times are UTC (ISO-8601, 'Z'). Local times use display_timezone.",
                      "Times are when the recording computer received the video, not the camera's own clock.",
                      "Nothing in this export claims forensic validation."],
        }
        return (json.dumps(doc, indent=2, sort_keys=False, ensure_ascii=False) + "\n").encode("utf-8")

    def sums(self, documents: list[dict[str, Any]]) -> bytes:
        rows = [(i["sha256"], i["dest_rel_path"]) for i in self.items]
        rows += [(d["sha256"], d["path"]) for d in documents]
        lines = [f"{h}  {p}" for h, p in sorted(rows, key=lambda r: r[1])]
        # Plain "<hash>  <path>" lines, paths relative to the drive root, LF endings, no comment
        # lines, so the file also works with `sha256sum -c` run from the drive root.
        return ("\n".join(lines) + "\n").encode("utf-8")

    def html(self) -> bytes:
        b = self.batch
        e = html.escape
        derivs = self._derivatives()
        gaps = self._gaps()
        warns = self.plan.get("warnings", [])
        quarantined = [o for o in self._originals() if o["quarantined"]]
        cov = self.plan.get("coverage", {})
        rows = []
        for d in derivs:
            notes = "; ".join(FLAG_TEXT.get(f, f) for f in d["flags"])
            dur = parse_iso_utc(d["end_utc"]) - parse_iso_utc(d["start_utc"]) if d["start_utc"] and d["end_utc"] else 0
            lvl = d["content_check"]["level"]
            rows.append(f"<tr><td>{e(d['start_local'] or '')}</td><td>{e(d['end_local'] or '')}</td>"
                        f"<td>{_dur(dur)}</td><td class='path'>{e(d['path'].replace('/', chr(92)))}</td>"
                        f"<td>{e(HTML_LEVEL.get(lvl, lvl))}</td><td>{e(notes)}</td></tr>")
        gap_rows = []
        for g in gaps:
            dur = g.get("duration_s")
            gap_rows.append(f"<tr class='gap'><td>{e(g['start_local'] or 'unknown')}</td>"
                            f"<td>{e(g['end_local'] or ('still ongoing' if g.get('open') else 'unknown'))}</td>"
                            f"<td>{_dur(dur) if dur is not None else ''}</td><td>{e(g['reason_text'])}</td></tr>")
        warn_items = "".join(f"<li>{e(w.get('message', w.get('code', '')))}</li>" for w in warns)
        q_items = "".join(f"<li class='path'>{e(o['exported_path'].replace('/', chr(92)))} "
                          f"({e(o['start_local'] or 'time unknown')})</li>" for o in quarantined)
        unacc = cov.get("unaccounted") or []
        page = f"""<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>Sentinel export summary</title>
<style>
body{{font-family:Segoe UI,Arial,sans-serif;margin:24px;color:#111;background:#fff;max-width:1100px}}
h1{{font-size:24px}} h2{{font-size:19px;margin-top:28px}}
table{{border-collapse:collapse;width:100%}} th,td{{border:1px solid #bbb;padding:6px 8px;text-align:left;vertical-align:top}}
th{{background:#eee}} tr.gap td{{background:#fde2e2;color:#7a0000;font-weight:600}}
.box{{border:2px solid #2a6;background:#eefaf1;padding:10px 14px}} .warn{{border:2px solid #c60;background:#fff4e5;padding:10px 14px}}
.bad{{border:2px solid #b00;background:#fde2e2;padding:10px 14px}} .path{{font-family:Consolas,monospace;font-size:13px}}
small{{color:#555}}
</style></head><body>
<h1>Sentinel review drive &mdash; export summary</h1>
<div class="box"><b>Recordings from {e(self._local(b['interval_start_utc']) or '')} to {e(self._local(b['interval_end_utc']) or '')}</b><br>
Camera: {e(b['camera_id'])}. Every file listed was copied to this drive and then read back and checked.</div>
<h2>How to watch</h2>
<p>Open the <b>Recordings</b> folder, then the folder for the day, and double-click a video. File names are the local
start and end time followed by the time zone. A new file starts after every gap.</p>
<h2>Videos ({len(derivs)})</h2>
<table><tr><th>Starts</th><th>Ends</th><th>Length</th><th>File</th><th>Pictures compared with originals</th><th>Notes</th></tr>
{''.join(rows) or '<tr><td colspan=6>No videos in this export.</td></tr>'}
</table>
<p><small>Every file's bytes were read back from this drive and checked. "All frames checked": every picture of the
video was compared with the original recordings. "Sample of frames checked": the number of pictures and selected
parts were compared. "Pictures not yet checked": only the bytes were checked so far.</small></p>
<h2>Gaps &mdash; periods with no recording ({len(gaps)})</h2>
{'<p>None recorded.</p>' if not gaps else '<table><tr><th>From</th><th>To</th><th>Length</th><th>Reason</th></tr>' + ''.join(gap_rows) + '</table>'}
{'<div class="bad"><b>Unexplained time:</b> ' + str(len(unacc)) + ' period(s) have neither video nor a recorded gap. Treat them as missing.</div>' if unacc else ''}
{'<h2>Warnings</h2><div class="warn"><ul>' + warn_items + '</ul></div>' if warns else ''}
{'<h2>Damaged recordings (Quarantine folder)</h2><div class="warn"><p>These files could not be played. They are kept unchanged for examination.</p><ul>' + q_items + '</ul></div>' if quarantined else ''}
<h2>About this export</h2>
<table>
<tr><th>Export ID</th><td class="path">{e(b['batch_id'])}</td></tr>
<tr><th>Prepared</th><td>{e(self._local(b['manifest_generated_utc']) or '')} ({e(b['manifest_generated_utc'] or '')})</td></tr>
<tr><th>Operator</th><td>{e(b['operator_id'] or 'not recorded')}</td></tr>
<tr><th>Time zone used for names</th><td>{e(b['display_timezone'])}</td></tr>
<tr><th>Drive</th><td>{e(self.drive.get('label') or '')} <small class="path">{e(b['drive_id'])}</small></td></tr>
<tr><th>Software</th><td>Sentinel {e(b['app_version'])}; {e(b['ffmpeg_version'] or 'FFmpeg version unknown')}</td></tr>
</table>
<h2>Checking files</h2>
<p>The <b>Manifests</b> folder lists the SHA-256 fingerprint of every file. To check a file on Windows, open PowerShell and run
<span class="path">Get-FileHash -Algorithm SHA256 -LiteralPath "&lt;file&gt;"</span>, then compare the result with
<span class="path">SHA256SUMS.txt</span>. Instructions are in <span class="path">README_FIRST.txt</span>.
The original recordings (<b>Originals</b> folder) and the MP4 videos have different fingerprints because their file
format differs; the MP4 pictures were checked frame by frame against the originals.</p>
<p><small>Times are when the recording computer received the video. This summary does not claim forensic validation.</small></p>
</body></html>
"""
        return page.replace("\n", "\r\n").encode("utf-8")


def _level_of(i: dict[str, Any], d: dict[str, Any]) -> str:
    return i.get("content_check") or (d.get("content") or {}).get("level") or "full"


def _content_of(i: dict[str, Any], d: dict[str, Any]) -> dict[str, Any]:
    c = dict(d.get("content") or {})
    c["level"] = _level_of(i, d)
    c.setdefault("unverifiable_damaged_frames", [])
    c["checked_utc"] = i.get("content_verified_utc")
    return c


def _dur(seconds: float | None) -> str:
    if seconds is None:
        return ""
    s = int(round(seconds))
    h, rem = divmod(s, 3600)
    m, sec = divmod(rem, 60)
    if h:
        return f"{h} h {m:02d} min"
    if m:
        return f"{m} min {sec:02d} s"
    return f"{sec} s"
