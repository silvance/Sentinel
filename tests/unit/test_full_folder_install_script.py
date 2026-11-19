"""packaging/windows/INSTALL-FULL-FOLDER.cmd: installing a complete program folder without an update file
(docs/08_PACKAGING.md section 10). The script needs an administrator and a real installed program, so what it
does was tried by hand (2026-11-18); these are the mistakes a batch file hides until the day it is needed."""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "packaging" / "windows" / "INSTALL-FULL-FOLDER.cmd"


def _text() -> str:
    raw = SCRIPT.read_bytes()
    assert all(b < 127 for b in raw), "plain ASCII only: the console code page of the target computer is not known"
    return raw.decode("ascii").replace("\r\n", "\n")


def test_every_goto_leads_to_a_label_and_every_label_is_used() -> None:
    lines = [ln.strip() for ln in _text().split("\n")]
    code = [ln for ln in lines if not ln.lower().startswith(("rem ", "echo "))]
    labels = {ln[1:].lower() for ln in code if re.fullmatch(r":[A-Za-z_]\w*", ln)}
    gotos = {m.group(1).lower() for ln in code for m in re.finditer(r"\bgoto\s+:?(\w+)", ln, re.I)}
    assert gotos - labels == set(), "goto to a label that does not exist ends the script mid-install"
    assert labels - gotos == set()


def test_no_parenthesised_blocks_so_a_path_with_brackets_cannot_end_one() -> None:
    # "C:\Program Files (x86)\..." inside an if ( ... ) block closes the block at its ")".
    for ln in _text().split("\n"):
        s = ln.strip().lower()
        if s.startswith(("rem ", "echo ")):
            continue
        assert not s.endswith("(") and not s.startswith(")"), ln


def test_it_switches_with_sentinels_own_command_and_says_why() -> None:
    from sentinel.update.apply import FOLDER_INSTALL_REASON

    text = _text()
    assert '"%APP%\\Sentinel.exe" update install --yes --reason "%WHY%"' in text
    # an installed version that has no "install" yet: the same swap by its older name, its words kept off the window
    assert '"%APP%\\Sentinel.exe" update rollback --yes --reason "%WHY%" %EXTRA% >"%CHECK%" 2>&1 <nul' in text
    why = re.search(r'set "WHY=([^"]+)"', text).group(1)
    assert "no update file" in why
    # ... and the new version tells from this reason that it was installed, not gone back to (cmd_finish)
    assert why.startswith(FOLDER_INSTALL_REASON + " of %NEWVER%")
    # owner 2026-11-19: nobody is asked to type BACK, and "going back" is said only to explain an older window
    assert "BACK and press" not in text and "Type YES and press Enter" in text
    said = [ln for ln in text.split("\n") if ln.lstrip().lower().startswith(("echo", "if")) and "going back" in ln.lower()]
    assert len(said) == 2 and all('"%VIA%"=="rollback"' in ln or "What the installed Sentinel said" in ln for ln in said)
    # checked before and after copying, copied as a mirror, and never from inside a program folder
    assert text.count("about --verify") == 2 and "/MIR" in text
    assert text.index('cd /d "%SystemRoot%"') < text.index('"%NEW%\\Sentinel.exe" update status')
    # the version is read from the folder beside the script, not written into it
    assert not re.search(r"\b\d+\.\d+\.\d+\b", text.replace("127.0.0.1", ""))


# ---------------------------------------------------------------------- the script itself, run
# The real thing needs an administrator and swaps a program folder. Here the script runs as it is, except that
# each Sentinel.exe is a stand-in batch file and the administrator check is skipped: what is under test is the
# script's own path through an install - what it asks, which command it gives, and what it lets onto the window.
STUB = r"""@echo off
setlocal EnableExtensions
set "HERE=%~dp0"
set "KEPT=%HERE:~0,-1%.previous"
set /p VER=<"%HERE%version.txt"
>>"%HERE%..\calls.txt" echo %*
if "%~1 %~2"=="update status" goto status
if "%~1 %~2"=="about --verify" goto verify
if "%~1 %~2 %~3"=="update install --help" goto help
if "%~1 %~2"=="update install" goto install
if "%~1 %~2"=="update rollback" goto rollback
exit /b 9
:status
echo Installed: Sentinel %VER%
set "PV=none"
if exist "%KEPT%\version.txt" set /p PV=<"%KEPT%\version.txt"
echo Previous version kept: %PV%
exit /b 0
:verify
echo Sentinel %VER% (stand-in)
echo installed files match the build manifest
exit /b 0
:help
if exist "%HERE%knows-install.txt" exit /b 0
echo Sentinel update: error: argument cmd: invalid choice: 'install' 1>&2
exit /b 2
:install
if not exist "%HERE%knows-install.txt" exit /b 2
if exist "%HERE%refuse.txt" goto refused
echo The install continues in a new window; this one closes now.
goto swap
:rollback
if exist "%HERE%refuse.txt" goto refused
echo Going back continues in a new window; this one closes now.
:swap
copy /y "%KEPT%\version.txt" "%HERE%version.txt" >nul
exit /b 0
:refused
echo Sentinel did not go back to the previous version: the recorder did not stop. Recording was started again.
exit /b 2
"""


def _run_script(tmp_path: Path, *, knows_install: bool, args: tuple[str, ...] = ("--yes",), typed: str = "",
                refuse: bool = False, script: Path = SCRIPT):  # noqa: ANN202
    import os
    import subprocess

    stick, app = tmp_path / "stick", tmp_path / "pc" / "Sentinel"
    for folder, version in ((stick / "Sentinel", "9.9.2"), (app, "9.9.1")):
        folder.mkdir(parents=True)
        (folder / "Sentinel.exe").write_bytes(b"")              # the script looks for it; the stand-in is run
        (folder / "Sentinel.cmd").write_bytes(STUB.replace("\n", "\r\n").encode("ascii"))
        (folder / "version.txt").write_text(version + "\n", "ascii")
    (stick / "Sentinel" / "knows-install.txt").write_text("x", "ascii")      # the new version always does
    if knows_install:
        (app / "knows-install.txt").write_text("x", "ascii")
    if refuse:
        (app / "refuse.txt").write_text("x", "ascii")
    text = script.read_bytes().decode("ascii")
    text, n = re.subn(r'(?m)^"%(APP|NEW|PREV)%\\Sentinel\.exe" ', r'call "%\1%\\Sentinel.cmd" ', text)
    assert n >= 10 and "Sentinel.exe\" update" not in text
    assert "if errorlevel 1 goto not_admin" in text
    text = text.replace("if errorlevel 1 goto not_admin", "rem (test: runs without an administrator)")
    run = stick / script.name
    run.write_bytes(text.encode("ascii"))
    temp = tmp_path / "temp"
    temp.mkdir()
    env = {**os.environ, "TEMP": str(temp), "TMP": str(temp)}
    done = subprocess.run(["cmd.exe", "/d", "/c", str(run), str(app), *args], input=typed, capture_output=True,
                          text=True, timeout=120, env=env)
    calls = (app.parent / "calls.txt").read_text("ascii").split("\n")
    return done, [c.strip() for c in calls if c.strip()], (app / "version.txt").read_text("ascii").strip()


windows_only = __import__("pytest").mark.skipif(__import__("sys").platform != "win32", reason="a Windows batch file")


@windows_only
def test_an_install_never_says_going_back_when_the_installed_version_knows_install(tmp_path: Path) -> None:
    done, calls, version = _run_script(tmp_path, knows_install=True, args=(), typed="yes\n")
    assert done.returncode == 0, done.stdout + done.stderr
    assert version == "9.9.2" and "DONE: Sentinel 9.9.2 is installed" in done.stdout
    switch = [c for c in calls if c.startswith(("update install --yes", "update rollback"))]
    assert len(switch) == 1 and switch[0].startswith('update install --yes --reason "Full program folder install of 9.9.2 ')
    assert "going back" not in done.stdout.lower() and "BACK" not in done.stdout
    assert "Type YES and press Enter to install Sentinel 9.9.2 now" in done.stdout


@windows_only
def test_an_older_installed_version_is_asked_by_its_old_command_and_its_words_stay_off_the_window(
        tmp_path: Path) -> None:
    done, calls, version = _run_script(tmp_path, knows_install=False)            # --yes: no question
    assert done.returncode == 0, done.stdout + done.stderr
    assert version == "9.9.2" and "DONE: Sentinel 9.9.2 is installed" in done.stdout
    assert calls.index("update install --help") < len(calls) - 1
    switch = [c for c in calls if c.startswith(("update install --yes", "update rollback"))]
    assert len(switch) == 1 and switch[0].startswith('update rollback --yes --reason "Full program folder install of 9.9.2 ')
    assert "Going back continues" not in done.stdout and "Type YES" not in done.stdout
    # said once, on purpose: the older version's own window will use the words, and that must not alarm anyone
    assert done.stdout.lower().count("going back") == 1 and "it is installing 9.9.2" in done.stdout


@windows_only
def test_without_a_yes_nothing_is_stopped_or_changed(tmp_path: Path) -> None:
    done, calls, version = _run_script(tmp_path, knows_install=True, args=(), typed="\n")
    assert done.returncode == 1 and version == "9.9.1"
    assert "Not confirmed" in done.stdout and "The installed program was not changed." in done.stdout
    assert not [c for c in calls if c.startswith(("update install --yes", "update rollback"))]


@windows_only
def test_a_refusal_from_an_older_version_is_shown_with_what_it_said(tmp_path: Path) -> None:
    done, _calls, version = _run_script(tmp_path, knows_install=False, refuse=True)
    assert done.returncode == 1 and version == "9.9.1"
    assert "What the installed Sentinel said" in done.stdout and "the recorder did not stop" in done.stdout
    assert "Sentinel did not start the install" in done.stdout and "DONE" not in done.stdout


def test_batch_files_are_checked_out_with_windows_line_endings() -> None:
    rules = (ROOT / ".gitattributes").read_text("utf-8").split("\n")
    assert "*.cmd text eol=crlf" in [r.strip() for r in rules]
