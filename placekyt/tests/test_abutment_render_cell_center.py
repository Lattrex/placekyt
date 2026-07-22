# SPDX-License-Identifier: GPL-3.0
"""An ABUTMENT connection (adjacent-block hop, no corridor waypoints) renders as a
cell-center-to-cell-center line — identical to a manual/corridor route — NOT as a short
angled "jumper" between the two port-edge anchors.

An auto-routed hop between two neighbouring blocks becomes an abutment (their I/O cells
touch, the build synthesises the @1 handoff). It used to draw a solid link between the
two PORT-EDGE anchors (offset to the cell corners by port face), which read as a little
diagonal jumper — visually different from the clean green line a manual route draws. This
asserts both draw through the cell CENTERS.

Run:
    QT_QPA_PLATFORM=offscreen placekyt/.venv/bin/python -m pytest \
        placekyt/tests/test_abutment_render_cell_center.py -q
"""

from __future__ import annotations

import os

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication  # noqa: E402

from ui.main_window import MainWindow  # noqa: E402
from ui.canvas.connection_item import ConnectionItem  # noqa: E402
from ui.canvas.cell_item import CELL_PX  # noqa: E402
from model.connection import BlockEndpoint, ABUTMENT_ROUTE  # noqa: E402


@pytest.fixture(scope="module")
def qapp():
    return QApplication.instance() or QApplication([])


def test_abutment_renders_cell_center_to_cell_center(qapp):
    w = MainWindow()
    w.controller.new_project("abut", "kyttar_10x12")
    # Two adjacent single-cell blocks: a at (2,2), b at (3,2). a.out -> b.sample abuts.
    a = w.controller.place_block("GainBlock", 0, 2, 2, library="lattrex.official",
                                 params={"gain": 1.0})
    b = w.controller.place_block("GainBlock", 0, 3, 2, library="lattrex.official",
                                 params={"gain": 1.0})
    w.controller.add_route(BlockEndpoint(block=a, port="out"),
                           BlockEndpoint(block=b, port="sample"), [])
    for c in w.controller.project.connections:
        if getattr(c.source, "block", "") == a:
            c.route = ABUTMENT_ROUTE
    w.canvas.set_project(w.controller.project, w.controller.chip_types())

    items = [it for it in w.canvas._scene.items()
             if isinstance(it, ConnectionItem) and it.connection_name]
    assert items, "the abutment connection did not render"
    it = items[0]
    assert not it.is_fly, "an abutment is a ROUTED line, not a dashed fly line"
    pts = [(round(p.x()), round(p.y())) for p in it._pts]
    # Cell centers of (2,2) and (3,2): x = c*CELL_PX + CELL_PX/2, y likewise.
    def _center(cx, cy):
        return (cx * CELL_PX + CELL_PX // 2, cy * CELL_PX + CELL_PX // 2)
    assert pts == [_center(2, 2), _center(3, 2)], (
        f"abutment must draw cell-center to cell-center (like a manual route); got {pts}")
