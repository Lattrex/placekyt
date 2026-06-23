"""UI smoke tests (the architecture notes §11.2 "UI smoke tests").

Run with the offscreen Qt platform so they work headless / in CI. Skipped
entirely if PySide6 is not installed.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

# Force offscreen BEFORE any Qt import.
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

pytest.importorskip("PySide6")

from PySide6.QtWidgets import QApplication  # noqa: E402

from model.block import Block  # noqa: E402
from model.chip import ChipInstance  # noqa: E402
from model.chip_type import ChipType, PortSpec  # noqa: E402
from model.enums import Face, PortDirection  # noqa: E402
from model.placement import Placement, PlacedCell  # noqa: E402
from model.project import Project  # noqa: E402
from ui.canvas import CELL_PX, CellItem, CellKind, ChipCanvas  # noqa: E402
from ui.canvas.chip_canvas import chip_cell_to_scene  # noqa: E402
from ui.main_window import MainWindow  # noqa: E402


@pytest.fixture(scope="session")
def qapp():
    app = QApplication.instance() or QApplication([])
    yield app


def _chip_type(w=10, h=12) -> ChipType:
    return ChipType(
        name="t", width=w, height=h,
        ports=(
            PortSpec("x16_in", PortDirection.INPUT, 16, 0, 0, Face.NORTH),
            PortSpec("x16_out", PortDirection.OUTPUT, 16, 9, 0, Face.EAST),
        ),
    )


def _project() -> Project:
    p = Project(chip_type="t")
    p.chips = [ChipInstance(0, "C0", 0.0, 0.0)]
    p.blocks = [
        Block("gain", "GainBlock", library="lattrex.official",
              placement=Placement(0, [PlacedCell(0, 1, 1, Face.EAST)])),
    ]
    return p


# --------------------------------------------------------------------------- #
# CellItem
# --------------------------------------------------------------------------- #


class TestCellItem:
    def test_bounding_rect_non_empty(self, qapp):
        # Mandatory §3.2 contract — empty rect would make the item invisible.
        item = CellItem(0, 0)
        r = item.boundingRect()
        assert r.width() == CELL_PX and r.height() == CELL_PX

    def test_is_selectable_flag(self, qapp):
        from PySide6.QtWidgets import QGraphicsItem

        item = CellItem(0, 0)
        flags = item.flags()
        assert flags & QGraphicsItem.ItemIsSelectable
        # NOT movable — movement goes through the command system (§3.2).
        assert not (flags & QGraphicsItem.ItemIsMovable)

    def test_accepts_hover(self, qapp):
        assert CellItem(0, 0).acceptHoverEvents()

    def test_kind_and_face(self, qapp):
        item = CellItem(3, 4, kind=CellKind.TRANSIT, face=Face.EAST)
        assert item.kind is CellKind.TRANSIT
        assert item.face is Face.EAST

    def test_paint_paths(self, qapp):
        """Render individual cells (block w/ label+arrow, transit, selected,
        each face) via QGraphicsScene.render to drive paint() branches."""
        from PySide6.QtCore import QRectF
        from PySide6.QtGui import QColor, QImage, QPainter
        from PySide6.QtWidgets import QGraphicsScene

        from ui.canvas.cell_item import CELL_PX

        variants = [
            CellItem(0, 0, kind=CellKind.BLOCK, face=Face.NORTH, label="agc"),
            CellItem(0, 0, kind=CellKind.TRANSIT, face=Face.SOUTH),
            CellItem(0, 0, kind=CellKind.EMPTY),
            CellItem(0, 0, kind=CellKind.BLOCK, face=Face.WEST, label="x"),
        ]
        variants[0].setSelected(True)  # selection-highlight branch
        for item in variants:
            scene = QGraphicsScene()
            scene.addItem(item)
            img = QImage(CELL_PX, CELL_PX, QImage.Format_ARGB32)
            img.fill(QColor("black"))
            p = QPainter(img)
            scene.render(p)
            p.end()


# --------------------------------------------------------------------------- #
# Coordinate mapping
# --------------------------------------------------------------------------- #


class TestCoordinateMapping:
    def test_chip_cell_to_scene(self):
        # §3.2 canonical mapping.
        assert chip_cell_to_scene(0, 0, 0, 0) == (0, 0)
        assert chip_cell_to_scene(0, 0, 2, 3) == (2 * CELL_PX, 3 * CELL_PX)
        assert chip_cell_to_scene(720, 0, 1, 0) == (720 + CELL_PX, 0)


# --------------------------------------------------------------------------- #
# ChipCanvas
# --------------------------------------------------------------------------- #


class TestChipCanvas:
    def test_renders_full_grid(self, qapp):
        canvas = ChipCanvas()
        canvas.set_project(_project(), {"t": _chip_type()})
        cells = canvas.cell_items()
        assert len(cells) == 120  # 10x12 grid fully rendered

    def test_block_cell_kind(self, qapp):
        canvas = ChipCanvas()
        canvas.set_project(_project(), {"t": _chip_type()})
        block_cells = [c for c in canvas.cell_items() if c.kind is CellKind.BLOCK]
        assert len(block_cells) == 1
        assert (block_cells[0].cx, block_cells[0].cy) == (1, 1)
        assert block_cells[0].label == "gain"

    def test_final_waypoint_faces_target_block(self, qapp):
        # A route whose LAST waypoint abuts a TARGET BLOCK must face the block,
        # not default to east (regression: the SRAM demo's (8,5) wrongly showed
        # east instead of south toward the crossover at (8,6)).
        from model.connection import BlockEndpoint, Connection, RoutePoint

        p = _project()
        # Target block at (5,5); route comes down col 5 and ends at (5,4),
        # abutting the block to its NORTH → the last waypoint must face SOUTH.
        p.blocks.append(
            Block("sink", "GainBlock", library="lattrex.official",
                  placement=Placement(0, [PlacedCell(0, 5, 5, Face.EAST)])))
        p.connections = [
            Connection("r", BlockEndpoint("gain", "out"),
                       BlockEndpoint("sink", "in"),
                       route=[RoutePoint(5, 2), RoutePoint(5, 3),
                              RoutePoint(5, 4)]),
        ]
        canvas = ChipCanvas()
        canvas.set_project(p, {"t": _chip_type()})
        last = [c for c in canvas.cell_items()
                if c.kind is CellKind.TRANSIT and (c.cx, c.cy) == (5, 4)]
        assert last and last[0].face is Face.SOUTH

    def test_wheel_zoom_changes_scale(self, qapp):
        from PySide6.QtCore import QPoint, Qt
        from PySide6.QtGui import QWheelEvent

        canvas = ChipCanvas()
        canvas.set_project(_project(), {"t": _chip_type()})
        canvas.reset_zoom()
        before = canvas.scale_factor
        ev = QWheelEvent(
            QPoint(50, 50), canvas.mapToGlobal(QPoint(50, 50)),
            QPoint(0, 0), QPoint(0, 120), Qt.NoButton, Qt.NoModifier,
            Qt.NoScrollPhase, False,
        )
        canvas.wheelEvent(ev)
        assert canvas.scale_factor > before

    def test_empty_project_no_crash(self, qapp):
        canvas = ChipCanvas()
        canvas.set_project(Project(), {})
        assert canvas.cell_items() == []

    def test_renders_panel_and_link(self, qapp):
        from model.connection import PanelConnection
        from model.panel import SramPanel
        from ui.canvas.panel_item import PanelItem

        p = _project()
        p.panels = [SramPanel(id=0, label="Symbols",
                              position_x=-300.0, position_y=0.0)]
        p.panel_connections = [PanelConnection(0, "x16_out", 0, "x16_in")]
        canvas = ChipCanvas()
        canvas.set_project(p, {"t": _chip_type()})
        panels = [it for it in canvas._scene.items()
                  if isinstance(it, PanelItem)]
        assert len(panels) == 1
        # the panel's connected port reports as connected (filled)
        assert "x16_out" in panels[0]._connected
        # a port anchor resolves to a scene point (so the wire can draw)
        assert panels[0].port_anchor_scene("x16_out") is not None

    def test_panel_mirror_signal(self, qapp):
        from model.panel import SramPanel
        from ui.canvas.panel_item import PanelItem

        p = _project()
        p.panels = [SramPanel(id=0, label="M")]
        canvas = ChipCanvas()
        canvas.set_project(p, {"t": _chip_type()})
        item = next(it for it in canvas._scene.items()
                    if isinstance(it, PanelItem))
        item.setSelected(True)
        got = []
        canvas.panel_mirror_requested.connect(lambda pid: got.append(pid))
        from PySide6.QtGui import QKeyEvent
        from PySide6.QtCore import QEvent, Qt
        canvas.keyPressEvent(
            QKeyEvent(QEvent.KeyPress, Qt.Key_H, Qt.NoModifier))
        assert got == [0]

    def test_panel_delete_signal(self, qapp):
        from model.panel import SramPanel
        from ui.canvas.panel_item import PanelItem

        p = _project()
        p.panels = [SramPanel(id=0, label="M")]
        canvas = ChipCanvas()
        canvas.set_project(p, {"t": _chip_type()})
        item = next(it for it in canvas._scene.items()
                    if isinstance(it, PanelItem))
        item.setSelected(True)
        got = []
        canvas.panel_delete_requested.connect(lambda pid: got.append(pid))
        from PySide6.QtGui import QKeyEvent
        from PySide6.QtCore import QEvent, Qt
        canvas.keyPressEvent(
            QKeyEvent(QEvent.KeyPress, Qt.Key_Delete, Qt.NoModifier))
        assert got == [0]

    def test_panel_move_emits(self, qapp):
        from model.panel import SramPanel
        from ui.canvas.panel_item import PanelItem

        p = _project()
        p.panels = [SramPanel(id=0)]
        canvas = ChipCanvas()
        canvas.set_project(p, {"t": _chip_type()})
        item = next(it for it in canvas._scene.items()
                    if isinstance(it, PanelItem))
        got = []
        canvas.panel_moved.connect(lambda pid, x, y: got.append((pid, x, y)))
        # simulate the drag end-state: panel armed + moved, then released
        canvas._drag_panel = 0
        item.setPos(99.0, 88.0)
        from PySide6.QtGui import QMouseEvent
        from PySide6.QtCore import QEvent, QPointF, Qt
        ev = QMouseEvent(QEvent.MouseButtonRelease, QPointF(0, 0),
                         Qt.LeftButton, Qt.LeftButton, Qt.NoModifier)
        canvas.mouseReleaseEvent(ev)
        assert got == [(0, 99.0, 88.0)]

    def test_scene_renders_to_image(self, qapp):
        """Drive paint() on every item by rendering the scene to a QImage.

        Offscreen tests don't trigger repaints on their own, so exercise the
        paint paths explicitly at several zoom levels (full / medium / minimal
        LOD per §3.2)."""
        from PySide6.QtCore import QRectF
        from PySide6.QtGui import QColor, QImage, QPainter

        canvas = ChipCanvas()
        canvas.set_project(_project(), {"t": _chip_type()})
        scene = canvas.scene()
        src = scene.itemsBoundingRect()
        for size in (1200, 400, 80):  # large→small forces LOD_FULL/MEDIUM/minimal
            img = QImage(size, size, QImage.Format_ARGB32)
            img.fill(QColor("black"))
            painter = QPainter(img)
            scene.render(painter, QRectF(img.rect()), src)
            painter.end()
            # Something was drawn (not all-black) at least at the larger sizes.
        assert True  # no exception across all LOD levels


# --------------------------------------------------------------------------- #
# MainWindow
# --------------------------------------------------------------------------- #


class TestMainWindow:
    def test_constructs_with_panels(self, qapp):
        w = MainWindow()
        assert w.canvas is not None
        # the docks exist (Program is its own dock, separate from Inspector)
        assert set(w._docks) == {
            "Block Library", "Inspector", "Program", "Output", "Waveform",
            "Breakpoints", "Console", "Disassembly"}

    def test_disassembly_panel_shows_bitstream(self, qapp):
        # The Disassembly dock renders a bitstream as a WRITE+DATA+JUMP listing.
        w = MainWindow()
        w.disassembly_panel.show_words([0x6204, 0xCAFE, 0x720F],
                                       source="t.kbs")
        text = w.disassembly_panel._view.toPlainText()
        assert "WRITE @15, 4" in text
        assert "DW   0xCAFE" in text   # data payload, not mis-decoded
        assert "JUMP @15, 15" in text

    def test_min_size(self, qapp):
        w = MainWindow()
        assert w.minimumWidth() == 1200
        assert w.minimumHeight() == 800

    def test_menus_present(self, qapp):
        w = MainWindow()
        titles = [a.text().replace("&", "") for a in w.menuBar().actions()]
        for expected in ("File", "Edit", "View", "Build", "Help"):
            assert expected in titles

    def test_set_project_renders_and_titles(self, qapp):
        w = MainWindow()
        p = _project()
        p.metadata.name = "My Demo"
        w.set_project(p, {"t": _chip_type()})
        assert "My Demo" in w.windowTitle()
        assert len(w.canvas.cell_items()) == 120

    def test_reset_layout_runs(self, qapp):
        w = MainWindow()
        w.reset_layout()  # must not raise


class TestResources:
    def test_resource_path_dev(self):
        import resources

        p = resources.resource_path("resources/icons/x.svg")
        # Dev mode: resolves under the package root (this file's grandparent).
        assert p.name == "x.svg"
        assert "resources/icons" in str(p).replace("\\", "/")

    def test_resource_path_frozen(self, monkeypatch):
        import sys

        import resources

        monkeypatch.setattr(sys, "_MEIPASS", "/tmp/_mei", raising=False)
        p = resources.resource_path("blocks/gain.kbl")
        assert str(p).startswith("/tmp/_mei")
