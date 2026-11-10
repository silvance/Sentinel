"""Render the owner-review screenshots of the operator GUI (offscreen, 1366x768).

    python tests/render_gui_screenshots.py [OUT_DIR]      (default: docs/branches/gui-screenshots)

Each scenario writes a real ledger and a status document produced by the recorder's own status
logic into a temporary folder, opens the real MainWindow on it and saves a PNG.
"""

from __future__ import annotations

import os
import sys
import tempfile
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path[:0] = [str(HERE.parent / "src"), str(HERE)]
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from gui_support import SCENARIOS, gui_config_dict, is_test_camera, populate_ledger, scenario, write_doc  # noqa: E402
from sentinel.config import load_config_dict  # noqa: E402
from sentinel.gui.window import prepare_qt_environment  # noqa: E402

prepare_qt_environment()

from PySide6.QtWidgets import QApplication  # noqa: E402

from sentinel.gui.theme import DARK, LIGHT  # noqa: E402
from sentinel.gui.window import MainWindow, apply_app_style  # noqa: E402

SIZE = (1366, 768)


def make_cfg(tmp: Path, test_camera: bool):  # noqa: ANN201
    return load_config_dict(gui_config_dict(tmp, test_camera), base_dir=tmp)


def render(app: QApplication, name: str, out: Path, *, page: str = "dashboard", dark: bool = False) -> Path:
    now = time.time()
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        cfg = make_cfg(tmp, is_test_camera(name))
        doc, last_end = scenario(name, now)
        gap_reason = (doc.get("open_gap") or {}).get("reason")
        populate_ledger(cfg.data_path / "sentinel.db", now, end_at=last_end, open_gap_reason=gap_reason)
        write_doc(cfg.data_path / "status.json", doc)
        win = MainWindow(cfg, DARK if dark else LIGHT, export_available=False, auto_refresh=False,
                         clock=lambda: now)
        win.resize(*SIZE)
        win.show()
        if page == "timeline":
            win.show_timeline()
        elif page == "exports":
            win.show_exports()
        for _ in range(5):
            app.processEvents()
        suffix = {"dashboard": "", "timeline": "_timeline", "exports": "_exports"}[page] + ("_dark" if dark else "")
        target = out / f"{name}{suffix}.png"
        win.grab().save(str(target))
        win.close()
        win.deleteLater()
        app.processEvents()
    return target


def main() -> int:
    out = Path(sys.argv[1]) if len(sys.argv) > 1 else HERE.parent / "docs" / "branches" / "gui-screenshots"
    out.mkdir(parents=True, exist_ok=True)
    app = QApplication.instance() or QApplication([])
    apply_app_style(app)  # type: ignore[arg-type]
    made = [render(app, n, out) for n in SCENARIOS]
    made.append(render(app, "r003_notice", out, page="timeline"))
    made.append(render(app, "recording_test_camera", out, page="exports"))
    made.append(render(app, "r003_critical", out, dark=True))
    for p in made:
        print(p)
    return 0


if __name__ == "__main__":
    sys.exit(main())
