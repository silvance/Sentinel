"""Lighting (owner request 2026-11-18, 0.3.22). The room has one light switch and windows: the light being
switched means someone is there and must count as motion at once; daylight getting brighter or dimmer, and the
camera adjusting to it, must not. A change of the whole picture never sets the no-motion count back."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from conftest import base_config_dict, make_config
from sentinel.config import ConfigError, load_config_dict
from sentinel.motion import (FRAME_BYTES, LIGHT_SETTLE_S, MotionAnalyzer, MotionConfig, MotionDetector,
                             run_motion_test)

W, H = 160, 120


def flat(level: int) -> bytearray:
    return bytearray([level]) * FRAME_BYTES


def scene(left: int = 200, right: int = 60, box: int | None = None, window: bool = False) -> bytes:
    """A room: a bright half and a dark half; optionally a 20x20 bright box at column ``box`` (someone moving),
    and a window (40x30 points that stay white whatever the room does)."""
    b = bytearray(FRAME_BYTES)
    for r in range(H):
        b[r * W:r * W + W // 2] = bytes([left]) * (W // 2)
        b[r * W + W // 2:(r + 1) * W] = bytes([right]) * (W // 2)
    if window:
        for r in range(30):
            b[r * W:r * W + 40] = bytes([255]) * 40
    if box is not None:
        for r in range(70, 90):
            b[r * W + box:r * W + box + 20] = bytes([255]) * 20
    return bytes(b)


def analyzer(**kw: Any) -> MotionAnalyzer:
    return MotionAnalyzer(MotionConfig(**{"changed_percent": 1.0, "pixel_threshold": 25, "min_frames": 2, **kw}))


# ---------------------------------------------------------------------- settings
def test_it_is_off_unless_asked_for_and_the_settings_are_checked(tmp_path: Path) -> None:
    m = make_config(tmp_path).motion
    assert m.light_aware is False and m.light_change_percent == 60.0
    on = make_config(tmp_path, motion={"light_aware": True, "light_change_percent": 45}).motion
    assert on.light_aware is True and on.light_change_percent == 45.0
    for bad, match in (({"light_aware": "yes"}, "true or false"), ({"light_change_percent": 10}, "light_change_percent"),
                       ({"light_change_percent": 101}, "light_change_percent"),
                       ({"light_change_percent": 30, "changed_percent": 40}, "larger than changed_percent")):
        with pytest.raises(ConfigError, match=match):
            load_config_dict(base_config_dict(tmp_path, motion=bad), base_dir=tmp_path)


# ---------------------------------------------------------------------- the light being switched
def test_a_clean_light_switch_is_one_changed_frame_and_was_missed() -> None:
    """Why the option exists: with "Frames in a row" at 2, a light that goes on in one step changes one frame."""
    a = analyzer()
    dark, lit = bytes(flat(30)), bytes(flat(170))
    assert [a.feed(f) for f in (dark, dark + b"", lit, lit, lit)] == [False] * 5
    assert a.last_raw == 0.0


def test_with_lighting_on_the_light_being_switched_is_motion_at_once() -> None:
    a = analyzer(light_aware=True, stream_path="stream2", analysis_fps=4)
    dark, lit = scene(60, 20), scene(220, 150)
    assert a.feed(dark) is False
    assert a.feed(scene(60, 21)) is False and not a.light
    assert a.feed(lit) is True                                   # on: the whole picture changed, motion now
    assert a.light and a.light_started and a.last_raw == 100.0 and a.last_score == 100.0 and a.run_length >= 2
    assert a.feed(scene(221, 150)) is False and a.light and not a.light_started
    assert a.feed(dark) is True and not a.light_started          # off again within the settling time: same event
    for i in range(int(LIGHT_SETTLE_S * 4) + 1):
        a.feed(scene(60, 20 + i % 2))
    assert not a.light
    assert a.feed(lit) is True and a.light_started               # later: a new event


def test_a_smaller_share_than_the_limit_is_not_the_light() -> None:
    a = analyzer(light_aware=True, light_change_percent=60)
    a.feed(scene(200, 60))
    assert a.feed(scene(100, 60)) is False                       # half the picture: a first frame of motion, at most
    assert not a.light and a.last_raw == 50.0


# ---------------------------------------------------------------------- an even change of brightness
def test_an_even_change_of_brightness_is_cancelled_out() -> None:
    """Daylight through the window, or the camera adjusting: everything 20 % brighter. Only the bright half
    changes by more than the threshold (200 -> 240), which is half the picture: motion, unless it is cancelled."""
    before, after, more = scene(200, 60), scene(240, 72), scene(200, 60)
    plain = analyzer()
    assert [plain.feed(f) for f in (before, after, more)] == [False, False, True]         # counted as motion
    assert plain.last_raw == 50.0
    a = analyzer(light_aware=True)
    assert [a.feed(f) for f in (before, after, more)] == [False, False, False]
    assert a.last_raw == 50.0 and a.last_score == 0.0 and not a.light


def test_someone_moving_while_the_brightness_changes_is_still_motion() -> None:
    a = analyzer(light_aware=True)
    frames = [scene(200, 60, box=85), scene(240, 72, box=110), scene(200, 60, box=135), scene(240, 72, box=110)]
    out = [a.feed(f) for f in frames]
    assert out == [False, False, True, True]
    assert a.last_score == pytest.approx(100.0 * 800 / FRAME_BYTES)          # the box and where it was: 4.2 %
    assert a.last_raw > 50.0                                     # ...not the half of the picture that brightened


def test_cancelling_never_adds_a_change() -> None:
    """The room darkens by a quarter; the window stays white. Scaling the earlier picture would turn the
    window into a change (255 -> 191 against 255); a point counts only if it changed without scaling too."""
    a = analyzer(light_aware=True)
    a.feed(scene(200, 60, window=True))
    assert a.feed(scene(150, 45, window=True)) is False
    assert a.last_score == 0.0 and 40.0 < a.last_raw < 50.0      # the bright half without the window: 43.75 %
    dark = analyzer(light_aware=True)                            # nearly black pictures: nothing to scale by
    dark.feed(bytes(flat(0)))
    assert dark.feed(scene(0, 0, box=50)) is False and dark.last_score == dark.last_raw > 1.0


def test_without_the_option_nothing_is_cancelled_and_the_numbers_are_as_before() -> None:
    a, b = analyzer(), analyzer(light_aware=True)
    still, moved = scene(200, 60, box=10), scene(200, 60, box=60)
    for x in (a, b):
        x.feed(still)
        x.feed(moved)
    assert a.last_score == b.last_score == a.last_raw == pytest.approx(100.0 * 800 / FRAME_BYTES)


# ---------------------------------------------------------------------- the detector's counts
class Clock:
    def __init__(self) -> None:
        self.t = 1000.0

    def __call__(self) -> float:
        return self.t


def detector(**kw: Any) -> tuple[MotionDetector, Clock]:
    c = Clock()
    m = MotionConfig(**{"stream_path": "stream2", "analysis_fps": 4, "min_frames": 2, **kw})
    d = MotionDetector("ffmpeg", "rtsp://h/s", m, clock=c, mono=c)
    d.started_mono = c.t
    return d, c


def show(d: MotionDetector, c: Clock, frames: list[bytes]) -> None:
    for f in frames:
        c.t += 0.25
        d._on_frame(f)


def test_the_light_records_but_only_movement_counts_for_the_no_motion_fail_safe() -> None:
    d, c = detector(light_aware=True)
    dark, lit = scene(60, 20), scene(220, 150)
    show(d, c, [dark, scene(60, 21), lit])
    assert d.motion_count == 1 and d.light_count == 1 and d.scene_motion_count == 0
    assert d.last_trigger == "light" and d.recording_wanted() and d.last_light_epoch == c.t
    # the camera settles for a moment: still the light, not movement
    show(d, c, [scene(215, 146), scene(222, 152), scene(220, 150)])
    assert d.scene_motion_count == 0 and d.light_count == 1
    c.t += LIGHT_SETTLE_S
    show(d, c, [scene(220, 150, box=10), scene(220, 150, box=40), scene(220, 150, box=70)])
    assert d.scene_motion_count == 2 and d.last_scene_motion_epoch == c.t and d.light_count == 1
    snap = d.snapshot()
    assert snap["trigger"] == "light" and snap["light_count"] == 1 and snap["scene_motion_count"] == 2


def test_without_the_option_a_light_change_still_does_not_count_as_movement() -> None:
    """Off: the light is motion only when two frames in a row changed (as before 0.3.22), and even then it does
    not show that the detector notices movement (a camera changing to night vision every evening)."""
    d, c = detector()
    show(d, c, [scene(60, 20), scene(60, 21), scene(220, 150), scene(180, 120), scene(200, 135)])
    assert d.motion_count >= 1 and d.light_count == 1 and d.scene_motion_count == 0 and d.last_trigger == "light"
    c.t += LIGHT_SETTLE_S + 120
    show(d, c, [scene(200, 135, box=10), scene(200, 135, box=40), scene(200, 135, box=70)])
    assert d.scene_motion_count == 2 and d.last_trigger == "movement"


# ---------------------------------------------------------------------- the live test says what it saw
def test_the_live_test_shows_a_change_of_the_whole_picture(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import sentinel.motion as motion

    made: list[Any] = []

    class Fake:
        def __init__(self, *_a: Any, **_k: Any) -> None:
            self.motion_count = self.light_count = self.scene_motion_count = 0
            self.last_frame_mono, self.last_error, self.second = 1.0, "", 0
            made.append(self)

        def start(self) -> Any:
            return self

        def stop(self) -> None:
            pass

        def snapshot(self) -> dict[str, Any]:
            self.second += 1
            if self.second == 2:                                 # the light is switched in the second second
                self.motion_count, self.light_count = 1, 1
            if self.second == 3:
                self.motion_count, self.scene_motion_count = 2, 1
            return {"healthy": True, "warming_up": False, "last_score_percent": 100.0 if self.second == 2 else 3.0}

    monkeypatch.setattr(motion, "MotionDetector", Fake)
    for aware, first in ((True, "Lighting is on"), (False, "A change of at least 60 % of the picture")):
        out: list[str] = []
        cfg = make_config(tmp_path / str(aware), motion={"light_aware": aware})
        assert run_motion_test(cfg, 3, out.append, sleep=lambda _s: None) == 0
        assert out[1].startswith(first)
        assert "LIGHT" not in out[2] and "MOTION" not in out[2]
        assert "MOTION" in out[3] and "LIGHT" in out[3] and "changed 100.00 %" in out[3]
        assert "MOTION" in out[4] and "LIGHT" not in out[4]
        assert out[5].startswith("Done: motion seen 2 time(s), of which movement in part of the picture 1; "
                                 "the whole picture changed 1 time(s).")
