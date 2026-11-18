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
    text = _text()
    assert '"%APP%\\Sentinel.exe" update rollback --reason "%WHY%"' in text
    assert "no update file" in re.search(r'set "WHY=([^"]+)"', text).group(1)
    # checked before and after copying, copied as a mirror, and never from inside a program folder
    assert text.count("about --verify") == 2 and "/MIR" in text
    assert text.index('cd /d "%SystemRoot%"') < text.index('"%NEW%\\Sentinel.exe" update status')
    # the version is read from the folder beside the script, not written into it
    assert not re.search(r"\b\d+\.\d+\.\d+\b", text.replace("127.0.0.1", ""))


def test_batch_files_are_checked_out_with_windows_line_endings() -> None:
    rules = (ROOT / ".gitattributes").read_text("utf-8").split("\n")
    assert "*.cmd text eol=crlf" in [r.strip() for r in rules]
