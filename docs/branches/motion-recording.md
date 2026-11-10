# Motion-triggered recording (2026-11-10)

Owner request: record all the time during set working hours; outside them record only when motion
is detected, for a set time after the detection, restarting that timer at every new detection.
Camera in use: MAXSUR M961 series (e.g. M961-5-CP-IP: 5 MP, H.264, multi-streaming, ONVIF
Profile S/T, built-in motion detection).

## How to set it up

```toml
[schedule]
enabled = true
default = "motion"                 # outside the periods below: record on motion only

[[schedule.weekly]]                # working hours: record all the time
action = "record"
days = ["weekdays"]
start = "07:00"
end = "18:00"

[motion]
stream_path = "<second stream path from the camera documentation>"
keep_recording_s = 120             # keep recording 2 minutes after the last motion
```

Check with `Sentinel.exe schedule --days 14` (periods show RECORD / PAUSE / MOTION) and tune with
`Sentinel.exe motion-test` (walk through the scene; adjust `changed_percent` / `pixel_threshold`
until walking through is always MOTION and the empty scene never is). Holidays: a
`[[schedule.dates]]` period with `action = "motion"` (or "pause"/"record") overrides the week.

## What happens

* **Detector.** The recorder service runs a second, low-priority FFmpeg that reads the camera's
  second (low-resolution) stream at `analysis_fps`, or only the key frames of the main stream when
  `stream_path` is empty, shrinks each frame to 160x120 grey levels and compares it with the
  previous one (`sentinel.motion.MotionAnalyzer`). Motion = `changed_percent` of the picture changed
  by more than `pixel_threshold`, in `min_frames` frames in a row.
* **Waiting.** In a motion period with no motion for `keep_recording_s`, the current recording
  file is closed normally (run end reason `motion_idle`), the recording log gets a gap with reason
  `motion_idle` ("no motion outside recording hours; planned") and a `motion_standby` event, and
  the screen shows **"Watching for motion"** (P-003, information, not an error).
* **Motion.** Within a fraction of a second of a confirmed detection the recorder starts
  recording again (event `motion_record`, with the detection time) and keeps recording until
  `keep_recording_s` has passed since the last motion; motion while recording restarts the timer.
* **Fail safe.** If the detector gets no pictures for `unhealthy_after_s` (15 s), the recorder
  records all the time and the status shows **W-001** "Motion detection is not working, so the
  recorder records all the time". A schedule or detector state that cannot be evaluated also
  records. At service start the detector has 15 s to deliver its first picture before this applies.
* Working-hours (`record`) and `pause` periods behave as before; the detector keeps running in
  them (so a motion period starts with a warm detector).

## Limits (read before relying on it)

* **Recording starts a few seconds after motion begins.** Detection takes about 0.5–1 s on the
  second stream (`min_frames` analysed frames), 2 s or more on main-stream key frames; then the
  recorder connects to the main stream and FFmpeg waits for its next key frame (up to the camera's
  key-frame interval, which should be ≤ 2 s). Nothing from before the trigger is kept (no
  pre-recording buffer). Someone moving quickly through the edge of the picture can be missed.
* Sentinel's detector, not the camera's built-in one, is used: it works with any camera and is
  tested here with the simulated camera. The camera's ONVIF motion events could be added later,
  after the bench test on the real camera.
* Frame differencing reacts to any change of the picture: the camera's day/night (IR) switch,
  lights switched on, headlights, moving foliage or a screen in view trigger it; slow movement in
  the dark or small/distant objects may not. Point the camera away from such sources or raise the
  thresholds; check at night, too (docs/09 §4a).
* Gaps between motion recordings are planned and recorded in the log, but a reviewer will see many
  of them; the export manifest names each one ("No motion was detected outside the recording hours").

## Tests

* `tests/unit/test_motion.py`: analysis thresholds, settings validation, detector command, timer
  restart, fail safe (no pictures → record), the supervisor's record/wait decision, schedule action
  "motion", P-003 / W-001 wording, unique alert codes.
* `tests/integration/test_motion_service.py` (real recorder, simulated camera with a still noisy
  scene and a box that moves at set times): nothing recorded while still; motion starts ONE
  recording; a second motion within the timer extends it; it stops `keep_recording_s` after the
  last motion; files sealed; one `motion_idle` gap; working hours record without motion.
* Measured with the simulator (key frames, 1 per second, `min_frames = 2`): detection ≈ 2.7 s after
  the box started moving; `Sentinel.exe motion-test` output: 0.00 % for the still scene, 6–12 % with
  the box.
