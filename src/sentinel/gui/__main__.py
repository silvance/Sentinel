"""Operator GUI entry point.

    python -m sentinel.gui --config <sentinel.toml> [--theme auto|light|dark] [--windowed]
    python -m sentinel.gui --config <sentinel.toml> --set-admin-pin

The GUI is a viewer only. Starting or closing it never starts, stops or signals the recorder.
"""

from __future__ import annotations

import argparse
import getpass
import sys

from ..config import ConfigError, load_config
from .admin_pin import PinError, admin_pin_dir, set_pin

EXIT_OK, EXIT_CONFIG, EXIT_USAGE = 0, 2, 64


def _set_pin(data_dir_cfg: str) -> int:
    cfg = load_config(data_dir_cfg)
    first = getpass.getpass("New administrator PIN (6-12 digits): ")
    second = getpass.getpass("Repeat the PIN: ")
    if first != second:
        print("The two PINs do not match. Nothing was changed.", file=sys.stderr)
        return EXIT_USAGE
    try:
        path = set_pin(admin_pin_dir(cfg), first)
    except PinError as exc:
        print(f"{exc} Nothing was changed.", file=sys.stderr)
        return EXIT_USAGE
    print(f"Administrator PIN set ({path}).")
    return EXIT_OK


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="sentinel-gui", description="Sentinel operator screen (read-only viewer)")
    ap.add_argument("--config", required=True, help="path to the recorder's TOML configuration")
    ap.add_argument("--theme", choices=("auto", "light", "dark"), default=None,
                    help="display for this session only (default: the user's remembered choice, "
                         "else Follow Windows)")
    ap.add_argument("--windowed", action="store_true", help="start in a window instead of maximised")
    ap.add_argument("--set-admin-pin", action="store_true", help="set the administrator PIN and exit")
    args = ap.parse_args(argv)

    if args.set_admin_pin:
        try:
            return _set_pin(args.config)
        except ConfigError as exc:
            print(f"configuration error: {exc}", file=sys.stderr)
            return EXIT_CONFIG

    app = _app()
    try:
        cfg = load_config(args.config)
    except ConfigError as exc:
        from PySide6.QtWidgets import QMessageBox

        print(f"configuration error: {exc}", file=sys.stderr)
        QMessageBox.critical(None, "Sentinel",
                             "The Sentinel screen cannot start because its settings could not be read.\n\n"
                             "Recording is not affected by this screen. Contact technical support.")
        return EXIT_CONFIG
    return run_window(cfg, theme=args.theme, windowed=args.windowed, app=app)


def _app():  # noqa: ANN202
    from .window import prepare_qt_environment
    prepare_qt_environment()
    from PySide6.QtWidgets import QApplication

    from .window import apply_app_style

    app = QApplication.instance() or QApplication(sys.argv[:1])
    app.setApplicationName("Sentinel")
    apply_app_style(app)  # type: ignore[arg-type]
    return app


def run_window(cfg, theme: str | None = None, windowed: bool = False, app=None) -> int:  # noqa: ANN001
    """Open the operator screen for a loaded configuration and run until it is closed (also the
    launcher's view: ``Sentinel.exe``)."""
    app = app or _app()
    from .window import MainWindow

    win = MainWindow(cfg, appearance=theme)
    if windowed:
        win.resize(1366, 768)
        win.show()
    else:
        win.showMaximized()
    return int(app.exec())


if __name__ == "__main__":
    sys.exit(main())
