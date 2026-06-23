"""Block appearance + face-sync + icon tests (GUI-review follow-ups).

Covers: per-block colour rotation, manual colour override (+ round-trip),
build-resolved face arrow sync, and the app window icon.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
pytest.importorskip("PySide6")

from PySide6.QtWidgets import QApplication  # noqa: E402

from engine.catalog import BlockCatalog  # noqa: E402
from ui.canvas.cell_item import (  # noqa: E402
    CellItem,
    CellKind,
    block_palette_color,
)
from ui.controller import AppController  # noqa: E402
from ui.main_window import MainWindow  # noqa: E402

from tests.conftest import CHIP_YAML as CT_PATH  # noqa: E402
DEMO = Path(__file__).parent / "data" / "demo" / "gain_demo.kyt"
pytestmark = pytest.mark.skipif(
    not (CT_PATH.exists() and DEMO.exists()), reason="chip yaml / demo absent")


@pytest.fixture(scope="module")
def qapp():
    return QApplication.instance() or QApplication([])


@pytest.fixture(scope="module")
def catalog():
    return BlockCatalog.from_gr_kyttar()


@pytest.fixture
def controller(qapp, catalog):
    return AppController(catalog=catalog)


def _window(controller):
    w = MainWindow(controller=controller)
    controller.open_project(DEMO)
    w._after_project_loaded()
    return w


def _block_fills(w):
    fills = {}
    for it in w.canvas.cell_items():
        if isinstance(it, CellItem) and it.kind == CellKind.BLOCK and it.label:
            fills.setdefault(it.label, it._fill_color().name())
    return fills


class TestBlockColor:
    def test_palette_is_stable_and_cyclic(self):
        assert block_palette_color(0).name() == block_palette_color(0).name()
        assert block_palette_color(0).name() == block_palette_color(10).name()

    def test_distinct_blocks_get_distinct_colors(self, controller):
        w = _window(controller)
        controller.place_block("DCBlockerBlock", 0, 5, 5,
                               library="lattrex.official")
        w._on_model_changed()
        fills = _block_fills(w)
        assert len(fills) == 2
        assert len(set(fills.values())) == 2  # gain ≠ dcblocker

    def test_block_color_avoids_transit_blue(self):
        from ui.canvas.cell_item import _TRANSIT_FILL
        for i in range(10):
            assert block_palette_color(i).name() != _TRANSIT_FILL.name()

    def test_manual_color_override_and_reset(self, controller):
        w = _window(controller)
        name = controller.project.blocks[0].name
        w._on_block_color_requested(name, "#ff8800")
        w._on_model_changed()
        assert controller.project.block(name).color == "#ff8800"
        assert _block_fills(w)[name].lower() == "#ff8800"
        w._on_block_color_requested(name, None)
        assert controller.project.block(name).color is None

    def test_color_round_trips_through_save(self, controller, tmp_path):
        from engine.io.project_io import load_project, save_project
        controller.open_project(DEMO)
        controller.project.blocks[0].color = "#123456"
        out = tmp_path / "p.kyt"
        save_project(controller.project, out)
        reloaded = load_project(out)
        assert reloaded.blocks[0].color == "#123456"


class TestSimStateColorShift:
    def test_block_color_preserved_under_sim_state(self, qapp):
        # The sim state SHIFTS the block's own colour (brighter once executed)
        # rather than replacing it with a generic green — hue is preserved.
        from PySide6.QtGui import QColor
        base = QColor("#b48c5a")  # tan
        it = CellItem(0, 0, kind=CellKind.BLOCK, fill=base, label="x")
        base_hue = base.getHsv()[0]
        for state in ("idle", "active", "executing", "halted"):
            it.set_sim_state(state)
            assert it._fill_color().getHsv()[0] == base_hue  # same hue

    def test_executed_is_brighter_than_idle(self, qapp):
        from PySide6.QtGui import QColor
        it = CellItem(0, 0, kind=CellKind.BLOCK, fill=QColor("#b48c5a"))
        it.set_sim_state("idle")
        idle_v = it._fill_color().getHsv()[2]
        it.set_sim_state("executing")
        exec_v = it._fill_color().getHsv()[2]
        assert exec_v > idle_v  # executing is brighter than idle

    def test_empty_cell_uses_generic_state_fill(self, qapp):
        from ui.canvas.cell_item import _SIM_ACTIVE
        it = CellItem(0, 0, kind=CellKind.EMPTY)
        it.set_sim_state("active")
        # No own colour → falls back to the generic state fill (visible on blanks).
        assert it._fill_color().name() == _SIM_ACTIVE.name()


class TestDeletedRouteAppearance:
    def test_empty_cell_program_reads_empty(self, controller):
        # After a route is gone, its cells are EMPTY in the model and must read
        # as unprogrammed in the Inspector — NOT as a build-auto-forwarding
        # "routing cell".
        w = _window(controller)
        controller.build()
        for c in list(controller.project.connections):
            controller.remove_connection(c.name)
        w._on_model_changed()
        sel = {"cell": (3, 0), "chip": 0, "kind": "empty",
               "block": None, "face": None, "route": None}
        w.inspector.show_selection(sel)
        assert not w.program_view.isVisible()


class TestFaceArrowSync:
    def test_arrow_matches_build_resolved_face(self, controller):
        # BLOCK/TRANSIT cells sync to the build-resolved face; EMPTY cells are
        # SKIPPED (the build auto-fills a forwarding face on every downstream
        # cell — honouring those would put arrows on blank cells).
        from model.enums import Face
        w = _window(controller)
        result = controller.build()
        w._sync_resolved_faces()
        cells0 = result.chips[0].cells if 0 in result.chips else {}
        for it in w.canvas.cell_items():
            if not isinstance(it, CellItem) or it.kind is CellKind.EMPTY:
                continue
            info = cells0.get((it.cx, it.cy))
            if info and info.get("face"):
                assert it.face == Face.from_str(info["face"])

    def test_empty_cells_get_no_build_arrow(self, controller):
        # A blank cell must not show an arrow even though the build assigns it a
        # forwarding face (the deleted-route / blank-cell symptom).
        w = _window(controller)
        controller.build()
        w._sync_resolved_faces()
        empties = [it for it in w.canvas.cell_items()
                   if isinstance(it, CellItem) and it.kind is CellKind.EMPTY]
        assert empties  # there are blank cells
        assert all(it.face is None for it in empties)

    def test_sync_no_build_is_noop(self, controller):
        # No cached build → apply_resolved_faces leaves arrows untouched.
        w = _window(controller)
        w.canvas.apply_resolved_faces(None)  # must not raise


class TestAppIcon:
    def test_window_icon_set(self, controller):
        w = _window(controller)
        assert not w.windowIcon().isNull()

    def test_icon_asset_exists(self):
        icon = (Path(__file__).resolve().parent.parent / "resources" / "icons"
                / "lattrex_logo.png")
        assert icon.exists()


class TestDFELayout:
    """The DFE block: 45 cells with explicit faces + a PROGRAMMED lock-driver
    relay (lock_drv) feeding the decision cell (not a dumb transit relay)."""

    def test_dfe_layout_has_lock_driver(self, catalog):
        lay = catalog.default_layout("DFEEqualizerBlock", None, library=None)
        assert "lock_drv" in lay
        assert lay["lock_drv"][2] == "west"  # relay faces WEST into DC
        assert "transit_rly" not in lay      # it's a programmed cell now
        assert len(lay) == 45                # 44 + lock_drv

    def test_dfe_faces_are_explicit_not_inferred(self, catalog):
        lay = catalog.default_layout("DFEEqualizerBlock", None, library=None)
        # ff20 turns NORTH to the lock-driver (inference pointed it WEST to DC).
        assert lay["ff20"][2] == "north"
        # fb1 routes WEST along the spiral (was reported showing SOUTH).
        assert lay["fb1"][2] == "west"
        assert lay["dc"][2] == "north" and lay["dc_b"][2] == "east"

    def test_dfe_lock_driver_is_programmed_not_transit(self, controller):
        lib = controller.catalog.get("DFEEqualizerBlock").library
        controller.new_project("t", "kyttar_10x12")
        name = controller.place_block("DFEEqualizerBlock", 0, 1, 3, library=lib)
        pl = controller.project.block(name).placement
        assert len(pl.cells) == 45          # all programmed (incl. lock_drv)
        assert len(pl.transit_cells) == 0   # lock_drv is NOT a transit cell
        assert any(c.cell_id == "lock_drv" for c in pl.cells)

    def test_dfe_builds_clean(self, controller):
        lib = controller.catalog.get("DFEEqualizerBlock").library
        controller.new_project("t", "kyttar_10x12")
        controller.place_block("DFEEqualizerBlock", 0, 1, 3, library=lib)
        result = controller.build()
        assert result.ok, [str(e) for e in result.errors]

    def test_dfe_internal_routing_faces(self, controller):
        # The build resolves the non-linear internal handoffs via the block's
        # internal_connections: ff20 NORTH -> lock_drv WEST -> dc; dc NORTH ->
        # dc_b EAST (block output). dc_b stays the exit cell.
        from model.enums import Face
        lib = controller.catalog.get("DFEEqualizerBlock").library
        controller.new_project("t", "kyttar_10x12")
        name = controller.place_block("DFEEqualizerBlock", 0, 1, 3, library=lib)
        pl = controller.project.block(name).placement
        result = controller.build()
        cells0 = result.chips[0].cells
        posof = {c.cell_id: (c.x, c.y) for c in pl.cells}
        assert cells0[posof["ff20"]]["face"] == "north"
        assert cells0[posof["lock_drv"]]["face"] == "west"
        assert cells0[posof["dc"]]["face"] == "north"
        assert cells0[posof["dc_b"]]["face"] == "east"


class TestInternalTransitMechanism:
    """The general default_layout transit-cell mechanism ('transit'-prefixed
    ids → routing-only TransitCells). Exercised via the controller directly
    since no shipped block currently uses it (the DFE relay is programmed)."""

    def test_transit_prefixed_layout_entry_becomes_transit_cell(self, controller):
        from unittest.mock import patch
        from model.enums import Face
        # A synthetic layout with one programmed cell + one 'transit' entry.
        fake = {0: (0, 0, "east"), "transit_r": (1, 0, "north")}
        with patch.object(controller.catalog, "default_layout",
                          return_value=fake):
            cells, transit = controller.default_cells(
                "GainBlock", "lattrex.official", 0, 2, 2)
        assert len(cells) == 1 and len(transit) == 1
        assert transit[0].face is Face.NORTH
