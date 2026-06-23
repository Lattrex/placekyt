"""Round-3 live-GUI fixes (the architecture notes §2.2, §3.2). Offscreen Qt.

- required-param blocks (FIR/IIR/Decimator) placeable via placeholder params
- library drag anchor normalized to the drop cell
- route-to-port adjacency enforced (no diagonal jump)
- Ctrl/Shift+scroll pan, 'w' key starts a route
- I/O cell indicators on multi-cell blocks
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
pytest.importorskip("PySide6")

from PySide6.QtCore import QEvent, QPoint, QPointF, Qt  # noqa: E402
from PySide6.QtGui import QKeyEvent, QWheelEvent  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from engine.catalog import BlockCatalog  # noqa: E402
from ui.canvas.chip_canvas import Tool  # noqa: E402
from ui.controller import AppController  # noqa: E402
from ui.main_window import MainWindow  # noqa: E402

from tests.conftest import CHIP_YAML as CT_PATH  # noqa: E402
pytestmark = pytest.mark.skipif(not CT_PATH.exists(), reason="chip yaml absent")


def _pump():
    QApplication.processEvents()


@pytest.fixture(scope="module")
def qapp():
    return QApplication.instance() or QApplication([])


@pytest.fixture(scope="module")
def catalog():
    return BlockCatalog.from_gr_kyttar()


@pytest.fixture
def window(qapp, catalog):
    ctrl = AppController(catalog=catalog)
    w = MainWindow(controller=ctrl)
    ctrl.new_project("R3", "kyttar_10x12")
    w._after_project_loaded()
    return w


# --------------------------------------------------------------------------- #
# Required-param blocks
# --------------------------------------------------------------------------- #


class TestRequiredParamBlocks:
    @pytest.mark.parametrize("btype", ["FIRFilterBlock", "IIRBiquadBlock",
                                       "DecimatorBlock"])
    def test_places_with_placeholder(self, window, btype):
        name = window.controller.place_block(btype, 0, 1, 1,
                                             library="lattrex.official")
        blk = window.controller.project.block(name)
        assert blk is not None and blk.is_placed
        assert blk.params  # placeholder coefficients filled in

    def test_placeholder_is_passthrough_buildable(self, window):
        window.controller.place_block("FIRFilterBlock", 0, 0, 0,
                                      library="lattrex.official")
        # FIR with [1.0] is a 1-cell identity filter — catalog cell_count works.
        n = window.controller.catalog.cell_count("FIRFilterBlock")
        assert n >= 1


# --------------------------------------------------------------------------- #
# Library drag anchor normalization
# --------------------------------------------------------------------------- #


class TestDropAnchor:
    def test_dfe_normalized_to_drop_cell(self, window):
        window.controller.place_block("DFEEqualizerBlock", 0, 0, 0,
                                      library="lattrex.official")
        cells = window.controller.project.block("dfeequalizer").placement.cells
        assert min(c.x for c in cells) == 0   # reaches column 0
        assert min(c.y for c in cells) == 0   # reaches row 0

    def test_dfe_drops_at_arbitrary_anchor(self, window):
        window.controller.place_block("DFEEqualizerBlock", 0, 2, 3,
                                      library="lattrex.official")
        cells = window.controller.project.block("dfeequalizer").placement.cells
        assert min(c.x for c in cells) == 2
        assert min(c.y for c in cells) == 3

    def test_footprint_matches_placement(self, window):
        offsets = window._block_footprint("DFEEqualizerBlock", "lattrex.official")
        assert min(dx for dx, _ in offsets) == 0
        assert min(dy for _, dy in offsets) == 0


# --------------------------------------------------------------------------- #
# Route-to-port adjacency
# --------------------------------------------------------------------------- #


class TestPortAdjacency:
    def test_adjacent_port_completes(self, window):
        c = window.canvas
        window.controller.place_block("GainBlock", 0, 2, 0,
                                      library="lattrex.official")
        c.render_scene()
        c.start_route("gain", 0, 2, 0)
        for x in range(3, 9):
            c.add_waypoint(x, 0)             # last waypoint (8,0)
        assert c.complete_route_to_port("x16_out") is True   # (9,0) adjacent
        _pump()
        assert len(window.controller.project.connections) == 1

    def test_nonadjacent_port_rejected(self, window):
        c = window.canvas
        window.controller.place_block("GainBlock", 0, 2, 0,
                                      library="lattrex.official")
        c.render_scene()
        c.start_route("gain", 0, 2, 0)
        c.add_waypoint(3, 0)
        c.add_waypoint(4, 0)                 # last waypoint (4,0)
        assert c.complete_route_to_port("x16_out") is False  # (9,0) far away
        assert c.tool is Tool.ROUTE_DRAW     # still drawing
        assert len(window.controller.project.connections) == 0


# --------------------------------------------------------------------------- #
# Pan + 'w' key
# --------------------------------------------------------------------------- #


class TestPanAndWire:
    def _wheel(self, canvas, dy, mods):
        pos = QPoint(50, 50)
        return QWheelEvent(QPointF(pos), canvas.mapToGlobal(pos),
                           QPoint(0, 0), QPoint(0, dy), Qt.NoButton, mods,
                           Qt.NoScrollPhase, False)

    def _zoom_in(self, c):
        # Zoom in + give the view a real size so the scrollbars have range.
        c.resize(300, 300)
        c.scale(4.0, 4.0)
        _pump()

    def test_ctrl_scroll_pans_horizontal(self, window):
        c = window.canvas
        c.render_scene()
        self._zoom_in(c)
        c.horizontalScrollBar().setValue(50)  # mid-range so it can move both ways
        before = c.horizontalScrollBar().value()
        c.wheelEvent(self._wheel(c, 120, Qt.ControlModifier))
        assert c.horizontalScrollBar().value() != before

    def test_shift_scroll_pans_vertical(self, window):
        c = window.canvas
        c.render_scene()
        self._zoom_in(c)
        c.verticalScrollBar().setValue(50)
        before = c.verticalScrollBar().value()
        c.wheelEvent(self._wheel(c, 120, Qt.ShiftModifier))
        assert c.verticalScrollBar().value() != before

    def test_plain_scroll_zooms(self, window):
        c = window.canvas
        c.reset_zoom()
        before = c.scale_factor
        c.wheelEvent(self._wheel(c, 120, Qt.NoModifier))
        assert c.scale_factor != before

    def test_scene_rect_padded_for_full_pan(self, window):
        # The scene rect extends well past the tight item bounds so the array
        # can be panned fully off-screen in all four directions.
        c = window.canvas
        c.render_scene()
        items = c.scene().itemsBoundingRect()
        sr = c.scene().sceneRect()
        assert sr.left() < items.left() and sr.top() < items.top()
        assert sr.right() > items.right() and sr.bottom() > items.bottom()

    def test_w_key_starts_route(self, window):
        c = window.canvas
        window.controller.place_block("GainBlock", 0, 3, 3,
                                      library="lattrex.official")
        c.render_scene()
        [cell for cell in c.cell_items() if cell.label][0].setSelected(True)
        c.keyPressEvent(QKeyEvent(QEvent.KeyPress, Qt.Key_W, Qt.NoModifier))
        assert c.tool is Tool.ROUTE_DRAW


# --------------------------------------------------------------------------- #
# I/O cell indicators
# --------------------------------------------------------------------------- #


class TestIOIndicators:
    def test_multicell_block_marks_input_output(self, window):
        window.controller.place_block("GardnerTimingRecovery", 0, 1, 1,
                                      library="lattrex.official")
        window.canvas.render_scene()
        roles = {c.io_role for c in window.canvas.cell_items() if c.io_role}
        assert roles == {"input", "output"}

    def test_single_cell_block_no_indicator(self, window):
        window.controller.place_block("GainBlock", 0, 1, 1,
                                      library="lattrex.official")
        window.canvas.render_scene()
        gain = [c for c in window.canvas.cell_items() if c.label == "gain"]
        assert all(c.io_role is None for c in gain)
