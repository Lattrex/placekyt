"""Route-drawing tool tests (the architecture notes §3.2). Offscreen Qt.

Drives the canvas route state machine and pumps the event loop (the live-GUI
lesson: signal cascades + widget lifetime only surface with a real event loop).
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
pytest.importorskip("PySide6")

from PySide6.QtWidgets import QApplication  # noqa: E402

from engine.catalog import BlockCatalog  # noqa: E402
from model.connection import BlockEndpoint, ChipPortEndpoint  # noqa: E402
from ui.canvas.chip_canvas import Tool  # noqa: E402
from ui.canvas.connection_item import ConnectionItem  # noqa: E402
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
    ctrl.new_project("Route Test", "kyttar_10x12")
    w._after_project_loaded()
    ctrl.place_block("GainBlock", 0, 1, 1, library="lattrex.official")
    ctrl.place_block("DCBlockerBlock", 0, 1, 5, library="lattrex.official")
    w.canvas.render_scene()
    return w


# --------------------------------------------------------------------------- #
# Controller add_route
# --------------------------------------------------------------------------- #


class TestAddRoute:
    def test_creates_routed_connection(self, window):
        ctrl = window.controller
        name = ctrl.add_route(
            BlockEndpoint("gain", "out"), BlockEndpoint("dcblocker", "in"),
            [(1, 1), (1, 2), (1, 3)])
        conn = ctrl.project.connection(name)
        assert conn is not None and conn.is_routed
        assert [(p.x, p.y) for p in conn.route] == [(1, 1), (1, 2), (1, 3)]
        assert ctrl.can_undo()

    def test_hop_overflow_rejected(self, window):
        with pytest.raises(ValueError, match="hops"):
            window.controller.add_route(
                BlockEndpoint("gain", "out"), BlockEndpoint("dcblocker", "in"),
                [(0, i) for i in range(35)])  # 34 hops > 31

    def test_chip_output_plus_one(self, window):
        # 31 waypoints → 30 hops; +1 for chip-output → 31, OK. 32 → 32 > 31.
        with pytest.raises(ValueError):
            window.controller.add_route(
                BlockEndpoint("gain", "out"), ChipPortEndpoint(0, "x16_out"),
                [(0, i) for i in range(33)])


# --------------------------------------------------------------------------- #
# Canvas route state machine
# --------------------------------------------------------------------------- #


class TestRouteStateMachine:
    def test_start_sets_mode(self, window):
        c = window.canvas
        c.start_route("gain", 0, 1, 1)
        assert c.tool is Tool.ROUTE_DRAW
        assert c._route_points == [(1, 1)]

    def test_adjacent_waypoints_accepted(self, window):
        c = window.canvas
        c.start_route("gain", 0, 1, 1)
        assert c.add_waypoint(1, 2)
        assert c.add_waypoint(1, 3)
        assert c.route_hops == 2

    def test_diagonal_rejected(self, window):
        c = window.canvas
        c.start_route("gain", 0, 1, 1)
        assert not c.add_waypoint(2, 2)  # diagonal

    def test_nonadjacent_rejected(self, window):
        c = window.canvas
        c.start_route("gain", 0, 1, 1)
        assert not c.add_waypoint(1, 4)  # jump

    def test_repeat_rejected(self, window):
        c = window.canvas
        c.start_route("gain", 0, 1, 1)
        c.add_waypoint(1, 2)
        assert not c.add_waypoint(1, 1)  # back onto the source


class TestRouteFromPort:
    def test_start_from_input_port(self, window):
        c = window.canvas
        # x16_in is at cell (0,0); the route begins there with a port source.
        assert c.start_route_from_port(0, "x16_in")
        assert c.tool is Tool.ROUTE_DRAW
        assert c._route_source == ("port", 0, "x16_in")
        assert c._route_points == [(0, 0)]

    def test_port_to_block_creates_chip_port_source(self, window):
        ctrl = window.controller
        # A route from the input port to the gain block at (1,1).
        ctrl.place_block("GainBlock", 0, 3, 0, library="lattrex.official",
                         name="gain_p")
        from model.connection import ChipPortEndpoint

        name = ctrl.add_route(
            ChipPortEndpoint(0, "x16_in"), BlockEndpoint("gain_p", "in"),
            [(0, 0), (1, 0), (2, 0), (3, 0)])
        conn = ctrl.project.connection(name)
        assert isinstance(conn.source, ChipPortEndpoint)
        assert conn.source.port == "x16_in"

    def test_w_key_from_selected_port(self, window):
        c = window.canvas
        port = next(p for p in c.port_items() if p.name == "x16_in")
        port.setSelected(True)
        from PySide6.QtCore import Qt
        from PySide6.QtGui import QKeyEvent
        from PySide6.QtCore import QEvent

        ev = QKeyEvent(QEvent.KeyPress, Qt.Key_W, Qt.NoModifier)
        c.keyPressEvent(ev)
        assert c.tool is Tool.ROUTE_DRAW
        assert c._route_source == ("port", 0, "x16_in")

    def test_block_cell_rejected_as_waypoint(self, window):
        c = window.canvas
        c.start_route("gain", 0, 1, 1)
        c.add_waypoint(1, 2)
        c.add_waypoint(1, 3)
        c.add_waypoint(1, 4)
        # (1,5) is the dcblocker cell — not a plain waypoint
        assert not c.add_waypoint(1, 5)

    def test_backspace_removes_last(self, window):
        c = window.canvas
        c.start_route("gain", 0, 1, 1)
        c.add_waypoint(1, 2)
        c.add_waypoint(1, 3)
        c.undo_waypoint()
        assert c._route_points == [(1, 1), (1, 2)]
        # never removes the source
        c.undo_waypoint()
        c.undo_waypoint()
        assert c._route_points == [(1, 1)]

    def test_escape_cancels(self, window):
        c = window.canvas
        c.start_route("gain", 0, 1, 1)
        c.add_waypoint(1, 2)
        c.cancel_route()
        assert c.tool is Tool.SELECT
        assert c._route_points == []
        assert c._preview_item is None

    def test_preview_item_appears_and_clears(self, window):
        c = window.canvas
        c.start_route("gain", 0, 1, 1)
        c.add_waypoint(1, 2)
        assert c._preview_item is not None
        c.cancel_route()
        assert c._preview_item is None


# --------------------------------------------------------------------------- #
# Full draw → command → render → undo
# --------------------------------------------------------------------------- #


class TestRouteCompletion:
    def test_complete_creates_connection_and_renders(self, window):
        c = window.canvas
        ctrl = window.controller
        c.start_route("gain", 0, 1, 1)
        c.add_waypoint(1, 2)
        c.add_waypoint(1, 3)
        c.add_waypoint(1, 4)
        c.complete_route("dcblocker", (1, 5))
        _pump()
        assert c.tool is Tool.SELECT
        conns = ctrl.project.connections
        assert len(conns) == 1
        assert [(p.x, p.y) for p in conns[0].route] == \
            [(1, 1), (1, 2), (1, 3), (1, 4), (1, 5)]
        # rendered as a ConnectionItem
        assert any(isinstance(it, ConnectionItem) for it in c.scene().items())

    def test_undo_removes_route(self, window):
        c = window.canvas
        ctrl = window.controller
        c.start_route("gain", 0, 1, 1)
        c.add_waypoint(1, 2)
        c.complete_route("dcblocker", (1, 3))
        _pump()
        assert len(ctrl.project.connections) == 1
        ctrl.undo()
        _pump()
        assert len(ctrl.project.connections) == 0

    def test_status_bar_shows_hops(self, window):
        c = window.canvas
        c.start_route("gain", 0, 1, 1)
        c.add_waypoint(1, 2)
        _pump()
        assert "hops" in window.statusBar().currentMessage()

    @pytest.mark.filterwarnings("ignore::DeprecationWarning")
    def test_mouse_click_adds_waypoint(self, window):
        # Exercise the real mousePressEvent route branch via a synthetic click.
        # (The 6-arg QMouseEvent ctor is deprecated in Qt 6.11 but the
        # non-deprecated form crashes PySide6 when hand-constructed.)
        from PySide6.QtCore import QEvent, QPointF, Qt
        from PySide6.QtGui import QMouseEvent

        c = window.canvas
        c.start_route("gain", 0, 1, 1)
        sx, sy = 1 * 64 + 32, 2 * 64 + 32  # centre of cell (1,2)
        vp = c.mapFromScene(QPointF(sx, sy))  # QPoint
        gp = c.viewport().mapToGlobal(vp)
        ev = QMouseEvent(QEvent.MouseButtonPress, QPointF(vp), QPointF(gp),
                         Qt.LeftButton, Qt.LeftButton, Qt.NoModifier)
        c.mousePressEvent(ev)
        assert (1, 2) in c._route_points

    @pytest.mark.filterwarnings("ignore::DeprecationWarning")
    def test_mouse_click_completes_on_target_block(self, window):
        from PySide6.QtCore import QEvent, QPointF, Qt
        from PySide6.QtGui import QMouseEvent

        c = window.canvas
        ctrl = window.controller
        c.start_route("gain", 0, 1, 1)
        for y in (2, 3, 4):
            c.add_waypoint(1, y)
        # click the dcblocker cell at (1,5) → completes
        sx, sy = 1 * 64 + 32, 5 * 64 + 32
        vp = c.mapFromScene(QPointF(sx, sy))  # QPoint
        gp = c.viewport().mapToGlobal(vp)
        ev = QMouseEvent(QEvent.MouseButtonPress, QPointF(vp), QPointF(gp),
                         Qt.LeftButton, Qt.LeftButton, Qt.NoModifier)
        c.mousePressEvent(ev)
        _pump()
        assert c.tool is Tool.SELECT
        assert len(ctrl.project.connections) == 1


class TestConnectionItemRendering:
    """Drive ConnectionItem.paint() / boundingRect by rendering to a QImage."""

    def _render(self, item):
        from PySide6.QtCore import QRectF
        from PySide6.QtGui import QColor, QImage, QPainter
        from PySide6.QtWidgets import QGraphicsScene

        scene = QGraphicsScene()
        scene.addItem(item)
        rect = item.boundingRect()
        img = QImage(max(8, int(rect.width())), max(8, int(rect.height())),
                     QImage.Format_ARGB32)
        img.fill(QColor("black"))
        p = QPainter(img)
        scene.render(p, QRectF(img.rect()), rect)
        p.end()

    def test_routed_line_paints(self, qapp):
        self._render(ConnectionItem([(0, 0), (0, 1), (1, 1)], (0.0, 0.0)))

    def test_preview_line_paints(self, qapp):
        self._render(ConnectionItem([(2, 2), (3, 2)], (0.0, 0.0), preview=True))

    def test_single_point_no_crash(self, qapp):
        # < 2 points → paint returns early; boundingRect still valid.
        item = ConnectionItem([(0, 0)], (0.0, 0.0))
        assert item.boundingRect() is not None
        self._render(item)

    def test_empty_bounding_rect(self, qapp):
        item = ConnectionItem([], (0.0, 0.0))
        assert item.boundingRect().isEmpty()

    def test_named_route_is_selectable(self, qapp):
        from PySide6.QtWidgets import QGraphicsItem

        item = ConnectionItem([(0, 0), (0, 1)], (0.0, 0.0), name="r1")
        assert item.connection_name == "r1"
        assert item.flags() & QGraphicsItem.ItemIsSelectable
        # fat hit area so the thin line is clickable
        assert item.shape().boundingRect().width() > 3

    def test_preview_not_selectable(self, qapp):
        from PySide6.QtWidgets import QGraphicsItem

        item = ConnectionItem([(0, 0), (0, 1)], (0.0, 0.0), preview=True)
        assert not (item.flags() & QGraphicsItem.ItemIsSelectable)


class TestConnectionDelete:
    """Deleting an existing route (the gap the user found)."""

    def _window(self, catalog):
        ctrl = AppController(catalog=catalog)
        w = MainWindow(controller=ctrl)
        ctrl.open_project(Path(__file__).parent / "data" / "demo" / "gain_demo.kyt")
        w._after_project_loaded()
        return w

    def test_controller_remove_connection(self, qapp, catalog):
        w = self._window(catalog)
        assert w.controller.project.connection("gain_to_dac") is not None
        w.controller.remove_connection("gain_to_dac")
        assert w.controller.project.connection("gain_to_dac") is None
        w.controller.undo()
        assert w.controller.project.connection("gain_to_dac") is not None

    def test_delete_via_handler_updates_canvas(self, qapp, catalog):
        w = self._window(catalog)
        _pump()
        # Count only ROUTED connection lines — the demo's unrouted adc_to_gain
        # now also renders a dashed fly line (auto-P&R P2.3), which is not the
        # routed line under test.
        before = [it for it in w.canvas.scene().items()
                  if isinstance(it, ConnectionItem) and not it.is_fly]
        assert len(before) == 1
        w._on_delete_connection("gain_to_dac")
        _pump()
        after = [it for it in w.canvas.scene().items()
                 if isinstance(it, ConnectionItem) and not it.is_fly]
        assert after == []  # route line removed from canvas

    def test_delete_key_removes_selected_route(self, qapp, catalog):
        from PySide6.QtCore import QEvent, Qt
        from PySide6.QtGui import QKeyEvent

        w = self._window(catalog)
        _pump()
        route = [it for it in w.canvas.scene().items()
                 if isinstance(it, ConnectionItem) and not it.is_fly][0]
        route.setSelected(True)
        requested = []
        w.canvas.delete_connection_requested.connect(requested.append)
        w.canvas.keyPressEvent(
            QKeyEvent(QEvent.KeyPress, Qt.Key_Delete, Qt.NoModifier))
        assert requested == ["gain_to_dac"]


class TestFlyLines:
    """Auto-P&R P2.3: an UNROUTED logical net renders as a dashed fly line
    between its endpoint anchors (the captured wiring the Phase-3 router will
    materialise). Routed connections still render a solid route line."""

    def _window_two_blocks(self, catalog):
        ctrl = AppController(catalog=catalog)
        w = MainWindow(controller=ctrl)
        ctrl.new_project("Fly Test", "kyttar_10x12")
        w._after_project_loaded()
        # Two placed blocks: an input landing block and a downstream block.
        ctrl.place_block("GainBlock", 0, 1, 1, library="lattrex.official")
        ctrl.place_block("DCBlockerBlock", 0, 5, 1, library="lattrex.official")
        return w, ctrl

    def _fly_items(self, w):
        return [it for it in w.canvas.scene().items()
                if isinstance(it, ConnectionItem) and it.is_fly]

    def test_unrouted_block_connection_draws_fly_line(self, qapp, catalog):
        w, ctrl = self._window_two_blocks(catalog)
        names = [b.name for b in ctrl.project.blocks]
        ctrl.add_logical_connection(
            BlockEndpoint(block=names[0], port="out"),
            BlockEndpoint(block=names[1], port="sample"),
            name="g_to_dc",
        )
        w.canvas.render_scene()
        _pump()
        flies = self._fly_items(w)
        assert len(flies) == 1
        assert flies[0].connection_name == "g_to_dc"

    def test_unrouted_chip_input_to_block_draws_no_fly_line(self, qapp, catalog):
        """A chip INPUT-port → block net injects directly at the port edge cell —
        it has no physical route by design, so it must NOT draw a fly line (which
        would falsely read as 'not connected'; the user-reported top-left
        artifact). Block→block unrouted nets still fly-line (test above)."""
        w, ctrl = self._window_two_blocks(catalog)
        names = [b.name for b in ctrl.project.blocks]
        ctrl.add_logical_connection(
            ChipPortEndpoint(chip=0, port="x16_in"),
            BlockEndpoint(block=names[0], port="sample"),
            name="in_to_g",
        )
        w.canvas.render_scene()
        _pump()
        assert len(self._fly_items(w)) == 0

    def test_fly_line_is_selectable(self, qapp, catalog):
        w, ctrl = self._window_two_blocks(catalog)
        names = [b.name for b in ctrl.project.blocks]
        ctrl.add_logical_connection(
            BlockEndpoint(block=names[0], port="out"),
            BlockEndpoint(block=names[1], port="sample"),
            name="g_to_dc",
        )
        w.canvas.render_scene()
        _pump()
        fly = self._fly_items(w)[0]
        from PySide6.QtWidgets import QGraphicsItem
        assert fly.flags() & QGraphicsItem.ItemIsSelectable


class TestBlockPortStubs:
    """Auto-P&R P2.3 part 2: labelled block-port stubs (PortMap named-port
    markers) toggle on/off and sit at each placed block's external ports."""

    def _window_one_block(self, catalog):
        ctrl = AppController(catalog=catalog)
        w = MainWindow(controller=ctrl)
        ctrl.new_project("Stub Test", "kyttar_10x12")
        w._after_project_loaded()
        ctrl.place_block("GainBlock", 0, 2, 2, library="lattrex.official")
        w.canvas.render_scene()
        return w, ctrl

    def _stub_items(self, w):
        from ui.canvas.block_port_stub_item import BlockPortStubItem
        return [it for it in w.canvas.scene().items()
                if isinstance(it, BlockPortStubItem)]

    def test_stubs_hidden_by_default(self, qapp, catalog):
        w, _ = self._window_one_block(catalog)
        _pump()
        assert self._stub_items(w) == []

    def test_toggle_renders_named_stubs(self, qapp, catalog):
        w, _ = self._window_one_block(catalog)
        w.canvas.set_show_port_stubs(True)
        _pump()
        stubs = self._stub_items(w)
        # GainBlock is single-cell: in (sample) + out (out) at the same cell.
        names = {s.port_name for s in stubs}
        assert "sample" in names and "out" in names
        dirs = {s.direction for s in stubs}
        assert "in" in dirs and "out" in dirs

    def test_toggle_off_clears_stubs(self, qapp, catalog):
        w, _ = self._window_one_block(catalog)
        w.canvas.set_show_port_stubs(True)
        _pump()
        assert self._stub_items(w)
        w.canvas.set_show_port_stubs(False)
        _pump()
        assert self._stub_items(w) == []

    def test_multicell_stub_at_distinct_cells(self, qapp, catalog):
        # A multi-cell block: input stub at the landing cell, output at the
        # output cell — different cells, so the stub anchors differ.
        ctrl = AppController(catalog=catalog)
        w = MainWindow(controller=ctrl)
        ctrl.new_project("Stub Multi", "kyttar_10x12")
        w._after_project_loaded()
        ctrl.place_block("CoherentRXBlock", 0, 0, 0, library="lattrex.official")
        w.canvas.set_show_port_stubs(True)
        w.canvas.render_scene()
        _pump()
        stubs = self._stub_items(w)
        names = {s.port_name for s in stubs}
        assert {"xi", "xq", "bit"} <= names
        # the input stubs and the output stub are at different anchor points
        ins = [s for s in stubs if s.direction == "in"]
        outs = [s for s in stubs if s.direction == "out"]
        assert ins and outs
        in_pts = {(round(s._anchor.x()), round(s._anchor.y())) for s in ins}
        out_pts = {(round(s._anchor.x()), round(s._anchor.y())) for s in outs}
        assert in_pts.isdisjoint(out_pts)


class TestClickToWire:
    """Auto-P&R P2.3 part 2b: click one block-port stub then another creates a
    logical net (output→input normalised), drawn as a fly line."""

    def _window_two_blocks(self, catalog):
        ctrl = AppController(catalog=catalog)
        w = MainWindow(controller=ctrl)
        ctrl.new_project("Wire Test", "kyttar_10x12")
        w._after_project_loaded()
        ctrl.place_block("GainBlock", 0, 1, 1, library="lattrex.official")
        ctrl.place_block("DCBlockerBlock", 0, 5, 1, library="lattrex.official")
        w.canvas.set_show_port_stubs(True)
        w.canvas.render_scene()
        return w, ctrl

    def _stub(self, w, block, port):
        from ui.canvas.block_port_stub_item import BlockPortStubItem
        for it in w.canvas.scene().items():
            if isinstance(it, BlockPortStubItem) \
                    and it.block_name == block and it.port_name == port:
                return it
        return None

    def test_click_out_then_in_creates_net(self, qapp, catalog):
        w, ctrl = self._window_two_blocks(catalog)
        names = [b.name for b in ctrl.project.blocks]
        out = self._stub(w, names[0], "out")
        inp = self._stub(w, names[1], "sample")
        assert out is not None and inp is not None
        n0 = len(ctrl.project.connections)
        w.canvas._handle_stub_click(out)
        w.canvas._handle_stub_click(inp)
        assert len(ctrl.project.connections) == n0 + 1
        conn = ctrl.project.connections[-1]
        from model.connection import BlockEndpoint
        assert isinstance(conn.source, BlockEndpoint)
        assert conn.source.block == names[0] and conn.source.port == "out"
        assert conn.target.block == names[1] and conn.target.port == "sample"
        assert not conn.is_routed   # logical net => fly line

    def test_click_in_then_out_normalises_direction(self, qapp, catalog):
        w, ctrl = self._window_two_blocks(catalog)
        names = [b.name for b in ctrl.project.blocks]
        # click the consumer input FIRST, then the producer output
        w.canvas._handle_stub_click(self._stub(w, names[1], "sample"))
        w.canvas._handle_stub_click(self._stub(w, names[0], "out"))
        conn = ctrl.project.connections[-1]
        # still normalised producer(out)->consumer(in)
        assert conn.source.block == names[0] and conn.source.port == "out"
        assert conn.target.block == names[1] and conn.target.port == "sample"

    def test_two_outputs_do_not_wire(self, qapp, catalog):
        w, ctrl = self._window_two_blocks(catalog)
        names = [b.name for b in ctrl.project.blocks]
        n0 = len(ctrl.project.connections)
        w.canvas._handle_stub_click(self._stub(w, names[0], "out"))
        w.canvas._handle_stub_click(self._stub(w, names[1], "out"))
        assert len(ctrl.project.connections) == n0   # invalid (out↔out): no net

    def test_same_block_cancels(self, qapp, catalog):
        w, ctrl = self._window_two_blocks(catalog)
        names = [b.name for b in ctrl.project.blocks]
        n0 = len(ctrl.project.connections)
        w.canvas._handle_stub_click(self._stub(w, names[0], "out"))
        w.canvas._handle_stub_click(self._stub(w, names[0], "sample"))
        assert len(ctrl.project.connections) == n0   # same block: no self-wire


class TestRouteAll:
    """Auto-P&R P3.3: the GUI "Route All" action materialises logical nets into
    real routes via the controller's undoable auto-router."""

    def _window_chain(self, catalog):
        ctrl = AppController(catalog=catalog)
        w = MainWindow(controller=ctrl)
        ctrl.new_project("Route All", "kyttar_10x12")
        w._after_project_loaded()
        a = ctrl.place_block("GainBlock", 0, 1, 1, library="lattrex.official")
        b = ctrl.place_block("DCBlockerBlock", 0, 5, 1, library="lattrex.official")
        ctrl.add_logical_connection(
            BlockEndpoint(block=a, port="out"),
            BlockEndpoint(block=b, port="sample"), name="ab")
        w.canvas.render_scene()
        return w, ctrl

    def test_route_all_materialises_nets(self, qapp, catalog):
        w, ctrl = self._window_chain(catalog)
        assert not ctrl.project.connection("ab").is_routed
        w._route_all_nets()
        _pump()
        assert ctrl.project.connection("ab").is_routed
        # the canvas now shows a routed (solid) line, not a fly line
        routed = [it for it in w.canvas.scene().items()
                  if isinstance(it, ConnectionItem) and not it.is_fly]
        assert routed

    def test_route_all_is_undoable(self, qapp, catalog):
        w, ctrl = self._window_chain(catalog)
        w._route_all_nets()
        _pump()
        assert ctrl.project.connection("ab").is_routed
        ctrl.undo()
        _pump()
        assert not ctrl.project.connection("ab").is_routed


class TestAutoPlace:
    """Auto-P&R §8: the GUI "Auto-Place Blocks" action flow-orders blocks."""

    def _window(self, catalog):
        ctrl = AppController(catalog=catalog)
        w = MainWindow(controller=ctrl)
        ctrl.new_project("Auto Place", "kyttar_10x12")
        w._after_project_loaded()
        c = ctrl.place_block("BPSKSlicerBlock", 0, 1, 3, library="lattrex.official")
        a = ctrl.place_block("GainBlock", 0, 6, 3, library="lattrex.official")
        b = ctrl.place_block("DCBlockerBlock", 0, 3, 3, library="lattrex.official")
        ctrl.add_logical_connection(
            BlockEndpoint(block=a, port="out"),
            BlockEndpoint(block=b, port="sample"), name="ab")
        ctrl.add_logical_connection(
            BlockEndpoint(block=b, port="out"),
            BlockEndpoint(block=c, port="llr"), name="bc")
        w.canvas.render_scene()
        return w, ctrl, (a, b, c)

    def test_auto_place_orders_blocks(self, qapp, catalog):
        w, ctrl, (a, b, c) = self._window(catalog)
        w._auto_place_blocks()
        _pump()
        xs = [ctrl.project.block(n).placement.cells[0].x for n in (a, b, c)]
        assert xs[0] < xs[1] < xs[2]                 # A→B→C left to right

    def test_auto_place_undoable(self, qapp, catalog):
        w, ctrl, (a, b, c) = self._window(catalog)
        before = [ctrl.project.block(n).placement.cells[0].x for n in (a, b, c)]
        w._auto_place_blocks()
        _pump()
        ctrl.undo()
        _pump()
        after = [ctrl.project.block(n).placement.cells[0].x for n in (a, b, c)]
        assert after == before
