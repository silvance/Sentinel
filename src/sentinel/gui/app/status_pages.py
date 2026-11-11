"""Status, Recordings and System pages."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from PySide6.QtCore import QSize, Qt
from PySide6.QtGui import QFont
from PySide6.QtWidgets import (QComboBox, QFormLayout, QFrame, QGroupBox, QHBoxLayout, QLabel, QLineEdit,
                               QPlainTextEdit, QPushButton, QSplitter, QTabWidget, QTreeWidget, QTreeWidgetItem,
                               QVBoxLayout, QWidget)

from .. import textfmt as tf
from ..ledger_view import SpanKind, load_admin, load_timeline, pretty_json
from ..viewmodel import ReadKind, StatusRead, Tone
from .base import CommandPanel, OutputLog, Page, ReadOnlyBar, capped, fill, icon_button, muted, table
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


class StatusPage(Page):
    key = "status"
    title = "Status"
    description = "What the recorder is doing now."

    # (key, label, page it opens)
    ROWS = [("camera", "Camera", "camera"), ("link", "Camera connection", "camera"),
            ("last", "Last recording saved", "recordings"), ("since", "Current run started", "recordings"),
            ("storage", "Recording storage", "storage"), ("archive", "Archive drive", "drives"),
            ("schedule", "Schedule", "schedule"), ("motion", "Motion", "motion"),
            ("clock", "Clock synchronised", "system"), ("service", "Recorder", "system")]

    def __init__(self, ctx, parent=None) -> None:  # noqa: ANN001
        super().__init__(ctx, parent)
        self.swap_btn = self.command("Archive drive", "drives", lambda: self.ctx.navigate("drives"),
                                     "Swap or check the archive drive")
        self.command("Diagnostic report…", "document", lambda: self.window().save_report(),
                     "Save a report for technical support")
        self.command("Lock computer", "lock", lambda: self.window().lock_computer(),
                     "Locks Windows. Recording continues.")

        # Summary: one large status icon and the headline, as in Windows Security.
        summary = QFrame()
        summary.setObjectName("summary")
        sl = QHBoxLayout(summary)
        sl.setContentsMargins(16, 14, 16, 14)
        sl.setSpacing(14)
        self.big_icon = QLabel()
        self.big_icon.setFixedSize(44, 44)
        sl.addWidget(self.big_icon, 0, Qt.AlignmentFlag.AlignTop)
        texts = QVBoxLayout()
        texts.setSpacing(2)
        self.headline = QLabel("…")
        f = self.headline.font()
        f.setPointSizeF(f.pointSizeF() + 6)
        f.setWeight(QFont.Weight.DemiBold)
        self.headline.setFont(f)
        texts.addWidget(self.headline)
        self.supporting = muted()
        texts.addWidget(self.supporting)
        sl.addLayout(texts, 1)
        self.body.addWidget(summary)

        split = QSplitter(Qt.Orientation.Vertical)
        split.setChildrenCollapsible(False)
        abox = QGroupBox("Alerts")
        al = QVBoxLayout(abox)
        self.alerts = _tree(["Code", "Message"])
        self.alerts.itemActivated.connect(lambda it, _c: self._open_alert(self.alerts.indexOfTopLevelItem(it), 0))
        self.alerts.setToolTip("Double-click or press Enter to open the page where this is dealt with")
        al.addWidget(self.alerts)
        split.addWidget(abox)
        dbox = QGroupBox("Details")
        dl = QVBoxLayout(dbox)
        self.details = _tree(["Item", "Now"])
        self.details.itemActivated.connect(lambda it, _c: self.ctx.navigate(it.data(0, Qt.ItemDataRole.UserRole)))
        self.details.setToolTip("Double-click or press Enter to open the matching page")
        self.items: dict[str, QTreeWidgetItem] = {}
        for k, text, page in self.ROWS:
            it = QTreeWidgetItem([text, "—"])
            it.setData(0, Qt.ItemDataRole.UserRole, page)
            self.details.addTopLevelItem(it)
            self.items[k] = it
        self.details.resizeColumnToContents(0)
        self.details.setColumnWidth(0, max(190, self.details.columnWidth(0) + 30))
        dl.addWidget(self.details)
        split.addWidget(dbox)
        split.setStretchFactor(0, 1)
        split.setStretchFactor(1, 2)
        self.body.addWidget(split, 1)

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

    def on_status(self, read: StatusRead | None) -> None:
        from .icons import glyph, glyph_pixmap

        win = self.window()
        v = getattr(win, "view", None)
        if v is None or read is None:
            return
        p = self.ctx.palette
        icon = {Tone.OK: "ok", Tone.WARNING: "warning", Tone.CRITICAL: "critical"}.get(v.tone, "info")
        self.big_icon.setPixmap(glyph_pixmap(icon, p.tone(v.tone), 44))
        self.headline.setText(v.headline)
        sup = " ".join(x for x in (v.supporting, v.detail) if x)
        self.supporting.setText(sup + (f"  ·  {v.updated}" if v.updated else ""))
        doc = read.doc or {}
        rows: list[tuple[str, str, str]] = []
        if read.kind is not ReadKind.OK:
            rows.append(("X-001", "critical", "The recorder's status is not updating. The recorder may have "
                                              "stopped or this computer may be overloaded."))
        for a in doc.get("alerts") or []:
            if isinstance(a, dict):
                sev = a.get("severity")
                tone = "critical" if sev == 3 else "warning" if sev == 2 else "info"
                rows.append((str(a.get("code", "")), tone, str(a.get("message", ""))))
        self.alerts.clear()
        for code, tone, msg in rows:
            it = QTreeWidgetItem([code, msg])
            color = {"critical": p.critical, "warning": p.warning}.get(tone, p.info)
            it.setIcon(0, glyph(tone, color, 16))
            it.setToolTip(1, msg)
            self.alerts.addTopLevelItem(it)
        if not rows:
            it = QTreeWidgetItem(["", "No alerts."])
            it.setIcon(0, glyph("ok", p.ok, 16))
            it.setFlags(Qt.ItemFlag.ItemIsEnabled)
            self.alerts.addTopLevelItem(it)
        self.alerts.resizeColumnToContents(0)
        # As tall as its alerts (2 to 8 rows), so the details below get the room.
        n = min(8, max(2, self.alerts.topLevelItemCount()))
        row_h = max(22, self.alerts.sizeHintForRow(0))
        self.alerts.setFixedHeight(self.alerts.header().height() + n * row_h + 6)
        self._facts(doc, read)

    def _facts(self, doc: dict[str, Any], read: StatusRead) -> None:
        cfg, tz, now = self.ctx.cfg, self.ctx.tz, (self.ctx.clock or __import__("time").time)()
        if cfg is not None:
            from ...launcher.setcamera import current_address

            host, path, port = current_address(cfg)
            self._set("camera", f"{cfg.camera.name} — not added yet" if cfg.camera.pending else
                      f"{cfg.camera.name} — {host}:{port}/{path}" if host else f"{cfg.camera.name} — {cfg.camera.id}",
                      "warning" if cfg.camera.pending else "")
        link = str(doc.get("camera_link") or "—")
        self._set("link", link.replace("_", " "), "ok" if link == "connected" else "warning" if link != "—" else "")
        last = doc.get("last_segment") or {}
        le = _ep(last.get("end_utc"))
        self._set("last", tf.when(le, tz, now) if le else "none yet")
        rec = doc.get("recording") or {}
        since = _ep(rec.get("run_started_utc"))
        self._set("since", tf.when(since, tz, now) if since else "—")
        st = doc.get("storage") or {}
        free = st.get("free_bytes")
        secs = st.get("seconds_until_attention")
        txt = f"{tf.size(free)} free" if isinstance(free, (int, float)) else "—"
        if isinstance(secs, (int, float)):
            txt += f", {tf.approx_remaining(secs)} of recording left"
        self._set("storage", txt, {"ok": "ok", "warning": "warning", "critical": "critical"}.get(st.get("level"), ""))
        arc = doc.get("archive") or {}
        self._set("archive", arc.get("message") or ("not enabled" if not arc else "—"),
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
        else:
            broken = mo.get("healthy") is False
            state = ("waiting for motion" if mo.get("waiting") else
                     "detector not working (recording all the time)" if broken else "watching")
            self._set("motion", f"{state}; now: {mo.get('mode_now', '—')}", "warning" if broken else "ok")
        svc = doc.get("service") or {}
        sync = svc.get("clock_synchronized")
        self._set("clock", "yes" if sync is True else "no (T-002)" if sync is False else "unknown",
                  "ok" if sync is True else "warning" if sync is False else "")
        up = svc.get("uptime_s")
        if read.kind is ReadKind.OK:
            self._set("service", f"running, version {svc.get('version', '?')}"
                      + (f", up {tf.duration(up)}" if isinstance(up, (int, float)) else ""), "ok")
        else:
            self._set("service", "not responding", "critical")


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
        tl = load_timeline(db, cfg.camera.id, now, hours=hours, live=live)
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
        self.log = OutputLog()
        sl.addWidget(CommandPanel(self.log, ctx.palette), 1)
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

    def _run(self, argv: list[str], title: str) -> None:
        self.log.run(argv, title)

    def on_show(self) -> None:
        cfg = self.cfg
        if cfg is None:
            return
        db = cfg.ledger_snapshot_path or (cfg.data_path / "sentinel.db")
        data = load_admin(db, cfg.camera.id)
        fill(self.events, [[e.get("ts_utc", ""), e.get("severity", ""), e.get("category", ""), e.get("code", ""),
                            e.get("message", "")] for e in data.events])
        fill(self.gaps, [[g.get("gap_id", ""), g.get("kind", ""), g.get("start_utc") or "", g.get("end_utc") or "open",
                          f"{g['duration_s']:.1f}" if isinstance(g.get("duration_s"), (int, float)) else "",
                          g.get("reason", "")] for g in data.recent_gaps])
        read = self.ctx.read
        self.raw.setPlainText(pretty_json(read.doc) if read is not None and read.doc else "No status available.")
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

        self.ctx.restart(on_done=done)

    def _stop(self) -> None:
        from PySide6.QtWidgets import QMessageBox

        if QMessageBox.question(self, "Sentinel", "Stop the recorder? Nothing is recorded until it is started "
                                "again (Restart recorder, or restarting this computer).") == \
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
