# Recording the camera's sound (0.3.20, 2026-11-18)

Requirement change, at short notice: the customer now wants sound. The camera (KNC-HMSS347-iP4) is to get a
microphone soldered on and new firmware; neither was available when this was written, so **nothing here has
been tried against the real camera's sound**.

Sound stays **off by default**. Whether sound may be recorded is a collection-scope decision of the deployment
(docs/00 R5, docs/02 A7, docs/07 checklist); the window asks once when it is switched on, and every recorder
start with it on is an `audio_enabled` entry in the recording log.

## What the option does

Settings › Camera › **Record the camera's sound with the video** (`[camera] include_audio = true`).

* **The video is stored exactly as before**: copied, never re-encoded.
* **The sound is converted to AAC** by the recorder (`-c:v copy -c:a aac -b:a 96k`; FFmpeg lowers the bit rate
  where the sample rate allows less: 48 kbit/s at 8 kHz). This is the default because of what most cameras send:

  | Camera sends | Copied unchanged into the recording format (MPEG-TS) | Converted to AAC |
  |---|---|---|
  | G.711 A-law, 8 kHz (measured, bundled FFmpeg 9.0.2 LGPL) | written as an unlabelled data stream; reads back as `bin_data`, **not as sound** | AAC, 8000 Hz, 1 channel |
  | AAC | AAC, as sent | AAC, re-encoded |
  | no sound | video only | video only (the option changes nothing) |

  The converted sound is the recorded evidence item: there is no copy of the camera's original sound bytes.
* Advanced › **Sound format** › *Keep the camera's own format* (`audio_format = "copy"`) for a camera that sends
  AAC. With any other format the check below reports that the recordings have no sound.
  The default is not written into the settings file, so 0.3.19 and earlier (which refuse a key they do not
  know) can still read it; `audio_format = "copy"` would have to be removed before going back to them.

## Sound never costs the video

A camera that announces a sound stream and then sends none keeps FFmpeg waiting for it: nothing at all is
written. `recorder/audio.AudioGuard`: when two recording attempts in a row that included sound got nothing
**while the camera itself answers** (one short FFprobe question after such an attempt), the next attempt is
made without sound. If that one records, sound was the cause: it stays off until the recorder is restarted,
with **N-002** on the screen and `audio_suspended` in the log. If it does not record either, the camera is the
problem and sound is tried again. An ordinary outage (camera not answering) is never blamed on the sound.
Cost when it happens: about two connection time-outs of video, once per recorder start.

## Knowing whether sound is really recorded

* **Test connection** (Camera page) now ends with "It sends sound: G.711 A-law, 8000 Hz, 1 channel." or "It
  sends no sound." This is the quickest check after the microphone and firmware are in.
* **Status › Recording › Sound**: about ten seconds into every recording the recorder reads its own file back and
  says what is in it: "Recorded (AAC, 8000 Hz, 1 channel; converted by the recorder)", or "NOT recorded: the
  camera sends no sound" with alert **N-001** (video continues). The first time, and whenever it changes, it is
  also an entry in the recording log (`audio_recorded` / `audio_missing`).
* Every sealed recording made while the option is on notes its sound stream in `validation_detail.audio`.
* **Review drive**: videos are joined only from recordings with the same sound, so a video has sound throughout
  or not at all. The stored AAC goes in unchanged; the summary and manifest say which videos have sound. The
  picture check against the originals is about the video only.

## Things to know

* In the first file after a (re)connection the sound starts a little before the first picture (the video can
  only start on a key frame; 0.7 s with the simulator). A review video that begins there shows its first
  picture that much after its start; times in the recording log are those of the pictures.
* Motion detection, the live view and all time measurements use the video only.
* The export's sampled picture check assumed the first picture of a video is at time zero; with sound in the
  recordings it is not. `frame_hashes_span` now always keeps the file's timestamps (`-copyts`), which changes
  nothing for videos that start at zero.

## Tested, and not

* `tests/integration/test_audio_recording.py` (real recorder, simulated camera with a tone): converted to AAC
  with the video copied and files contiguous and sealed; kept as sent (AAC); option on with a silent camera
  (N-001, video unaffected); option off with a camera that sends sound (no sound stored); a review drive whose
  videos have the sound and pass the picture check.
* `tests/unit/test_audio.py`: the guard's decisions, the FFmpeg options for each case, settings, status and
  alerts, what is noted with a sealed recording, the connection test's wording, review-video options. Window
  tests: the question before switching on, the default not being written, the Status line.
* Full suite on the second development machine on 0.3.20 (added after the commit that made the change): 1593
  passed, 2 skipped, 1 failed. The failure, `test_recorder_faults.py::
  test_corrupted_stream_data_does_not_stop_recording` ("corruption injected into the stream left no trace"),
  had not failed before; alone it then passed 5 times out of 5 on 0.3.20 and 5 out of 5 on 0.3.19. The machine
  was in use during the run (a screen-capture stream, MediaMTX and an installed Sentinel recording it). It is
  the third full run that day with a different single one-off failure in a real-time test (field report
  2026-11-18), so it is put down to the machine, without proof. Run the full suite on the first development
  machine, idle, before the next keyed release.
* First run outside the simulator (2026-11-18, the installed 0.3.20 on the second development machine, a
  screen capture sent by OBS through MediaMTX as RTSP): "Sound is recorded with the video (AAC, 48000 Hz,
  2 channels; converted by the recorder)". The source was AAC, not G.711.
* **Not tried:** the real camera's sound (format unknown until the firmware arrives); G.711 through the whole
  recorder (the simulator sends MPEG-TS, which cannot carry it: MP2 stood in for "not AAC"; G.711 itself was
  tried with FFmpeg directly, table above); a camera that announces sound and sends none (the guard is
  unit-tested only); sound over days (drift between the camera's sound and picture clocks); recording with the
  Windows services installed.
