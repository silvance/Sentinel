"""Pictures lost to damaged camera data leave a trace (0.3.29).

Found 2026-11-20: a burst of corrupted data from the camera could take 11 of a 2-second file's 30 pictures and
leave nothing in the recording log. The hole (0.73 s) is shorter than ``max_internal_gap_s``, so it is no gap; the
decode pass, where it runs at all, happened not to complain. Three things now say it: the file's own rhythm
(``pictures_missing``), the same rhythm across the cut between two files, and the fingerprint pass, which decodes
every recording once (``pictures_not_decodable``).

The numbers of the healthy case are the site camera's own: 30 pictures a second, every step one interval."""

from __future__ import annotations

from types import SimpleNamespace

from sentinel.media.probe import HOLE_STEPS, ProbeResult, hole_before, picture_holes
from sentinel.media.videohash import ContentHash
from sentinel.recorder.integrity import IntegrityWorker

STEP30 = 1 / 30
STEP15 = 1 / 15


def _pts(n: int, step: float, start: float = 1.4) -> list[float]:
    return [start + i * step for i in range(n)]


def test_a_steady_stream_has_no_pictures_missing() -> None:
    # 16 s of the site camera, with the jitter its recordings show (steps of 33 to 40 ms)
    pts = [1.4]
    for i in range(480):
        pts.append(pts[-1] + (0.040 if i % 7 == 0 else 0.0333 if i % 3 else 0.0267))
    assert picture_holes(pts, STEP30, 2.0) == (0, [])


def test_the_burst_that_left_no_trace_is_counted() -> None:
    # The file of 2026-11-20: 19 of 30 pictures at 15 a second, one hole of 0.733 s after 0.467 s.
    pts = _pts(8, STEP15) + _pts(11, STEP15, start=1.4 + 7 * STEP15 + 0.733333)
    missing, holes = picture_holes(pts, STEP15, 2.0)
    assert missing == 10 and len(holes) == 1
    assert abs(holes[0][0] - 7 * STEP15) < 1e-6 and abs(holes[0][1] - 0.733333) < 1e-6


def test_one_or_two_pictures_dropped_are_not_reported_but_three_in_a_row_are() -> None:
    assert HOLE_STEPS == 4
    for skipped, expected in ((1, 0), (2, 0), (3, 3), (5, 5)):
        pts = _pts(20, STEP30) + _pts(20, STEP30, start=1.4 + 19 * STEP30 + (skipped + 1) * STEP30)
        assert picture_holes(pts, STEP30, 2.0)[0] == expected, skipped


def test_a_gap_or_a_leap_of_the_cameras_time_labels_is_not_counted_as_pictures() -> None:
    # Longer than max_internal_gap_s: recorded as a gap; hours long: the camera's labels leapt (0.3.28).
    for step in (2.5, 11983.601):
        pts = _pts(100, STEP30) + _pts(100, STEP30, start=1.4 + 99 * STEP30 + step)
        assert picture_holes(pts, STEP30, 2.0) == (0, [])


def test_a_handful_of_pictures_has_no_rhythm_to_judge_by() -> None:
    assert picture_holes([1.0, 1.0333, 1.5, 1.5333], STEP30, 2.0) == (0, [])
    assert picture_holes(_pts(30, STEP30), 0.0, 2.0) == (0, [])


def test_pictures_missing_across_the_cut_between_two_files() -> None:
    last = 9.4
    assert hole_before(last + STEP15, last, STEP15, 2.0) == (0, 0.0)                 # the next picture: no hole
    assert hole_before(last + 3 * STEP15, last, STEP15, 2.0) == (0, 0.0)             # two dropped: not reported
    n, length = hole_before(last + 12 * STEP15, last, STEP15, 2.0)
    assert n == 11 and abs(length - 0.8) < 1e-6
    assert hole_before(last + 2.5, last, STEP15, 2.0) == (0, 0.0)                    # a gap: the gap rules' business
    assert hole_before(last - 5.0, last, STEP15, 2.0) == (0, 0.0)                    # labels restarted: not a hole
    assert hole_before(None, last, STEP15, 2.0) == (0, 0.0) and hole_before(last + 1, None, STEP15, 2.0) == (0, 0.0)
    assert hole_before(last + 1, last, None, 2.0) == (0, 0.0)


def test_the_record_of_a_healthy_file_keeps_its_form() -> None:
    res = ProbeResult(playable=True, packet_count=30, frame_interval_s=STEP15)
    assert "pictures_missing" not in res.detail() and "picture_holes" not in res.detail()
    res.pictures_missing, res.picture_holes = 10, [(0.466667, 0.733333)]
    d = res.detail()
    assert d["pictures_missing"] == 10 and d["picture_holes"] == [(0.466667, 0.733333)]
    assert "hole_before_start_s" not in d
    res.missing_before_start, res.hole_before_start_s = 4, 0.333333
    assert res.detail()["pictures_missing"] == 14 and res.detail()["hole_before_start_s"] == 0.333333


class _Log:
    def __init__(self) -> None:
        self.events: list[tuple] = []

    def add_event(self, *a) -> None:  # noqa: ANN002
        self.events.append(a)


def _note(packets: int | None, decoded: int, messages: tuple[str, ...] = ()) -> list[tuple]:
    me = SimpleNamespace(ledger=_Log())
    seg = {"segment_id": "s1", "rel_path": "CAM01/run/seg_000003.ts", "packet_count": packets}
    IntegrityWorker._note_undecodable(me, seg, ContentHash("0" * 64, "m", decoded, messages))
    return me.ledger.events


def test_the_fingerprint_pass_says_when_a_recording_does_not_decode_in_full() -> None:
    assert _note(30, 30) == [] and _note(None, 30) == []
    (ev,) = _note(20, 16)
    assert ev[:3] == ("warning", "integrity", "pictures_not_decodable")
    assert ev[3] == "4 of 20 picture(s) in CAM01/run/seg_000003.ts could not be decoded"
    assert ev[4] == {"segment_id": "s1", "packet_count": 20, "decoded_frame_count": 16, "decoder_said": []}
    (ev,) = _note(30, 30, ("[h264 @ 0x1] error while decoding MB 12 7", "[h264 @ 0x1] concealing 320 DC errors"))
    assert ev[3] == "The decoder reported errors in CAM01/run/seg_000003.ts" and len(ev[4]["decoder_said"]) == 2
    assert _note(30, 31) == []          # more pictures than packets is not this problem
