"""placeKYT GUI entry point (the architecture notes §3.1, §6).

Creates the QApplication and MainWindow. Optionally opens a project given on
the command line. The headless CLI lives in ``cli.py``; this module is the GUI.
"""

from __future__ import annotations

import sys


def main(argv: list[str] | None = None) -> int:
    from PySide6.QtWidgets import QApplication

    from ui.main_window import MainWindow

    argv = sys.argv if argv is None else argv
    app = QApplication(argv)
    app.setApplicationName("placeKYT")
    app.setOrganizationName("Lattrex")
    _apply_app_icon(app)

    window = MainWindow()

    # Optional: open a project passed as the first positional arg.
    if len(argv) > 1 and argv[1].endswith(".kyt"):
        _try_open(window, argv[1])

    window.show()
    return app.exec()


def _apply_app_icon(app) -> None:
    """Set the Lattrex logo as the application (taskbar/title) icon. Best-effort
    — a missing asset just leaves the default icon."""
    from pathlib import Path

    from PySide6.QtGui import QIcon

    icon_path = Path(__file__).resolve().parent / "resources" / "icons" \
        / "lattrex_logo.png"
    if icon_path.exists():
        app.setWindowIcon(QIcon(str(icon_path)))


def _try_open(window, kyt_path: str) -> None:
    """Best-effort open of a project on launch (errors go to the status bar)."""
    try:
        # Go through the controller so the project loads with the GUI's
        # populated chip-type registry (bundled resources/chips/ + user dir) —
        # set_project alone would pass an empty chip-types dict and the canvas
        # would render nothing.
        window.controller.open_project(kyt_path)
        window._after_project_loaded()
    except Exception as exc:  # noqa: BLE001 — launch must not crash on bad arg
        window.statusBar().showMessage(f"Could not open {kyt_path}: {exc}")


if __name__ == "__main__":
    sys.exit(main())
