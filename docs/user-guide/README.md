# The user guide

`Sentinel User Guide.pdf` is the guide for the people who use the recorder: is it recording, changing the
archive drive, watching recordings, what to do when the screen shows a problem. The build puts it at the top
of the program folder beside `Sentinel.exe`, and **Help › User guide (PDF)** (F1) opens it.

Owner request, 2026-11-18: "a pdf user guide that you update with any major changes that affect the end user
as we improve the tool", at the top level of the tool folder. The people who use the recorder have not been
trained on it, and the main user will hand over to someone else for a while.

## Changing it

1. Edit `user-guide.html` (the text) in this folder.
2. `python packaging/userguide.py` makes the PDF and `fingerprint.json`.
3. Commit all three.

`python packaging/userguide.py --check` says whether the PDF is the one for the guide as it is now.

## What keeps it up to date

The build refuses, and `tests/unit/test_user_guide.py` fails, when:

* the guide has no entry under **What changed** for the current version. Every version gets one, even if it
  says only that nothing changed for the people who use the recorder;
* words the guide marks as shown on the screen (`<b class="ui">...</b>`) are no longer in the program: a
  renamed button has to be renamed in the guide;
* the PDF was made from an earlier text, an earlier version number or an earlier list of messages.

The list of messages and codes at the end is not written by hand: it is taken from `sentinel.alerts.CATALOG`
when the PDF is made.

## What belongs in it

Things a person at the recorder does or sees. A change belongs under "What changed" when it alters what they
see, press or have to know: a renamed or moved button, a new message, a different folder on a drive, a new
step. Settings only the installer touches get a line in part 6 at most. How it works inside belongs in
`docs/`, not here.

There are no screenshots yet. The text is Letter size, made with Qt (`QTextDocument` to `QPdfWriter`), which
the build and test environments already have.
