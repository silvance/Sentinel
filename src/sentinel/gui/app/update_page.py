"""System > Updates: apply a version update file, go back to the previous version, and what the
last update did (owner request 2026-11-10). The work itself is ``Sentinel update apply|rollback``
(``sentinel.update``), started in its own console window because the program folder is replaced
while this window is closed."""

from __future__ import annotations

import subprocess
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

from PySide6.QtWidgets import (QFileDialog, QFormLayout, QGroupBox, QHBoxLayout, QLabel, QMessageBox, QVBoxLayout,
                               QWidget)

from ... import __version__
from ...paths import app_dir
from .base import Ctx, icon_button, muted
from .procs import sentinel_argv

Popen = Callable[..., Any]


def _folders():  # noqa: ANN202
    from ...update.apply import Folders

    app = app_dir()
    return Folders(app) if app is not None else None


def start_detached_console(argv: list[str], popen: Popen = subprocess.Popen) -> None:
    """Run ``argv`` in a new, visible console window that outlives this one."""
    flags = 0
    if sys.platform == "win32":
        flags = getattr(subprocess, "CREATE_NEW_CONSOLE", 0x10) | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0x200)
    popen(argv, creationflags=flags, close_fds=True)


class UpdatesTab(QWidget):
    def __init__(self, ctx: Ctx, parent: QWidget | None = None, popen: Popen = subprocess.Popen) -> None:
        super().__init__(parent)
        self.ctx, self._popen = ctx, popen
        lay = QVBoxLayout(self)
        lay.setSpacing(10)
        box = QGroupBox("Installed version")
        f = QFormLayout(box)
        self.installed = QLabel()
        self.previous = QLabel()
        self.last = QLabel()
        self.last.setWordWrap(True)
        f.addRow("This computer", self.installed)
        f.addRow("Previous version kept", self.previous)
        f.addRow("Last update", self.last)
        lay.addWidget(box)
        c = ctx.palette.text
        row = QHBoxLayout()
        self.b_apply = icon_button("Apply update file…", "download", c)
        # lambda: clicked(bool) would otherwise pass False as the file path / reason (field report
        # 2026-11-11: the button did nothing).
        self.b_apply.clicked.connect(lambda: self.apply_file())
        self.b_back = icon_button("Go back to the previous version…", "refresh", c)
        self.b_back.clicked.connect(lambda: self.go_back())
        row.addWidget(self.b_apply)
        row.addWidget(self.b_back)
        row.addStretch(1)
        lay.addLayout(row)
        lay.addWidget(muted(
            "Copy the update file (Sentinel-update-<version>.sntu) anywhere on this computer or a USB stick, "
            "press Apply update file… and pick it. It carries only what changed. Sentinel checks its "
            "signature first, prepares the new version while recording continues, then stops recording for "
            "about a minute, swaps the program files and checks the new version. If the check fails, or the "
            "recorder does not work within 3 minutes, it goes back to the version that was installed before "
            "by itself. Every update is written to the recording log. You can also drop the file onto "
            "Sentinel.exe."))
        lay.addStretch(1)
        self.b_apply.setEnabled(ctx.admin)
        self.refresh()

    # ---------------------------------------------------------------- display
    def refresh(self) -> None:
        from ...update.apply import folder_version, read_result

        folders = _folders()
        built = ""
        if folders is not None:
            from ...update.apply import installed_manifest

            m = installed_manifest(folders.app)
            built = f" (built {m.get('built_utc', '?')}, commit {str(m.get('git_commit', ''))[:10]})" if m else ""
        self.installed.setText(f"Sentinel {__version__}{built}")
        prev = folder_version(folders.previous) if folders is not None else None
        self.previous.setText(f"Sentinel {prev}" if prev else "none")
        self.b_back.setEnabled(self.ctx.admin and prev is not None)
        res = read_result(folders) if folders is not None else None
        self.last.setText(f"{res.get('finished_utc', '')}: {res.get('message', '')}" if res else "none on record")

    # ---------------------------------------------------------------- actions
    def _ready(self) -> bool:
        if app_dir() is None:
            QMessageBox.information(self, "Sentinel", "Updates apply to the installed program (Sentinel.exe), not to "
                                    "a source checkout.")
            return False
        win = self.window()
        busy = win.busy_work() if hasattr(win, "busy_work") else []
        if busy:
            QMessageBox.information(self, "Sentinel", "Wait until this has finished first: " + ", ".join(busy) + ".")
            return False
        return True

    def _hand_over(self, argv: list[str]) -> None:
        start_detached_console(argv, self._popen)
        win = self.window()
        if hasattr(win, "close_for_update"):
            win.close_for_update()

    def apply_file(self, path: str | None = None) -> None:
        from ...update.package import EXTENSION, UpdateRefused, read_package

        if not self._ready():
            return
        if not isinstance(path, str) or not path:
            path, _ = QFileDialog.getOpenFileName(self, "Sentinel update file", "",
                                                  f"Sentinel update (*{EXTENSION});;All files (*)")
            if not path:
                return
        try:
            pkg = read_package(Path(path), installed_version=__version__)
        except UpdateRefused as exc:
            QMessageBox.warning(self, "Sentinel", f"This update file cannot be used:\n\n{exc}")
            return
        if QMessageBox.question(
                self, "Sentinel", f"Update from Sentinel {__version__}?\n\n{pkg.summary()}\n\nRecording stops for "
                "about a minute while the program files are swapped, then restarts by itself. This window closes "
                "and reopens; progress is shown in a separate window.",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No) != QMessageBox.StandardButton.Yes:
            return
        argv = sentinel_argv("update", "apply", str(Path(path).resolve()), "--yes")
        if self.ctx.cfg_path is not None:
            argv += ["--config", str(self.ctx.cfg_path)]
        self._hand_over(argv)

    def go_back(self, reason: str = "", ask: bool = True) -> None:
        from ...update.apply import folder_version

        folders = _folders()
        prev = folder_version(folders.previous) if folders is not None else None
        if prev is None or not self._ready():
            return
        if ask and QMessageBox.question(
                self, "Sentinel", f"Go back from Sentinel {__version__} to {prev}?\n\nRecording stops for about a "
                "minute. The current version is kept, so you can return to it the same way.",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No) != QMessageBox.StandardButton.Yes:
            return
        argv = sentinel_argv("update", "rollback", "--yes", "--reason", reason or "requested by an administrator")
        if self.ctx.cfg_path is not None:
            argv += ["--config", str(self.ctx.cfg_path)]
        self._hand_over(argv)
