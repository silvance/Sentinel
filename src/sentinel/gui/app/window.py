"""The Sentinel main window: section list on the left, page on the right, status bar at the bottom."""

from __future__ import annotations

import time
from collections.abc import Callable
from pathlib import Path
from zoneinfo import ZoneInfo

from PySide6.QtCore import QSize, Qt, QTimer
from PySide6.QtGui import QAction, QActionGroup, QColor, QKeySequence
from PySide6.QtWidgets import (QApplication, QFileDialog, QHBoxLayout, QLabel, QListWidget, QListWidgetItem,
                               QMainWindow, QMessageBox, QPushButton, QStackedWidget, QToolButton, QVBoxLayout,
                               QWidget)

from ... import __version__
from ...config import AppConfig
from .. import textfmt as tf
from ..ledger_view import recording_since, snapshot
from ..prefs import load_appearance, load_pref, save_appearance, save_pref
from ..theme import pick_palette
from ..viewmodel import DashboardView, ReadKind, StatusRead, Tone, build_dashboard, read_status_robust
from . import style
from .base import Ctx, Page
from .elevation import is_admin, relaunch_elevated
from .procs import Task

REFRESH_MS = 3000
WATCH_EVERY_MS = 5000          # after a settings change: check every 5 s ...
WATCH_LIMIT_S = 180.0          # ... for up to 3 minutes ...
WATCH_PASSES = 3               # ... until it passes 3 times in a row (not a passing moment while starting)

# Where an alert is dealt with (first letter of the code; some codes individually).
CODE_PAGES = {"P-001": "schedule", "P-003": "motion", "W-001": "motion", "W-002": "camera", "P-002": "camera",
              "S-001": "export", "R": "camera",
              "C": "system", "S": "system", "T": "system", "H": "system", "X": "system", "V": "system",
              "A": "drives", "M": "drives", "L": "drives", "E": "export", "Z": "camera"}


def page_for_code(code: str) -> str:
    return CODE_PAGES.get(code) or CODE_PAGES.get(code[:1], "status")


def admin_config_problem(cfg_path: Path, cfg: AppConfig) -> str:
    """Why this configuration must not be used with administrator rights ("" = it may)."""
    try:
        from ...install_check import location_problems

        problems = location_problems(cfg_path, cfg)
    except Exception as exc:  # noqa: BLE001 - cannot check: do not act as administrator
        return f"the configuration file could not be checked ({exc})"
    if problems:
        return ("this configuration file or its folders can be changed by non-administrators, so it is "
                "not used for administrator actions: " + "; ".join(problems[:2]))
    return ""


TONE_NAME = {Tone.OK: "ok", Tone.INFO: "muted", Tone.WARNING: "warning", Tone.CRITICAL: "critical",
             Tone.NEUTRAL: "muted"}


class AppWindow(QMainWindow):
    def __init__(self, cfg: AppConfig | None, cfg_path: Path | None, *, admin: bool | None = None,
                 appearance: str | None = None, auto_refresh: bool = True,
                 clock: Callable[[], float] = time.time, prefs_path: Path | None = None,
                 on_configured: Callable[[AppConfig, Path], str | None] | None = None,
                 setup_target: Path | None = None) -> None:
        super().__init__()
        self.clock = clock
        self.prefs_path = prefs_path
        self.appearance = appearance or load_appearance(prefs_path)
        self.on_configured = on_configured
        admin = is_admin() if admin is None else admin
        self.setup_target = setup_target
        self.admin_refused = ""
        if admin and cfg is not None and cfg_path is not None:
            # Administrator actions run tools named in this file: only a file that only
            # administrators can change may be used (review 2026-11-10: an operator's own file
            # passed through Unlock ran its tools as administrator).
            self.admin_refused = admin_config_problem(Path(cfg_path), cfg)
            if self.admin_refused:
                admin = False
        tz = ZoneInfo(cfg.recording.display_timezone) if cfg else ZoneInfo("UTC")
        self.ctx = Ctx(cfg=cfg, cfg_path=Path(cfg_path) if cfg_path else None, admin=admin,
                       palette=pick_palette(self.appearance), tz=tz, navigate=self.navigate,
                       unlock=self.unlock, config_changed=self._config_changed, clock=clock,
                       restart=self.restart_recorder, after_change=self.watch_after_change)
        self.view: DashboardView | None = None
        self.pages: dict[str, Page] = {}
        self._tasks: list[Task] = []
        self._restart_task: Task | None = None
        self._restart_again = False
        self._restart_callbacks: list[Callable[[str], None]] = []
        self._cfg_mtime = self._mtime()
        self._watch: dict | None = None            # check after a settings change (see watch_after_change)
        self.watch_timer = QTimer(self)
        self.watch_timer.setInterval(WATCH_EVERY_MS)
        self.watch_timer.timeout.connect(self._watch_tick)
        self.setMinimumSize(980, 620)
        self._build()
        self.timer = QTimer(self)
        self.timer.setInterval(REFRESH_MS)
        self.timer.timeout.connect(self.refresh)
        self.refresh()
        if auto_refresh and cfg is not None:
            self.timer.start()
        self._closing_for_update = False
        if auto_refresh:
            QTimer.singleShot(500, self.show_update_result)

    # ------------------------------------------------------------------ layout
    def _sections(self) -> list[type[Page]]:
        if self.ctx.cfg is None:
            from .setup_page import SetupPage

            return [SetupPage]
        from .drive_pages import DrivesPage, ExportPage
        from .settings_pages import CameraPage, GeneralPage, MotionPage, SchedulePage, StoragePage
        from .status_pages import RecordingsPage, StatusPage, SystemPage

        return [StatusPage, RecordingsPage, GeneralPage, CameraPage, SchedulePage, MotionPage, StoragePage,
                DrivesPage, ExportPage, SystemPage]

    # Sections are grouped as in other Windows administration tools.
    GROUPS = (("Monitor", ("status", "recordings")),
              ("Settings", ("general", "camera", "schedule", "motion", "storage")),
              ("Maintenance", ("drives", "export", "system")),
              ("", ("setup",)))

    def _build(self) -> None:
        from .icons import brand_pixmap, glyph

        cfg = self.ctx.cfg
        cam = (f"{cfg.camera.name} ({cfg.camera.id})" if cfg.camera.name else cfg.camera.id) if cfg else "Setup"
        self.setWindowTitle(f"{cam} — Sentinel" + ("" if self.ctx.admin else " (read-only)"))
        p = self.ctx.palette
        root = QWidget()
        h = QHBoxLayout(root)
        h.setContentsMargins(0, 0, 0, 0)
        h.setSpacing(0)

        nav_panel = QWidget()
        nav_panel.setObjectName("navPanel")
        nav_panel.setFixedWidth(208)
        nv = QVBoxLayout(nav_panel)
        nv.setContentsMargins(8, 10, 8, 8)
        nv.setSpacing(6)
        brand = QHBoxLayout()
        logo = QLabel()
        logo.setPixmap(brand_pixmap(28))
        brand.addWidget(logo)
        names = QVBoxLayout()
        names.setSpacing(0)
        app_name = QLabel("Sentinel")
        fnt = app_name.font()
        fnt.setPointSizeF(fnt.pointSizeF() + 2)
        fnt.setBold(True)
        app_name.setFont(fnt)
        names.addWidget(app_name)
        org = cfg.site.organization if cfg else ""
        from html import escape

        cam_label = QLabel(f"{escape(org)}<br>{escape(cam)}" if org else cam)
        cam_label.setTextFormat(Qt.TextFormat.RichText if org else Qt.TextFormat.PlainText)
        cam_label.setObjectName("muted")
        cam_label.setToolTip(f"Organisation: {org}" if org else "")
        names.addWidget(cam_label)
        brand.addLayout(names, 1)
        nv.addLayout(brand)
        self.state_label = QLabel("…")
        self.state_label.setWordWrap(True)
        self.state_label.setContentsMargins(4, 2, 4, 2)
        nv.addWidget(self.state_label)
        self.nav = QListWidget()
        self.nav.setObjectName("nav")
        self.nav.setIconSize(QSize(18, 18))
        nv.addWidget(self.nav, 1)
        self.access_label = QLabel()
        self.access_label.setObjectName("muted")
        self.access_label.setContentsMargins(4, 0, 4, 0)
        self.access_label.setWordWrap(True)
        nv.addWidget(self.access_label)
        self.unlock_btn = QPushButton(glyph("shield", p.text, 16), " Unlock settings…")
        self.unlock_btn.setToolTip("Reopen Sentinel as administrator (Windows asks for approval)")
        self.unlock_btn.clicked.connect(self.unlock)
        nv.addWidget(self.unlock_btn)
        h.addWidget(nav_panel)

        self.stack = QStackedWidget()
        h.addWidget(self.stack, 1)
        self.setCentralWidget(root)

        classes = {cls.key: cls for cls in self._sections()}
        self._rows: dict[int, str] = {}
        for group, keys in self.GROUPS:
            present = [k for k in keys if k in classes]
            if not present:
                continue
            if group:
                head = QListWidgetItem(group.upper())
                head.setFlags(Qt.ItemFlag.NoItemFlags)
                hf = head.font()
                hf.setPointSizeF(hf.pointSizeF() - 1)
                hf.setBold(True)
                head.setFont(hf)
                head.setForeground(QColor(p.text2))
                self.nav.addItem(head)
            for k in present:
                cls = classes[k]
                page = cls(self.ctx)
                self.pages[k] = page
                self.stack.addWidget(page)
                item = QListWidgetItem(glyph(k if k != "setup" else "system", p.text, 18), cls.title)
                item.setData(Qt.ItemDataRole.UserRole, k)
                self.nav.addItem(item)
                self._rows[self.nav.count() - 1] = k
        self.nav.currentRowChanged.connect(self._nav_changed)
        last = load_pref("last_section", None, self.prefs_path)
        self.navigate(last if isinstance(last, str) and last in self.pages else next(iter(self.pages)))
        self._access_text()
        self._build_menu()
        self._build_statusbar()

    def section_names(self) -> list[str]:
        return [self.nav.item(r).text() for r in sorted(self._rows)]

    def _access_text(self) -> None:
        if self.admin_refused:
            self.access_label.setText(f"Read-only: {self.admin_refused}")
            self.unlock_btn.hide()
        elif self.ctx.admin:
            self.access_label.setText("Administrator mode")
            self.access_label.setToolTip("Settings can be changed.")
            self.unlock_btn.hide()
        else:
            self.access_label.setText("Read-only mode")
            self.access_label.setToolTip("Viewing and operator tasks. Unlock settings to change them.")
            self.unlock_btn.show()

    def _build_menu(self) -> None:
        mb = self.menuBar()
        mb.clear()
        m = mb.addMenu("&File")
        if self.ctx.cfg is not None:
            a = m.addAction("Save &diagnostic report…")
            a.setShortcut(QKeySequence("Ctrl+D"))
            a.triggered.connect(self.save_report)
            a = m.addAction("&Lock this computer")
            a.setShortcut(QKeySequence("Ctrl+L"))
            a.triggered.connect(self.lock_computer)
            m.addSeparator()
        if not self.ctx.admin and not self.admin_refused:
            a = m.addAction("&Unlock settings (administrator)…")
            a.triggered.connect(self.unlock)
            m.addSeparator()
        a = m.addAction("&Close")
        a.setShortcut(QKeySequence("Ctrl+W"))
        a.triggered.connect(self.close)

        v = mb.addMenu("&View")
        a = v.addAction("&Refresh")
        a.setShortcut(QKeySequence("F5"))
        a.triggered.connect(self.refresh_all)
        v.addSeparator()
        grp = QActionGroup(self)
        for key, text in (("auto", "Follow &Windows"), ("light", "&Light"), ("dark", "&Dark")):
            act = QAction(text, self, checkable=True)
            act.setChecked(self.appearance == key)
            act.triggered.connect(lambda _c=False, k=key: self.set_appearance(k))
            grp.addAction(act)
            v.addAction(act)
        go = mb.addMenu("&Go")
        for n, row in enumerate(sorted(self._rows), 1):
            act = go.addAction(self.nav.item(row).text())
            if n <= 9:
                act.setShortcut(QKeySequence(f"Ctrl+{n}"))
            act.triggered.connect(lambda _c=False, k=self._rows[row]: self.navigate(k))
        hm = mb.addMenu("&Help")
        a = hm.addAction("Open the &licences folder")
        a.triggered.connect(self.open_licences)
        hm.addSeparator()
        a = hm.addAction("&About Sentinel")
        a.triggered.connect(self.about)

    def _build_statusbar(self) -> None:
        if hasattr(self, "sb_state"):
            return
        sb = self.statusBar()
        sb.setSizeGripEnabled(True)
        self.sb_state = self._sb_button("status")
        self.sb_alerts = self._sb_button("status")
        self.sb_storage = self._sb_button("storage")
        self.sb_archive = self._sb_button("drives")
        self.sb_updated = QLabel()
        self.sb_updated.setObjectName("muted")
        for w in (self.sb_state, self.sb_alerts, self.sb_storage, self.sb_archive):
            sb.addWidget(w)
        sb.addPermanentWidget(self.sb_updated)

    def _sb_button(self, key: str) -> QToolButton:
        b = QToolButton()
        b.setAutoRaise(True)
        b.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
        b.setCursor(Qt.CursorShape.PointingHandCursor)
        b.clicked.connect(lambda: self.navigate(key))
        return b

    # ------------------------------------------------------------------ navigation
    def navigate(self, key: str) -> None:
        for row, k in self._rows.items():
            if k == key:
                self.nav.setCurrentRow(row)
                return

    def _nav_changed(self, row: int) -> None:
        key = self._rows.get(row)
        if key is None:
            return
        page = self.pages[key]
        old = self.stack.currentWidget()
        if isinstance(old, Page) and old is not page:
            old.on_hide()
        self.stack.setCurrentWidget(page)
        save_pref("last_section", key, self.prefs_path)
        page.on_show()
        page.on_status(self.ctx.read)

    def current_page(self) -> Page | None:
        w = self.stack.currentWidget()
        return w if isinstance(w, Page) else None

    # ------------------------------------------------------------------ window size
    def restore_geometry(self) -> bool:
        """Last size and position (per user); False if none is remembered."""
        g = load_pref("window_geometry", None, self.prefs_path)
        if isinstance(g, str):
            from PySide6.QtCore import QByteArray

            return bool(self.restoreGeometry(QByteArray.fromBase64(g.encode("ascii"))))
        return False

    def _save_geometry(self) -> None:
        save_pref("window_geometry", bytes(self.saveGeometry().toBase64()).decode("ascii"), self.prefs_path)

    def open_licences(self) -> None:
        import sys
        from pathlib import Path as _P

        from PySide6.QtCore import QUrl
        from PySide6.QtGui import QDesktopServices

        folder = _P(sys.executable).parent / "licences"
        if not folder.is_dir():
            folder = _P(__file__).resolve().parents[4] / "packaging" / "licences"
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(folder)))

    # ------------------------------------------------------------------ status
    def refresh_all(self) -> None:
        self.refresh()
        page = self.current_page()
        if page is not None:
            page.on_show()

    def _mtime(self) -> float | None:
        try:
            return self.ctx.cfg_path.stat().st_mtime if self.ctx.cfg_path else None
        except OSError:
            return None

    def _reload_if_changed(self) -> None:
        """The configuration file was edited elsewhere (Notepad): use the new settings."""
        m = self._mtime()
        if m is None or m == self._cfg_mtime or self.ctx.cfg_path is None:
            return
        self._cfg_mtime = m
        from ...config import ConfigError, load_config

        try:
            cfg = load_config(self.ctx.cfg_path)
        except (ConfigError, OSError) as exc:
            self.statusBar().showMessage(f"The configuration file has an error and was not loaded: {exc}", 15000)
            return
        self._config_changed(cfg)
        for page in self.pages.values():
            if hasattr(page, "load"):
                page.load()
        self.statusBar().showMessage("The configuration file changed; the new settings are shown. "
                                     "Restart the recorder (System) to use them.", 15000)

    def refresh(self) -> None:
        cfg = self.ctx.cfg
        if cfg is not None:
            self._reload_if_changed()
            cfg = self.ctx.cfg
        if cfg is None:
            self.state_label.setText("Not set up")
            self.sb_state.setText("Sentinel is not set up on this computer")
            return
        now = self.clock()
        max_age = max(15.0, 5 * cfg.service.status_interval_s)
        read = read_status_robust(cfg.status_path, max_age_s=max_age)
        db = cfg.ledger_snapshot_path or (cfg.data_path / "sentinel.db")
        snap = snapshot(db, cfg.camera.id)
        self.recording_since_epoch = recording_since(snap)   # current uninterrupted stretch (Status header)
        self.view = build_dashboard(read, tz=self.ctx.tz, now=now, last_saved_epoch=snap.last_saved_epoch,
                                    recording_since_epoch=self.recording_since_epoch,
                                    config_test_camera=bool(getattr(cfg.camera, "development_source", False)))
        self.ctx.read = read
        self._show_summary(self.view, read)
        status_page = self.pages.get("status")
        if status_page is not None:
            status_page.on_status(read)
        page = self.current_page()
        if page is not None and page is not status_page:
            page.on_status(read)

    def _show_summary(self, v: DashboardView, read: StatusRead) -> None:
        from .icons import glyph

        p = self.ctx.palette
        color = p.tone(v.tone)
        tone_icon = {Tone.OK: "ok", Tone.WARNING: "warning", Tone.CRITICAL: "critical"}.get(v.tone, "info")
        test = (f"<br><span style='color:{p.critical}'>TEST CAMERA — not evidence</span>"
                if v.test_camera is not None else "")
        updating = self._update_in_progress()
        # Review 2026-11-12: only while the recorder is actually down. A failed or cancelled update can leave its
        # state file behind; a running recorder's real status must not be hidden behind "Updating…".
        recorder_up = read.kind is ReadKind.OK and (read.doc or {}).get("overall") not in ("STOPPED", None)
        if updating and not recorder_up:       # recording is stopped on purpose for the swap; not a "System error"
            from dataclasses import replace

            v = replace(v, headline=f"{updating}: recording restarts by itself", tone=Tone.INFO)
            color, tone_icon = p.tone(Tone.INFO), "info"
        self.state_label.setText(f"<span style='color:{color}'>●</span> <b>{v.headline}</b>{test}")
        self.sb_state.setIcon(glyph(tone_icon, color, 16))
        self.sb_state.setText(v.headline + ("  ·  TEST CAMERA — not evidence" if test else ""))
        self.sb_alerts.setIcon(glyph("info", p.text2, 16))
        self.sb_storage.setIcon(glyph("storage", p.text2, 16))
        self.sb_archive.setIcon(glyph("drives", p.text2, 16))
        doc = read.doc or {}
        alerts = [a for a in (doc.get("alerts") or []) if isinstance(a, dict)]
        if read.kind is not ReadKind.OK:
            self.sb_alerts.setText("Recorder status not updating (X-001)")
        elif alerts:
            codes = ", ".join(str(a.get("code")) for a in alerts[:4]) + ("…" if len(alerts) > 4 else "")
            self.sb_alerts.setText(f"{len(alerts)} alert{'s' if len(alerts) != 1 else ''}: {codes}")
        else:
            self.sb_alerts.setText("No alerts")
        st = doc.get("storage") or {}
        free = st.get("free_bytes")
        self.sb_storage.setText(f"Storage: {tf.size(free)} free" if isinstance(free, (int, float)) else "Storage: —")
        arc = doc.get("archive") or {}
        if not arc:
            self.sb_archive.setText("Archive: —")
        elif not arc.get("running"):
            self.sb_archive.setText("Archive: copying not running")
        else:
            from ...archive.status import ST_ABSENT, state_words

            words = state_words(arc.get("state"))
            label = arc.get("drive_label")
            if label and arc.get("state") != ST_ABSENT:
                self.sb_archive.setText(f"Archive: {label} · {words}" if words else f"Archive: {label}")
            else:
                self.sb_archive.setText(f"Archive: {words or 'no drive inserted'}")
        self.sb_updated.setText(v.updated)

    # ------------------------------------------------------------------ actions
    def unlock(self) -> None:
        if self.ctx.admin:
            return
        if self.ctx.cfg_path:
            extra = ["--config", str(self.ctx.cfg_path), "--no-start"]
        else:
            extra = ["--config", str(self.setup_target)] if self.setup_target else []
        if relaunch_elevated(extra):
            self.close()
            return
        QMessageBox.information(
            self, "Sentinel",
            "Settings stay read-only: Windows did not give administrator rights (the request was declined, "
            "or this account is not an administrator).\n\nSign in with an administrator account, or ask "
            "an administrator to approve the request.")

    def set_appearance(self, mode: str) -> None:
        self.appearance = mode
        save_appearance(mode, self.prefs_path)
        app = QApplication.instance()
        if app is not None:
            self.ctx.palette = style.apply(app, mode)  # type: ignore[arg-type]
        for page in self.pages.values():
            if hasattr(page, "on_palette"):
                page.on_palette()
        self.refresh()

    def lock_computer(self) -> None:
        from ...power import lock_workstation

        self.showMinimized()
        if not lock_workstation():
            self.showNormal()

    def save_report(self) -> None:
        cfg = self.ctx.cfg
        if cfg is None:
            return
        start = str(Path.home() / "Desktop") if (Path.home() / "Desktop").is_dir() else str(Path.home())
        folder = QFileDialog.getExistingDirectory(self, "Save diagnostic report in", start)
        if not folder:
            return
        from ..report import save_diagnostic_report

        self.statusBar().showMessage("Saving diagnostic report…", 5000)
        task = Task(lambda: save_diagnostic_report(cfg, Path(folder), "requested from the Sentinel window"), self)
        task.done.connect(self._report_done)
        self._tasks.append(task)
        task.start()

    def _report_done(self, path: object, err: object) -> None:
        if err is not None:
            QMessageBox.warning(self, "Sentinel", f"The diagnostic report could not be saved: {err}")
        else:
            QMessageBox.information(self, "Sentinel", f"Diagnostic report saved:\n{path}\n\n"
                                                      "Send it to technical support.")

    def about(self) -> None:
        from PySide6.QtWidgets import QDialog, QDialogButtonBox, QGridLayout

        from .icons import brand_pixmap

        cfg = self.ctx.cfg
        dlg = QDialog(self)
        dlg.setWindowTitle("About Sentinel")
        grid = QGridLayout(dlg)
        grid.setHorizontalSpacing(16)
        logo = QLabel()
        logo.setPixmap(brand_pixmap(64))
        grid.addWidget(logo, 0, 0, 3, 1, Qt.AlignmentFlag.AlignTop)
        title = QLabel(f"<b style='font-size:14pt'>Sentinel</b><br>Version {__version__}")
        grid.addWidget(title, 0, 1)
        lines = ["Single-camera recorder with a hash-chained recording log.",
                 "Local only: no Internet services, no telemetry."]
        if cfg is not None and cfg.site.organization:
            lines.append(f"Used by: {cfg.site.organization}")
        if cfg is not None and self.ctx.cfg_path is not None:
            lines.append(f"Configuration: {self.ctx.cfg_path}")
            lines.append(f"Times shown in {self.ctx.tz.key} ({tf.utc_offset_text(self.ctx.tz, self.clock())})")
        info = QLabel("<br>".join(lines))
        info.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        grid.addWidget(info, 1, 1)
        lic = QLabel("<a href='#'>Third-party licences and the source offer</a>")
        lic.linkActivated.connect(lambda _h: self.open_licences())
        grid.addWidget(lic, 2, 1)
        bb = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        bb.rejected.connect(dlg.reject)
        bb.accepted.connect(dlg.accept)
        grid.addWidget(bb, 3, 0, 1, 2)
        dlg.exec()

    def _config_changed(self, cfg: AppConfig) -> None:
        """A page saved new settings (or setup wrote the first configuration)."""
        first = self.ctx.cfg is None
        self.ctx.cfg = cfg
        if cfg.source_path:
            self.ctx.cfg_path = Path(cfg.source_path)
        self._cfg_mtime = self._mtime()
        self.ctx.tz = ZoneInfo(cfg.recording.display_timezone)
        if not first and hasattr(self, "nav"):
            cam = f"{cfg.camera.name} ({cfg.camera.id})" if cfg.camera.name else cfg.camera.id
            self.setWindowTitle(f"{cam} — Sentinel" + ("" if self.ctx.admin else " (read-only)"))
        if first:
            self._rebuild()
            self.timer.start()
            if self.on_configured is not None and self.ctx.cfg_path is not None:
                self._start_after_setup(cfg, self.ctx.cfg_path)

    def _start_after_setup(self, cfg: AppConfig, path: Path) -> None:
        """Start the recorder in the background and say what happened (review 2026-11-10: a
        refused start was silent and the window froze for up to 20 s)."""
        cb = self.on_configured
        assert cb is not None
        self.statusBar().showMessage("Starting the recorder…")
        task = Task(lambda: cb(cfg, path), self)

        def done(problem: object, err: object) -> None:
            text = str(err) if err is not None else (str(problem) if problem else "")
            if text:
                QMessageBox.warning(self, "Sentinel", f"The recorder was not started:\n\n{text}\n\n"
                                    "Fix this, then use System › Restart recorder.")
                self.statusBar().showMessage("The recorder was not started.", 15000)
            else:
                self.statusBar().showMessage("The recorder was started.", 10000)

        task.done.connect(done)
        self._tasks.append(task)
        task.start()

    # ------------------------------------------------------------------ recorder restart (one at a time)
    def restart_recorder(self, on_done: Callable[[str], None] | None = None) -> None:
        """Restart the recorder with the saved settings. Requests while one is running are merged
        into one more restart afterwards (review 2026-11-10: overlapping restarts failed)."""
        if on_done is not None:
            self._restart_callbacks.append(on_done)
        if self._restart_task is not None and self._restart_task.running():
            self._restart_again = True
            return
        cfg, path = self.ctx.cfg, self.ctx.cfg_path
        if cfg is None or path is None:
            return
        from . import base

        self.statusBar().showMessage("Restarting the recorder…")
        task = Task(lambda: base.restart_recorder(path, cfg), self)

        def done(msg: object, err: object) -> None:
            if self._restart_again:
                self._restart_again = False
                self._restart_task = None
                self.restart_recorder()
                return
            text = str(err) if err is not None else str(msg)
            self.statusBar().showMessage(text, 15000)
            callbacks, self._restart_callbacks = self._restart_callbacks, []
            for c in callbacks:
                c(text)

        task.done.connect(done)
        self._restart_task = task
        task.start()

    # ------------------------------------------------------------------ check after a change, rollback
    def _status_reader(self):  # noqa: ANN202
        from ...status import read_status

        cfg = self.ctx.cfg
        assert cfg is not None
        return lambda: read_status(cfg.status_path, max_age_s=max(15.0, 5 * cfg.service.status_interval_s))

    def check_now(self, require_saved_file: bool = True, ignore_alerts: frozenset[str] = frozenset()):  # noqa: ANN201
        """Run the self-check; a pass (as administrator) is remembered as the last known good settings."""
        from ...selfcheck import mark_known_good, run_checks

        if self.ctx.cfg is None or self.ctx.cfg_path is None:
            return None
        rep = run_checks(self.ctx.cfg_path, self._status_reader(), self.clock(), require_saved_file, ignore_alerts)
        if rep.ok and self.ctx.admin:
            try:
                mark_known_good(self.ctx.cfg_path, rep)
            except OSError:
                pass
        return rep

    def watch_after_change(self, mode: str = "settings") -> None:
        """After a save or restore: if the recorder does not work within 3 minutes, put the last
        known good settings back (owner request 2026-11-10: a change must not leave the tool broken).
        ``mode="update"``: after a version update; if the recorder does not run, go back to the
        previous version instead."""
        if self.ctx.cfg is None or not self.ctx.admin:
            return
        from ...selfcheck import unrelated_alerts

        ignore = unrelated_alerts(self._status_reader()) if mode != "update" else frozenset()
        self._watch = {"started": self.clock(), "passes": 0, "last": None, "mode": mode, "ignore": ignore}
        what = "new version" if mode == "update" else "new settings"
        self.statusBar().showMessage(f"Checking the recorder with the {what} (up to 3 minutes)…")
        self.watch_timer.start()

    def watching(self) -> bool:
        return self.watch_timer.isActive()

    def _watch_tick(self) -> None:
        from ...selfcheck import good_path, same_settings

        w = self._watch
        if w is None or (self._restart_task is not None and self._restart_task.running()):
            return
        rep = self.check_now(require_saved_file=False, ignore_alerts=w.get("ignore") or frozenset())
        if rep is None:
            return
        w["last"] = rep
        w["passes"] = w["passes"] + 1 if rep.ok else 0
        if w["passes"] >= WATCH_PASSES:
            self.watch_timer.stop()
            self._watch = None
            if w.get("mode") == "update":
                self._update_watch_done()
                self.statusBar().showMessage("New version checked: the recorder works.", 20000)
                return
            self.statusBar().showMessage("New settings checked: the recorder works. Saved as the last known "
                                         "good settings.", 20000)
            return
        if self.clock() - w["started"] < WATCH_LIMIT_S:
            return
        self.watch_timer.stop()
        self._watch = None
        if w.get("mode") == "update":
            self._update_watch_failed(rep)
            return
        path = self.ctx.cfg_path
        good = good_path(path) if path is not None else None
        if good is not None and good.is_file() and not same_settings(good, path):
            self.rollback_to_known_good(f"The recorder did not work with the new settings: {rep.summary()}")
        else:
            QMessageBox.warning(self, "Sentinel", "The recorder does not work with the current settings:\n\n"
                                f"{rep.summary()}\n\nThere are no earlier working settings to go back to. "
                                "See System › Check and history.")

    def rollback_to_known_good(self, why: str = "") -> bool:
        from ...config import ConfigError, load_config
        from ...selfcheck import restore_known_good

        path = self.ctx.cfg_path
        if path is None:
            return False
        try:
            backup = restore_known_good(path, self.clock)
            cfg = load_config(path)
        except (ConfigError, OSError) as exc:
            QMessageBox.warning(self, "Sentinel", f"The last working settings could not be put back: {exc}")
            return False
        self._config_changed(cfg)
        for page in self.pages.values():
            if hasattr(page, "load"):
                page.load()
        self.restart_recorder()
        QMessageBox.information(self, "Sentinel", (why + "\n\n" if why else "") +
                                "The last working settings were put back and the recorder was restarted. "
                                f"The settings that did not work are kept as {backup.name} "
                                "(System › Check and history).")
        self.watch_after_change()
        return True

    def busy_work(self) -> list[str]:
        """What would be stopped if the window closed now."""
        from .base import OutputLog

        out = []
        if self.watching():
            out.append("checking the new settings (if they do not work, closing now means the last working "
                       "settings are not put back automatically)")
        if self._restart_task is not None and self._restart_task.running():
            out.append("restarting the recorder")
        out += ["starting the recorder" for t in self._tasks if t.running()]
        for log_ in self.findChildren(OutputLog):
            cmd = log_.current
            if cmd is not None and cmd.running():
                out.append(str(getattr(cmd, "title", "a command")).lower())
        return out

    def closeEvent(self, e) -> None:  # noqa: ANN001, N802
        """Closing stops child commands and background work (review 2026-11-10): a restart must
        finish (the recorder would stay stopped); other work needs a confirmation."""
        if getattr(self, "_closing_for_update", False):   # the update checked busy_work() before handing over
            self.timer.stop()
            self._save_geometry()
            e.accept()
            return
        work = self.busy_work()
        if any(w in ("restarting the recorder", "starting the recorder") for w in work):
            QMessageBox.information(self, "Sentinel", "Wait until the recorder has been restarted, then close "
                                    "the window. Closing now would leave it stopped.")
            e.ignore()
            return
        if work and QMessageBox.question(
                self, "Sentinel", "Still running: " + ", ".join(work) + ".\n\nClosing the window stops it "
                "part-way. Close anyway?", QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No) != QMessageBox.StandardButton.Yes:
            e.ignore()
            return
        self.timer.stop()
        self._save_geometry()
        e.accept()

    # ------------------------------------------------------------------ updates (owner request 2026-11-10)
    def _update_folders(self):  # noqa: ANN202
        from ...paths import app_dir
        from ...update.apply import Folders

        app = app_dir()
        return Folders(app) if app is not None else None

    def _update_in_progress(self) -> str | None:
        from ...update.apply import in_progress

        folders = self._update_folders()
        return in_progress(folders) if folders is not None else None

    def close_for_update(self) -> None:
        """The update (in its own console) replaces the program folder: close without questions."""
        self._closing_for_update = True
        self.watch_timer.stop()
        self._watch = None
        self.close()

    def show_update_result(self, folders=None) -> dict | None:  # noqa: ANN001
        """After Sentinel reopens from an update or rollback: say what happened (once), and watch the
        recorder for 3 minutes after an update."""
        from ...update.apply import mark_result, read_result

        if not self.ctx.admin:
            return None
        folders = folders or self._update_folders()
        res = read_result(folders) if folders is not None else None
        if not res or res.get("shown"):
            return res
        try:
            mark_result(folders, shown=True)
        except OSError:
            pass
        good = (res.get("outcome"), res.get("action")) in (("updated", "update"), ("rolled_back", "rollback"))
        # Review 2026-11-12: start the 3-minute check first; the message does not wait for a click.
        if res.get("outcome") == "updated" and res.get("watch") and self.ctx.cfg is not None:
            self._update_folders_override = folders
            self.watch_after_change(mode="update")
        self._notice(str(res.get("message", "")), warning=not good)
        return res

    def _notice(self, text: str, warning: bool = False) -> None:
        """A message that does not block anything (unattended laptop: nobody may be there to click OK)."""
        box = QMessageBox(QMessageBox.Icon.Warning if warning else QMessageBox.Icon.Information, "Sentinel", text,
                          QMessageBox.StandardButton.Ok, self)
        box.setWindowModality(Qt.WindowModality.NonModal)
        box.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        box.show()

    def _update_watch_done(self) -> None:
        from ...update.apply import mark_result

        folders = getattr(self, "_update_folders_override", None) or self._update_folders()
        if folders is not None:
            try:
                mark_result(folders, watch=False)
            except OSError:
                pass

    def _update_watch_failed(self, rep) -> None:  # noqa: ANN001
        """The recorder did not work for 3 minutes after an update. If it does not run at all (or the
        settings do not load), that is the new version's fault: go back by itself. Other problems
        (camera, disk) are not caused by an update: say so and offer going back."""
        from ...update.apply import folder_version

        self._update_watch_done()
        folders = getattr(self, "_update_folders_override", None) or self._update_folders()
        prev = folder_version(folders.previous) if folders is not None else None
        broken = {c.name for c in rep.failures} & {"Settings file", "Recorder running"}
        system = self.pages.get("system")
        tab = getattr(system, "updates", None)
        if broken and prev and tab is not None:
            self._notice(f"The recorder does not work with Sentinel {__version__}:\n\n{rep.summary()}\n\n"
                         f"Sentinel now goes back to version {prev}.", warning=True)
            tab.go_back(reason=f"the recorder did not work after the update: {rep.summary()}", ask=False)
            return
        self._notice(f"The recorder reports a problem since the update:\n\n{rep.summary()}\n\nThis is usually not "
                     "caused by the update (camera, drive). To go back to the previous version anyway: "
                     "System › Updates.", warning=True)

    def _rebuild(self) -> None:
        self.pages.clear()
        self._build()          # setCentralWidget() deletes the previous pages
        self.refresh()
