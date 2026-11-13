"""Drives (archive drives, erasing and setting up drives) and Export (review drives) pages.

Every action runs the same command the command line uses (``Sentinel.exe drives ...``,
``archive ...``, ``export ...``), so the refusals, identity checks and audit records are the same.
Erasing a drive always needs: administrator rights, the drive picked from the list, the person's
name, and the last characters of its serial number typed by the person.
"""

from __future__ import annotations

import getpass
import json
import time
from typing import Any

from PySide6.QtCore import QDate, QTimer
from PySide6.QtWidgets import (QCheckBox, QComboBox, QDateEdit, QDialog, QDialogButtonBox, QFileDialog, QFormLayout,
                               QGroupBox, QHBoxLayout, QInputDialog, QLabel, QLineEdit, QMenu, QMessageBox,
                               QPushButton, QVBoxLayout)

from .. import textfmt as tf
from ...archive.status import ST_REMOVE_MANUALLY, ST_SAFE, ST_SWAP_PENDING
from ...drives.classify import evidence_why
from ...drives.model import HIDDEN_SERIAL_WORD
from ..ledger_view import load_exports
from .base import CommandPanel, OutputLog, Page, ReadOnlyBar, capped, fill, icon_button, muted, selected_data, table
from .icons import glyph
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
    if d.get("class") == "FOREIGN_DATA" and "unknown_contents" in (d.get("reasons") or []):
        words = "contents unknown"           # Windows cannot read it (e.g. a new, never-used drive)
    if role:
        # GUI simplification 2026-11-13: the name has its own column; "set up, ready to use" goes without saying.
        return f"{role} drive" + ("" if d.get("class") == "READY" else f" — {words}")
    return words


# Drives that can never be used here (the computer's own disk, the recording storage, internal disks, an empty
# card reader) are not listed unless "Show all drives" is ticked (GUI simplification 2026-11-13).
NEVER_USABLE = {"system_disk", "recording_storage", "page_file", "not_external"}


def is_noise(d: dict[str, Any]) -> bool:
    if d.get("class") != "UNSUITABLE" or d.get("role"):
        return False
    return bool(NEVER_USABLE & set(d.get("reasons") or [])) or not d.get("size_bytes")


def drive_letters_now() -> frozenset[str]:
    """The drive letters Windows shows now (cheap): a change means a drive was plugged in or taken out."""
    import os

    try:
        return frozenset(os.listdrives())        # Windows, Python 3.12+
    except (AttributeError, OSError):
        return frozenset()


def _letters(d: dict[str, Any]) -> list[str]:
    return [p for p in d.get("mount_paths") or [] if len(p) == 3 and p[1:] == ":\\"] or list(d.get("mount_paths") or [])



_USE_WORDS = {"in_use": "In use", "full": "Full (swap it)", "standby": "Next", "take_out": "Ready to take out",
              "awaiting_return": "Back from the office (return it to service)"}


SWAP_STATES = (ST_SWAP_PENDING, ST_SAFE, ST_REMOVE_MANUALLY)
AUTO_REMOVE_S = 900.0     # after Swap and safely remove: eject the drive when the copier reports it finished


def swap_under_way(arc: dict[str, Any]) -> bool:
    """A swap is being prepared, or a drive was made safe to remove and is still waiting to be taken out
    (with two drives, copying goes on meanwhile: the state is then COPYING and ``removal`` is set)."""
    return bool(arc.get("running")) and (arc.get("state") in SWAP_STATES or bool(arc.get("removal"))
                                         or bool(arc.get("awaiting_return")))


def archive_drives_text(arc: dict[str, Any]) -> str:
    """Every archive drive inserted, from the archive status (owner request 2026-11-11: two-bay dock),
    e.g. "In use: SENTINEL_2 (1.2 TB free). Full (swap it): SENTINEL_1."."""
    drives = [d for d in arc.get("drives") or [] if isinstance(d, dict)]
    if not drives:
        return f"Drive: {arc.get('drive_label') or 'none inserted'}."
    parts = []
    for d in drives:
        name = str(d.get("label") or "unnamed archive drive")
        words = _USE_WORDS.get(str(d.get("use")), "Inserted")
        free = d.get("free_bytes")
        extra = f" ({tf.size(free)} free)" if d.get("use") == "in_use" and isinstance(free, (int, float)) else ""
        parts.append(f"{words}: {name}{extra}.")
    return " ".join(parts)

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
    """Who is erasing, and proof it is the drive in hand: the serial's last characters from the drive's
    label, or the word ERASE when the dock hides the serial (it reports a placeholder such as zeros)."""

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
        self.hidden = bool(d.get("serial_hidden"))
        if self.hidden:
            self.expected_len = len(HIDDEN_SERIAL_WORD)
            self.tail.setPlaceholderText(HIDDEN_SERIAL_WORD)
            f.addRow(f"Type {HIDDEN_SERIAL_WORD} to confirm", self.tail)
            note = ("The drive dock hides this drive's serial number, so it cannot be checked against the label. "
                    "Make sure this is the only drive in the dock.")
        else:
            self.expected_len = chars
            self.tail.setPlaceholderText(f"last {chars} characters of the serial number")
            f.addRow(f"Serial number, last {chars} characters", self.tail)
            note = ("The serial number is printed on the drive's label and shown in the list. "
                    "Type it from the drive itself, to be sure it is the drive in your hand.")
        lay.addLayout(f)
        lay.addWidget(muted(note))
        bb = QDialogButtonBox(QDialogButtonBox.StandardButton.Cancel)
        self.ok = bb.addButton("Erase drive", QDialogButtonBox.ButtonRole.DestructiveRole)
        self.ok.clicked.connect(self.accept)
        bb.rejected.connect(self.reject)
        lay.addWidget(bb)


class ReturnDialog(QDialog):
    """Return an archive drive to service after the office copied it (0.3.9, owner request 2026-11-12):
    where the recordings went, who copied them and when, and that the office checked every file."""

    CHECK_TEXT = ("The office copied the whole drive and checked every file against SHA256SUMS.txt; "
                  "all of them matched.")

    def __init__(self, d: dict[str, Any], parent=None) -> None:  # noqa: ANN001
        super().__init__(parent)
        self.setWindowTitle("Return drive to service")
        lay = QVBoxLayout(self)
        label = d.get("label") or d.get("description", "")
        warn = QLabel(f"<b>This deletes the recordings from {label}</b> so the drive can be used again. Sentinel "
                      "records where the recordings were copied. Use it only after the office has copied this "
                      "drive and checked the copy (office transfer checklist).")
        warn.setWordWrap(True)
        lay.addWidget(warn)
        f = QFormLayout()
        self.destination = QLineEdit()
        self.destination.setPlaceholderText("e.g. Office archive drive 3")
        f.addRow("Copied to", self.destination)
        self.copied_by = QLineEdit()
        f.addRow("Copied by", self.copied_by)
        self.copied_on = QDateEdit(QDate.currentDate())
        self.copied_on.setCalendarPopup(True)
        self.copied_on.setDisplayFormat("yyyy-MM-dd")
        self.copied_on.setMaximumDate(QDate.currentDate())
        f.addRow("Date copied", self.copied_on)
        self.who = QLineEdit(_who())
        f.addRow("Your name or ID (recorded)", self.who)
        lay.addLayout(f)
        self.checked = QCheckBox(self.CHECK_TEXT)
        lay.addWidget(self.checked)
        lay.addWidget(muted("Recording is not affected. The drive keeps its name; copying to it starts again."))
        bb = QDialogButtonBox(QDialogButtonBox.StandardButton.Cancel)
        self.ok = bb.addButton("Delete and return to service", QDialogButtonBox.ButtonRole.DestructiveRole)
        self.ok.clicked.connect(self.accept)
        bb.rejected.connect(self.reject)
        lay.addWidget(bb)
        for w in (self.destination, self.copied_by, self.who):
            w.textChanged.connect(self._update)
        self.checked.toggled.connect(self._update)
        self._update()

    def _update(self) -> None:
        self.ok.setEnabled(all(w.text().strip() for w in (self.destination, self.copied_by, self.who))
                           and self.checked.isChecked())

    def args(self) -> list[str]:
        return ["--admin-id", self.who.text().strip(), "--destination", self.destination.text().strip(),
                "--copied-by", self.copied_by.text().strip(),
                "--copied-on", self.copied_on.date().toString("yyyy-MM-dd"), "--checked"]


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
        self.swap_btn = icon_button("Swap and safely remove", "swap", c)
        self.swap_btn.setToolTip("Finish copying to the archive drive and make it safe to remove; take it out when "
                                 "this page says it is safe to remove")
        # Owner request 2026-11-11: "Keep using this drive" was always shown, though it only undoes a swap.
        # Now "Cancel swap", shown only while a swap is being prepared or a drive waits to be taken out.
        self.resume_btn = icon_button("Cancel swap", "play", c)
        self.resume_btn.setToolTip("Stop the swap and keep copying to this drive; or keep using a drive that was "
                                   "made safe to remove but has not been taken out")
        self.resume_btn.setVisible(False)
        self.ack_btn = icon_button("Acknowledge all problems…", "ok", c)
        # Field report 2026-11-13: when the copier cannot make the swapped drive safe to remove by itself, the
        # page offers the removal for exactly that drive.
        self.remove_btn = icon_button("Safely remove", "eject", c)
        self.remove_btn.setVisible(False)
        # Rarely needed (technical support): behind "More" instead of always on the page.
        self.more_btn = QPushButton(glyph("info", c, 16), " More")
        menu = QMenu(self.more_btn)
        self.arc_status_act = menu.addAction("Archive details")
        self.problems_act = menu.addAction("Copies that could not be checked (A-006)")
        self.more_btn.setMenu(menu)
        for b in (self.swap_btn, self.remove_btn, self.resume_btn, self.ack_btn, self.more_btn):
            row.addWidget(b)
        row.addStretch(1)
        al.addLayout(row)
        self.body.addWidget(arc)

        box = QGroupBox("Drives plugged in")
        bl = QVBoxLayout(box)
        self.table = table(["Drive", "Name", "What it is", "Size", "Model", "Serial number"])
        self.table.itemSelectionChanged.connect(self._update_actions)
        bl.addWidget(self.table)
        self.hint = muted()
        bl.addWidget(self.hint)
        self.problems = muted()
        bl.addWidget(self.problems)
        row = QHBoxLayout()
        # The list follows drives being plugged in and taken out by itself; this stays for technical support.
        self.refresh_btn = icon_button("Refresh", "refresh", c)
        self.refresh_btn.setToolTip("Look for drives again (the list also updates by itself)")
        self.show_all = QCheckBox("Show all drives")
        self.show_all.setToolTip("Also list the computer's own disks and card readers with no card in them")
        self.show_all.toggled.connect(lambda _on: self._show_drives())
        self.erase_btn = icon_button("Erase and set up…", "erase", c)
        self.designate_btn = icon_button("Use as archive drive…", "drives", c)
        self.eject_btn = icon_button("Safely remove…", "eject", c)
        self.return_btn = icon_button("Return to service…", "refresh", c)
        self.return_btn.setToolTip("After the office has copied and checked a full archive drive: delete its "
                                   "recordings and use it again")
        self.rename_btn = icon_button("Rename…", "edit", c)
        self.rename_btn.setToolTip("Give a Sentinel drive the permanent name on its sticker, e.g. SSD_1. "
                                   "Its recordings stay; the rename is recorded.")
        self.action_btns = (self.erase_btn, self.designate_btn, self.return_btn, self.rename_btn, self.eject_btn)
        for b in self.action_btns:
            row.addWidget(b)
        row.addStretch(1)
        row.addWidget(self.show_all)
        row.addWidget(self.refresh_btn)
        bl.addLayout(row)
        self.body.addWidget(box, 2)
        self.log = OutputLog()
        self.body.addWidget(CommandPanel(self.log, ctx.palette), 1)

        self.list = DriveList(self, self.log, self._show_drives)
        self.refresh_btn.clicked.connect(self.list.refresh)
        self.erase_btn.clicked.connect(self._erase)
        self.designate_btn.clicked.connect(self._designate)
        self.eject_btn.clicked.connect(self._eject)
        self.return_btn.clicked.connect(self._return)
        self.rename_btn.clicked.connect(self._rename)
        self.swap_btn.clicked.connect(self._swap)
        self.resume_btn.clicked.connect(self._resume)
        self.arc_status_act.triggered.connect(lambda: self._arc("status"))
        self.problems_act.triggered.connect(lambda: self._arc_cmd(["acknowledge", "--admin-id", _who()],
                                                                  "Archive problems"))
        self.ack_btn.clicked.connect(self._acknowledge)
        self.remove_btn.clicked.connect(self._remove_swapped)
        self.remove_btn.setEnabled(ctx.admin)
        self._removal: dict[str, Any] | None = None
        self._auto_remove_until: float | None = None
        self.ack_btn.setEnabled(ctx.admin)
        # Swapping is an operator task (every few days): operators ask the archive copier through
        # its request folder; erasing, designating and removing drives stay administrator-only
        # (review 2026-11-10).
        for b in (self.erase_btn, self.designate_btn, self.return_btn, self.rename_btn, self.eject_btn):
            b.setEnabled(ctx.admin)
        operator_ok = ctx.admin or bool(ctx.cfg is not None and ctx.cfg.storage.status_dir)
        for b in (self.swap_btn, self.resume_btn):
            b.setEnabled(operator_ok)
            if not operator_ok:
                b.setToolTip("Needs an administrator on this computer (no operator request folder is set up)")
        self._after_load: Any = None
        self._arc_running = True      # until the first status arrives; the command itself checks too
        self._loaded_once = False
        self._letters = drive_letters_now()
        # Field report 2026-11-13: the list stayed empty until Refresh list was pressed. It now follows drives being
        # plugged in or taken out by itself while the page is open.
        self.watch = QTimer(self)
        self.watch.setInterval(2000)
        self.watch.timeout.connect(self._poll_drives)
        self._update_actions()

    def on_show(self) -> None:
        # Always re-read: a drive may have been plugged in, or a setting (such as allowing erasing)
        # changed since the page was last open. Field report 2026-11-10: the list kept 'erase_disabled'.
        if not self._loaded_once:
            fill(self.table, [], empty="Looking for drives…")
        self._letters = drive_letters_now()
        self.list.refresh()
        self.watch.start()

    def on_hide(self) -> None:
        self.watch.stop()

    def _poll_drives(self) -> None:
        now = drive_letters_now()
        if now != self._letters:
            self._letters = now
            self.list.refresh()

    def on_status(self, read) -> None:  # noqa: ANN001
        arc = ((read.doc or {}).get("archive") or {}) if read is not None else {}
        self._arc_running = bool(arc.get("running"))
        self.resume_btn.setVisible(swap_under_way(arc))
        # Only the actions that apply now (GUI simplification 2026-11-13).
        codes = {str(a.get("code")) for a in ((read.doc or {}).get("alerts") or []) if isinstance(a, dict)}             if read is not None else set()
        for b in (self.swap_btn, self.more_btn):
            b.setVisible(bool(arc))
        self.ack_btn.setVisible(bool(arc) and "A-006" in codes)
        rem = arc.get("removal") if arc.get("running") else None
        # Only when the copier asks for a Windows removal (one drive in the dock). With a second drive in the dock it
        # says to take the drive out as it is: a removal there could stop the other drive too.
        self._removal = rem if (isinstance(rem, dict) and not rem.get("safe") and not rem.get("text")
                                and arc.get("state") == ST_REMOVE_MANUALLY) else None
        if isinstance(rem, dict) and rem.get("safe"):
            self._auto_remove_until = None       # the copier made it safe to remove by itself
        self._update_remove_btn()
        self._auto_remove()
        if not arc:
            self.arc_text.setText("Archive copying is off. Turn it on in Settings › Storage.")
            return
        bk = arc.get("backlog") or {}
        backlog = str(bk.get("text") or "")
        if not arc.get("running"):
            # Field report 2026-11-13: "Drive: unknown (archive copying is not running). Archive copying is not
            # running. Not yet archived: 0 h." said the same thing twice.
            secs = bk.get("seconds")
            waiting = f" {backlog}." if backlog and isinstance(secs, (int, float)) and secs > 0 else ""
            self.arc_text.setText("Archive copying is not running." + waiting)
            return
        msg = str(arc.get("message") or "")
        if self._removal is not None:
            # Only what to do now (field report 2026-11-13: "Drive: none inserted." while it was still plugged in).
            self.arc_text.setText(str(self._removal.get("text") or msg))
            return
        if not isinstance(bk.get("seconds"), (int, float)) or bk.get("seconds", 0) <= 0:
            backlog = ""                   # "Not yet archived: 0 h" says nothing
        parts = [archive_drives_text(arc), msg] + ([backlog] if backlog and backlog not in msg else [])
        self.arc_text.setText(" ".join(x for x in parts if x))

    def _cfg(self) -> str:
        return str(self.ctx.cfg_path)

    def _arc(self, *args: str, stop_note: str | None = None) -> None:
        # Resume stays available during a swap: it cancels it (review 2026-11-10).
        self.log.run(sentinel_argv("archive", args[0], "--config", self._cfg(), *args[1:]),
                     f"Archive {args[0]}", busy=[self.swap_btn], stop_note=stop_note)

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
                                     "the drive is ready to be taken out, then unplug it and insert the next one. "
                                     "To undo it, press Cancel swap.\n")
        else:
            self.log.appendPlainText("── Cancel swap: requested.\n")

    def _swap(self) -> None:
        if not self._arc_running:
            # Field report 2026-11-12: nothing answers a swap while the copier is stopped.
            from ...archive.status import COPIER_NOT_RUNNING

            self.problem(COPIER_NOT_RUNNING)
            return
        if not self.ctx.admin:
            self._operator_request("swap")
            return
        # Owner request 2026-11-13: one button does both. When the copier cannot make the drive safe to remove by
        # itself, the page does it as soon as the copier reports the drive finished (see _auto_remove).
        self._auto_remove_until = (self.ctx.clock or time.time)() + AUTO_REMOVE_S
        self.log.run(sentinel_argv("archive", "swap", "--config", self._cfg(), "--timeout", "600"),
                     "Swap and safely remove", busy=[self.swap_btn],
                     stop_note="If the archive copier was already preparing the drive, it carries on: wait until "
                               "this page says the drive is ready to be taken out, or press Cancel swap.",
                     on_done=lambda _c, _cmd: self._auto_remove())

    def _auto_remove(self) -> None:
        """After Swap and safely remove: the copier finished with the drive but could not eject it (it runs as a
        service); eject it from here, once."""
        until = self._auto_remove_until
        if until is None:
            return
        if (self.ctx.clock or time.time)() > until or (self.log.current is not None and self.log.current.running()):
            if (self.ctx.clock or time.time)() > until:
                self._auto_remove_until = None
            return
        d = self._swapped_drive()
        if d is None:
            return
        self._auto_remove_until = None
        self._run_eject(d)

    def _resume(self) -> None:
        """Cancel a swap / keep using the drive: its own command, even while a swap is waiting."""
        self._auto_remove_until = None
        if not self.ctx.admin:
            self._operator_request("resume")
            return
        cmd = Command(sentinel_argv("archive", "resume", "--config", self._cfg()), self)
        self.log.appendPlainText("── Cancel swap")
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
        self._loaded_once = True
        cur = selected_data(self.table)
        want = (cur.get("fingerprint") or cur.get("disk_id")) if isinstance(cur, dict) else None
        shown = [d for d in self.list.drives if self.show_all.isChecked() or not is_noise(d)]
        rows = [[", ".join(x.rstrip("\\") for x in _letters(d)) or "—", d.get("label") or "", _what(d),
                 tf.size(d["size_bytes"]) if isinstance(d.get("size_bytes"), (int, float)) else "",
                 d.get("model") or "",
                 "hidden by the dock" if d.get("serial_hidden") else (d.get("serial") or "")] for d in shown]
        fill(self.table, rows, shown, empty="No drives plugged in. Plug in a drive: it appears here by itself.")
        for i, d in enumerate(shown):
            note = str(d.get("message") or "")
            for j in range(self.table.columnCount()):
                it = self.table.item(i, j)
                if it is not None and note:
                    it.setToolTip(note)
        if want is not None:                  # keep the selection on the same drive after a refresh
            for i, d in enumerate(shown):
                if want in (d.get("fingerprint"), d.get("disk_id")):
                    self.table.selectRow(i)
                    break
        self._update_actions()
        self._update_remove_btn()
        self._auto_remove()
        self.problems.setText(("Erasing is blocked: " + "; ".join(self.list.problems)) if self.list.problems else "")
        then, self._after_load = self._after_load, None
        if then is not None:
            then()

    def _swapped_drive(self) -> dict[str, Any] | None:
        """The drive in the list that the copier prepared for removal and could not make safe by itself."""
        rem = self._removal
        if not rem:
            return None
        return next((d for d in self.list.drives if d.get("drive_uuid")
                     and f"drive_uuid:{d['drive_uuid']}" == rem.get("drive_id") and _letters(d)), None)

    def _update_remove_btn(self) -> None:
        d = self._swapped_drive()
        self.remove_btn.setVisible(d is not None)
        if d is not None:
            self.remove_btn.setText(f" Safely remove {d.get('label') or (self._removal or {}).get('label') or ''}".rstrip())
            self.swap_btn.setVisible(False)          # the swap is done; this is the next (and only) step

    def _remove_swapped(self) -> None:
        d = self._swapped_drive()
        if d is None:
            self.info("The drive is no longer plugged in.")
            return
        self._run_eject(d)

    def _update_actions(self) -> None:
        """Only the actions that apply to the selected drive are shown (GUI simplification 2026-11-13)."""
        d = selected_data(self.table)
        d = d if isinstance(d, dict) else None
        role, cls = (d or {}).get("role"), (d or {}).get("class")
        show = {
            self.erase_btn: d is not None and role is None and (bool(d.get("can_prepare"))
                                                               or d.get("refusal") == "erase_disabled"),
            self.designate_btn: d is not None and cls == "READY" and role != "archive",
            self.return_btn: d is not None and cls == "READY" and role == "archive",
            self.rename_btn: d is not None and cls == "READY",
            self.eject_btn: d is not None and bool(_letters(d)) and not is_noise(d),
        }
        for b, on in show.items():
            b.setVisible(on)
        if d is None:
            self.hint.setText("Select a drive to see what can be done with it."
                              if self._loaded_once and any(not is_noise(x) for x in self.list.drives) else "")
        elif not any(show.values()):
            self.hint.setText(str(d.get("message") or "Nothing can be done with this drive here."))
        else:
            self.hint.setText(str(d.get("message") or ""))

    def _selected(self) -> dict[str, Any] | None:
        d = selected_data(self.table)
        if d is None:
            self.info("Select a drive in the list first.")
        return d

    def _erase(self) -> None:
        self._erase_disk(self._selected())

    def _erase_disk(self, d: dict[str, Any] | None, *, fresh: bool = False) -> None:
        cfg = self.cfg
        if d is None or cfg is None:
            return
        if not fresh and d.get("refusal") == "erase_disabled" and cfg.drives.erase_enabled:
            # The row predates the setting being turned on: read the list again, then decide.
            want = d.get("disk_id")
            self._after_load = lambda: self._erase_disk(
                next((x for x in self.list.drives if x.get("disk_id") == want), None), fresh=True)
            self.list.refresh()
            return
        if not d.get("can_prepare"):
            self._cannot_erase(d)
            return
        chars = cfg.drives.serial_confirm_chars
        dlg = EraseDialog(d, chars, self)
        if dlg.exec() != QDialog.DialogCode.Accepted:
            return
        who, tail = dlg.who.text().strip(), dlg.tail.text().strip().upper()
        if not who or len(tail) != dlg.expected_len:
            what = f"the word {HIDDEN_SERIAL_WORD}" if dlg.hidden else f"exactly {chars} characters of the serial number"
            self.problem(f"Enter your name and {what}. Nothing was erased.")
            return
        seen = [*(["--fingerprint", str(d["fingerprint"])] if d.get("fingerprint") else []),
                *(["--expect-class", str(d["class"])] if d.get("class") else [])]
        self.log.run(sentinel_argv("drives", "prepare", "--config", self._cfg(), "--disk", str(d["disk_id"]),
                                   "--who", who, "--admin", "--yes", "--serial-tail", tail, *seen),
                     f"Erase and set up disk {d['disk_id']}", busy=[self.erase_btn],
                     on_done=lambda _c, _cmd: self.list.refresh())

    def _cannot_erase(self, d: dict[str, Any]) -> None:
        """Why 'Erase and set up' is refused, in plain words, with the way forward where there is one."""
        if d.get("refusal") != "erase_disabled":
            why = evidence_why(d.get("reasons") or []) if d.get("refusal") == "sentinel_evidence" else ""
            why = f"\n\nWhy: {why}" if why else ""
            self.problem(f"This drive cannot be erased and set up.\n\n{d.get('message') or ''}{why}"
                         "\n\nNothing was erased.")
            return
        text = ("Erasing drives is turned off on this computer, so Sentinel cannot set up this drive yet.\n\n"
                "An administrator can turn it on: Settings › Storage › \"Allow 'Erase and set up'\", then Save.")
        if not self.ctx.admin:
            self.problem(text + "\n\nNothing was erased.")
            return
        box = QMessageBox(QMessageBox.Icon.Information, "Sentinel", text, QMessageBox.StandardButton.Close, self)
        go = box.addButton("Open Storage settings", QMessageBox.ButtonRole.AcceptRole)
        box.setDefaultButton(go)
        box.exec()
        if box.clickedButton() is go:
            self.ctx.navigate("storage")

    def _designate(self) -> None:
        d = self._selected()
        if d is None:
            return
        if d.get("class") != "READY":
            self.problem("Sentinel has not set up this drive, so it cannot be the archive drive yet. "
                         "Select it and press Erase and set up first.\n\n(Formatting it in Disk Management is "
                         "not enough: setting up also records the drive's identity and checks it can be written.)")
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

    def _return(self) -> None:
        d = self._selected()
        if d is None:
            return
        if d.get("class") != "READY" or d.get("role") != "archive":
            self.problem("Select the archive drive that came back from the office (it shows as \"archive drive\" "
                         "in the list).")
            return
        letters = _letters(d)
        if not letters:
            self.problem("This drive has no drive letter. Unplug it and plug it in again.")
            return
        dlg = ReturnDialog(d, self)
        if dlg.exec() != QDialog.DialogCode.Accepted:
            return
        expect = ["--expect-uuid", str(d["drive_uuid"])] if d.get("drive_uuid") else []
        self.log.run(sentinel_argv("archive", "return", "--config", self._cfg(), "--drive", letters[0], *expect,
                                   *dlg.args()),
                     f"Return {d.get('label') or letters[0]} to service", busy=[self.return_btn],
                     on_done=lambda _c, _cmd: self.list.refresh())

    def _rename(self) -> None:
        """Owner request 2026-11-12: each drive carries a sticker with a permanent name (SSD_1, SSD_2, …)."""
        d = self._selected()
        if d is None:
            return
        if d.get("class") != "READY":
            self.problem("Only a drive Sentinel has set up can be renamed. Select it and press Erase and set up "
                         "first; it is then named automatically (SSD_1, SSD_2, …).")
            return
        letters = _letters(d)
        if not letters:
            self.problem("This drive has no drive letter. Unplug it and plug it in again.")
            return
        old = d.get("label") or letters[0]
        name, ok = QInputDialog.getText(
            self, "Rename drive", f"New permanent name for {old} (letters, digits and _; for example SSD_1).\n\n"
            "Write the same name on the drive's sticker. Its recordings stay; the rename is recorded.",
            text=str(old))
        name = (name or "").strip().upper()
        if not ok or not name or name == str(old).upper():
            return
        expect = ["--expect-uuid", str(d["drive_uuid"])] if d.get("drive_uuid") else []   # review 2026-11-12
        self.log.run(sentinel_argv("drives", "rename", "--config", self._cfg(), "--drive", letters[0],
                                   "--name", name, "--who", _who(), *expect),
                     f"Rename {old} to {name}", busy=[self.rename_btn], on_done=lambda _c, _cmd: self.list.refresh())

    def _eject(self) -> None:
        d = self._selected()
        if d is None:
            return
        if QMessageBox.question(self, "Sentinel", f"Safely remove disk {d['disk_id']}: {d.get('description', '')} "
                                f"({_what(d)})?") != QMessageBox.StandardButton.Yes:
            return
        self._run_eject(d)

    def _run_eject(self, d: dict[str, Any]) -> None:
        name = d.get("label") or f"disk {d['disk_id']}"
        seen = ["--fingerprint", str(d["fingerprint"])] if d.get("fingerprint") else []    # review 2026-11-12
        self.log.run(sentinel_argv("drives", "eject", "--config", self._cfg(), "--disk", str(d["disk_id"]), *seen),
                     f"Safely remove {name}", busy=[self.eject_btn, self.remove_btn],
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
