"""packaging/windows/START-A-NEW-RECORDING-LOG.cmd: the file the owner starts to clear test recordings.

Field report 2026-11-20, from the laptop, minutes before real use: "The command did nothing the first time. It
popped up with instructions, then said press any key to continue. The window closed and nothing else." The
cause was not found (the first run left no trace), so the script is held to this instead: whatever happened is
still on the screen when the person looks, and no key pressed too early or too often can end it unread.

The script is run here as it is, with a stand-in for Sentinel.exe. A window and a keyboard cannot be had in a
test: those parts were tried by hand in a real console (docs/branches/req-operator-gui.md section 13)."""

from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "packaging" / "windows" / "START-A-NEW-RECORDING-LOG.cmd"
GATE = "if errorlevel 1 goto not_admin"

# What the stand-in does is written in files beside it: its exit code, and whether it knows the command.
STUB = r"""@echo off
setlocal EnableExtensions
set "HERE=%~dp0"
>>"%HERE%calls.txt" echo %*
if "%~1 %~2"=="new-recording-log --help" goto help
if "%~1"=="new-recording-log" goto run
exit /b 9
:help
if exist "%HERE%knows.txt" exit /b 0
echo Sentinel: error: argument cmd: invalid choice: 'new-recording-log' 1>&2
exit /b 2
:run
set /p RC=<"%HERE%rc.txt"
echo (the plan, the question and what was done would be here)
exit /b %RC%
"""


def _text() -> str:
    raw = SCRIPT.read_bytes()
    assert all(b < 127 for b in raw), "plain ASCII only: the console code page of the target computer is not known"
    assert raw.count(b"\r\n") == raw.count(b"\n"), "Windows line endings: cmd misreads labels otherwise"
    return raw.decode("ascii").replace("\r\n", "\n")


def _code() -> list[str]:
    return [ln.strip() for ln in _text().split("\n") if ln.strip() and not ln.strip().lower().startswith("rem ")]


def test_every_goto_leads_to_a_label_and_every_label_is_used() -> None:
    code = [ln for ln in _code() if not ln.lower().startswith("echo ")]
    labels = {ln[1:].lower() for ln in code if re.fullmatch(r":[A-Za-z_]\w*", ln)}
    gotos = {m.group(1).lower() for ln in code for m in re.finditer(r"\bgoto\s+:?(\w+)", ln, re.I)}
    assert gotos - labels == set(), "a goto to a label that does not exist ends the script, and its window"
    assert labels - gotos == set()


def test_no_parenthesised_blocks_so_a_path_with_brackets_cannot_end_one() -> None:
    for ln in _code():
        if not ln.lower().startswith("echo "):
            assert not ln.endswith("(") and not ln.startswith(")"), ln


def test_the_window_never_closes_on_a_key_pressed_by_accident() -> None:
    code = _code()
    # One "press a key" only: before anything is looked at, where a key too many does no harm ...
    assert code.count("pause") == 1
    assert code.index("pause") < code.index('"%APP%\\Sentinel.exe" new-recording-log')
    # ... and the person is told to press once and then wait: nothing shows while the drives are counted.
    text = _text()
    assert "press a key ONCE" in text and "press nothing more" in text
    # Every way out goes through ":end", which waits for a word, not for any key, and asks again otherwise.
    end = code.index(":end")
    tail = code[end:]
    assert tail[-2:] == [":closed", "endlocal"]
    assert 'set /p "ANS= To close it, type CLOSE and press Enter: "' in tail
    assert sum(ln.endswith("goto wait_close") for ln in tail) == 3 and 'if /i "%ANS%"=="CLOSE" goto closed' in tail
    labels = [i for i, ln in enumerate(code) if ln.startswith(":")]
    for way_out in ("done", "not_admin", "no_app", "too_old"):
        start = code.index(f":{way_out}")
        section = code[start:min(i for i in labels if i > start)]
        assert section[-1] == "goto end", way_out
    assert code[code.index('set "RC=%ERRORLEVEL%"'):code.index(":done_with_notes")][-1] == "goto end"   # NOT DONE
    assert not any(re.match(r"exit\b", ln, re.I) for ln in code)
    # Without a keyboard (a test, a technician's own script) nothing waits.
    assert 'if "%KEYBOARD%"=="0" goto closed' in tail and '"%SYS%\\timeout.exe" /t 0 /nobreak >nul 2>&1' in code


def _run(tmp_path: Path, *, rc: int = 0, knows: bool = True, admin: bool = True, typed: str = ""):  # noqa: ANN202
    app = tmp_path / "pc" / "Sentinel"
    app.mkdir(parents=True)
    (app / "Sentinel.exe").write_bytes(b"")                     # the script looks for it; the stand-in is run
    (app / "Sentinel.cmd").write_bytes(STUB.replace("\n", "\r\n").encode("ascii"))
    (app / "rc.txt").write_text(f"{rc}\n", "ascii")
    if knows:
        (app / "knows.txt").write_text("x", "ascii")
    text = SCRIPT.read_bytes().decode("ascii")
    text, n = re.subn(r'(?m)^"%APP%\\Sentinel\.exe" ', r'call "%APP%\\Sentinel.cmd" ', text)
    assert n == 2
    assert text.count(GATE) == 1
    if admin:
        text = text.replace(GATE, "rem (test: runs without an administrator)")
    else:
        text = text.replace(GATE, "goto not_admin")
    run = tmp_path / "stick" / SCRIPT.name
    run.parent.mkdir()
    run.write_bytes(text.encode("ascii"))
    done = subprocess.run(["cmd.exe", "/d", "/c", str(run), str(app)], input=typed, capture_output=True, text=True,
                          timeout=120, env={**os.environ})
    calls_file = app / "calls.txt"
    calls = calls_file.read_text("ascii").split("\n") if calls_file.exists() else []
    return done, [c.strip() for c in calls if c.strip()]


windows_only = pytest.mark.skipif(sys.platform != "win32", reason="a Windows batch file")


@windows_only
def test_done_says_what_to_check_and_ends_without_a_keyboard(tmp_path: Path) -> None:
    done, calls = _run(tmp_path, rc=0)
    assert done.returncode == 0, done.stdout + done.stderr
    assert calls == ["new-recording-log --help", "new-recording-log"]
    out = done.stdout
    assert out.index("press a key ONCE") < out.index("One moment") < out.index("(the plan, the question")
    assert "NEXT: open Sentinel from its usual icon." in out and "NOT DONE" not in out
    assert "type CLOSE" not in out            # nobody to type it: the script ends (a person gets the question)


@windows_only
def test_a_run_that_was_refused_or_cancelled_says_not_done_in_a_box(tmp_path: Path) -> None:
    done, calls = _run(tmp_path, rc=3)
    out = done.stdout
    assert calls[-1] == "new-recording-log"
    assert "NOT DONE. Nothing was removed unless the lines above say so." in out and "NEXT: open Sentinel" not in out
    assert "To try again: close this window and start this file again." in out
    box = [ln for ln in out.split("\n") if ln.strip().startswith("=====")]
    assert len(box) == 3                      # the title's underline, then the box around NOT DONE


@windows_only
def test_done_with_something_left_over_says_so_and_still_what_to_check(tmp_path: Path) -> None:
    done, _calls = _run(tmp_path, rc=4)
    assert "See the lines above: something did not go as expected." in done.stdout
    assert "NEXT: open Sentinel from its usual icon." in done.stdout and "NOT DONE" not in done.stdout


@windows_only
def test_an_older_sentinel_is_named_and_not_asked_to_do_it(tmp_path: Path) -> None:
    done, calls = _run(tmp_path, knows=False)
    assert calls == ["new-recording-log --help"]
    assert "older than 0.3.27" in done.stdout and "invalid choice" not in done.stdout + done.stderr


@windows_only
def test_without_administrator_rights_it_says_not_started_and_how_to_start_it(tmp_path: Path) -> None:
    done, calls = _run(tmp_path, admin=False)
    assert calls == []
    assert "NOT STARTED: this needs administrator rights. Nothing was changed." in done.stdout
    assert 'RIGHT-click START-A-NEW-RECORDING-LOG.cmd and choose' in done.stdout
    assert "Before you go on" not in done.stdout
