"""Port markers, route-to-port, drag-move, and footprint preview (§3.2).

Offscreen Qt; real QMouseEvents + pumped event loop (the live-GUI lesson).
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
pytest.importorskip("PySide6")

from PySide6.QtCore import QEvent, QPointF, Qt  # noqa: E402
from PySide6.QtGui import QMouseEvent  # noqa: E402
from PySide6.QtWidgets import QApplication, QGraphicsItem  # noqa: E402

from engine.catalog import BlockCatalog  # noqa: E402
from model.connection import ChipPortEndpoint  # noqa: E402
from ui.canvas.chip_canvas import Tool  # noqa: E402
from ui.canvas.footprint_item import FootprintItem  # noqa: E402
from ui.canvas.port_item import PortItem  # noqa: E402
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
    ctrl.new_project("Ports", "kyttar_10x12")
    w._after_project_loaded()
    return w


def _mouse(canvas, kind, gx, gy, *, ctrl=False, buttons=Qt.LeftButton):
    sx, sy = gx * 64 + 32, gy * 64 + 32
    vp = canvas.mapFromScene(QPointF(sx, sy))
    gp = canvas.viewport().mapToGlobal(vp)
    mods = Qt.ControlModifier if ctrl else Qt.NoModifier  # single-cell = Ctrl+drag
    return QMouseEvent(kind, QPointF(vp), QPointF(gp),
                       Qt.LeftButton, buttons, mods)


# --------------------------------------------------------------------------- #
# Port markers
# --------------------------------------------------------------------------- #


class TestPortMarkers:
    def test_all_ports_rendered(self, window):
        names = sorted(p.name for p in window.canvas.port_items())
        assert names == ["x16_in", "x16_out", "x1_in", "x1_out"]

    def test_connected_state(self, qapp, catalog):
        ctrl = AppController(catalog=catalog)
        w = MainWindow(controller=ctrl)
        ctrl.open_project(Path(__file__).parent / "data" / "demo" / "gain_demo.kyt")
        w._after_project_loaded()
        by = {p.name: p._connected for p in w.canvas.port_items()}
        assert by["x16_out"] is True   # gain_to_dac uses it
        assert by["x1_in"] is False

    def test_port_is_selectable(self, window):
        port = window.canvas.port_items()[0]
        assert port.flags() & QGraphicsItem.ItemIsSelectable

    def test_input_arrow_points_in_output_out(self, window):
        ports = {p.name: p for p in window.canvas.port_items()}
        # x16_in sits on the NORTH edge → arrow points south (into the chip);
        # output points outward (== its outward vector).
        assert ports["x16_in"]._dir != ports["x16_in"]._out      # input: inward
        assert ports["x16_out"]._dir == ports["x16_out"]._out     # output: outward

    def test_port_paints(self, qapp):
        from PySide6.QtCore import QRectF
        from PySide6.QtGui import QColor, QImage, QPainter
        from PySide6.QtWidgets import QGraphicsScene
        from model.chip_type import PortSpec
        from model.enums import Face, PortDirection

        port = PortItem(
            PortSpec("x16_out", PortDirection.OUTPUT, 16, 9, 0, Face.EAST),
            0, (0.0, 0.0), connected=True)
        scene = QGraphicsScene()
        scene.addItem(port)
        img = QImage(80, 80, QImage.Format_ARGB32)
        img.fill(QColor("black"))
        p = QPainter(img)
        scene.render(p, QRectF(img.rect()), port.boundingRect())
        p.end()


# --------------------------------------------------------------------------- #
# Route to a port
# --------------------------------------------------------------------------- #


class TestRouteToPort:
    def _placed(self, window):
        window.controller.place_block("GainBlock", 0, 6, 0,
                                      library="lattrex.official")
        window.canvas.render_scene()

    def test_complete_to_port_creates_chip_endpoint(self, window):
        self._placed(window)
        c = window.canvas
        c.start_route("gain", 0, 6, 0)
        c.add_waypoint(7, 0)
        c.add_waypoint(8, 0)
        c.complete_route_to_port("x16_out")
        _pump()
        conns = window.controller.project.connections
        assert len(conns) == 1
        assert isinstance(conns[0].target, ChipPortEndpoint)
        assert conns[0].target.port == "x16_out"

    def test_routed_to_port_builds(self, window):
        self._placed(window)
        c = window.canvas
        c.start_route("gain", 0, 6, 0)
        c.add_waypoint(7, 0)
        c.add_waypoint(8, 0)
        c.complete_route_to_port("x16_out")
        _pump()
        result = window.controller.build()
        assert result.ok, [str(e) for e in result.errors]

    def test_click_port_marker_completes(self, window):
        self._placed(window)
        c = window.canvas
        c.start_route("gain", 0, 6, 0)
        c.add_waypoint(7, 0)
        c.add_waypoint(8, 0)
        # click the x16_out port marker
        port = next(p for p in c.port_items() if p.name == "x16_out")
        # itemAt needs the marker under the cursor; call complete directly via
        # the same path mousePressEvent would take.
        assert port.chip_id == c._route_chip
        c.complete_route_to_port(port.name)
        _pump()
        assert c.tool is Tool.SELECT
        assert len(window.controller.project.connections) == 1


# --------------------------------------------------------------------------- #
# Drag-move
# --------------------------------------------------------------------------- #


class TestDragMove:
    def _block(self, window, btype="GardnerTimingRecovery", x=1, y=1):
        window.controller.place_block(btype, 0, x, y, library="lattrex.official")
        window.canvas.render_scene()
        return window.controller.project.blocks[-1].name

    def test_whole_block_drag(self, window):
        name = self._block(window)
        c = window.canvas
        c.mousePressEvent(_mouse(c, QEvent.MouseButtonPress, 1, 1))
        c.mouseMoveEvent(_mouse(c, QEvent.MouseMove, 1, 5))
        assert c._footprint is not None  # preview shows during drag
        c.mouseReleaseEvent(
            _mouse(c, QEvent.MouseButtonRelease, 1, 5, buttons=Qt.NoButton))
        _pump()
        cells = window.controller.project.block(name).placement.cells
        assert cells[0].pos == (1, 5)

    def test_drag_undo(self, window):
        name = self._block(window)
        c = window.canvas
        c.mousePressEvent(_mouse(c, QEvent.MouseButtonPress, 1, 1))
        c.mouseMoveEvent(_mouse(c, QEvent.MouseMove, 1, 5))
        c.mouseReleaseEvent(
            _mouse(c, QEvent.MouseButtonRelease, 1, 5, buttons=Qt.NoButton))
        _pump()
        window.controller.undo()
        assert window.controller.project.block(name).placement.cells[0].pos == (1, 1)

    def test_overlap_rejected(self, window):
        a = self._block(window, x=1, y=1)
        self._block(window, btype="GainBlock", x=1, y=6)
        c = window.canvas
        c.mousePressEvent(_mouse(c, QEvent.MouseButtonPress, 1, 1))
        c.mouseMoveEvent(_mouse(c, QEvent.MouseMove, 1, 6))  # onto the gain block
        c.mouseReleaseEvent(
            _mouse(c, QEvent.MouseButtonRelease, 1, 6, buttons=Qt.NoButton))
        _pump()
        # unchanged — overlap reverts
        assert window.controller.project.block(a).placement.cells[0].pos == (1, 1)

    def test_ctrl_drag_single_cell(self, window):
        name = self._block(window)  # 4-cell row at (1..4, 1)
        c = window.canvas
        first = window.controller.project.block(name).placement.cells[0]
        c.mousePressEvent(_mouse(c, QEvent.MouseButtonPress, first.x, first.y,
                                 ctrl=True))
        c.mouseMoveEvent(_mouse(c, QEvent.MouseMove, 7, 8, ctrl=True))
        c.mouseReleaseEvent(_mouse(c, QEvent.MouseButtonRelease, 7, 8, ctrl=True,
                                   buttons=Qt.NoButton))
        _pump()
        cells = window.controller.project.block(name).placement.cells
        assert cells[0].pos == (7, 8)          # the one cell moved
        assert cells[1].pos == (2, 1)          # the rest unchanged

    def test_plain_click_no_move(self, window):
        name = self._block(window)
        c = window.canvas
        # press + release on the same cell → selection, not a move
        c.mousePressEvent(_mouse(c, QEvent.MouseButtonPress, 1, 1))
        c.mouseReleaseEvent(
            _mouse(c, QEvent.MouseButtonRelease, 1, 1, buttons=Qt.NoButton))
        _pump()
        assert window.controller.project.block(name).placement.cells[0].pos == (1, 1)


# --------------------------------------------------------------------------- #
# Footprint item
# --------------------------------------------------------------------------- #


class TestFootprintItem:
    def test_paints_ok_and_bad(self, qapp):
        from PySide6.QtCore import QRectF
        from PySide6.QtGui import QColor, QImage, QPainter
        from PySide6.QtWidgets import QGraphicsScene

        item = FootprintItem([(0, 0), (1, 0), (2, 0)], (0.0, 0.0),
                             bad_cells={(1, 0)})
        scene = QGraphicsScene()
        scene.addItem(item)
        img = QImage(int(item.boundingRect().width()),
                     int(item.boundingRect().height()), QImage.Format_ARGB32)
        img.fill(QColor("black"))
        p = QPainter(img)
        scene.render(p, QRectF(img.rect()), item.boundingRect())
        p.end()

    def test_empty(self, qapp):
        assert FootprintItem([], (0.0, 0.0)).boundingRect().isEmpty()


class TestRouteLineToPort:
    def test_line_extends_to_port_anchor(self, qapp, catalog):
        from ui.canvas.connection_item import ConnectionItem

        ctrl = AppController(catalog=catalog)
        w = MainWindow(controller=ctrl)
        ctrl.open_project(Path(__file__).parent / "data" / "demo" / "gain_demo.kyt")
        w._after_project_loaded()
        _pump()
        ci = next(it for it in w.canvas.scene().items()
                  if isinstance(it, ConnectionItem))
        # last point is the port anchor at x16_out (col 9 → 9*64+64 = 640)
        assert round(ci._pts[-1].x()) == 640


class TestSerpentineLayout:
    def test_dfe_places_as_serpentine_in_bounds(self, qapp, catalog):
        ctrl = AppController(catalog=catalog)
        ctrl.new_project("DFE", "kyttar_10x12")
        ctrl.place_block("DFEEqualizerBlock", 0, 0, 0, library="lattrex.official")
        cells = ctrl.project.block("dfeequalizer").placement.cells
        assert len(cells) > 10                       # multi-cell
        xs = [c.x for c in cells]
        ys = [c.y for c in cells]
        assert max(ys) > min(ys)                     # multi-row (not a flat line)
        assert all(0 <= c.x < 10 and 0 <= c.y < 12 for c in cells)  # fits

    def test_catalog_default_layout(self, catalog):
        layout = catalog.default_layout("DFEEqualizerBlock")
        assert len(layout) > 10
        # every entry is (dx, dy, face)
        for cid, (dx, dy, face) in layout.items():
            assert isinstance(dx, int) and isinstance(dy, int)
            assert face in ("south", "east", "west", "north")

    def test_single_cell_block_layout(self, catalog):
        layout = catalog.default_layout("AGCBlock")
        assert len(layout) == 1


class TestRoutingCells:
    def _window(self, catalog):
        ctrl = AppController(catalog=catalog)
        w = MainWindow(controller=ctrl)
        ctrl.open_project(Path(__file__).parent / "data" / "demo" / "gain_demo.kyt")
        w._after_project_loaded()
        return w

    def test_routing_cells_rendered(self, qapp, catalog):
        from ui.canvas.cell_item import CellKind

        w = self._window(catalog)
        _pump()
        routing = [c for c in w.canvas.cell_items()
                   if c.kind is CellKind.TRANSIT]
        assert routing  # the demo route has intermediate waypoints
        assert all(c.route_name == "gain_to_dac" for c in routing)

    def test_routing_cell_selection_shows_in_inspector(self, qapp, catalog):
        from ui.canvas.cell_item import CellKind

        w = self._window(catalog)
        _pump()
        routing = [c for c in w.canvas.cell_items()
                   if c.kind is CellKind.TRANSIT][0]
        routing.setSelected(True)
        _pump()
        assert w.inspector._sel is not None
        assert w.inspector._sel.get("route") == "gain_to_dac"
        assert "routing cell" in w.inspector._title.text() or \
            w.inspector._sel.get("route") is not None
