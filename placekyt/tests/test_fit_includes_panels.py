# SPDX-License-Identifier: GPL-3.0-or-later
"""Project-open fit must include SRAM/peripheral PANELS in the initial view
(user request 2026-08-12): a panel-backed design used to open zoomed onto the
cell array only, hiding the panel off-screen."""
from __future__ import annotations

import os
from pathlib import Path

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication  # noqa: E402

from engine.catalog import BlockCatalog  # noqa: E402
from engine.io.chip_type_io import load_chip_type  # noqa: E402
from engine.io.project_io import load_project  # noqa: E402
from ui.canvas.chip_canvas import ChipCanvas  # noqa: E402

from tests.conftest import CHIP_YAML  # noqa: E402

_ROOT = Path(__file__).resolve().parents[2]
KYT = _ROOT / "examples" / "cw_transceiver" / "cw_transceiver.kyt"

pytestmark = pytest.mark.skipif(not KYT.exists(), reason="cw kyt absent")


def test_fit_rect_unites_panel(qapp=None):
    QApplication.instance() or QApplication([])
    from ui.canvas.cell_item import CellItem
    from ui.canvas.panel_item import PanelItem

    cat = BlockCatalog.from_gr_kyttar()
    ct = load_chip_type(str(CHIP_YAML))
    project = load_project(str(KYT))
    assert project.panels, "cw transceiver must carry its SRAM panel"
    canvas = ChipCanvas()
    canvas.port_cell_provider = lambda bt, lib, params=None: {
        p.name: (p.cell_id, p.direction)
        for p in cat.port_map(bt, params, library=lib).ports}
    canvas.set_project(project, {getattr(ct, "name", "kyttar_10x12"): ct})
    canvas.render_scene()

    panel_items = [it for it in canvas._scene.items()
                   if isinstance(it, PanelItem)]
    assert panel_items, "the panel must render"
    cells_rect = None
    for it in canvas._scene.items():
        if isinstance(it, CellItem):
            r = it.sceneBoundingRect()
            cells_rect = r if cells_rect is None else cells_rect.united(r)
    fit = canvas._grid_fit_rect()
    for p in panel_items:
        assert fit.contains(p.sceneBoundingRect()), \
            "initial fit must include the SRAM panel"
    assert fit.contains(cells_rect), "initial fit must still include the array"
