# Window review, 2026-11-17 (0.3.18)

Owner request: a full look over the window, so that it reads as a finished tool for this use, easy for the
person running the laptop and with the advanced settings still there. Every page was rendered (light and dark,
operator and administrator, at 980x620, 1280x760 and 1536x800 logical pixels, Windows 11 style at 125 %) and
compared before and after. Navigation, page structure, colours, icons, the status bar and the System tabs were
left as they are.

## Looked broken

| Was | Now |
|---|---|
| "Details" (Status page, and under every command result) turned into a solid blue block with unreadable text when opened | Drawn flat in every state (`disclosure_button`, `#disclosure` in `style.py`) |
| Live view had black bars at the sides, and on a wide window its caption was drawn over the picture: the Status page set the height from the width the panel had before the new layout | The panel sizes its own picture from the width it has: 16:9, at most 520 high, never wider than that height allows (`LiveView.resizeEvent`) |
| Status header values were cut off mid-word in a small window; "Archive" was cut at the normal size | Below 1000 px the facts go under the state; a value that still does not fit ends in "…" (`ElidedLabel`); the Archive fact is shorter ("SSD_1 · SSD_2 standing by") |
| "Not yet archived: 0 h" on the Drives page, in Status › Details and in the header | Dropped when zero (`archive_message`); a real backlog is still shown |
| Cut-off text: "Pause (do not recorc", "Saved (replaced a", the serial hint in the Erase dialog, the dates in Days off | Wide enough |
| Number fields as wide as the form | 170 px (`SettingsPage.editor`) |
| Blank tables (check results, events, gaps; the two schedule tables) | Say why they are empty, or what to press |
| Empty status-bar cells on the first-run screen | Hidden until Sentinel is set up |

## Clearer for the operator

* **Drives page:** one line per archive drive (name, state, free space), then the copier's message, then what a
  second drive does. It was one run-on sentence.
* **Alert details** are in words: "Since: 21:53 (7 min ago)", "Not recording for: 7 min", "Cause: camera
  disconnected (ffmpeg_error)". The recorder's own reason code stays in brackets for technical support.
* **Return to service…** (it deletes the drive's recordings) is not offered for an archive drive the running
  copier reports as in use, standing by, or full and not yet swapped. It was the first button offered for the
  drive in use. Unchanged for a drive back from the office, and when the copier is not running. The hint under
  the list says what the selected archive drive is doing.
* **Operators** no longer read "Read-only" on the Drives page while Swap works: the notice says what needs an
  administrator and that swapping does not.
* **Schedule:** a line says when the schedule is off; the empty time zone shows "same as the General tab (…)".
* **Wording:** "…already copied to an archive drive that has been taken out" (was "a REMOVED archive drive").

## Advanced settings, one click away

`AdvancedRows` hides rows of a form until "Advanced" is opened; hidden fields keep their values and are saved.

| Tab | Always shown | Under Advanced |
|---|---|---|
| Motion | mark motion, keep recording after motion, share of picture that must change | second stream path, brightness change, frames in a row, analysis rate, fail-safe, join markers |
| Storage | archive copying and its alarm; retention, newest kept, days between swaps; allow Erase and set up | free up to; serial characters to type, smallest drive accepted |
| Camera | label, address, port, stream path, account | transport, encrypted stream (opened by itself when either is not the default) |

## Polish

Tick boxes in forms start at the left edge; help text under a field no longer has a gap above and below
(`Note`); "Quick setup" is as wide as its words; "No action needed." stays on one line.

## Tests

`tests/unit/test_gui_app.py` (GUI review 2026-11-17 section): drive lines and the empty backlog, Return to
service per drive state, alert details, Advanced rows (hidden, saved, opened for a non-default camera setting),
the schedule-off line and empty tables, flat toggles, short number fields, the live view's size at three window
widths, the operator notice. Unit suite on the second development machine (Windows 11, Python 3.14.10): 1463
passed, 2 failed, 3 skipped; the 2 failures are `test_launcher` objecting to FFmpeg on a drive any user can
write (the checkout is on a removable drive there), not to this change.

## NOT executed / not verified

* **The integration tests were not run on this change.** The second development machine has only the LGPL FFmpeg
  bundled for release, which has no libx264; the simulated camera needs it, so every test that records fails
  there with "no media". They need a full FFmpeg on PATH (as on the first machine).
* Not seen in the built `Sentinel.exe` or on the laptop's own display; the renders were made from source.
