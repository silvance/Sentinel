"""Status, Recordings and System pages."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from PySide6.QtCore import QSize, Qt
from PySide6.QtGui import QFont
from PySide6.QtWidgets import (QBoxLayout, QComboBox, QFormLayout, QFrame, QGridLayout, QGroupBox, QHBoxLayout,
                               QLabel, QLineEdit, QPlainTextEdit, QScrollArea, QSizePolicy, QTabWidget,
                               QTreeWidget, QTreeWidgetItem, QVBoxLayout, QWidget)

from .. import textfmt as tf
from ..ledger_view import SpanKind, load_admin, load_timeline, pretty_json
from ..viewmodel import ReadKind, StatusRead, Tone, recorder_stopped, stopped_at
from .base import (CommandPanel, ElidedLabel, OutputLog, Page, ReadOnlyBar, capped, disclosure_button, fill,
                   icon_button, muted, selected_data, table)
from .procs import Command, sentinel_argv, service_argv
from .timeline import TimelineBar
from .window import page_for_code

SEVERITY = {0: "info", 1: "notice", 2: "warning", 3: "critical"}


def _ep(iso: Any) -> float | None:
    from datetime import datetime

    if not isinstance(iso, str) or not iso:
        return None
    try:
        return datetime.fromisoformat(iso.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


DETAIL_LABELS = {"outage_s": "Not recording for", "outage_seconds": "Not recording for",
                 "backlog_s": "Waiting to be copied", "status_age_s": "Age of its last status",
                 "down_s": "Not running for",
                 "last_end_reason": "Cause", "reason": "Cause", "consecutive_errors": "Errors in a row",
                 "open_problems": "Copies to look at", "attempts": "Attempts"}


def alert_details(a: dict[str, Any], tone: str, tz: Any, now: float) -> list[tuple[str, str]]:
    """What is shown under an alert, in words (GUI review 2026-11-17: it read "outage s: 420.0", "last end
    reason: ffmpeg_error" and a UTC time stamp). The recorder's own reason code stays, in brackets, for
    technical support; a detail this page does not know is shown as it is."""
    from ..ledger_view import GAP_REASON_WORDS

    out = [("Severity", str(a.get("severity_name") or tone).lower())]
    since = _ep(a.get("since_utc"))
    if since is not None:
        out.append(("Since", f"{tf.when(since, tz, now)} ({tf.duration(max(0.0, now - since))} ago)"))
    elif a.get("since_utc"):
        out.append(("Since (UTC)", str(a["since_utc"])))
    if a.get("recording_stopped"):
        out.append(("Recording", "stopped by this problem"))
    for key, value in (a.get("detail") or {}).items():
        k = str(key)
        label = DETAIL_LABELS.get(k, k.replace("_", " ").capitalize())
        if isinstance(value, (int, float)) and not isinstance(value, bool) and (k.endswith("_s") or
                                                                                 k.endswith("_seconds")):
            text = tf.duration(value)
            if k not in DETAIL_LABELS:
                label = k.rsplit("_", 1)[0].replace("_", " ").capitalize()
        elif label == "Cause" and str(value) in GAP_REASON_WORDS:
            text = f"{GAP_REASON_WORDS[str(value)]} ({value})"
        else:
            text = str(value)
        out.append((label, text))
    return out


class StatusPage(Page):
    """Operational overview (layout refinement 2026-11-10): a compact status header, then two columns
    sized by their content: alerts and details on the left, the live view on the right. Nothing is
    stretched to fill the window; free space stays below the content."""

    scrolls = False                      # it has its own scroll area (horizontal scrolling off)

    key = "status"
    title = "Status"
    description = "What the recorder is doing now."

    # Details grouped as an operator reads them: (group, [(key, label, page it opens)])
    GROUPS = [("Camera", [("camera", "Camera", "camera"), ("link", "Connection", "camera")]),
              ("Recording", [("last", "Last file saved", "recordings"), ("since", "Current run started", "recordings"),
                             ("sound", "Sound", "camera")]),
              ("Storage and archive", [("storage", "Recording storage", "storage"), ("archive", "Archive drive", "drives")]),
              ("Schedule and motion", [("schedule", "Schedule", "schedule"), ("motion", "Motion", "motion"),
                                       ("quiet", "No-motion fail-safe", "motion")]),
              ("System", [("clock", "Clock synchronised", "system"), ("service", "Recorder", "system")])]
    STACK_BELOW_PX = 1150      # narrower than this: the live view goes under the details
    HEADER_STACK_BELOW_PX = 1000   # narrower than this: the header's facts go under the state
    START_TEXT = "Start recording"
    STOPPED_UNKNOWN = "not reported (recorder stopped)"
    STALE_UNKNOWN = "unknown (status not updating)"

    def __init__(self, ctx, parent=None) -> None:  # noqa: ANN001
        super().__init__(ctx, parent)
        self.swap_btn = self.command("Archive drive", "drives", lambda: self.ctx.navigate("drives"),
                                     "Swap or check the archive drive")
        self.command("Diagnostic report…", "document", lambda: self.window().save_report(),
                     "Save a report for technical support")
        self.command("Lock computer", "lock", lambda: self.window().lock_computer(),
                     "Locks Windows. Recording continues.")

        # Everything below the page title scrolls in a small window instead of being clipped.
        content = QWidget()
        cl = QVBoxLayout(content)
        cl.setContentsMargins(0, 0, 4, 0)
        cl.setSpacing(8)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)   # scroll down only
        scroll.setWidget(content)
        self.body.addWidget(scroll, 1)

        # ---- compact status header: state + six key facts, two rows of label/value pairs
        header = QFrame()
        header.setObjectName("summary")
        # Side by side in a wide window; in a narrow one the facts go under the state (see resizeEvent), so
        # their values are not cut off mid-word (GUI review 2026-11-17).
        hl = QBoxLayout(QBoxLayout.Direction.LeftToRight, header)
        self.header_lay = hl
        hl.setContentsMargins(12, 8, 12, 8)
        hl.setSpacing(12)
        state_row = QHBoxLayout()
        state_row.setSpacing(12)
        self.big_icon = QLabel()
        self.big_icon.setFixedSize(32, 32)
        state_row.addWidget(self.big_icon, 0, Qt.AlignmentFlag.AlignVCenter)
        state = QVBoxLayout()
        state.setSpacing(0)
        self.headline = QLabel("…")
        f = self.headline.font()
        f.setPointSizeF(f.pointSizeF() + 4)
        f.setWeight(QFont.Weight.DemiBold)
        self.headline.setFont(f)
        state.addWidget(self.headline)
        self.supporting = muted()
        self.supporting.setMinimumWidth(300)     # "Since 13:59 today · No action needed." on one line
        self.supporting.setMaximumWidth(380)
        state.addWidget(self.supporting)
        # Shown only while the recorder is stopped (field report 2026-11-19): the way back to recording is
        # under the words that say it is stopped, not two pages away on System.
        self.start_btn = icon_button(self.START_TEXT, "play", ctx.palette.text)
        self.start_btn.setToolTip("Start the recorder again. Recording continues when this window is closed.")
        self.start_btn.clicked.connect(self._start)
        self.start_box = QWidget()               # hidden as a whole, so it takes no room while recording
        sb = QHBoxLayout(self.start_box)
        sb.setContentsMargins(0, 6, 0, 0)
        sb.addWidget(self.start_btn)
        sb.addStretch(1)
        self.start_box.setVisible(False)
        state.addWidget(self.start_box)
        state_row.addLayout(state)
        state_row.addStretch(1)
        hl.addLayout(state_row)
        sep = QFrame()
        sep.setFrameShape(QFrame.Shape.VLine)
        sep.setObjectName("sep")
        self.header_sep = sep
        hl.addWidget(sep)
        self.facts_grid = QGridLayout()
        self.facts_grid.setHorizontalSpacing(10)
        self.facts_grid.setVerticalSpacing(2)
        self.facts: dict[str, QLabel] = {}
        self._fact_cells: list[tuple[QLabel, QLabel]] = []
        vf = self.font()
        vf.setPointSizeF(vf.pointSizeF() + 1)
        for key, label in (("camera", "Camera"), ("duration", "Recording for"), ("storage", "Free space"),
                           ("archive", "Archive"), ("verified", "Last verified file"), ("updated", "Status")):
            lab = muted(label)
            lab.setWordWrap(False)
            val = ElidedLabel("—")
            val.setFont(vf)
            val.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
            # A narrow window cuts a long value (full text in its tooltip) rather than widening the page.
            val.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
            val.setMinimumWidth(80)
            self.facts[key] = val
            self._fact_cells.append((lab, val))
        self._fact_cols = 0
        self._layout_facts(3)
        hl.addLayout(self.facts_grid, 1)
        cl.addWidget(header)

        # ---- two columns; content-sized, no splitter stretching
        self.columns = QBoxLayout(QBoxLayout.Direction.LeftToRight)
        self.columns.setSpacing(12)
        left = QVBoxLayout()
        left.setSpacing(4)
        self.alerts_title = QLabel("Alerts")
        self.alerts_title.setObjectName("sectionTitle")
        left.addWidget(self.alerts_title)
        self.alerts = _tree(["Code", "Message"])
        self.alerts.setRootIsDecorated(True)
        self.alerts.setUniformRowHeights(False)
        self.alerts.itemActivated.connect(self._alert_activated)
        self.alerts.setToolTip("Click the arrow for details; double-click or Enter opens the page where it is dealt with")
        self.alerts.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.alerts.itemExpanded.connect(lambda _i: self._fit_tree(self.alerts, 8))
        self.alerts.itemCollapsed.connect(lambda _i: self._fit_tree(self.alerts, 8))
        left.addWidget(self.alerts)
        left.addSpacing(8)
        # GUI simplification 2026-11-13: the header already says what matters; the full list is one click away.
        self.details_btn = disclosure_button("Details")
        self.details_btn.toggled.connect(self._show_details)
        left.addWidget(self.details_btn)
        self.details = _tree(["Item", "Now"])
        self.details.setVisible(False)
        self.details.setRootIsDecorated(False)
        self.details.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.details.itemActivated.connect(self._detail_activated)
        self.details.setToolTip("Double-click or press Enter to open the matching page")
        self.items: dict[str, QTreeWidgetItem] = {}
        bold = self.font()
        bold.setBold(True)
        for group, rows in self.GROUPS:
            g = QTreeWidgetItem([group, ""])
            g.setFont(0, bold)
            g.setFlags(Qt.ItemFlag.ItemIsEnabled)
            g.setFirstColumnSpanned(True)
            self.details.addTopLevelItem(g)
            for k, text, page in rows:
                it = QTreeWidgetItem(["   " + text, "—"])
                it.setData(0, Qt.ItemDataRole.UserRole, page)
                self.details.addTopLevelItem(it)
                self.items[k] = it
        self.details.setColumnWidth(0, 210)
        left.addWidget(self.details)
        left.addStretch(1)                       # free space goes below the content
        self.columns.addLayout(left, 3)
        right = QVBoxLayout()
        right.setSpacing(4)
        lt = QLabel("Live view")
        lt.setObjectName("sectionTitle")
        right.addWidget(lt)
        from .liveview import LiveView

        self.live = LiveView(ctx, fixed=False)       # sizes its own picture: 16:9 for the width it gets
        right.addWidget(self.live)
        right.addStretch(1)
        self.columns.addLayout(right, 2)
        cl.addLayout(self.columns)
        cl.addStretch(1)
        self._fit_tree(self.details, 30)

    # ---- sizing
    def _layout_facts(self, cols: int) -> None:
        """Header facts in ``cols`` columns of label/value pairs (3 wide, 2 when the window is narrow)."""
        if cols == self._fact_cols:
            return
        self._fact_cols = cols
        g = self.facts_grid
        for lab, val in self._fact_cells:
            g.removeWidget(lab)
            g.removeWidget(val)
        for c in range(6):
            g.setColumnStretch(c, 0)
        for i, (lab, val) in enumerate(self._fact_cells):
            r, c = divmod(i, cols)
            g.addWidget(lab, r, c * 2, Qt.AlignmentFlag.AlignRight)
            g.addWidget(val, r, c * 2 + 1)
            g.setColumnStretch(c * 2 + 1, 1)

    @staticmethod
    def _fit_tree(t: QTreeWidget, max_rows: int) -> None:
        """Height = header + visible rows (at most ``max_rows``; scrolls beyond)."""
        rows, total = 0, 0
        it = t.topLevelItem(0)
        fallback = max(t.sizeHintForRow(0), t.fontMetrics().height() + 6)
        while it is not None and rows < max_rows:
            h = t.visualItemRect(it).height()
            total += h if h > 0 else fallback
            rows += 1
            it = t.itemBelow(it)
        head = 0 if t.isHeaderHidden() else t.header().sizeHint().height()
        t.setFixedHeight(head + max(total, 24) + 2 * t.frameWidth() + 2)

    def resizeEvent(self, e) -> None:  # noqa: ANN001, N802
        super().resizeEvent(e)
        narrow = self.width() < self.STACK_BELOW_PX
        self._layout_facts(2 if self.width() < 1400 else 3)
        self.columns.setDirection(QBoxLayout.Direction.TopToBottom if narrow else QBoxLayout.Direction.LeftToRight)
        stacked = self.width() < self.HEADER_STACK_BELOW_PX
        self.header_lay.setDirection(QBoxLayout.Direction.TopToBottom if stacked
                                     else QBoxLayout.Direction.LeftToRight)
        self.header_sep.setFrameShape(QFrame.Shape.HLine if stacked else QFrame.Shape.VLine)

    def _show_details(self, on: bool) -> None:
        self.details.setVisible(on)
        if on:
            self._fit_tree(self.details, 30)

    def on_show(self) -> None:
        self.live.start()

    def on_hide(self) -> None:
        self.live.stop()

    def _alert_activated(self, it: QTreeWidgetItem, _c: int) -> None:
        top = it if it.parent() is None else it.parent()
        self._open_alert(self.alerts.indexOfTopLevelItem(top), 0)

    def _detail_activated(self, it: QTreeWidgetItem, _c: int) -> None:
        page = it.data(0, Qt.ItemDataRole.UserRole)
        if page:
            self.ctx.navigate(page)

    def _open_alert(self, row: int, _col: int) -> None:
        item = self.alerts.topLevelItem(row)
        code = item.text(0) if item is not None else ""
        if code:
            self.ctx.navigate(page_for_code(code))

    def _set(self, key: str, text: str, tone: str = "") -> None:
        from .icons import glyph

        p = self.ctx.palette
        it = self.items[key]
        it.setText(1, text)
        it.setToolTip(1, text)
        color = {"ok": p.ok, "warning": p.warning, "critical": p.critical}.get(tone, p.text3)
        it.setIcon(0, glyph({"ok": "ok", "warning": "warning", "critical": "critical"}.get(tone, "info"), color, 16))

    def _fact(self, key: str, text: str, tone: str = "") -> None:
        lab = self.facts[key]
        lab.setText(text)
        lab.setToolTip(text)
        lab.setObjectName(tone)
        lab.style().polish(lab)

    # ---- data
    def on_status(self, read: StatusRead | None) -> None:
        from .icons import glyph, glyph_pixmap

        win = self.window()
        v = getattr(win, "view", None)
        if v is None or read is None:
            return
        p = self.ctx.palette
        icon = {Tone.OK: "ok", Tone.WARNING: "warning", Tone.CRITICAL: "critical"}.get(v.tone, "info")
        self.big_icon.setPixmap(glyph_pixmap(icon, p.tone(v.tone), 32))
        self.headline.setText(v.headline)
        self.headline.setStyleSheet(f"color: {p.tone(v.tone)};" if v.tone is not Tone.OK else "")
        self.supporting.setText(" ".join(x for x in (v.supporting, v.detail) if x))
        doc = read.doc or {}
        stopped = recorder_stopped(read)
        # Not while a new version is being put in place: recording restarts by itself then.
        updating = bool(getattr(win, "_update_in_progress", lambda: None)())
        busy = bool(getattr(win, "recorder_busy", lambda: False)())
        self.start_box.setVisible(stopped and not updating)
        self.start_btn.setEnabled(not busy)
        self.start_btn.setText(" Starting…" if busy else f" {self.START_TEXT}")
        rows: list[tuple[str, str, str, dict]] = []
        if read.kind is not ReadKind.OK and not stopped:
            rows.append(("X-001", "critical", "The recorder's status is not updating. The recorder may have "
                                              "stopped or this computer may be overloaded.", {}))
        # Review 2026-11-12: alerts in a document that is no longer updated are not current; X-001 says so.
        # The same holds for a recorder that was stopped: nothing measures them any more (2026-11-19).
        for a in (doc.get("alerts") or []) if read.kind is ReadKind.OK and not stopped else []:
            if isinstance(a, dict):
                sev = a.get("severity")
                tone = "critical" if sev == 3 else "warning" if sev == 2 else "info"
                rows.append((str(a.get("code", "")), tone, str(a.get("message", "")), a))
        expanded = {self.alerts.topLevelItem(i).text(0) for i in range(self.alerts.topLevelItemCount())
                    if self.alerts.topLevelItem(i).isExpanded()}
        self.alerts.clear()
        for code, tone, msg, a in rows:
            it = QTreeWidgetItem([code, msg])
            color = {"critical": p.critical, "warning": p.warning}.get(tone, p.info)
            it.setIcon(0, glyph(tone, color, 16))
            it.setToolTip(1, msg)
            for k2, v2 in alert_details(a, tone, self.ctx.tz, (self.ctx.clock or __import__("time").time)()):
                child = QTreeWidgetItem(["", f"{k2}: {v2}"])
                child.setFlags(Qt.ItemFlag.ItemIsEnabled)
                it.addChild(child)
            self.alerts.addTopLevelItem(it)
            it.setExpanded(code in expanded)
        if not rows:
            # A green tick and "No alerts." beside "Not recording" would read as all clear.
            it = QTreeWidgetItem(["", "No alerts are reported while the recorder is stopped." if stopped
                                  else "No alerts."])
            it.setIcon(0, glyph("info", p.text3, 16) if stopped else glyph("ok", p.ok, 16))
            it.setFlags(Qt.ItemFlag.ItemIsEnabled)
            self.alerts.addTopLevelItem(it)
        self.alerts_title.setText(f"Alerts ({len(rows)})" if rows else "Alerts")
        self.alerts.setHeaderHidden(not rows)           # "No alerts." needs no Code/Message columns
        self.alerts.resizeColumnToContents(0)
        self._fit_tree(self.alerts, 8)
        self._facts(doc, read)
        self._header(doc, read, v)
        self._fit_tree(self.details, 30)                 # measured now that rows are laid out

    def _header(self, doc: dict[str, Any], read: StatusRead, v) -> None:  # noqa: ANN001
        now = (self.ctx.clock or __import__("time").time)()
        tz = self.ctx.tz
        cfg = self.ctx.cfg
        link = str(doc.get("camera_link") or "")
        stopped = recorder_stopped(read)
        stale = read.kind is not ReadKind.OK or stopped
        unknown = self.STOPPED_UNKNOWN if stopped else self.STALE_UNKNOWN
        if cfg is not None and cfg.camera.pending:
            self._fact("camera", "not added yet", "warning")
        elif stopped:
            # "disconnected" blamed the camera; the recorder is simply not reading from it (2026-11-19).
            self._fact("camera", "not in use (recorder stopped)")
        elif stale:
            self._fact("camera", unknown)
        else:
            self._fact("camera", link.replace("_", " ") or "—", "ok" if link == "connected" else "warning")
        since = getattr(self.window(), "recording_since_epoch", None) or _ep((doc.get("recording") or {})
                                                                               .get("run_started_utc"))
        if v.live and since:
            self._fact("duration", f"{tf.duration(now - since)} (since {tf.when(since, tz, now)})")
        elif v.live:
            self._fact("duration", "—")
            self.facts["duration"].setToolTip("Recording; the start time is not known yet")
        else:
            self._fact("duration", "not recording now", "" if doc.get("overall") in ("PAUSED", "WATCHING")
                       else "warning")
        st = doc.get("storage") or {}
        free, secs = st.get("free_bytes"), st.get("seconds_until_attention")
        text = f"{tf.size(free)} free" if isinstance(free, (int, float)) else "—"
        if isinstance(secs, (int, float)):
            text += f" · {tf.approx_remaining(secs)} left"
        self._fact("storage", text, {"warning": "warning", "critical": "critical"}.get(st.get("level"), ""))
        arc = doc.get("archive") or {}
        if stale:
            self._fact("archive", unknown)
        elif not arc:
            self._fact("archive", "not enabled")
        elif not arc.get("running"):
            self._fact("archive", "copying not running", "warning")
        else:
            backlog = arc.get("backlog") or {}
            secs = backlog.get("seconds")
            # "Not yet archived: 0 h" says nothing (GUI review 2026-11-17: it also pushed the rest out of view).
            bk = str(backlog.get("text") or "") if not isinstance(secs, (int, float)) or secs > 0 else ""
            label = arc.get("drive_label") or "no drive"
            drives = [d for d in arc.get("drives") or [] if isinstance(d, dict)]
            uses = {d.get("use") for d in drives}
            if "take_out" in uses:
                label += " · a drive is ready to be taken out"
            elif "full" in uses:
                label += " · a full drive is waiting to be swapped"
            elif "standby" in uses:
                other = next((d.get("label") for d in drives if d.get("use") == "standby" and d.get("label")), None)
                label += f" · {other} standing by" if other else " · second drive standing by"
            self._fact("archive", label + (f" · {bk}" if bk else ""),
                       "" if arc.get("drive_label") else "warning")
        last = (doc.get("last_segment") or {}).get("end_utc")
        le = _ep(last)
        self._fact("verified", f"{tf.when(le, tz, now, seconds=True)} (SHA-256 sealed)" if le else "none yet")
        self._fact("updated", v.updated if read.kind is ReadKind.OK or stopped else "not updating (X-001)",
                   "" if read.kind is ReadKind.OK or stopped else "critical")

    def _facts(self, doc: dict[str, Any], read: StatusRead) -> None:
        cfg, tz, now = self.ctx.cfg, self.ctx.tz, (self.ctx.clock or __import__("time").time)()
        if cfg is not None:
            from ...launcher.setcamera import current_address

            host, path, port = current_address(cfg)
            self._set("camera", f"{cfg.camera.name} — not added yet" if cfg.camera.pending else
                      f"{cfg.camera.name} — {host}:{port}/{path}" if host else f"{cfg.camera.name} — {cfg.camera.id}",
                      "warning" if cfg.camera.pending else "")
        stopped = recorder_stopped(read)
        stale = read.kind is not ReadKind.OK or stopped
        unknown = self.STOPPED_UNKNOWN if stopped else self.STALE_UNKNOWN
        link = ("not in use (recorder stopped)" if stopped else unknown if stale
                else str(doc.get("camera_link") or "—"))
        self._set("link", link.replace("_", " "), "ok" if link == "connected" else "warning" if link != "—" and
                  not stale else "")
        last = doc.get("last_segment") or {}
        le = _ep(last.get("end_utc"))
        self._set("last", tf.when(le, tz, now) if le else "none yet")
        rec = doc.get("recording") or {}
        since = _ep(rec.get("run_started_utc"))
        self._set("since", tf.when(since, tz, now) if since else "—")
        # Sound (2026-11-18): what the recorder says the current recording really holds.
        snd = rec.get("audio") if isinstance(rec.get("audio"), dict) else None
        if cfg is None or not cfg.camera.include_audio:
            self._set("sound", "not recorded (switched off)")
        elif stopped:
            self._set("sound", "switched on (nothing is recorded while the recorder is stopped)")
        elif stale or snd is None:
            self._set("sound", "switched on; no report from the recorder yet")
        else:
            stored = snd.get("stored") if isinstance(snd.get("stored"), dict) else None
            good = bool(stored and stored.get("codec")) and not snd.get("suspended")
            waiting = stored is None and not snd.get("suspended") and snd.get("action") != "off"
            self._set("sound", str(snd.get("text") or "—"), "ok" if good else "" if waiting else "warning")
        st = doc.get("storage") or {}
        free = st.get("free_bytes")
        secs = st.get("seconds_until_attention")
        txt = f"{tf.size(free)} free" if isinstance(free, (int, float)) else "—"
        if isinstance(secs, (int, float)):
            txt += f", {tf.approx_remaining(secs)} of recording left"
        self._set("storage", txt, {"ok": "ok", "warning": "warning", "critical": "critical"}.get(st.get("level"), ""))
        arc = doc.get("archive") or {}
        if stale:
            self._set("archive", "not reported (recorder stopped); see Drives" if stopped else unknown)
        else:
            from .drive_pages import archive_message

            self._set("archive", archive_message(arc) or ("not enabled" if not arc else "—"),
                      "" if not arc else "ok" if arc.get("running") else "warning")
        sch = doc.get("schedule") or {}
        if not sch:
            self._set("schedule", "off — records all the time")
        else:
            nxt = sch.get("next_change_text")
            self._set("schedule", ("paused" if sch.get("paused") else "on")
                      + (f"; next change {nxt} ({sch.get('next_change_is')})" if nxt else ""))
        mo = doc.get("motion") or {}
        if not mo:
            self._set("motion", "not in use")
        elif stopped:
            self._set("motion", "not watching while the recorder is stopped")
        else:
            broken = mo.get("healthy") is False
            state = ("waiting for motion" if mo.get("waiting") else
                     "detector not working (recording all the time)" if broken else "watching")
            self._set("motion", f"{state}; now: {mo.get('mode_now', '—')}", "warning" if broken else "ok")
        qt = mo.get("quiet") if isinstance(mo.get("quiet"), dict) else None
        if qt is None:
            self._set("quiet", "off")
        else:
            self._set("quiet", str(qt.get("text") or "—"), "warning" if qt.get("active") else "ok")
        svc = doc.get("service") or {}
        sync = svc.get("clock_synchronized")
        if stopped:                              # the recorder is what checks it
            self._set("clock", "not checked while the recorder is stopped")
        else:
            self._set("clock", "yes" if sync is True else "no (T-002)" if sync is False else "unknown",
                      "ok" if sync is True else "warning" if sync is False else "")
        up = svc.get("uptime_s")
        if stopped:
            at = stopped_at(doc)
            self._set("service", "stopped" + (f" ({tf.when(at, tz, now)})" if at is not None else ""), "critical")
        elif read.kind is ReadKind.OK:
            self._set("service", f"running, version {svc.get('version', '?')}"
                      + (f", up {tf.duration(up)}" if isinstance(up, (int, float)) else ""), "ok")
        else:
            self._set("service", "not responding", "critical")

    # ---- start a stopped recorder
    def _start(self) -> None:
        from .base import RUNNING_ALREADY, STARTED

        self.start_btn.setEnabled(False)
        self.start_btn.setText(" Starting…")

        def done(text: str) -> None:
            self.start_btn.setText(f" {self.START_TEXT}")
            self.start_btn.setEnabled(True)
            # A restart asked for at the same moment answers for both; its good news is good news here too.
            if not text.startswith((STARTED, RUNNING_ALREADY, "The recorder was restarted")):
                self.problem(text)

        self.ctx.start(on_done=done)


def _tree(headers: list[str]) -> QTreeWidget:
    t = QTreeWidget()
    t.setHeaderLabels(headers)
    t.setRootIsDecorated(False)
    t.setUniformRowHeights(True)
    t.setAlternatingRowColors(True)
    t.setAllColumnsShowFocus(True)
    t.header().setStretchLastSection(True)
    t.setIconSize(QSize(16, 16))
    return t


class RecordingsPage(Page):
    key = "recordings"
    title = "Recordings"
    description = "What was recorded, gaps and their reasons, and motion."

    def __init__(self, ctx, parent=None) -> None:  # noqa: ANN001
        super().__init__(ctx, parent)
        self.actions.addWidget(QLabel("Show the last"))
        self.hours = QComboBox()
        for h, t in ((6, "6 hours"), (24, "24 hours"), (72, "3 days"), (168, "7 days")):
            self.hours.addItem(t, h)
        self.hours.setCurrentIndex(1)
        self.hours.currentIndexChanged.connect(self.on_show)
        self.actions.addWidget(self.hours)
        self.command("Refresh", "refresh", self.on_show)
        tl_box = QGroupBox("Timeline")
        tb = QVBoxLayout(tl_box)
        self.summary = muted()
        tb.addWidget(self.summary)
        self.bar = TimelineBar(ctx.palette)
        self.bar.setToolTip("Click a point in time to find it in the table below")
        self.bar.clicked_at.connect(self._select_time)
        tb.addWidget(self.bar)
        tb.addWidget(muted("Green = recorded · red hatching = not recorded · yellow cross-hatching = damaged · "
                           "grey dots = no information · blue strip along the top = motion"))
        self.body.addWidget(tl_box)
        tabs = QTabWidget()
        self.spans = table(["From", "To", "Length", "What", "Reason"])
        self.motion = table(["Motion from", "To", "Length"])
        tabs.addTab(self.spans, "Recorded and not recorded")
        tabs.addTab(self.motion, "Motion")
        self.body.addWidget(tabs, 1)

    def on_palette(self) -> None:
        self.bar.p = self.ctx.palette
        self.bar.update()

    def on_show(self) -> None:
        cfg = self.cfg
        if cfg is None:
            return
        import time

        now = (self.ctx.clock or time.time)()
        hours = float(self.hours.currentData() or 24)
        db = cfg.ledger_snapshot_path or (cfg.data_path / "sentinel.db")
        live = bool(getattr(self.window(), "view", None) and self.window().view.live)
        # Review 2026-11-12: the hour being recorded is sealed only at its end and the window's copy of the
        # ledger is refreshed every few minutes, so allow a whole recording file plus that delay as "live".
        slack = cfg.recording.segment_seconds * 1.5 + 600.0
        read = self.ctx.read
        run = _ep(((read.doc or {}).get("recording") or {}).get("run_started_utc")) \
            if read is not None and read.kind is ReadKind.OK else None
        tl = load_timeline(db, cfg.camera.id, now, hours=hours, live=live, live_slack_s=slack,
                           live_since=run if live else None)
        self.bar.set_timeline(tl, self.ctx.tz)
        tz = self.ctx.tz
        if not tl.available:
            self.summary.setText("The recording log cannot be read from this account.")
        else:
            self.summary.setText(f"Recorded {tf.duration(tl.recorded_s)}; not recorded {tf.duration(tl.missing_s)} "
                                 f"in {len(tl.interruptions)} gap(s).")
        words = {SpanKind.RECORDED: "recorded", SpanKind.LIVE: "recording now", SpanKind.GAP: "not recorded",
                 SpanKind.DAMAGED: "damaged", SpanKind.NO_INFO: "no information"}
        rows = [[tf.when(s.start, tz, now, seconds=True), "now" if s.ongoing else tf.when(s.end, tz, now, seconds=True),
                 tf.duration(s.seconds), words.get(s.kind, str(s.kind)), s.reason]
                for s in reversed(tl.spans)]
        self._span_times = [(s.start, s.end) for s in reversed(tl.spans)]
        fill(self.spans, rows, empty="Nothing in this period." if tl.available else
             "The recording log cannot be read from this account.")
        fill(self.motion, [[tf.when(a, tz, now, seconds=True), tf.when(b, tz, now, seconds=True), tf.duration(b - a)]
                           for a, b in reversed(tl.motion)], empty="No motion marked in this period.")


    def _select_time(self, epoch: float) -> None:
        for i, (a, b) in enumerate(getattr(self, "_span_times", [])):
            if a <= epoch <= b:
                self.spans.selectRow(i)
                self.spans.scrollToItem(self.spans.item(i, 0))
                return


class SystemPage(Page):
    key = "system"
    title = "System"
    description = "Recorder service, time, storage, diagnostics."

    def __init__(self, ctx, parent=None) -> None:  # noqa: ANN001
        super().__init__(ctx, parent)
        self.body.addWidget(ReadOnlyBar(ctx, "the service"))
        tabs = QTabWidget()
        self.body.addWidget(tabs, 1)

        # --- service
        c = ctx.palette.text
        svc = QWidget()
        sl = QVBoxLayout(svc)
        sl.setSpacing(10)
        rec = QGroupBox("Recorder")
        rl = QHBoxLayout(rec)
        self.b_status = icon_button("Check service", "test", c)
        self.b_check = icon_button("Check settings", "document", c)
        self.b_restart = icon_button("Restart recorder", "refresh", c)
        self.b_stop = icon_button("Stop recorder…", "stop", c)
        for b in (self.b_status, self.b_check, self.b_restart, self.b_stop):
            rl.addWidget(b)
        rl.addStretch(1)
        sl.addWidget(rec)
        inst = QGroupBox("Windows service (starts recording with Windows)")
        il = QVBoxLayout(inst)
        form = QFormLayout()
        self.time_server = QLineEdit()
        self.time_server.setPlaceholderText("local time server (host name or IP), optional")
        form.addRow("Time server", self.time_server)
        self.readers = QComboBox()
        self.readers.addItem("Members of 'Sentinel Operators' (recommended)", "group")
        self.readers.addItem("All users of this computer", "users")
        form.addRow("Who may view the status", self.readers)
        il.addWidget(capped(_wrap(form)))
        row = QHBoxLayout()
        self.b_install = icon_button("Install / repair service…", "shield", c)
        self.b_uninstall = icon_button("Remove service…", "remove", c)
        row.addWidget(self.b_install)
        row.addWidget(self.b_uninstall)
        row.addStretch(1)
        il.addLayout(row)
        sl.addWidget(inst)
        ops = QGroupBox("Operators (Windows group 'Sentinel Operators': may view the status)")
        row2 = QHBoxLayout(ops)
        self.b_ops = icon_button("Show members", "people", c)
        self.b_ops_add = icon_button("Add operator…", "add", c)
        self.b_ops_del = icon_button("Remove operator…", "remove", c)
        for b in (self.b_ops, self.b_ops_add, self.b_ops_del):
            row2.addWidget(b)
        row2.addStretch(1)
        sl.addWidget(ops)
        self.b_ops.clicked.connect(self._operators)
        self.b_ops_add.clicked.connect(lambda: self._operator_change(True))
        self.b_ops_del.clicked.connect(lambda: self._operator_change(False))
        self.b_stop.clicked.connect(self._stop)
        for g in (rec, inst, ops):          # content height only; the output below takes the rest
            g.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Maximum)
        self.log = OutputLog()
        sl.addWidget(CommandPanel(self.log, ctx.palette), 1)
        sl.addStretch(1)                    # spare height below the boxes, not between them
        tabs.addTab(svc, "Service")
        self.b_status.clicked.connect(lambda: self._run(service_argv("status", "--config", self._cfgp()), "Service status"))
        self.b_check.clicked.connect(lambda: self._run(sentinel_argv("check-config", "--config", self._cfgp()),
                                                       "Check settings"))
        self.b_install.clicked.connect(self._install)
        self.b_uninstall.clicked.connect(self._uninstall)
        self.b_restart.clicked.connect(self._restart)
        for b in (self.b_install, self.b_uninstall, self.b_restart, self.b_stop, self.b_ops_add, self.b_ops_del):
            b.setEnabled(ctx.admin)

        # --- diagnostics
        diag = QWidget()
        dl = QVBoxLayout(diag)
        row = QHBoxLayout()
        b = icon_button("Refresh", "refresh", c)
        b.clicked.connect(self.on_show)
        row.addWidget(b)
        b = icon_button("Diagnostic report…", "document", c)
        b.clicked.connect(lambda: self.window().save_report())
        row.addWidget(b)
        b = icon_button("Verify installed files", "test", c)
        b.clicked.connect(lambda: self._run(sentinel_argv("about", "--verify"), "Verify installed files"))
        row.addWidget(b)
        row.addStretch(1)
        dl.addLayout(row)
        inner = QTabWidget()
        self.events = table(["Time (UTC)", "Severity", "Category", "Code", "Message"])
        self.gaps = table(["Gap", "Kind", "Start (UTC)", "End (UTC)", "Seconds", "Reason"])
        self.raw = QPlainTextEdit()
        self.raw.setObjectName("log")
        self.raw.setReadOnly(True)
        inner.addTab(self.events, "Events")
        inner.addTab(self.gaps, "Gaps")
        inner.addTab(self.raw, "Status data")
        dl.addWidget(inner, 1)
        tabs.addTab(diag, "Diagnostics")

        # --- check and history (owner request 2026-11-10)
        hist = QWidget()
        hl = QVBoxLayout(hist)
        top = QHBoxLayout()
        self.b_checkall = icon_button("Check everything", "test", c)
        self.b_checkall.setToolTip("Check the whole chain: settings, recorder, camera, saved recordings, alerts")
        self.b_checkall.clicked.connect(self._check_all)
        top.addWidget(self.b_checkall)
        self.check_result = QLabel()
        self.check_result.setWordWrap(True)
        top.addWidget(self.check_result, 1)
        hl.addLayout(top)
        self.checks = table(["Check", "Result", "Detail"])
        self.checks.setMaximumHeight(210)
        fill(self.checks, [], empty="Press Check everything to check the settings, the recorder, the camera, the "
                                    "saved recordings and the alerts.")
        hl.addWidget(self.checks)
        good = QGroupBox("Last known good settings")
        gl = QHBoxLayout(good)
        self.good_label = muted()
        gl.addWidget(self.good_label, 1)
        self.b_good = icon_button("Put them back…", "refresh", c)
        self.b_good.clicked.connect(self._restore_good)
        gl.addWidget(self.b_good)
        hl.addWidget(good)
        hbox = QGroupBox("Settings history (every save keeps the previous settings)")
        hb = QVBoxLayout(hbox)
        self.history = table(["Saved (replaced at)", "File"])
        hb.addWidget(self.history)
        hrow = QHBoxLayout()
        self.b_undo = icon_button("Roll back last change…", "refresh", c)
        self.b_undo.clicked.connect(lambda: self._restore_history(latest=True))
        self.b_restore = icon_button("Restore selected…", "document", c)
        self.b_restore.clicked.connect(lambda: self._restore_history(latest=False))
        hrow.addWidget(self.b_undo)
        hrow.addWidget(self.b_restore)
        hrow.addWidget(muted("A restore keeps the current settings in the history too, so it can be undone. "
                             "After a change Sentinel checks the recorder for up to 3 minutes and puts the last "
                             "known good settings back by itself if it does not work."), 1)
        hb.addLayout(hrow)
        hl.addWidget(hbox, 1)
        tamper = QGroupBox("Recording log against a drive (tamper evidence)")
        tl = QVBoxLayout(tamper)
        trow = QHBoxLayout()
        self.b_tamper = icon_button("Check the log against a drive…", "shield", c)
        self.b_tamper.setToolTip("Archive and review drives carry the recording log's fingerprint from when they "
                                 "were written; the log on this computer must still match it")
        self.b_tamper.clicked.connect(self._check_against_drive)
        trow.addWidget(self.b_tamper)
        trow.addWidget(muted("Pick an archive drive or a review drive. Any later change to the recording log on "
                             "this computer shows up as ALTERED."), 1)
        tl.addLayout(trow)
        self.tamper_log = OutputLog()
        tl.addWidget(CommandPanel(self.tamper_log, ctx.palette))
        hl.addWidget(tamper)
        for b in (self.b_good, self.b_undo, self.b_restore, self.b_tamper):
            b.setEnabled(ctx.admin)
        tabs.addTab(hist, "Check and history")

        # --- updates (owner request 2026-11-10)
        from .update_page import UpdatesTab

        self.updates = UpdatesTab(ctx)
        tabs.addTab(self.updates, "Updates")

        # --- configuration file
        conf = QWidget()
        cl = QVBoxLayout(conf)
        cl.addWidget(muted("Rarely changed settings (storage folders, time accuracy, advanced archive options) are "
                           "only in the configuration file. After editing it, Sentinel shows the new settings; "
                           "use Restart recorder to apply them."))
        row = QHBoxLayout()
        b = icon_button("Open in Notepad", "document", c)
        b.setEnabled(ctx.admin)
        b.clicked.connect(self._open_config)
        row.addWidget(b)
        row.addStretch(1)
        cl.addLayout(row)
        self.config_text = QPlainTextEdit()
        self.config_text.setObjectName("log")
        self.config_text.setReadOnly(True)
        cl.addWidget(self.config_text, 1)
        tabs.addTab(conf, "Configuration file")

    def _cfgp(self) -> str:
        return str(self.ctx.cfg_path or "")

    # ---- check and history
    def _load_history(self) -> None:
        from ...selfcheck import good_path, history, known_good_info

        path = self.ctx.cfg_path
        if path is None:
            return
        import time as _t

        now = _t.time()
        files = history(path)
        fill(self.history, [[tf.when(f.stat().st_mtime, self.ctx.tz, now, seconds=True), f.name] for f in files],
             files, empty="No earlier settings yet.")
        info = known_good_info(path)
        if good_path(path).is_file() and info:
            when = info.get("checked_utc", "")
            self.good_label.setText(f"Saved when the self-check passed ({when}).")
            self.b_good.setEnabled(self.ctx.admin)
        else:
            self.good_label.setText("None yet: they are saved the first time the self-check passes "
                                    "(as administrator).")
            self.b_good.setEnabled(False)

    def _check_all(self) -> None:
        rep = self.window().check_now()
        if rep is None:
            return
        word = {True: "OK", False: "FAILED", None: "—"}
        fill(self.checks, [[c.name, "note" if c.warning else word[c.ok], c.detail] for c in rep.checks])
        self.check_result.setText(rep.summary())
        self.check_result.setObjectName("ok" if rep.ok else "critical")
        self.check_result.style().polish(self.check_result)
        self._load_history()

    def _check_against_drive(self) -> None:
        from PySide6.QtWidgets import QFileDialog

        folder = QFileDialog.getExistingDirectory(self, "Archive or review drive (its top folder)")
        if folder:
            self.tamper_log.run(sentinel_argv("ledger", "verify", "--config", self._cfgp(), "--anchors-from", folder),
                                "Check the recording log against " + folder)

    def _restore_good(self) -> None:
        from PySide6.QtWidgets import QMessageBox

        if QMessageBox.question(self, "Sentinel", "Put back the last known good settings and restart the "
                                "recorder? The current settings are kept in the history.") \
                == QMessageBox.StandardButton.Yes:
            self.window().rollback_to_known_good()
            self._load_history()

    def _restore_history(self, latest: bool) -> None:
        from PySide6.QtWidgets import QMessageBox

        from ...config import ConfigError, load_config
        from ...selfcheck import history, restore

        path = self.ctx.cfg_path
        if path is None:
            return
        files = history(path)
        src = files[0] if latest and files else (selected_data(self.history) if not latest else None)
        if src is None:
            self.info("There is nothing to roll back to." if latest else "Select an entry in the history first.")
            return
        if QMessageBox.question(self, "Sentinel", f"Restore the settings from {src.name} and restart the recorder? "
                                "The current settings are kept in the history.") != QMessageBox.StandardButton.Yes:
            return
        try:
            restore(path, src)
            cfg = load_config(path)
        except (ConfigError, OSError) as exc:
            self.problem(f"Those settings could not be restored: {exc}")
            return
        win = self.window()
        win._config_changed(cfg)
        for page in win.pages.values():
            if hasattr(page, "load"):
                page.load()
        win.restart_recorder()
        win.watch_after_change()
        self._load_history()

    def _run(self, argv: list[str], title: str) -> None:
        self.log.run(argv, title)

    def on_show(self) -> None:
        cfg = self.cfg
        if cfg is None:
            return
        db = cfg.ledger_snapshot_path or (cfg.data_path / "sentinel.db")
        data = load_admin(db, cfg.camera.id)
        fill(self.events, [[e.get("ts_utc", ""), e.get("severity", ""), e.get("category", ""), e.get("code", ""),
                            e.get("message", "")] for e in data.events],
             empty="No events to show (none recorded yet, or the recording log cannot be read from this account).")
        fill(self.gaps, [[g.get("gap_id", ""), g.get("kind", ""), g.get("start_utc") or "", g.get("end_utc") or "open",
                          f"{g['duration_s']:.1f}" if isinstance(g.get("duration_s"), (int, float)) else "",
                          g.get("reason", "")] for g in data.recent_gaps],
             empty="No gaps to show (none recorded yet, or the recording log cannot be read from this account).")
        read = self.ctx.read
        self.raw.setPlainText(pretty_json(read.doc) if read is not None and read.doc else "No status available.")
        self._load_history()
        self.updates.refresh()
        try:
            self.config_text.setPlainText(Path(self._cfgp()).read_text(encoding="utf-8"))
        except OSError as exc:
            self.config_text.setPlainText(f"The configuration file cannot be read from this account ({exc}).")

    def _install(self) -> None:
        from PySide6.QtWidgets import QMessageBox

        args = ["install", "--config", self._cfgp(), "--status-readers", str(self.readers.currentData())]
        if self.time_server.text().strip():
            args += ["--time-server", self.time_server.text().strip()]
        dry = Command(service_argv(*args, "--dry-run"))
        self.log.appendPlainText("── Installation plan (nothing is changed yet)")
        dry.output.connect(self.log.appendPlainText)

        def go(code: int) -> None:
            if code != 0:
                self.log.appendPlainText("── The plan could not be made; nothing was changed.\n")
                return
            if QMessageBox.question(self, "Sentinel", "Install the recorder service as shown in the plan?\n\n"
                                    "This registers the Windows service, sets folder permissions, power settings "
                                    "and time synchronisation.") == QMessageBox.StandardButton.Yes:
                self._run(service_argv(*args), "Install service")

        dry.finished.connect(go)
        dry.start()
        self._dry = dry

    def _uninstall(self) -> None:
        from PySide6.QtWidgets import QMessageBox

        if QMessageBox.question(self, "Sentinel", "Remove the recorder service? Recording stops. Recordings and "
                                "settings are kept.") == QMessageBox.StandardButton.Yes:
            self._run(service_argv("uninstall"), "Remove service")

    def _restart(self) -> None:
        self.b_restart.setEnabled(False)
        self.log.appendPlainText("── Restarting the recorder…")

        def done(text: str) -> None:
            self.b_restart.setEnabled(True)
            self.log.appendPlainText(text + "\n")
            watch = getattr(self.window(), "watch_after_change", None)
            if text.startswith("The recorder was restarted") and callable(watch):
                watch()        # review 2026-11-12: same check (and way back) as after saving settings

        self.ctx.restart(on_done=done)

    def _stop(self) -> None:
        from PySide6.QtWidgets import QMessageBox

        if QMessageBox.question(self, "Sentinel", "Stop the recorder? Nothing is recorded until it is started "
                                "again (Start recording on the Status page, Restart recorder here, or "
                                "restarting this computer).") == \
                QMessageBox.StandardButton.Yes:
            self._run(sentinel_argv("stop", "--config", self._cfgp()), "Stop recorder")

    # ---- the "Sentinel Operators" group (bench step 1.6), review 2026-11-10
    def _operators(self) -> None:
        self._run(["net", "localgroup", "Sentinel Operators"], "Sentinel Operators: members")

    def _operator_change(self, add: bool) -> None:
        import re

        from PySide6.QtWidgets import QInputDialog, QMessageBox

        name, ok = QInputDialog.getText(self, "Sentinel", "Windows account name (for example agent1 or "
                                        "DOMAIN\\agent1):")
        name = name.strip()
        if not ok or not name:
            return
        if not re.fullmatch(r"[A-Za-z0-9._ -]+(\\[A-Za-z0-9._ -]+)?", name) or len(name) > 104:
            self.problem("That is not a Windows account name. Use letters, digits, '.', '_', '-' and spaces, "
                         "optionally DOMAIN\\name.")
            return
        verb = "Add" if add else "Remove"
        if QMessageBox.question(self, "Sentinel", f"{verb} {name} {'to' if add else 'from'} 'Sentinel Operators'?"
                                ) != QMessageBox.StandardButton.Yes:
            return
        self._run(["net", "localgroup", "Sentinel Operators", name, "/add" if add else "/delete"],
                  f"{verb} operator {name}")

    def _open_config(self) -> None:
        import subprocess

        try:
            subprocess.Popen(["notepad.exe", self._cfgp()])  # noqa: S603,S607 - fixed program
        except OSError as exc:
            self.problem(f"Notepad could not be opened: {exc}")


def _wrap(layout: QFormLayout) -> QWidget:
    w = QWidget()
    layout.setContentsMargins(0, 0, 0, 0)
    w.setLayout(layout)
    return w
