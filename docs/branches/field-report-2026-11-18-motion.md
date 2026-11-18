# Field report 2026-11-18: "motion detection does not work" (0.3.19)

First day with the real camera (KNC-HMSS347-iP4, three streams: `stream0` main, `stream1`, `stream2`) and
0.3.18 on the laptop. Motion detection itself worked: the Recordings page listed eleven motion periods in the
first twenty minutes, from the key frames of the main stream. What made it look broken was the window.

| What was seen | Cause | Change |
|---|---|---|
| Live test (Settings › Motion): "Motion test: failed (exit code 62097)", no lines at all | The built program held everything it printed until it ended: through a pipe Python collects output in blocks, and the built program does not read `PYTHONUNBUFFERED`, which the window sets. Nothing appeared during the test; *Stop* (seen as the only way out) discarded it. 62097 (0xF291) is the code Qt gives a process it was asked to end | `logging_setup.line_buffered_output()` at the start of `Sentinel.exe` and `SentinelService.exe`: every line is written when it is printed. Applies to every command the window runs (drive setup, export, service install), not only this one |
| The same test from source showed its lines | From source the window's `PYTHONUNBUFFERED=1` is honoured; only the built program was affected, so no test saw it | `tests/unit/test_output_lines.py` runs the command through a pipe **without** `PYTHONUNBUFFERED` and checks the first line arrives well before the end (fails without the change) |
| Even when a test ran to its end, its lines were behind a small "Details" arrow | The one-line result with details on request suits commands that change something; here the lines are the result | The live test opens Details when it starts; a sentence above it says how to read a line |
| *Stop* read "failed (exit code …)" | Any non-zero exit code was a failure | A command stopped with the page's Stop button ends as "stopped." (`OutputLog.stop()`) |
| Second stream path `192.168.0.10:554/stream2` was accepted; the detector then read `rtsp://192.168.0.10:554/192.168.0.10:554/stream2` | Only spaces, `@` and `://` were refused | `motion.stream_path_only()`: an address is reduced to its path (an account in it is dropped). Used when the page saves **and** in `detection_url()`, so a value saved by 0.3.18 works without being edited. Validation was not tightened: a settings file that loaded before must still load after an update. The page shows "Detection will read rtsp://…/stream2" under the field |
| The second stream path was under *Advanced* (since 0.3.18) | GUI review 2026-11-17 put everything but three settings there | Back in sight: it is set per camera at installation, and decides how fast detection is |
| Status bar: "The recorder was restarted with the saved settings." drawn through "1 alert: T-002 · Storage: 430 GB free" | 0.3.18 hid the alert, storage and archive items before setup with `setVisible()` at **every** refresh; Qt hides those items while it shows a message, and the refresh showed them again on top of it | Their visibility is decided in one place (`AppWindow._sb_show_items`): set up, and no message showing; re-applied when the message changes |

## What to set for this camera

* Camera › Stream path: `stream0` (what is recorded).
* Motion › Second stream path: `stream2` (only the path). Detection then analyses 4 pictures a second of the
  low-resolution stream instead of only the key frames of the main stream.
* Schedule: "Record on motion" applies only in the periods that say so. Recording continues for *Keep recording
  after motion* (120 s) after the last movement, so with someone in view it never stops: leave the scene empty
  for longer than that to see "Watching for motion".

## Checked, and not

* Unit tests for each row above (`test_output_lines.py`, `test_motion.py`, the "field report 2026-11-18" section
  of `test_gui_app.py`); the pipe test was run with the change removed and failed as intended.
* The built 0.3.18 program was timed through a pipe on the second development machine: all ten lines of an 8 s
  test arrived together at the end; from source they arrived one a second.
* The built 0.3.19 program, timed the same way without `PYTHONUNBUFFERED`: one line a second. With
  `192.168.0.10:554/stream2` saved as the second stream path it says "Watching the second stream stream2".
* Full suite on the second development machine, twice, on 0.3.19 (added after the commits that made the
  change): 1567 passed, 2 skipped, 2 failed each time. `test_time_accuracy.py::
  test_absorbed_freeze_is_still_a_gap_under_drift` failed in both (it also fails on unchanged 0.3.16 there).
  The other failure differed: `test_service_process.py::test_hard_kill_restart_recovery_and_graceful_stop`
  in the first run (its failure message was not kept; it then passed 6 times out of 6 alone and in the second
  full run), `test_time_accuracy.py::test_fast_camera_stays_on_reception_time` in the second. The timing file
  was then run three times each on 0.3.19 and on unchanged 0.3.18, alternating: 2 failures in 12 tests on
  0.3.19, 3 in 12 on 0.3.18, the "fast camera" test failing on 0.3.18 too. So the timing failures belong to
  that machine (it was in use during these runs; the 0.3.17 and 0.3.18 runs were made on it idle), not to
  this change. The single hard-kill failure is unexplained. Run the full suite on the first development
  machine before the next keyed release.
* **Not known:** why Status showed "Recording normally" with no W-001 while the second stream path was the
  address. Either the camera serves a stream for a path it does not know (the detector then works, and people
  in view keep it recording), or W-001 was missed; it could not be told from the screenshots. To be checked on
  the laptop with the path corrected.
* Not tried against the real camera from here; `stream1` / `stream2` as the names of the other streams are taken
  from the address in use (`stream0`), not from the camera's documentation.
