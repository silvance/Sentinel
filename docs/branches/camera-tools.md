# Two camera tools: picture-and-sound test, ONVIF probe (0.3.22, 2026-11-18)

Both are for the person installing or supporting the recorder. Neither touches the recorder, the recordings
or the recording log, and neither changes anything on the camera.

## Test picture and sound

Owner request: "some way to quickly test audio and video. The live view is okay, but the 5 second frames
don't help for audio." The site is waiting for a microphone to be soldered onto the camera and for new
firmware; this is what to press when they arrive.

Settings › Camera › **Test picture and sound** (or `Sentinel.exe av-test [--seconds N]`, 2 to 60, default 10).

1. One short FFprobe question to the camera: what does it send?
2. FFmpeg records the next ten seconds straight from the camera into one small MP4: the video copied as it
   is, the first sound stream (if there is one) converted to AAC, as the recorder does, so that any player
   opens it.
3. It says what arrived:
   * `Picture: H.264, 1920x1080, 30 pictures a second; 10.0 s received (300 pictures).`
   * `Sound: the camera sends G.711 A-law, 8000 Hz, 1 channel. In this test: loudest -5 dB, average -27 dB
     (0 dB is the loudest possible). Play the clip to hear it.`
   * or `... but it is SILENT in this test (loudest -91 dB). Check the microphone and the camera's own sound
     settings.` A sound stream that is there and carries nothing is what a camera with its microphone input
     switched on and no microphone sends; -60 dB and below is called silent.
   * or `Sound: the camera sends no sound.`
   * and, when the camera sends sound, whether Sentinel is set to record it.
4. **Play the test clip** opens it in the computer's own player.

The clip is **not evidence**: it never enters the recordings, the recording log or an archive drive. It holds
ten seconds of the scene with sound, so it does not stay: it is in the user's temporary folder
(`Sentinel-camera-test\camera-test.mp4`), is replaced by the next test, and is deleted when the Camera page
is left and when the window is next opened. A player that still has it open keeps it until it is closed.
Run from the command line, nothing removes it until the next test: the command says so and asks for it to be
deleted by hand.
The test works whether or not sound recording is switched on; switching that on remains a separate step
with its own question (docs/00 R5).

It is a second connection to the camera's main stream, like Test connection. A camera that allows only one
viewer of that stream would refuse it (or drop the recorder); none has been seen to.

Tests: `tests/unit/test_avtest.py` (what it says for each case, the two FFmpeg commands, the clip's place and
removal, nothing left when the camera does not answer, the account never shown),
`tests/integration/test_av_test.py` (simulated camera with a tone: picture and sound arrive, the clip holds
H.264 and AAC; a camera without sound; a camera that is not there), a window test for the button.
**Not tried:** on the real camera; G.711 sound (the simulated camera cannot send it); the "silent" case with
a real stream; which player Windows opens on the laptop.

## ONVIF probe (read-only)

Owner question: the camera's own page can set motion, zoom and so on; can Sentinel? Answer given: reading,
yes, through ONVIF, the protocol most cameras speak beside their own page; writing is patchy between makers
and would put a camera administrator's password on the laptop. The first step is to find out what this
camera answers. A reseller's page lists the KNC-HMSS347 as ONVIF Profile S and T; the camera's own page has
an ONVIF setting that offers MD5 or SHA-256 (the hash used to sign in, not anything about the video).

`Sentinel.exe onvif-probe [--port N] [--user NAME] [--host ADDRESS] [--out FILE]`

* Asks the camera in the settings (or `--host`), with the account Sentinel already uses for it, or `--user`
  (the password is then asked for, not shown, not kept).
* Looks for ONVIF on ports 80, 8080, 8000, 8899 and 2020 unless `--port` is given.
* Sends only these questions, all of which read: `GetSystemDateAndTime`, `GetDeviceInformation`, `GetScopes`,
  `GetServices`, `GetCapabilities`, `GetProfiles`, `GetStreamUri`, `GetVideoSources`, `GetEventProperties`,
  `GetImagingSettings`. The code cannot send anything else (`sentinel.onvif.QUESTIONS`; a test checks it).
* Signs in both ways cameras ask for: WS-Security UsernameToken (password digest, with the camera's own
  clock) and HTTP digest (RFC 7616; SHA-256 if the camera offers it, otherwise MD5). The password itself is
  never sent, shown or written.
* Shows: the camera's clock against this computer's; maker, model, firmware, serial number; the ONVIF
  profiles it says it conforms to; each stream with codec, size, picture rate, bit rate, key-frame interval,
  sound and its address; the events it can send, with motion and tamper picked out; the picture settings;
  and what did not work.
* Saves the whole report, with the camera's answers as it gave them, to
  `<log folder>\onvif-probe-<date>-<time>.txt` (or `--out`). That file goes to whoever adapts Sentinel to
  the camera. Accounts inside stream addresses are removed from it.
* Exit code 0: it answered as an ONVIF device; 4: no ONVIF answer; 5: it speaks ONVIF but did not accept the
  account (many cameras keep ONVIF users apart from the web page's users).

What this is for, and what it is not: with the report in hand, Sentinel could later (a) write the camera's
settings into the recording log at every start and warn when they change, and (b) take the camera's own
motion and tamper events as a second trigger. Neither is built. Changing camera settings from Sentinel is
not planned.

Only the Python standard library is used (no new package in the build). An answer larger than 2 MB, or one
that declares a document type, is refused.

Tests: `tests/unit/test_onvif_probe.py`: HTTP digest against the examples in RFC 7616 (MD5 and SHA-256); the
stronger challenge chosen; the WS password digest; only questions can be sent; answers that are not plain
ONVIF refused; the whole report against a pretend camera for each way of signing in (and with the camera's
clock an hour off); a wrong account; no ONVIF; over real HTTP on this computer; the command, the saved file,
and that no password is in either. **Not tried against any real camera.** Real cameras differ in the
details, which is what the saved answers are for.
