# SPDX-License-Identifier: GPL-3.0-or-later
"""Inter-chip landing cells RENDER as transit cells (offscreen Qt).

User-reported on gain_2p2s: the chain tails' cell (0,0) — the destination of
each transparent inter-chip wire — drew as an EMPTY cell, so the chain read as
broken at the seam. Verified functional (the build programs that cell with a
2-word forward program and all four streams recover end-to-end); the gap was
purely rendering: no design-level route covers the landing cell. The canvas now
draws a TRANSIT marker there, faced INTO the chip (the direction relayed words
continue).
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
pytest.importorskip("PySide6")

from PySide6.QtWidgets import QApplication  # noqa: E402

from engine.catalog import BlockCatalog  # noqa: E402
from ui.controller import AppController  # noqa: E402

from tests.conftest import CHIP_YAML as CT_PATH  # noqa: E402

REPO = Path(__file__).resolve().parents[2]
KYT_2P2S = REPO / "examples" / "gain_2p2s" / "gain_2p2s.kyt"

pytestmark = pytest.mark.skipif(
    not (CT_PATH.exists() and KYT_2P2S.exists()),
    reason="chip yaml / gain_2p2s.kyt absent")


@pytest.fixture(scope="module")
def qapp():
    return QApplication.instance() or QApplication([])


def test_interchip_landing_cells_render_as_transit(qapp):
    from model.enums import Face
    from ui.canvas.cell_item import CellItem, CellKind
    from ui.canvas.chip_canvas import ChipCanvas

    ctrl = AppController(catalog=BlockCatalog.from_gr_kyttar())
    ctrl.open_project(str(KYT_2P2S))
    canvas = ChipCanvas()
    canvas.set_project(ctrl.project, ctrl.chip_types())
    canvas.render_scene()

    landings = {it.chip_id: it for it in canvas._scene.items()
                if isinstance(it, CellItem)
                and isinstance(getattr(it, "cell_id", None), tuple)
                and it.cell_id and it.cell_id[0] == "interchip"}
    # Both chain tails (chip1, chip3) get a transit marker at their x16_in
    # landing cell (0,0), faced EAST (into the chip, the bus direction).
    assert set(landings) == {1, 3}, sorted(landings)
    for it in landings.values():
        assert it.kind == CellKind.TRANSIT
        assert (it.cx, it.cy) == (0, 0)
        assert it.face == Face.EAST
    canvas.deleteLater()
