# Motion-triggered recording and motion markers (2026-11-10)

Owner request: record all the time during set working hours; outside them record only when motion
is detected, for a set time after the detection, restarting that timer at every new detection.
Camera in use: MAXSUR M961 series (e.g. M961-5-CP-IP: 5 MP, H.264, multi-streaming, ONVIF
Profile S/T, built-in motion detection).

## Motion markers for reviewers (all recordings)

Owner request: show reviewers when there is motion during a recording. With `[motion] markers = true`
(written by first-run setup; code default off) the detector runs all the time, including working
hours and without any "motion" schedule, and never starts or stops recording by itself.

* **Recording log.** Every stretch of motion is one `motion_period` event (start = first moving
  frame, end = last moving frame, peak share of the picture changed), joined when less than
  `marker_merge_s` (10 s) apart and split every 5 minutes so a crash loses at most the current
  part. Events are in the hash-chained, insert-only log, like every other event.
* **Review drive.** The export plan freezes the motion periods that fall in the exported videos.
  `Export_Summary.html` gets a column **"Motion (time into the video)"**, e.g.
  `0:00:21–0:00:28, 0:00:41–0:00:46`, so the reviewer can jump straight there (up to 12 per video;
  the rest are in the manifest). `Export_Manifest.json` has `motion` (each period with local times and
  `in_videos: [{video, from_s, to_s}]`), `derivatives[].motion`, and `motion_note`.
* **Operator screen.** The 24-hour timeline marks motion with blue ticks along the top of the bar
  (legend "Motion"; at least 6 px wide, so a few seconds of motion stays visible) and the summary
  line says "motion seen N times".
* Wording everywhere: motion times are approximate (start watching a few seconds earlier) and do not
  show that nothing happened at other times.

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

## No-motion fail safe (0.3.21, owner request 2026-11-18)

The fail safe above covers a detector that gets **no pictures**. It does not cover one that gets pictures
and no longer notices movement: the camera was moved or covered, the second stream shows a frozen picture,
the settings are too coarse for the scene. Nothing looks broken, and nothing is recorded outside the
recording hours.

Settings › Motion › **If no motion is seen for a long time** (off by default):

    [motion]
    no_motion_days = 2                    # 0 = off; up to 60
    no_motion_count_days = ["weekdays"]   # mon..sun, "weekdays", "weekend", "daily"

* **The count.** `sentinel.motion.QuietWatch` adds up the time the detector has been getting pictures
  without confirming any motion. It looks at the detector all the time, in recording hours too, so an
  ordinary working day sets it back to zero many times. Any confirmed motion sets it to zero.
* **Not counted:** days of the week outside `no_motion_count_days`; schedule pauses; **the schedule's
  particular dates** (Schedule › Days off…, holidays: `[[schedule.dates]]`, whatever their action), because
  nobody is expected to move then; time without pictures (that is W-001); time the recorder was not running.
* **At the limit** (`no_motion_days` × 24 h of counted watching) the recorder records all the time in motion
  periods: alert **W-003** "No motion has been seen for a long time, so the recorder records all the time",
  recording-log entry `motion_quiet_fail_safe` (with the hours watched and the last motion), and the
  recording that starts is logged as `motion_record` with `why = no_motion_fail_safe`.
* **It ends by itself** with the next confirmed motion (`motion_quiet_ended`): the detector works after
  all. A detector that really is blind never sees motion, so recording stays on until someone puts the
  camera or the settings right.
* **Status › Schedule and motion › No-motion fail-safe** shows the count ("no motion for 5 h of the 2 days
  allowed") and whether it is counting now.
* The count is kept in `motion_quiet.json` in the data folder across restarts of the recorder, and deleted
  when the option is switched off.
* Example with the defaults (2 days, Monday to Friday): last motion Friday 17:30; Saturday and Sunday are
  not counted; with nothing seen on Monday and Tuesday it switches on Tuesday 17:30.

Limits: a public holiday that is not entered as days off counts like any working day, so two of them in a
row switch it on (recording all the time is the safe side; it costs disk space, about 2.5 times the weekly
amount with the usual working-hours schedule). A detector that still fires on something that is not the
scene (the camera's day/night switch once an evening) looks like one that works. Recording all the time
fills the drives sooner; the storage and archive alerts (S-, A-, L-) apply as usual.

Neither key is written to the settings file while the option is off, so 0.3.20 and earlier (which refuse a
key they do not know) can still read the file; with it on, both keys have to be removed before going back.

## Lighting, dust and insects (0.3.22, owner request 2026-11-18)

The site: a camera in a ceiling tile, a room with windows and **one light switch**. The owner's rule: the
light going on or off means someone is there, so it must record; daylight getting brighter or dimmer must
not, and neither should dust or an insect passing the lens.

Settings › Motion › **Lighting** (off by default):

    [motion]
    light_aware = true
    light_change_percent = 60     # only written when it is not 60 (20..100)

* **The light being switched is motion at once.** A change of at least `light_change_percent` of the picture
  between two analysed frames starts recording immediately, whatever "Frames in a row" says. Without the
  option a light that changes in one step is ONE changed frame and is missed with "Frames in a row" at 2,
  unless the camera goes on adjusting its exposure in the next frame (likely on a real camera, never on the
  simulated one). The recording that starts is logged as `motion_record` with `trigger = "light"`
  (`"movement"` otherwise).
* **An even brightening or darkening of less than that share is cancelled out** before the frames are
  compared (`MotionAnalyzer.even_change_removed`): the earlier frame is made as bright as the later one is
  on average, and a point counts only if it differs both from the earlier frame as it was and from the
  brightened one. So cancelling can remove a change, never add one (a window that stays white while the
  room darkens is not a change). Example: the room 20 % brighter, half the picture above the brightness
  threshold: 50 % "changed" before, 0 % after.
* **Whether or not the option is on,** a change of the whole picture, and the three seconds after it while
  the camera settles, do not set the no-motion count back (previous section): only movement in part of the
  picture shows that the detector still notices movement. This is what keeps a camera that changes to night
  vision every evening from looking like one that works.
* **The live test** (Settings › Motion, or `Sentinel.exe motion-test`) shows `LIGHT` in the second in which
  the whole picture changed, and ends with how often that happened. Switch the room light off and on while
  it runs.

**Dust and insects** need no new option; two existing settings do it, tuned with the live test:
"Share of picture that must change" (a speck or a small insect covers far less of a ceiling camera's
picture than a person: try 2-3 %), and Advanced › "Frames in a row" (at 4 analysed frames a second, 4 means
the change must last a second; dust and an insect flying past are gone sooner).

Limits. What this does **not** defeat: an insect on or right at the lens (large, blurred and it stays: the
same as a person to this detector); light that really moves in part of the picture (a sun patch cut by a
fast cloud edge, shadows of branches or blinds in the wind, headlights sweeping across the room); only
ignore zones for those parts of the picture would, and they are not built. Slow changes of daylight were
never a problem (frames a quarter of a second apart barely differ). With the option on, anything that
changes most of the picture at once is motion: the camera changing to night vision, someone standing
directly under the lens. That costs one recording of `keep_recording_s`, and loses nothing.

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
* `tests/unit/test_motion_quiet.py` (0.3.21): the no-motion count (what counts, the limit, motion setting
  it back, a long gap between two looks, kept across restarts, an unreadable or unwritable count), the
  recorder's step with days off and weekends, W-003 only while video is recorded. Window tests: the option
  on the Motion page (not written while off, refused with no day picked), the Status line.
  `tests/integration/test_motion_service.py::test_no_motion_for_too_long_records_all_the_time_until_motion_is_seen`
  (real recorder, simulated camera, a limit of 15 s): still scene, recording starts by the fail safe with
  W-003, the box moves, the fail safe ends, recording stops `keep_recording_s` after the motion.
  **Not tried:** on the real camera; over real days (the tests use seconds); with the Windows services.
* Full suite on 0.3.21 on the second development machine (2026-11-18, evening): 1621 passed, 2 skipped,
  1 failed: `test_time_accuracy.py::test_absorbed_freeze_is_still_a_gap_under_drift` ("the absorbed jump
  leaked into the drift correction"), the test that fails now and then on that machine. That evening it
  failed most of the time, on the unchanged version as well: run alone and in turn, 0.3.20 passed 3 times
  out of 10 and 0.3.21 once out of 15 (the last ten of those with the Sentinel installed on that machine
  stopped, which made no difference). With counts this small the two cannot be told apart, and no code of
  0.3.21 runs in this test (motion recording is off in it). In every failure seen, the recorder reports its
  time correction as rate-limited at about 10 s into the run ("measured reception time moved faster than
  ... ppm at stream time 10.0 s / 10.5 s"); the same line appears in a motion test whose run is 10 s long.
  **Open:** why the estimate jumps there on that machine and whether it does on the laptop; to be looked at
  with a full run on the first development machine, idle, before the next keyed release.
* Full suite on 0.3.22, same machine, later that night: 1659 passed, 2 skipped, 2 failed: the timing test
  above, and `test_archive_drive.py::test_swap_during_active_recording_then_next_drive` ("timed out after 60s
  waiting for 3 sealed segments"). The second ran in the minutes in which a 340 MB release folder was being
  copied to the removable drive and checked; alone it then passed 3 times out of 3, and its whole file
  passed.
* Full suite on 0.3.23 (0.3.22 plus the user guide), same machine, **left alone for the whole run**: 1672
  passed, 2 skipped, none failed, the timing test included. So far that fits the failures above being the
  machine in use, not the code; it is one run.
* Full suite on 0.3.24 (0.3.23 plus the wording of a full-folder install; no recorder code changed), same
  machine the next morning, left alone except for one file search: 1683 passed, 2 skipped, 1 failed: the same
  timing test. Alone afterwards it failed 4 times out of 4 on 0.3.24. Then in turn, nothing else running:
  unchanged 0.3.23 passed 1 of 3, 0.3.24 passed 0 of 3. So the idle machine does **not** make it pass, as the
  0.3.23 run above had suggested, and the unchanged version fails too. The question stays open as written
  above; this machine cannot be used to clear that test.
* Full suite on 0.3.25 (0.3.24 plus how the window shows a recorder that was stopped; no recorder code
  changed), same machine that evening, left alone for the whole run: 1702 passed, 2 skipped, 1 failed: the
  same timing test again ("the absorbed jump leaked into the drift correction"). Not run again in turn with
  0.3.24 this time; the comparison of that morning stands.
* `tests/unit/test_motion_light.py` (0.3.22): a clean light switch is one changed frame (missed without the
  option, motion at once with it); an even change of brightness cancelled out; someone moving while the
  brightness changes; cancelling never adds a change; the detector's two counts (all motion / movement in
  part of the picture) and the settling time; the live test's `LIGHT`. Window test: the option on the Motion
  page (nothing written while off). `tests/integration/test_motion_service.py::
  test_the_room_light_being_switched_starts_a_recording_at_once` (real recorder; the simulated camera's
  still scene made brighter in one step at 20 s and darker again at 45 s): two recordings, each started by
  the light. **Not tried:** on the real camera and room; with real daylight; how this camera's exposure
  behaves when the light is switched.
* Measured with the simulator (key frames, 1 per second, `min_frames = 2`): detection ≈ 2.7 s after
  the box started moving; `Sentinel.exe motion-test` output: 0.00 % for the still scene, 6–12 % with
  the box.
