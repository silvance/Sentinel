"""The user guide (owner request 2026-11-18, 0.3.23): a PDF at the top of the program folder for the people who
use the recorder, kept up to date with every change that matters to them. These tests are what keeps it up to
date: they fail when a version has no entry in it, when it names something that is no longer on the screen,
and when the PDF in the repository was made from an earlier text."""

from __future__ import annotations

import html
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "packaging"))
import userguide  # noqa: E402

from sentinel import __version__  # noqa: E402
from sentinel.alerts import CATALOG  # noqa: E402


def test_the_guide_in_the_repository_is_the_one_for_this_version() -> None:
    """If this fails after a change: bring docs/user-guide/user-guide.html up to date where the change matters
    to the people who use the recorder (and add this version under "What changed"), then run
    ``python packaging/userguide.py`` and commit the PDF and fingerprint.json with it."""
    assert userguide.problems() == []


def test_it_names_this_version_and_says_what_changed_in_it() -> None:
    text = userguide.source_text()
    assert f"<h3>{__version__}</h3>" in text and "{{VERSION}}" in text
    assert f"for Sentinel {__version__}" in userguide.full_html()
    assert "{{" not in userguide.full_html() and "<!--" not in userguide.full_html()


def test_what_it_says_is_on_the_screen_is_in_the_program() -> None:
    marked = userguide.ui_strings()
    assert len(marked) >= 30
    # the three things a user does, and the words they look for
    for words in ("Recording normally", "Watching for motion", "Swap and safely remove", "Safely remove",
                  "1 - Watch the recordings", "START HERE.txt", "Lock computer", "Diagnostic report…",
                  "User guide (PDF)"):
        assert words in marked, words
    program = userguide.program_text()
    assert all(s in program for s in marked)


def test_every_message_the_program_can_show_is_in_the_list() -> None:
    table = userguide.alerts_html()
    for code, d in CATALOG.items():
        assert f">{code}<" in table, code
        assert html.escape(d.headline) in table, code
        assert code[0] in userguide.LETTERS, f"the guide has no heading for codes that start with {code[0]}"
    assert table.count("<b>Recording stops.</b>") == sum(1 for d in CATALOG.values() if d.stops_recording)
    assert "Contact technical support and provide diagnostic report." in table      # "Call" is explained once
    assert table.count("Contact technical support and provide diagnostic report.") == 1


def test_a_renamed_button_is_noticed(monkeypatch: pytest.MonkeyPatch) -> None:
    real = userguide.source_text()
    monkeypatch.setattr(userguide, "source_text", lambda: real + '<p><b class="ui">A button nobody made</b></p>')
    found = userguide.problems()
    assert any('"A button nobody made", which is not in the program' in p for p in found)
    assert any("changed since the PDF was made" in p for p in found)          # and the text differs from the PDF's


def test_a_new_version_without_an_entry_is_noticed(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(userguide, "version", lambda: "9.9.9")
    found = userguide.problems()
    assert any('no entry for 9.9.9 under "What changed"' in p for p in found)
    assert any("changed since the PDF was made" in p for p in found)


def test_a_pdf_that_is_not_the_one_described_is_noticed(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    other = tmp_path / userguide.PDF.name
    other.write_bytes(userguide.PDF.read_bytes() + b"\n% changed")
    monkeypatch.setattr(userguide, "PDF", other)
    assert any("is not the file fingerprint.json describes" in p for p in userguide.problems())
    monkeypatch.setattr(userguide, "PDF", tmp_path / "missing.pdf")
    assert any("is missing" in p for p in userguide.problems())
    monkeypatch.setattr(userguide, "FINGERPRINT", tmp_path / "none.json")
    assert any("fingerprint.json is missing" in p for p in userguide.problems())


def test_the_pdf_is_a_pdf_of_several_pages() -> None:
    data = userguide.PDF.read_bytes()
    assert data[:5] == b"%PDF-" and len(data) > 30_000
    fp = json.loads(userguide.FINGERPRINT.read_text(encoding="utf-8"))
    assert fp["version"] == __version__ and fp["pages"] >= 6


def test_the_build_puts_it_at_the_top_of_the_program_folder() -> None:
    from sentinel.gui.app.window import USER_GUIDE

    assert USER_GUIDE == userguide.PDF.name == "Sentinel User Guide.pdf"
    build = (ROOT / "packaging" / "build.py").read_text(encoding="utf-8")
    assert "userguide.problems()" in build and "shutil.copy2(userguide.PDF, app / userguide.PDF.name)" in build
    readme = (ROOT / "packaging" / "windows" / "READ-ME-FIRST.txt").read_text(encoding="utf-8")
    assert '"Sentinel User Guide.pdf"' in readme


def test_the_pages_can_be_made_again(tmp_path: Path) -> None:
    """The script still renders: the same number of pages as the PDF in the repository (pictures, no PDF)."""
    pytest.importorskip("PySide6.QtGui")
    pages = userguide.preview(userguide.full_html(), tmp_path, __version__)
    fp = json.loads(userguide.FINGERPRINT.read_text(encoding="utf-8"))
    assert pages == fp["pages"] == len(list(tmp_path.glob("page-*.png")))
