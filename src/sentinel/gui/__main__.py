"""Sentinel window entry point.

    python -m sentinel.gui --config <sentinel.toml> [--theme auto|light|dark] [--windowed]

Opening or closing the window never starts or stops the recorder. Settings and actions in it need
Windows administrator rights (see sentinel.gui.app).
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

from ..config import ConfigError, load_config

EXIT_OK, EXIT_CONFIG, EXIT_USAGE = 0, 2, 64


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="sentinel-gui", description="Sentinel window")
    ap.add_argument("--config", required=True, help="path to the recorder's TOML configuration")
    ap.add_argument("--theme", choices=("auto", "light", "dark"), default=None,
                    help="appearance for this session only (default: the user's remembered choice, "
                         "else Follow Windows)")
    ap.add_argument("--windowed", action="store_true", help="start in a window instead of maximised")
    args = ap.parse_args(argv)
    app = _app(args.theme)
    try:
        cfg = load_config(args.config)
    except ConfigError as exc:
        from PySide6.QtWidgets import QMessageBox

        print(f"configuration error: {exc}", file=sys.stderr)
        QMessageBox.critical(None, "Sentinel",
                             f"Sentinel cannot open because its settings could not be read:\n\n{exc}\n\n"
                             "Recording is not affected by this window.")
        return EXIT_CONFIG
    return run_window(cfg, theme=args.theme, windowed=args.windowed, app=app)


def prepare_qt_environment() -> None:
    """Offscreen rendering on Windows has no font database unless pointed at the system fonts."""
    if os.environ.get("QT_QPA_PLATFORM") == "offscreen" and sys.platform == "win32" \
            and not os.environ.get("QT_QPA_FONTDIR"):
        os.environ["QT_QPA_FONTDIR"] = str(Path(os.environ.get("WINDIR", r"C:\Windows")) / "Fonts")


def _app(appearance: str | None = None):  # noqa: ANN202
    prepare_qt_environment()
    from PySide6.QtWidgets import QApplication

    from .app import style
    from .prefs import load_appearance

    app = QApplication.instance() or QApplication(sys.argv[:1])
    app.setApplicationName("Sentinel")
    style.apply(app, appearance or load_appearance())  # type: ignore[arg-type]
    return app


def run_window(cfg, theme: str | None = None, windowed: bool = False, app=None) -> int:  # noqa: ANN001
    """Open the Sentinel window for a loaded configuration and run until it is closed (also the
    launcher's view: ``Sentinel.exe``)."""
    app = app or _app(theme)
    from pathlib import Path

    from .app.window import AppWindow

    win = AppWindow(cfg, Path(cfg.source_path) if cfg.source_path else None, appearance=theme)
    return _show(app, win, windowed)


def run_setup_window(on_configured=None, app=None, target=None) -> int:  # noqa: ANN001
    """First run (no configuration yet): the window opens on "Set up this computer". ``target`` is
    where the configuration is written (the launcher's --config / SENTINEL_CONFIG / default)."""
    app = app or _app()
    from .app.window import AppWindow

    win = AppWindow(None, None, on_configured=on_configured, setup_target=target)
    return _show(app, win, True)


def _show(app, win, windowed: bool) -> int:  # noqa: ANN001
    """The last size and position if remembered; otherwise maximised (or 1200x760 when windowed)."""
    if win.restore_geometry():
        win.show()
    elif windowed:
        win.resize(1200, 760)
        win.show()
    else:
        win.showMaximized()
    return int(app.exec())


if __name__ == "__main__":
    sys.exit(main())
