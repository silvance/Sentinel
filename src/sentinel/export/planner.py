"""Review-drive batch planning (docs/04 §6).

Interval: ``[start, end]`` where *start* is the end of the latest COMPLETE review batch for the
camera (on any drive), or the first recording if there is none, and *end* is the latest sealed
footage — capped at the first footage that is not yet sealed, so a segment still being written
or awaiting validation can never be skipped. Successive exports therefore leave no holes.

Eligible: ``FINALIZED`` / ``INTERRUPTED`` segments wholly inside the interval and not already
verified on this drive by a COMPLETE batch, plus *late* segments (sealed after an earlier
export had already passed their time, e.g. recovered after a crash) that no COMPLETE batch on
any drive contains. ``CORRUPT`` segments go to ``Quarantine/``.

Reviewer MP4s (pure function :func:`group_segments`): consecutive segments are joined only if
they are from the same recording run, consecutive in sequence, touch in time, have the same
codec / resolution / pixel format, start in the same local hour (and UTC offset), and no gap row
lies between them. A segment containing an internal gap gets a file of its own, so a gap always
starts a new file and is named in the manifest.

Space: units (one MP4 group with its originals, or one quarantined segment) are taken oldest
first while the estimate fits the free space; if the drive is too small the interval ends at the
first unit left out, and the next export continues from there.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from ..clock import parse_iso_utc
from ..db import Ledger
from . import layout
from .ledger import B_COMPLETE, I_VERIFIED, NewItem

CONTIGUITY_TOL_S = 0.5
DOC_RESERVE_BYTES = 8 * 1024 * 1024
MARGIN_FRACTION = 0.02
MIN_MARGIN_BYTES = 64 * 1024 * 1024
PLAYABLE = ("FINALIZED", "INTERRUPTED")
TERMINAL = ("FINALIZED", "INTERRUPTED", "CORRUPT")


class PlanError(RuntimeError):
    def __init__(self, code: str, message: str, detail: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.detail = detail or {}


@dataclass
class Group:
    segments: list[dict[str, Any]]
    internal_gap: bool = False

    @property
    def start(self) -> float:
        return parse_iso_utc(self.segments[0]["start_utc"])

    @property
    def end(self) -> float:
        return parse_iso_utc(self.segments[-1]["end_utc"])


@dataclass
class Plan:
    camera_id: str
    interval_start_utc: str
    interval_end_utc: str
    items: list[NewItem]
    groups: list[dict[str, Any]]
    gaps: list[dict[str, Any]]
    coverage: dict[str, Any]
    warnings: list[dict[str, Any]] = field(default_factory=list)
    planned_bytes: int = 0
    segments: dict[str, dict[str, Any]] = field(default_factory=dict)
    motion: list[dict[str, Any]] = field(default_factory=list)   # motion periods within the exported videos

    def to_json(self) -> dict[str, Any]:
        return {"groups": self.groups, "gaps": self.gaps, "coverage": self.coverage, "warnings": self.warnings,
                "planned_bytes": self.planned_bytes, "segments": self.segments, "motion": self.motion}


# Segment metadata frozen into the batch at planning time, so a resumed batch (and its
# regenerated manifest) never depends on ledger rows that the recorder fills in later.
SNAPSHOT_FIELDS = ("segment_id", "camera_id", "run_id", "seq", "rel_path", "state", "start_utc", "end_utc",
                   "duration_s", "time_source", "sha256", "size_bytes", "sealed_utc", "video_codec", "width",
                   "height", "pix_fmt", "content_sha256", "content_hash_method", "decoded_frame_count",
                   "framehash_rel_path", "framehash_sha256", "packet_count", "keyframe_count", "interrupted",
                   "clock_anomaly", "warnings")


def snapshot(s: dict[str, Any]) -> dict[str, Any]:
    return {k: s.get(k) for k in SNAPSHOT_FIELDS}


# ------------------------------------------------------------------------------ pure helpers
def _params(s: dict[str, Any]) -> tuple[Any, ...]:
    return (s.get("video_codec"), s.get("width"), s.get("height"), s.get("pix_fmt"))


def _overlaps(a0: float, a1: float, b0: float | None, b1: float | None) -> bool:
    lo = b0 if b0 is not None else float("-inf")
    hi = b1 if b1 is not None else float("inf")
    return lo < a1 and hi > a0


def _t(v: str | None) -> float | None:
    return parse_iso_utc(v) if v else None


def group_segments(segments: list[dict[str, Any]], gaps: list[dict[str, Any]], tz: ZoneInfo,
                   tol: float = CONTIGUITY_TOL_S) -> list[Group]:
    """Split playable segments (sorted by start) into reviewer-file groups. See module doc."""
    internal = {g["prev_segment_id"] for g in gaps if g.get("kind") == "within_segment" and g.get("prev_segment_id")}
    groups: list[Group] = []
    cur: Group | None = None
    for s in segments:
        sid = s["segment_id"]
        if sid in internal:
            groups.append(Group([s], internal_gap=True))
            cur = None
            continue
        if cur is not None and _joinable(cur, s, gaps, tz, tol):
            cur.segments.append(s)
            continue
        cur = Group([s])
        groups.append(cur)
    return groups


def _joinable(g: Group, s: dict[str, Any], gaps: list[dict[str, Any]], tz: ZoneInfo, tol: float) -> bool:
    p = g.segments[-1]
    if p["run_id"] != s["run_id"] or int(s["seq"]) != int(p["seq"]) + 1:
        return False
    if _params(p) != _params(s):
        return False
    p_end, s_start = parse_iso_utc(p["end_utc"]), parse_iso_utc(s["start_utc"])
    if abs(s_start - p_end) > tol:
        return False
    if layout.hour_key(s_start, tz) != layout.hour_key(g.start, tz):
        return False
    # The joint between the two segments, never reaching past the start of p or the end of s
    # (a very short s followed by a gap must not look as if the gap were before it).
    lo = max(parse_iso_utc(p["start_utc"]), min(p_end, s_start) - tol / 2)
    hi = min(parse_iso_utc(s["end_utc"]), max(p_end, s_start) + tol / 2)
    for gap in gaps:
        if gap.get("kind") == "between_runs" and (gap.get("prev_segment_id") == p["segment_id"]
                                                  or gap.get("next_segment_id") == s["segment_id"]):
            return False
        if gap.get("prev_segment_id") == s["segment_id"] or gap.get("next_segment_id") == p["segment_id"]:
            continue   # a gap after s or before p is not between them
        g0, g1 = _t(gap.get("start_utc")), _t(gap.get("end_utc"))
        if g0 is None and g1 is None:
            continue
        positive = g0 is None or g1 is None or g1 > g0
        if positive and _overlaps(lo, hi, g0, g1):   # any recorded gap reaching into the joint
            return False
    return True


def unaccounted(start: float, end: float, spans: list[tuple[float, float]], min_hole_s: float = 1.0) -> list[tuple[float, float]]:
    """Parts of [start, end] covered by neither footage nor a recorded gap (should be none)."""
    holes = []
    t = start
    for a, b in sorted(spans):
        if b <= t:
            continue
        if a > t:
            holes.append((t, min(a, end)))
        t = max(t, b)
        if t >= end:
            break
    if t < end:
        holes.append((t, end))
    return [(a, b) for a, b in holes if b - a >= min_hole_s]


# ------------------------------------------------------------------------------ planner
class Planner:
    def __init__(self, ledger: Ledger, spool: Path, camera_id: str, tz: ZoneInfo) -> None:
        self.ledger = ledger
        self.spool = spool
        self.camera_id = camera_id
        self.tz = tz

    def _rows(self, sql: str, params: tuple[Any, ...] = ()) -> list[dict[str, Any]]:
        return [dict(r) for r in self.ledger.query(sql, params)]

    def interval(self, prev_end: str | None) -> tuple[str | None, str | None]:
        cam = self.camera_id
        marks = ",".join("?" for _ in TERMINAL)
        start = prev_end
        if start is None:
            r = self._rows(f"SELECT MIN(start_utc) v FROM segments WHERE camera_id=? AND state IN ({marks}) "
                           "AND start_utc IS NOT NULL", (cam, *TERMINAL))
            start = r[0]["v"] if r else None
        r = self._rows(f"SELECT MAX(end_utc) v FROM segments WHERE camera_id=? AND state IN ({marks}) "
                       "AND end_utc IS NOT NULL", (cam, *TERMINAL))
        end = r[0]["v"] if r else None
        if end is None:
            return start, None
        caps = []
        for s in self._rows(f"SELECT * FROM segments WHERE camera_id=? AND state NOT IN ({marks})", (cam, *TERMINAL)):
            if s["start_utc"]:
                caps.append(s["start_utc"])
                continue
            prev = self._rows("SELECT MAX(end_utc) v FROM segments WHERE run_id=? AND seq<? AND end_utc IS NOT NULL",
                              (s["run_id"], s["seq"]))
            if prev and prev[0]["v"]:
                caps.append(prev[0]["v"])
                continue
            run = self._rows("SELECT anchor_utc, launched_utc FROM recording_runs WHERE run_id=?", (s["run_id"],))
            if run:
                caps.append(run[0]["anchor_utc"] or run[0]["launched_utc"])
        if caps:
            end = min(end, min(caps))
        return start, end

    def plan(self, drive_id: str, prev_end: str | None, free_bytes: int) -> Plan | None:
        start, end = self.interval(prev_end)
        cam = self.camera_id
        marks = ",".join("?" for _ in TERMINAL)
        not_on_drive = ("NOT EXISTS (SELECT 1 FROM export_items i JOIN export_batches b ON b.batch_id=i.batch_id "
                        "WHERE i.segment_id=s.segment_id AND i.kind IN ('original','quarantine') AND i.state=? "
                        "AND b.state=? AND b.drive_id=?)")
        not_anywhere = ("NOT EXISTS (SELECT 1 FROM export_items i JOIN export_batches b ON b.batch_id=i.batch_id "
                        "WHERE i.segment_id=s.segment_id AND i.kind IN ('original','quarantine') AND i.state=? "
                        "AND b.state=?)")
        main: list[dict[str, Any]] = []
        if start is not None and end is not None and start < end:
            main = self._rows(f"SELECT s.*, s.rowid AS rid FROM segments s WHERE s.camera_id=? AND s.state IN ({marks}) "
                              f"AND s.start_utc >= ? AND s.end_utc <= ? AND s.local_deleted_utc IS NULL AND {not_on_drive} "
                              f"ORDER BY s.start_utc, s.rowid",
                              (cam, *TERMINAL, start, end, I_VERIFIED, B_COMPLETE, drive_id))
        late: list[dict[str, Any]] = []
        if prev_end is not None:
            late = self._rows(f"SELECT s.*, s.rowid AS rid FROM segments s WHERE s.camera_id=? AND s.state IN ({marks}) "
                              f"AND (s.start_utc IS NULL OR s.start_utc < ?) AND s.local_deleted_utc IS NULL AND {not_anywhere} "
                              f"ORDER BY s.start_utc, s.rowid", (cam, *TERMINAL, prev_end, I_VERIFIED, B_COMPLETE))
        for s in late:
            s["late"] = True
        if not main and not late:
            return None
        if start is None or end is None or start > end:
            start = end = prev_end or (main or late)[0]["start_utc"] or ""

        warnings: list[dict[str, Any]] = []
        if start and end:
            # Originals removed locally by the retention rule (verified on archive drives) cannot be exported
            # from this computer; they are named in the manifest instead of failing the export.
            gone = self._rows(f"SELECT segment_id, start_utc, end_utc FROM segments s WHERE s.camera_id=? AND "
                              f"s.state IN ({marks}) AND s.local_deleted_utc IS NOT NULL AND s.start_utc >= ? AND "
                              f"s.end_utc <= ? ORDER BY s.rowid", (cam, *TERMINAL, start, end))
            if gone:
                warnings.append({"code": "footage_only_on_archive_drive", "segment_ids": [g["segment_id"] for g in gone],
                                 "message": "Some recordings in this period are kept only on archive drives and are "
                                            "not on this review drive.", "seconds": None})
        if prev_end is not None:
            # (review 2026-11-10) Late segments (completed after an earlier export had passed their time)
            # that the retention rule removed before any export took them: lie before this interval, so
            # the warning above misses them. Named once, in the first export after their removal.
            late_gone = self._rows(f"SELECT segment_id, start_utc, end_utc FROM segments s WHERE s.camera_id=? AND "
                                   f"s.state IN ({marks}) AND (s.start_utc IS NULL OR s.start_utc < ?) AND "
                                   f"s.local_deleted_utc IS NOT NULL AND {not_anywhere} ORDER BY s.rowid",
                                   (cam, *TERMINAL, prev_end, I_VERIFIED, B_COMPLETE))
            named = self._named_as_archive_only()
            late_gone = [g for g in late_gone if g["segment_id"] not in named]
            if late_gone:
                warnings.append({"code": "late_footage_only_on_archive_drive",
                                 "segment_ids": [g["segment_id"] for g in late_gone],
                                 "periods": [{"start_utc": g["start_utc"], "end_utc": g["end_utc"]} for g in late_gone],
                                 "message": "Some recordings that were completed after an earlier export (for example "
                                            "after a restart) were removed from this computer before they could be "
                                            "exported. They are kept only on archive drives and are not on this "
                                            "review drive.", "seconds": None})
        all_gaps = self._rows("SELECT * FROM recording_gaps WHERE camera_id=? ORDER BY gap_id", (cam,))
        late_ids = {s["segment_id"] for s in late}

        units: list[tuple[float, int, list[NewItem], dict[str, Any] | None, str]] = []
        playable = [s for s in late + main if s["state"] in PLAYABLE and s["start_utc"] and s["end_utc"]]
        playable.sort(key=lambda s: (s["start_utc"], s["rid"]))
        groups = group_segments(playable, all_gaps, self.tz)
        used_names: set[str] = set()
        for g in groups:
            for s in g.segments:
                self._check_source(s)
            items: list[NewItem] = []
            for s in g.segments:
                items.extend(self._original_items(s, "original", "framehash", layout.original_rel_path))
            name = layout.mp4_rel_path(g.start, g.end, self.tz)
            n = 2
            while name.lower() in used_names:
                name = layout.mp4_rel_path(g.start, g.end, self.tz, suffix=f"_{n}")
                n += 1
            used_names.add(name.lower())
            src_bytes = sum(int(s["size_bytes"] or 0) for s in g.segments)
            flags = sorted({w for s in g.segments for w in _segment_flags(s)} | ({"internal_gap"} if g.internal_gap else set()))
            meta = {"dest_rel_path": name, "start_utc": g.segments[0]["start_utc"], "end_utc": g.segments[-1]["end_utc"],
                    "segments": [s["segment_id"] for s in g.segments], "flags": flags,
                    "late": any(s["segment_id"] in late_ids for s in g.segments)}
            items.append(NewItem("mp4", name, sources=meta["segments"], planned_bytes=src_bytes,
                                 detail={"flags": flags, "start_utc": meta["start_utc"], "end_utc": meta["end_utc"]}))
            units.append((g.start, len(units), items, meta, g.segments[0]["start_utc"]))
        for s in late + main:
            if s["state"] != "CORRUPT":
                continue
            if not s["sha256"] or not (self.spool / s["rel_path"]).exists():
                warnings.append({"code": "corrupt_segment_file_missing", "segment_id": s["segment_id"],
                                 "message": "A damaged recording file was missing and could not be exported."})
                continue
            items = self._original_items(s, "quarantine", "quarantine_framehash", layout.quarantine_rel_path)
            t0 = parse_iso_utc(s["start_utc"]) if s["start_utc"] else float("-inf")
            units.append((t0, len(units), items, None, s["start_utc"] or start))
        units.sort(key=lambda u: (u[0], u[1]))

        budget = free_bytes - DOC_RESERVE_BYTES
        chosen: list[tuple[float, int, list[NewItem], dict[str, Any] | None, str]] = []
        need = 0
        for u in units:
            cost = sum(i.planned_bytes for i in u[2])
            total = need + cost
            if total + max(MIN_MARGIN_BYTES, int(total * MARGIN_FRACTION)) > budget:
                if not chosen:
                    raise PlanError("E-001", "the review drive is too small for the first recording",
                                    {"free_bytes": free_bytes, "needed_bytes": total})
                end = max(start, min(end, u[4]))
                warnings.append({"code": "drive_too_small", "message":
                                 "This drive was too small for all recordings. The export ends early; "
                                 "export the rest to another drive.", "export_ends_utc": end})
                break
            need = total
            chosen.append(u)
        items_out = [i for u in chosen for i in u[2]]
        group_meta = [u[3] for u in chosen if u[3] is not None]

        s_t, e_t = parse_iso_utc(start), parse_iso_utc(end)
        late_spans = [(parse_iso_utc(s["start_utc"]), parse_iso_utc(s["end_utc"])) for s in late
                      if s["start_utc"] and s["end_utc"]]
        gaps = []
        for g in all_gaps:
            g0, g1 = g["start_utc"], g["end_utc"]
            in_interval = (g0 is None or g0 <= end) and (g1 is None or g1 >= start)
            near_late = g["prev_segment_id"] in late_ids or g["next_segment_id"] in late_ids or any(
                _overlaps(a, b, _t(g0), _t(g1)) for a, b in late_spans)
            if in_interval or near_late:
                gaps.append({k: g[k] for k in ("gap_id", "kind", "start_utc", "end_utc", "duration_s", "reason",
                                               "prev_segment_id", "next_segment_id", "attempts", "clock_anomaly")}
                            | {"open": g1 is None})

        # Coverage of the interval by footage (all sealed segments, exported now or before) and gaps.
        spans: list[tuple[float, float]] = []
        for s in self._rows(f"SELECT start_utc, end_utc FROM segments WHERE camera_id=? AND state IN ({marks}) "
                            "AND start_utc IS NOT NULL AND end_utc IS NOT NULL AND start_utc <= ? AND end_utc >= ?",
                            (cam, *TERMINAL, end, start)):
            spans.append((parse_iso_utc(s["start_utc"]), parse_iso_utc(s["end_utc"])))
        recorded = sum(max(0.0, min(b, e_t) - max(a, s_t)) for a, b in spans)
        for g in gaps:
            g0 = _t(g["start_utc"]) if g["start_utc"] else s_t
            g1 = _t(g["end_utc"]) if g["end_utc"] else e_t
            spans.append((g0, g1))
        holes = unaccounted(s_t, e_t, spans)
        coverage = {"interval_s": round(e_t - s_t, 3), "recorded_s": round(recorded, 3),
                    "unaccounted": [{"start_epoch": a, "end_epoch": b, "duration_s": round(b - a, 3)} for a, b in holes]}
        if holes:
            warnings.append({"code": "unaccounted_time", "message":
                             "Part of the export period has neither recordings nor a recorded gap. "
                             "Treat it as missing.", "seconds": round(sum(b - a for a, b in holes), 3)})
        if any(i.segment_id in late_ids for i in items_out):
            warnings.append({"code": "late_segments", "message":
                             "Some recordings were completed after an earlier export had already covered their "
                             "time. They are included in this export."})
        exported_ids = {i.segment_id for i in items_out if i.segment_id}
        for s in late + main:
            if s.get("clock_anomaly") and s["segment_id"] in exported_ids:
                warnings.append({"code": "clock_anomaly", "segment_id": s["segment_id"], "message":
                                 "The system clock was changed while this recording was made; its times may be "
                                 "inaccurate."})
        used = exported_ids | {sid for i in items_out for sid in (i.sources or [])}
        snap = {s["segment_id"]: snapshot(s) for s in late + main if s["segment_id"] in used}
        return Plan(cam, start, end, items_out, group_meta, gaps, coverage, warnings,
                    planned_bytes=sum(i.planned_bytes for i in items_out), segments=snap,
                    motion=self._motion(cam, group_meta))

    def _named_as_archive_only(self) -> set[str]:
        """Segment ids already named as 'kept only on archive drives' by a COMPLETE export."""
        out: set[str] = set()
        for r in self._rows("SELECT plan FROM export_batches WHERE camera_id=? AND purpose='review' AND state=?",
                            (self.camera_id, B_COMPLETE)):
            try:
                plan = json.loads(r["plan"] or "{}")
            except ValueError:
                continue
            for w in plan.get("warnings") or []:
                if isinstance(w, dict) and w.get("code") in ("footage_only_on_archive_drive",
                                                             "late_footage_only_on_archive_drive"):
                    out.update(str(x) for x in w.get("segment_ids") or [])
        return out

    def _motion(self, cam: str, groups: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Motion periods (recording-log events ``motion_period``) overlapping the exported videos,
        frozen into the plan like the segment snapshot."""
        spans = [(g["start_utc"], g["end_utc"]) for g in groups if g.get("start_utc") and g.get("end_utc")]
        if not spans:
            return []
        lo, hi = min(a for a, _ in spans), max(b for _, b in spans)
        rows = self._rows("SELECT event_id, detail FROM events WHERE code='motion_period' "
                          "AND json_extract(detail, '$.end_utc') >= ? AND json_extract(detail, '$.start_utc') <= ? "
                          "ORDER BY json_extract(detail, '$.start_utc'), event_id", (lo, hi))
        out = []
        for r in rows:
            try:
                d = json.loads(r["detail"] or "{}")
            except ValueError:
                continue
            if d.get("camera_id", cam) != cam or not d.get("start_utc") or not d.get("end_utc"):
                continue
            if any(d["start_utc"] <= b and d["end_utc"] >= a for a, b in spans):
                out.append({"event_id": r["event_id"], "start_utc": d["start_utc"], "end_utc": d["end_utc"],
                            "peak_changed_percent": d.get("peak_changed_percent")})
        return out

    def _check_source(self, s: dict[str, Any]) -> None:
        p = self.spool / s["rel_path"]
        if not s["sha256"] or not p.exists():
            raise PlanError("E-006", "a sealed recording is missing from local storage",
                            {"segment_id": s["segment_id"], "rel_path": s["rel_path"]})

    def _original_items(self, s: dict[str, Any], kind: str, fh_kind: str, dest_fn: Any) -> list[NewItem]:
        detail = {"start_utc": s["start_utc"], "end_utc": s["end_utc"], "state": s["state"],
                  "late": bool(s.get("late"))}
        out = [NewItem(kind, dest_fn(s["rel_path"]), segment_id=s["segment_id"], source_rel_path=s["rel_path"],
                       expected_sha256=s["sha256"], planned_bytes=int(s["size_bytes"] or 0), detail=detail)]
        fh = s.get("framehash_rel_path")
        if fh and s.get("framehash_sha256"):
            p = self.spool / fh
            size = p.stat().st_size if p.exists() else 0
            out.append(NewItem(fh_kind, dest_fn(fh), segment_id=s["segment_id"], source_rel_path=fh,
                               expected_sha256=s["framehash_sha256"], planned_bytes=size))
        return out


def _segment_flags(s: dict[str, Any]) -> list[str]:
    flags = []
    if s["state"] == "INTERRUPTED":
        flags.append("interrupted")
    if s.get("clock_anomaly"):
        flags.append("clock_anomaly")
    try:
        flags.extend(json.loads(s.get("warnings") or "[]"))
    except ValueError:
        pass
    return flags
