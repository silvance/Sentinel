"""Drives (archive drives, erasing and setting up drives) and Export (review drives) pages.

Every action runs the same command the command line uses (``Sentinel.exe drives ...``,
``archive ...``, ``export ...``), so the refusals, identity checks and audit records are the same.
Erasing a drive always needs: administrator rights, the drive picked from the list, the person's
name, and the last characters of its serial number typed by the person.
"""

from __future__ import annotations

import getpass
import json
from typing import Any

from PySide6.QtWidgets import (QComboBox, QDialog, QDialogButtonBox, QFileDialog, QFormLayout, QGroupBox,
                               QHBoxLayout, QLabel, QLineEdit, QMessageBox, QPushButton, QVBoxLayout)

from .. import textfmt as tf
from ..ledger_view import load_exports
from .base import CommandPanel, OutputLog, Page, ReadOnlyBar, capped, fill, icon_button, muted, selected_data, table
from .procs import Command, sentinel_argv

CLASS_WORDS = {"READY": "set up, ready to use", "SENTINEL_EVIDENCE": "holds Sentinel recordings (never erased)",
               "BLANK": "blank", "WRONG_FORMAT": "not set up", "FOREIGN_DATA": "has other files",
               "UNSUITABLE": "cannot be used"}


def _who() -> str:
    try:
        return getpass.getuser()
    except Exception:  # noqa: BLE001
        return ""


def _what(d: dict[str, Any]) -> str:
    role = d.get("role")
    words = CLASS_WORDS.get(d.get("class", ""), d.get("class", ""))
    if role:
        return f"{role} drive {d.get('label') or ''}".strip() + f" — {words}"
    return words


def _letters(d: dict[str, Any]) -> list[str]:
    return [p for p in d.get("mount_paths") or [] if len(p) == 3 and p[1:] == ":\\"] or list(d.get("mount_paths") or [])


class DriveList:
    """Runs ``drives list --json`` and keeps the result."""

    def __init__(self, page: Page, log: OutputLog, on_loaded) -> None:  # noqa: ANN001
        self.page, self.log, self.on_loaded = page, log, on_loaded
        self.drives: list[dict[str, Any]] = []
        self.problems: list[str] = []
        self.cmd: Command | None = None

    def refresh(self) -> None:
        if self.page.ctx.cfg_path is None or (self.cmd is not None and self.cmd.running()):
            return
        cmd = Command(sentinel_argv("drives", "list", "--json", "--config", str(self.page.ctx.cfg_path)), self.page)

        def done(code: int) -> None:
            text = "\n".join(cmd.text)
            try:
                data = json.loads(text[text.index("{"):])
                self.drives, self.problems = data.get("drives", []), data.get("protection_problems", [])
            except (ValueError, json.JSONDecodeError):
                self.drives, self.problems = [], []
                self.log.appendPlainText(f"── The drive list could not be read (exit code {code}):\n{text}\n")
            self.on_loaded()

        cmd.finished.connect(done)
        self.cmd = cmd.start()


class EraseDialog(QDialog):
    def __init__(self, d: dict[str, Any], chars: int, parent=None) -> None:  # noqa: ANN001
        super().__init__(parent)
        self.setWindowTitle("Erase and set up drive")
        lay = QVBoxLayout(self)
        warn = QLabel(f"<b>This erases everything on:</b><br>{d.get('description', '')}<br>"
                      f"Now: {CLASS_WORDS.get(d.get('class', ''), d.get('class', ''))}"
                      + (f", about {tf.size(d['used_bytes'])} of files" if d.get("used_bytes") else ""))
        warn.setWordWrap(True)
        lay.addWidget(warn)
        f = QFormLayout()
        self.who = QLineEdit(_who())
        f.addRow("Your name or ID (recorded)", self.who)
        self.tail = QLineEdit()
        self.tail.setPlaceholderText(f"last {chars} characters of the serial number")
        f.addRow(f"Serial number, last {chars} characters", self.tail)
        lay.addLayout(f)
        lay.addWidget(muted("The serial number is printed on the drive's label and shown in the list. "
                            "Type it from the drive itself, to be sure it is the drive in your hand."))
        bb = QDialogButtonBox(QDialogButtonBox.StandardButton.Cancel)
        self.ok = bb.addButton("Erase drive", QDialogButtonBox.ButtonRole.DestructiveRole)
        self.ok.clicked.connect(self.accept)
        bb.rejected.connect(self.reject)
        lay.addWidget(bb)


class DrivesPage(Page):
    key = "drives"
    title = "Drives"
    description = "Archive drive copying and swaps; erasing and setting up drives."

    def __init__(self, ctx, parent=None) -> None:  # noqa: ANN001
        super().__init__(ctx, parent)
        self.body.addWidget(ReadOnlyBar(ctx, "drives"))
        arc = QGroupBox("Archive drive")
        al = QVBoxLayout(arc)
        self.arc_text = QLabel("—")
        self.arc_text.setWordWrap(True)
        al.addWidget(self.arc_text)
        row = QHBoxLayout()
        c = ctx.palette.text
        self.swap_btn = icon_button("Swap archive drive", "swap", c)
        self.swap_btn.setToolTip("Finish copying and prepare the drive for removal; wait for SAFE TO REMOVE")
        self.resume_btn = icon_button("Keep using this drive", "play", c)
        self.arc_status_btn = icon_button("Archive details", "info", c)
        self.problems_btn = icon_button("Archive problems", "warning", c)
        self.problems_btn.setToolTip("List copies on archive drives that could not be verified (A-006)")
        self.ack_btn = icon_button("Acknowledge all problems…", "ok", c)
        for b in (self.swap_btn, self.resume_btn, self.arc_status_btn, self.problems_btn, self.ack_btn):
            row.addWidget(b)
        row.addStretch(1)
        al.addLayout(row)
        self.body.addWidget(arc)

        box = QGroupBox("Drives connected to this computer")
        bl = QVBoxLayout(box)
        self.table = table(["Disk", "What it is", "Size", "Connection", "Model", "Serial", "Letters", "Notes"])
        bl.addWidget(self.table)
        self.problems = muted()
        bl.addWidget(self.problems)
        row = QHBoxLayout()
        self.refresh_btn = icon_button("Refresh list", "refresh", c)
        self.erase_btn = icon_button("Erase and set up…", "erase", c)
        self.designate_btn = icon_button("Use as archive drive…", "drives", c)
        self.eject_btn = icon_button("Safely remove…", "eject", c)
        for b in (self.refresh_btn, self.erase_btn, self.designate_btn, self.eject_btn):
            row.addWidget(b)
        row.addStretch(1)
        bl.addLayout(row)
        self.body.addWidget(box, 2)
        self.log = OutputLog()
        self.body.addWidget(CommandPanel(self.log, ctx.palette), 1)

        self.list = DriveList(self, self.log, self._show_drives)
        self.refresh_btn.clicked.connect(self.list.refresh)
        self.erase_btn.clicked.connect(self._erase)
        self.designate_btn.clicked.connect(self._designate)
        self.eject_btn.clicked.connect(self._eject)
        self.swap_btn.clicked.connect(self._swap)
        self.resume_btn.clicked.connect(self._resume)
        self.arc_status_btn.clicked.connect(lambda: self._arc("status"))
        self.problems_btn.clicked.connect(lambda: self._arc_cmd(["acknowledge", "--admin-id", _who()],
                                                                "Archive problems"))
        self.ack_btn.clicked.connect(self._acknowledge)
        self.ack_btn.setEnabled(ctx.admin)
        # Swapping is an operator task (every few days): operators ask the archive copier through
        # its request folder; erasing, designating and removing drives stay administrator-only
        # (review 2026-11-10).
        for b in (self.erase_btn, self.designate_btn, self.eject_btn):
            b.setEnabled(ctx.admin)
        operator_ok = ctx.admin or bool(ctx.cfg is not None and ctx.cfg.storage.status_dir)
        for b in (self.swap_btn, self.resume_btn):
            b.setEnabled(operator_ok)
            if not operator_ok:
                b.setToolTip("Needs an administrator on this computer (no operator request folder is set up)")
        self._loaded = False

    def on_show(self) -> None:
        if not self._loaded:
            self._loaded = True
            self.list.refresh()

    def on_status(self, read) -> None:  # noqa: ANN001
        arc = ((read.doc or {}).get("archive") or {}) if read is not None else {}
        if not arc:
            self.arc_text.setText("Archive copying is not enabled in the settings.")
            return
        bk = arc.get("backlog") or {}
        msg = str(arc.get("message") or "")
        backlog = str(bk.get("text") or "")
        if not arc.get("running"):
            drive = "Drive: unknown (archive copying is not running)."
        else:
            drive = f"Drive: {arc.get('drive_label') or 'none inserted'}."
        parts = [drive, msg] + ([backlog] if backlog and backlog not in msg else [])
        self.arc_text.setText(" ".join(x for x in parts if x))

    def _cfg(self) -> str:
        return str(self.ctx.cfg_path)

    def _arc(self, *args: str) -> None:
        # Resume stays available during a swap: it cancels it (review 2026-11-10).
        self.log.run(sentinel_argv("archive", args[0], "--config", self._cfg(), *args[1:]),
                     f"Archive {args[0]}", busy=[self.swap_btn])

    def _arc_cmd(self, args: list[str], title: str) -> None:
        self.log.run(sentinel_argv("archive", args[0], "--config", self._cfg(), *args[1:]), title)

    def _operator_request(self, kind: str) -> None:
        """Operator (no administrator rights): ask the archive copier through its request folder."""
        from ...archive.worker import request_resume_file, request_swap_file

        cfg = self.cfg
        if cfg is None:
            return
        try:
            (request_swap_file if kind == "swap" else request_resume_file)(cfg, operator=True)
        except OSError as exc:
            self.problem(f"The request could not be made ({exc}). Contact technical support.")
            return
        if kind == "swap":
            self.log.appendPlainText("── Swap requested. Copying finishes first: wait until this page says "
                                     "SAFE TO REMOVE, then unplug the archive drive and insert the next one.\n")
        else:
            self.log.appendPlainText("── Keep using this drive: requested.\n")

    def _swap(self) -> None:
        if not self.ctx.admin:
            self._operator_request("swap")
            return
        self._arc("swap", "--timeout", "600")

    def _resume(self) -> None:
        """Cancel a swap / keep using the drive: its own command, even while a swap is waiting."""
        if not self.ctx.admin:
            self._operator_request("resume")
            return
        cmd = Command(sentinel_argv("archive", "resume", "--config", self._cfg()), self)
        self.log.appendPlainText("── Keep using this drive")
        cmd.output.connect(self.log.appendPlainText)
        cmd.finished.connect(lambda code: self.log.appendPlainText(
            f"── finished ({'OK' if code == 0 else f'exit code {code}'})\n"))
        self._resume_cmd = cmd.start()

    def _acknowledge(self) -> None:
        if QMessageBox.question(self, "Sentinel", "Acknowledge every open archive problem (A-006)? Do this only "
                                "after the affected recordings have been checked or copied again. Your name "
                                f"({_who()}) is recorded.") != QMessageBox.StandardButton.Yes:
            return
        self._arc_cmd(["acknowledge", "--admin-id", _who(), "--all"], "Acknowledge archive problems")

    def _show_drives(self) -> None:
        rows = [[d.get("disk_id"), _what(d),
                 tf.size(d["size_bytes"]) if isinstance(d.get("size_bytes"), (int, float)) else "",
                 d.get("bus_type") or "", d.get("model") or "", d.get("serial") or "", ", ".join(_letters(d)),
                 d.get("message") or d.get("refusal") or ""] for d in self.list.drives]
        fill(self.table, rows, self.list.drives, empty="No drives found. Connect a drive and press Refresh list.")
        self.problems.setText(("Erasing is blocked: " + "; ".join(self.list.problems)) if self.list.problems else "")

    def _selected(self) -> dict[str, Any] | None:
        d = selected_data(self.table)
        if d is None:
            self.info("Select a drive in the list first.")
        return d

    def _erase(self) -> None:
        d = self._selected()
        cfg = self.cfg
        if d is None or cfg is None:
            return
        if not d.get("can_prepare"):
            self.problem(f"This drive cannot be erased and set up: {d.get('refusal') or d.get('message')}.\n\n"
                         "Nothing was erased.")
            return
        chars = cfg.drives.serial_confirm_chars
        dlg = EraseDialog(d, chars, self)
        if dlg.exec() != QDialog.DialogCode.Accepted:
            return
        who, tail = dlg.who.text().strip(), dlg.tail.text().strip().upper()
        if not who or len(tail) != chars:
            self.problem(f"Enter your name and exactly {chars} characters of the serial number. Nothing was erased.")
            return
        self.log.run(sentinel_argv("drives", "prepare", "--config", self._cfg(), "--disk", str(d["disk_id"]),
                                   "--who", who, "--admin", "--yes", "--serial-tail", tail),
                     f"Erase and set up disk {d['disk_id']}", busy=[self.erase_btn],
                     on_done=lambda _c, _cmd: self.list.refresh())

    def _designate(self) -> None:
        d = self._selected()
        if d is None:
            return
        letters = _letters(d)
        if not letters:
            self.problem("This drive has no drive letter. Erase and set it up first, or reconnect it.")
            return
        who = _who()
        if QMessageBox.question(self, "Sentinel", f"Use {letters[0]} ({d.get('description', '')}) as the archive "
                                f"drive?\n\nRecorded as approved by: {who}") != QMessageBox.StandardButton.Yes:
            return
        self.log.run(sentinel_argv("archive", "designate", "--config", self._cfg(), "--drive", letters[0],
                                   "--admin-id", who), f"Use {letters[0]} as archive drive",
                     busy=[self.designate_btn], on_done=lambda _c, _cmd: self.list.refresh())

    def _eject(self) -> None:
        d = self._selected()
        if d is None:
            return
        if QMessageBox.question(self, "Sentinel", f"Safely remove disk {d['disk_id']}: {d.get('description', '')} "
                                f"({_what(d)})?") != QMessageBox.StandardButton.Yes:
            return
        self.log.run(sentinel_argv("drives", "eject", "--config", self._cfg(), "--disk", str(d["disk_id"])),
                     f"Safely remove disk {d['disk_id']}", busy=[self.eject_btn],
                     on_done=lambda _c, _cmd: self.list.refresh())


class ExportPage(Page):
    key = "export"
    title = "Export"
    description = "Copy recordings to a review drive for reviewers, with a hash manifest."

    def __init__(self, ctx, parent=None) -> None:  # noqa: ANN001
        super().__init__(ctx, parent)
        self.body.addWidget(ReadOnlyBar(ctx, "review drives"))
        box = QGroupBox("Make a review drive")
        f = QFormLayout(box)
        row = QHBoxLayout()
        self.target = QComboBox()
        self.target.setEditable(True)
        self.target.setMinimumWidth(260)
        row.addWidget(self.target, 1)
        b = QPushButton("Browse…")
        b.clicked.connect(self._browse)
        row.addWidget(b)
        b2 = icon_button("Find drives", "refresh", ctx.palette.text)
        b2.clicked.connect(lambda: self.list.refresh())
        row.addWidget(b2)
        f.addRow("Review drive", row)
        self.who = QLineEdit(_who())
        f.addRow("Your name or ID (recorded)", self.who)
        btns = QHBoxLayout()
        self.run_btn = icon_button("Export new recordings", "export", ctx.palette.text)
        self.run_btn.setDefault(True)
        self.verify_btn = icon_button("Check the video on the drive", "test", ctx.palette.text)
        self.status_btn = icon_button("Last export status", "info", ctx.palette.text)
        for x in (self.run_btn, self.verify_btn, self.status_btn):
            btns.addWidget(x)
        btns.addStretch(1)
        f.addRow("", btns)
        self.body.addWidget(capped(box, 900))
        self.log = OutputLog()
        self.body.addWidget(CommandPanel(self.log, ctx.palette), 1)
        hist = QGroupBox("Previous exports")
        hl = QVBoxLayout(hist)
        self.history = table(["When", "Result", "Drive", "Recordings from", "To", "Code"])
        hl.addWidget(self.history)
        self.body.addWidget(hist, 1)
        self.list = DriveList(self, self.log, self._fill_targets)
        self.run_btn.clicked.connect(self._run)
        self.verify_btn.clicked.connect(lambda: self._cmd("verify-content", "Check video on review drive"))
        self.status_btn.clicked.connect(lambda: self.log.run(
            sentinel_argv("export", "status", "--config", str(self.ctx.cfg_path)), "Last export status"))
        for x in (self.run_btn, self.verify_btn):
            x.setEnabled(ctx.admin)
        self._loaded = False

    def on_show(self) -> None:
        if not self._loaded:
            self._loaded = True
            self.list.refresh()
        cfg = self.cfg
        if cfg is None:
            return
        db = cfg.ledger_snapshot_path or (cfg.data_path / "sentinel.db")
        ex = load_exports(db)
        now = __import__("time").time()
        fill(self.history, [[tf.when(r.when_epoch, self.ctx.tz, now) if r.when_epoch else "", r.outcome,
                             r.drive or "", tf.when(r.from_epoch, self.ctx.tz, now) if r.from_epoch else "",
                             tf.when(r.to_epoch, self.ctx.tz, now) if r.to_epoch else "", r.error_code or ""]
                            for r in ex.rows], empty="No review drives have been made yet.")

    def _fill_targets(self) -> None:
        cur = self.target.currentText()
        self.target.clear()
        for d in self.list.drives:
            if d.get("role") == "archive":
                continue                          # never offered as a review drive (review 2026-11-10)
            if d.get("class") in ("READY", "SENTINEL_EVIDENCE") or \
                    d.get("bus_type") in ("USB", "SD", "MMC"):
                for p in _letters(d):
                    self.target.addItem(f"{p}  {d.get('model') or ''} ({CLASS_WORDS.get(d.get('class', ''), '')})", p)
        if cur:
            self.target.setEditText(cur)

    def _browse(self) -> None:
        folder = QFileDialog.getExistingDirectory(self, "Review drive (its top folder)")
        if folder:
            self.target.setEditText(folder)

    def _target(self) -> str:
        i = self.target.currentIndex()
        data = self.target.itemData(i) if i >= 0 and self.target.itemText(i) == self.target.currentText() else None
        return str(data or self.target.currentText().split("  ")[0]).strip()

    def _run(self) -> None:
        t, who = self._target(), self.who.text().strip()
        if not t or not who:
            self.problem("Choose the review drive and enter your name or ID.")
            return
        self.log.run(sentinel_argv("export", "run", "--config", str(self.ctx.cfg_path), "--target", t,
                                   "--operator-id", who), f"Export to {t}", busy=[self.run_btn, self.verify_btn],
                     on_done=lambda _c, _cmd: self.on_show())

    def _cmd(self, sub: str, title: str) -> None:
        t = self._target()
        if not t:
            self.problem("Choose the review drive first.")
            return
        self.log.run(sentinel_argv("export", sub, "--config", str(self.ctx.cfg_path), "--target", t), title,
                     busy=[self.run_btn, self.verify_btn])
