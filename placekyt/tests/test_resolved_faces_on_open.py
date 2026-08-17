# SPDX-License-Identifier: GPL-3.0-or-later
"""Opening a project must paint BUILD-RESOLVED cell arrows (#135).

The canvas renders placement-default faces; the build's egress patch / route
resolution can point an output cell in a DIFFERENT direction than its
authored default (the QPSK slicer's authored north vs functional east). A
freshly-opened .kyt has no later trigger that re-syncs the arrows, so
``_after_project_loaded`` must apply the resolved faces itself — this gate
drives the REAL MainWindow open path on a shipped example and asserts every
non-empty cell's arrow equals its build-resolved face."""
from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
pytest.importorskip("PySide6")

_ROOT = Path(__file__).resolve().parents[2]
for _p in (str(_ROOT / "placekyt"), str(_ROOT / "runtime" / "python")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

_KYT = _ROOT / "examples" / "qpsk_modem" / "qpsk_modem.kyt"

pytestmark = pytest.mark.skipif(not _KYT.exists(), reason="example kyt absent")


def test_arrows_match_build_faces_on_open():
    from PySide6.QtWidgets import QApplication

    from ui.main_window import MainWindow

    app = QApplication.instance() or QApplication([])
    win = MainWindow()
    win.controller.open_project(str(_KYT))
    win._after_project_loaded()
    app.processEvents()

    build = win.controller.cached_build()
    assert build is not None, "example failed to build"
    cells = build.chips[0].cells
    bad = []
    for item in win.canvas.cell_items():
        if item.kind.name == "EMPTY":
            continue
        info = cells.get((item.cx, item.cy))
        bface = info.get("face") if info else None
        if bface and item.face.name.lower() != bface:
            bad.append(((item.cx, item.cy), item.face.name.lower(), bface))
    assert not bad, (
        f"{len(bad)} cell arrows show the placement default instead of the "
        f"build-resolved face: {bad[:6]}")
