"""Notes: a place for the people at the recorder to write down what happened (0.3.27).

Owner's request 2026-11-20, before the recorder went into real use: "add a Notes tab. I'll explain to the agents
that if they mess up or do something wrong, to just notate the 5 Ws in that tab and save it so there's a stored
reason for things."

A note is five short answers (who, what, when, where, why). It is saved as an entry of the recording log
(event ``operator_note``, category ``notes``), so it gets the time it was saved, cannot be changed or deleted
afterwards, is covered by the log's fingerprint chain like every other entry, and is in the diagnostic report.
Saving needs no administrator rights: where the recorder runs under the same Windows account as the window
(the set-up in use), the window writes the entry itself. Where the log is closed to that account (recorder
installed as a Windows service), saving says so plainly instead of pretending.
"""

from __future__ import annotations

import getpass
import json
import sqlite3
from pathlib import Path
from typing import Any

from PySide6.QtWidgets import QFormLayout, QGroupBox, QLineEdit, QPlainTextEdit, QVBoxLayout

from .. import textfmt as tf
from ...clock import parse_iso_utc
from .base import Page, capped, fill, icon_button, muted, table

NOTE_CODE = "operator_note"
NOTE_CATEGORY = "notes"
FIELDS = (("who", "Who", "Your name, and who else was involved"),
          ("what", "What", "What happened, or what was done differently"),
          ("when", "When", "The date and time it happened, as well as you know"),
          ("where", "Where", "For example: at the recorder, at the camera, which drive"),
          ("why", "Why", "Why it happened, or why it was done"))
MAX_CHARS = 2000
INTRO = ("If something went wrong, or was done differently from the instructions, write it down here: who, what, "
         "when, where and why. A saved note is kept with the recording log. It cannot be changed or deleted, so "
         "if a note needs correcting, save another one.")


def _windows_user() -> str:
    try:
        return getpass.getuser()
    except Exception:  # noqa: BLE001
        return ""


def save_note(db: Path, answers: dict[str, str]) -> None:
    """Write the note into the recording log. Raises OSError / sqlite3.Error when the log cannot be written."""
    from ...db import Ledger

    if not db.is_file():
        raise OSError("the recording log does not exist yet")
    detail = {k: str(answers.get(k) or "").strip()[:MAX_CHARS] for k, _label, _hint in FIELDS}
    detail["windows_user"] = _windows_user()
    led = Ledger(db)
    try:
        led.add_event("audit", NOTE_CATEGORY, NOTE_CODE, f"Note by {detail['who'] or 'someone at the recorder'}",
                      detail)
    finally:
        led.close()


def load_notes(db: Path, limit: int = 500) -> list[dict[str, Any]]:
    """Saved notes, newest first. [] when the log cannot be read."""
    from ...db import Ledger

    if not db.is_file():
        return []
    try:
        led = Ledger(db, readonly=True)
    except (sqlite3.Error, OSError):
        return []
    out = []
    try:
        for r in led.query("SELECT ts_utc, detail FROM events WHERE code=? ORDER BY event_id DESC LIMIT ?",
                           (NOTE_CODE, int(limit))):
            try:
                d = json.loads(r[1] or "{}")
            except ValueError:
                d = {}
            out.append({"saved_utc": r[0], **(d if isinstance(d, dict) else {})})
    except sqlite3.Error:
        return []
    finally:
        led.close()
    return out


class NotesPage(Page):
    key = "notes"
    title = "Notes"
    description = "Write down what happened: who, what, when, where and why."

    def __init__(self, ctx, parent=None) -> None:  # noqa: ANN001
        super().__init__(ctx, parent)
        intro = muted(INTRO)
        self.body.addWidget(capped(intro))
        box = QGroupBox("New note")
        form = QFormLayout(box)
        self.inputs: dict[str, Any] = {}
        for key, label, hint in FIELDS:
            w: Any
            if key in ("what", "why"):
                w = QPlainTextEdit()
                w.setFixedHeight(64)
                w.setTabChangesFocus(True)
                w.textChanged.connect(self._changed)
            else:
                w = QLineEdit()
                w.setMaxLength(200)
                w.textChanged.connect(self._changed)
            w.setPlaceholderText(hint)
            self.inputs[key] = w
            form.addRow(label, w)
        self.save_btn = icon_button("Save note", "ok", ctx.palette.text)
        self.save_btn.clicked.connect(self._save)
        self.saved_label = muted("")
        form.addRow("", self.save_btn)
        form.addRow("", self.saved_label)
        self.body.addWidget(capped(box))
        listed = QGroupBox("Saved notes")
        ll = QVBoxLayout(listed)
        self.table = table(["Saved", "Who", "What", "When", "Where", "Why"])
        self.table.setWordWrap(True)
        self.table.setMinimumHeight(180)
        ll.addWidget(self.table)
        self.body.addWidget(listed, 1)
        self._changed()

    # ------------------------------------------------------------------ helpers
    def _db(self) -> Path | None:
        return self.cfg.data_path / "sentinel.db" if self.cfg is not None else None

    def _text(self, key: str) -> str:
        w = self.inputs[key]
        return (w.toPlainText() if isinstance(w, QPlainTextEdit) else w.text()).strip()

    def _changed(self) -> None:
        # Who and what are the least a note needs; the rest helps, and an empty answer is saved as empty.
        self.save_btn.setEnabled(bool(self._text("who")) and bool(self._text("what")))

    def on_show(self) -> None:
        self.reload()

    def reload(self) -> None:
        db = self._db()
        notes = load_notes(db) if db is not None else []
        if not notes and self.cfg is not None and self.cfg.ledger_snapshot_path is not None:
            notes = load_notes(self.cfg.ledger_snapshot_path)       # the copy kept for accounts that cannot read the log
        rows = []
        for n in notes:
            try:
                saved = tf.local_dt(parse_iso_utc(str(n.get("saved_utc"))), self.ctx.tz).strftime("%Y-%m-%d %H:%M")
            except (ValueError, TypeError):
                saved = str(n.get("saved_utc") or "")
            rows.append([saved, *[str(n.get(k) or "") for k, _l, _h in FIELDS]])
        fill(self.table, rows, notes, empty="No notes have been saved.")
        for i, n in enumerate(notes):
            for j, (k, _l, _h) in enumerate(FIELDS, start=1):
                it = self.table.item(i, j)
                if it is not None:
                    it.setToolTip(str(n.get(k) or ""))
        self.table.resizeRowsToContents()

    def _save(self) -> None:
        db = self._db()
        if db is None:
            return
        answers = {k: self._text(k) for k, _l, _h in FIELDS}
        if not answers["who"] or not answers["what"]:
            return
        try:
            save_note(db, answers)
        except (OSError, sqlite3.Error) as exc:
            self.saved_label.setText("")
            self.problem("The note could not be saved, so it is NOT stored. Write it on paper and tell the person "
                         f"who looks after the system. ({type(exc).__name__}: {exc})")
            return
        for w in self.inputs.values():
            w.clear()
        self.saved_label.setText("Saved. The note is in the list below.")
        self.reload()
