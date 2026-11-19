"""Make the user guide: ``docs/user-guide/Sentinel User Guide.pdf`` from ``docs/user-guide/user-guide.html``.

    python packaging/userguide.py            make the PDF and note what it was made from
    python packaging/userguide.py --check    exit 1 if the PDF is not the one for the guide as it is now

The PDF is kept in the repository and the build copies it to the top of the program folder, beside
``Sentinel.exe`` (owner request 2026-11-18: a user guide that is kept up to date with every change that
matters to the people who use the recorder).

How it stays up to date, without anyone having to remember:

* The guide names the version it is for (``{{VERSION}}``, from ``src/sentinel/__init__.py``) and has an entry
  under "What changed" for that version. A new version without such an entry does not pass
  (:func:`problems`, checked by ``--check``, by the build and by ``tests/unit/test_user_guide.py``).
* The list of messages (``{{ALERTS}}``) is taken from ``sentinel.alerts.CATALOG`` when the PDF is made, so a
  new or changed message changes the guide.
* Words shown on the screen are marked ``<b class="ui">...</b>`` in the source; each must still be in the
  program's source. Renaming a button makes the check fail until the guide says the new name.
* ``fingerprint.json`` records what the PDF was made from (the finished text and this script's layout) and
  the PDF's own SHA-256. If the guide, the version or the messages changed since, the PDF is out of date.

Rendering uses Qt (``QTextDocument`` to ``QPdfWriter``), which the build and test environments already have:
no extra package. Only the rendering needs Qt; the checks do not.
"""

from __future__ import annotations

import argparse
import hashlib
import html
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
GUIDE_DIR = ROOT / "docs" / "user-guide"
SOURCE = GUIDE_DIR / "user-guide.html"
PDF = GUIDE_DIR / "Sentinel User Guide.pdf"
FINGERPRINT = GUIDE_DIR / "fingerprint.json"
PROGRAM_SOURCE = ROOT / "src" / "sentinel"

LETTERS = {
    "R": "Recording and the camera", "S": "Storage on the computer", "V": "Checks of the recordings and the log",
    "T": "The computer's clock", "C": "Setup", "D": "The recording database", "X": "The recorder program",
    "A": "The archive drive", "L": "Space on the computer and drive swaps", "M": "Drives: setting up and removing",
    "E": "Review drives (Export)", "P": "Planned; not a fault", "W": "Motion detection", "N": "Sound",
    "H": "Power settings", "Z": "Test system",
}
ORDER = "RSAWNPLMEVTCDXHZ"

CSS = """
body { font-family: 'Segoe UI', Arial, sans-serif; font-size: 10pt; color: #1b1b1b; }
h1 { font-size: 24pt; color: #12324a; margin-top: 0px; margin-bottom: 0px; }
h2 { font-size: 15pt; color: #12324a; margin-top: 16px; margin-bottom: 4px; }
h3 { font-size: 11pt; color: #1b1b1b; margin-top: 12px; margin-bottom: 2px; }
p { margin-top: 5px; margin-bottom: 5px; }
li { margin-top: 2px; margin-bottom: 2px; }
p.sub { font-size: 11pt; color: #555555; margin-top: 2px; margin-bottom: 12px; }
p.quote { margin-left: 18px; margin-right: 18px; color: #333333; font-style: italic; }
p.small { font-size: 8.5pt; color: #555555; }
.ui { font-weight: bold; color: #12324a; }
table.box td { background-color: #eef3f7; }
table.grid { border-color: #9aa7b3; border-style: solid; border-collapse: collapse; }
th { background-color: #dfe7ee; text-align: left; }
td.code { font-weight: bold; }
ul.never li { color: #7a1616; }
"""
LAYOUT = {"page": "Letter", "margins_mm": [18, 16, 18, 14], "resolution": 144, "font": ["Segoe UI", 10],
          "footer_inch": 0.34}


def version() -> str:
    text = (PROGRAM_SOURCE / "__init__.py").read_text(encoding="utf-8")
    m = re.search(r'^__version__\s*=\s*"([^"]+)"', text, re.M)
    if not m:
        raise SystemExit("the version could not be read from src/sentinel/__init__.py")
    return m[1]


def catalog() -> dict:
    sys.path.insert(0, str(ROOT / "src"))
    try:
        from sentinel.alerts import CATALOG
    finally:
        sys.path.pop(0)
    return CATALOG


def alerts_html() -> str:
    """The messages, grouped by the first letter of their code, as the program holds them now."""
    cat = catalog()
    sys.path.insert(0, str(ROOT / "src"))
    try:
        from sentinel.alerts import SUPPORT            # "Contact technical support and provide diagnostic report."
    finally:
        sys.path.pop(0)
    groups: dict[str, list] = {}
    for code in sorted(cat):
        groups.setdefault(code[0], []).append(cat[code])
    # Most messages end with the same sentence on the screen. In the list it is one word, explained once.
    out = [f'<p><b>Call</b> in the list below stands for what the screen says in full: "{html.escape(SUPPORT)}" '
           "Part 5 says how.</p>"]
    for letter in [x for x in ORDER if x in groups] + sorted(set(groups) - set(ORDER)):
        out.append(f"<h3>{letter} &nbsp;{html.escape(LETTERS.get(letter, 'Other'))}</h3>")
        out.append('<table class="grid" width="100%" border="1" cellpadding="4" cellspacing="0">'
                   '<tr><th width="9%">Code</th><th width="41%">The screen says</th><th>What to do</th></tr>')
        for d in groups[letter]:
            stops = " <b>Recording stops.</b>" if d.stops_recording else ""
            action = html.escape(d.action).replace(html.escape(SUPPORT), "<b>Call.</b>")
            out.append(f'<tr><td class="code">{html.escape(d.code)}</td><td>{html.escape(d.headline)}.{stops}</td>'
                       f"<td>{action}</td></tr>")
        out.append("</table>")
    return "\n".join(out)


def source_text() -> str:
    return SOURCE.read_text(encoding="utf-8")


def full_html() -> str:
    """The guide with the version and the messages filled in: what is rendered."""
    body = re.sub(r"<!--.*?-->", "", source_text(), flags=re.S)
    return body.replace("{{VERSION}}", html.escape(version())).replace("{{ALERTS}}", alerts_html())


def inputs_sha256() -> str:
    data = json.dumps({"html": full_html(), "css": CSS, "layout": LAYOUT}, sort_keys=True)
    return hashlib.sha256(data.encode("utf-8")).hexdigest()


def ui_strings() -> list[str]:
    """Words the guide says are on the screen."""
    found = re.findall(r'<b class="ui">(.*?)</b>', source_text(), flags=re.S)
    return sorted({html.unescape(re.sub(r"\s+", " ", s)).strip() for s in found})


def program_text() -> str:
    """The program's source as one text, without the ``&`` that marks a menu's shortcut letter."""
    parts = [p.read_text(encoding="utf-8", errors="replace") for p in sorted(PROGRAM_SOURCE.rglob("*.py"))]
    return re.sub(r"\s+", " ", "\n".join(parts).replace("&", ""))


def problems(check_pdf: bool = True) -> list[str]:
    """Why the guide is not ready for this version ([] = it is)."""
    out = []
    text, v = source_text(), version()
    if "{{VERSION}}" not in text or "{{ALERTS}}" not in text:
        out.append("user-guide.html must contain {{VERSION}} and {{ALERTS}}")
    if not re.search(rf"<h3>\s*{re.escape(v)}\s*</h3>", text):
        out.append(f'user-guide.html has no entry for {v} under "What changed" (add <h3>{v}</h3> and what changed for '
                   "the people who use the recorder, or that nothing did)")
    program = program_text()
    for s in ui_strings():
        if s not in program:
            out.append(f'the guide says the screen shows "{s}", which is not in the program (src/sentinel)')
    if not check_pdf:
        return out
    try:
        fp = json.loads(FINGERPRINT.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return [*out, "fingerprint.json is missing or unreadable: run python packaging/userguide.py"]
    if not PDF.is_file():
        out.append(f"{PDF.name} is missing: run python packaging/userguide.py")
    elif hashlib.sha256(PDF.read_bytes()).hexdigest() != fp.get("pdf_sha256"):
        out.append(f"{PDF.name} is not the file fingerprint.json describes: run python packaging/userguide.py")
    if fp.get("inputs_sha256") != inputs_sha256() or fp.get("version") != v:
        out.append("the guide, the version or the list of messages changed since the PDF was made: "
                   "run python packaging/userguide.py")
    return out


def _document(body: str, device: object, width: float, height: float):  # noqa: ANN202
    """The guide laid out for ``device`` on pages of ``width`` x ``height`` device points (footer left out)."""
    from PySide6.QtCore import QSizeF
    from PySide6.QtGui import QFont, QTextDocument

    doc = QTextDocument()
    doc.setDocumentMargin(0)
    doc.setDefaultFont(QFont(*LAYOUT["font"]))
    doc.setDefaultStyleSheet(CSS)
    doc.documentLayout().setPaintDevice(device)
    doc.setHtml(f"<html><body>{body}</body></html>")
    doc.setPageSize(QSizeF(width, height))
    return doc


def _paint_page(painter: object, doc: object, i: int, pages: int, footer: float, for_version: str) -> None:
    from PySide6.QtCore import QRectF, Qt
    from PySide6.QtGui import QColor, QFont

    page = doc.pageSize()
    painter.save()
    painter.translate(0, -i * page.height())
    doc.drawContents(painter, QRectF(0, i * page.height(), page.width(), page.height()))
    painter.restore()
    painter.setFont(QFont(LAYOUT["font"][0], 8))
    painter.setPen(QColor("#666666"))
    painter.drawText(QRectF(0, page.height(), page.width(), footer),
                     int(Qt.AlignmentFlag.AlignBottom | Qt.AlignmentFlag.AlignHCenter),
                     f"Sentinel user guide  ·  for Sentinel {for_version}  ·  page {i + 1} of {pages}")


class _Qt:
    """Qt needs an application object to lay text out. One that is already there is used; one made here is
    taken away again, so that a program (or a test run) that makes its own afterwards is not handed this one."""

    def __enter__(self) -> None:
        from PySide6.QtGui import QGuiApplication

        self.made = None if QGuiApplication.instance() is not None else QGuiApplication(["userguide"])

    def __exit__(self, *exc: object) -> None:
        if self.made is not None:
            self.made.shutdown()
            self.made = None


def render(body: str, pdf: Path, for_version: str) -> int:
    """Write the PDF; returns the number of pages."""
    with _Qt():
        return _render(body, pdf, for_version)


def _render(body: str, pdf: Path, for_version: str) -> int:
    from PySide6.QtCore import QMarginsF
    from PySide6.QtGui import QPageLayout, QPageSize, QPainter, QPdfWriter

    writer = QPdfWriter(str(pdf))
    writer.setPageSize(QPageSize(QPageSize.PageSizeId.Letter))
    writer.setPageMargins(QMarginsF(*LAYOUT["margins_mm"]), QPageLayout.Unit.Millimeter)
    writer.setResolution(LAYOUT["resolution"])
    writer.setTitle(f"Sentinel user guide (for Sentinel {for_version})")
    writer.setCreator("Sentinel packaging/userguide.py")
    area = writer.pageLayout().paintRectPixels(writer.resolution())
    footer = LAYOUT["footer_inch"] * writer.resolution()
    doc = _document(body, writer, area.width(), area.height() - footer)
    pages = doc.pageCount()
    painter = QPainter(writer)
    try:
        for i in range(pages):
            if i:
                writer.newPage()
            _paint_page(painter, doc, i, pages, footer, for_version)
    finally:
        painter.end()
    return pages


def preview(body: str, folder: Path, for_version: str) -> int:
    """The same pages as pictures (page-01.png, ...), to look at the layout where no PDF viewer is at hand."""
    with _Qt():
        return _preview(body, folder, for_version)


def _preview(body: str, folder: Path, for_version: str) -> int:
    from PySide6.QtGui import QColor, QImage, QPainter

    res = LAYOUT["resolution"]
    width, height = round(8.5 * res), round(11 * res)                    # Letter
    left, top, right, bottom = (m / 25.4 * res for m in LAYOUT["margins_mm"])
    footer = LAYOUT["footer_inch"] * res

    def sheet() -> QImage:
        img = QImage(width, height, QImage.Format.Format_RGB32)
        img.setDotsPerMeterX(round(res / 0.0254))
        img.setDotsPerMeterY(round(res / 0.0254))
        img.fill(QColor("white"))
        return img

    doc = _document(body, sheet(), width - left - right, height - top - bottom - footer)
    pages = doc.pageCount()
    folder.mkdir(parents=True, exist_ok=True)
    for i in range(pages):
        img = sheet()
        painter = QPainter(img)
        try:
            painter.translate(left, top)
            _paint_page(painter, doc, i, pages, footer, for_version)
        finally:
            painter.end()
        img.save(str(folder / f"page-{i + 1:02d}.png"))
    return pages


def make() -> int:
    early = problems(check_pdf=False)
    if early:
        for p in early:
            print(f"PROBLEM: {p}")
        return 1
    GUIDE_DIR.mkdir(parents=True, exist_ok=True)
    v = version()
    pages = render(full_html(), PDF, v)
    FINGERPRINT.write_text(json.dumps({
        "what": "what 'Sentinel User Guide.pdf' was made from; python packaging/userguide.py --check compares",
        "version": v, "inputs_sha256": inputs_sha256(), "pdf_sha256": hashlib.sha256(PDF.read_bytes()).hexdigest(),
        "pages": pages}, indent=2) + "\n", encoding="utf-8")
    print(f"{PDF.relative_to(ROOT)}: {pages} pages, {PDF.stat().st_size} bytes, for Sentinel {v}")
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Make or check the Sentinel user guide PDF.")
    ap.add_argument("--check", action="store_true", help="only check that the PDF is the one for the guide as it is")
    ap.add_argument("--preview", metavar="FOLDER", help="write the pages as PNG pictures into FOLDER (no PDF is made)")
    args = ap.parse_args(argv)
    if args.preview:
        n = preview(full_html(), Path(args.preview), version())
        print(f"{n} page pictures written to {args.preview}")
        return 0
    if not args.check:
        return make()
    found = problems()
    for p in found:
        print(f"PROBLEM: {p}")
    print("the user guide is up to date" if not found else "the user guide is NOT up to date")
    return 1 if found else 0


if __name__ == "__main__":
    sys.exit(main())
