"""Breakpoint tests (DEBUG step 5): model + run-loop pause + 3 entry paths.

The Qt-free model (engine/breakpoints.py) is tested directly; the run-loop
pause + UI entry paths (panel form, canvas right-click, program-pane click) +
canvas/scrubber/program markers go through MainWindow.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
pytest.importorskip("PySide6")

from PySide6.QtCore import Qt  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from engine.breakpoints import (  # noqa: E402
    BP_FACE,
    BP_PC,
    Breakpoint,
    BreakpointSet,
)
from engine.catalog import BlockCatalog  # noqa: E402
from ui.canvas.cell_item import CellItem  # noqa: E402
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


# --------------------------------------------------------------------------- #
# Pure model
# --------------------------------------------------------------------------- #


class TestBreakpointModel:
    def test_pc_matches(self):
        bp = Breakpoint(0, 0, 0, BP_PC, 30)
        assert bp.matches({"kind": "exec_tick", "cell_id": 0, "pc": 30}, 10)
        assert not bp.matches({"kind": "exec_tick", "cell_id": 0, "pc": 29}, 10)
        # wrong cell
        assert not bp.matches({"kind": "exec_tick", "cell_id": 1, "pc": 30}, 10)
        # wrong kind
        assert not bp.matches({"kind": "data_arrival", "cell_id": 0, "pc": 30}, 10)

    def test_face_matches(self):
        bp = Breakpoint(0, 1, 0, BP_FACE, "W")
        assert bp.matches(
            {"kind": "data_arrival", "cell_id": 1, "face": "W"}, 10)
        assert bp.matches(
            {"kind": "instr_arrival", "cell_id": 1, "face": "W"}, 10)
        assert not bp.matches(
            {"kind": "data_arrival", "cell_id": 1, "face": "N"}, 10)

    def test_disabled_never_matches(self):
        bp = Breakpoint(0, 0, 0, BP_PC, 5, enabled=False)
        assert not bp.matches({"kind": "exec_tick", "cell_id": 0, "pc": 5}, 10)

    def test_set_add_dedupes(self):
        s = BreakpointSet()
        s.add(Breakpoint(0, 0, 0, BP_PC, 5))
        s.add(Breakpoint(0, 0, 0, BP_PC, 5))  # identical → no duplicate
        assert len(s.breakpoints) == 1

    def test_first_hit(self):
        s = BreakpointSet()
        s.add(Breakpoint(0, 0, 0, BP_PC, 30))
        events = [
            {"kind": "exec_tick", "cell_id": 0, "pc": 28, "time_ns": 1.0},
            {"kind": "exec_tick", "cell_id": 0, "pc": 30, "time_ns": 2.0},
        ]
        hit = s.first_hit(0, events, 10)
        assert hit is not None and hit.time_ns == 2.0

    def test_first_hit_none_when_no_bp_for_chip(self):
        s = BreakpointSet()
        s.add(Breakpoint(1, 0, 0, BP_PC, 30))  # chip 1
        events = [{"kind": "exec_tick", "cell_id": 0, "pc": 30, "time_ns": 1.0}]
        assert s.first_hit(0, events, 10) is None  # chip-0 scan, no chip-0 bp

    def test_has_any_and_find(self):
        s = BreakpointSet()
        bp = s.add(Breakpoint(0, 2, 3, BP_PC, 7))
        assert s.has_any(0, 2, 3)
        assert not s.has_any(0, 9, 9)
        assert s.find(0, 2, 3, BP_PC, 7) is bp


# --------------------------------------------------------------------------- #
# Run-loop + UI integration
# --------------------------------------------------------------------------- #


class TestBreakpointIntegration:
    def _window(self, controller):
        w = MainWindow(controller=controller)
        controller.open_project(DEMO)
        w._after_project_loaded()
        return w

    def test_run_pauses_at_pc_breakpoint(self, controller):
        w = self._window(controller)
        w.breakpoint_panel.add_breakpoint(0, 0, 0, BP_PC, 30)
        w.sim.set_speed_index(0)  # 1 event/batch → stop exactly at the hit
        w.sim.start()
        for _ in range(400):
            if w.sim.paused:
                break
            w.sim._tick()
        assert w.sim.paused
        # the cursor parked at the hit's exec_tick (pc==30).
        assert w.sim.trace_model.cursor_ns > 0

    def test_run_stops_AT_hit_not_past_it(self, controller):
        # Regression: at default speed (batch=2000) the whole sim used to run in
        # one tick, so the pause happened AFTER everything had executed. With
        # breakpoints active the batch must drop to 1 so the run stops AT the hit
        # — the trace at pause holds only events up to (and incl.) the hit.
        w = self._window(controller)
        w.breakpoint_panel.add_breakpoint(0, 0, 0, BP_PC, 30)
        assert w.sim._effective_batch() == 1  # not the 2000 default
        w.sim.start()  # default speed
        for _ in range(2000):
            if w.sim.paused:
                break
            w.sim._tick()
        assert w.sim.paused
        evs = w.sim.engine.chip.get_trace()
        hit_t = w.sim.breakpoint_hit_times()[0]
        # No events past the hit time remain — we stopped exactly at it.
        assert not [e for e in evs if e.get("time_ns", 0) > hit_t]
        # And it really is a small slice of the full 512-event run.
        assert len(evs) < 50

    def test_hit_records_scrubber_marker(self, controller):
        w = self._window(controller)
        w.breakpoint_panel.add_breakpoint(0, 0, 0, BP_PC, 30)
        w.sim.set_speed_index(0)
        w.sim.start()
        for _ in range(400):
            if w.sim.paused:
                break
            w.sim._tick()
        bp_marks = [m for m in w.scrubber._markers if m[1] == "bp"]
        assert bp_marks

    def test_canvas_right_click_path_marks_cell(self, controller):
        w = self._window(controller)
        w.canvas.breakpoint_requested.emit(0, 0, 0, BP_PC, 12)
        assert w.sim.breakpoints.find(0, 0, 0, BP_PC, 12) is not None
        items = {(getattr(c, "chip_id", 0) or 0, c.cx, c.cy): c
                 for c in w.canvas.cell_items()}
        assert items[(0, 0, 0)].has_breakpoint

    def test_program_pane_toggle_path(self, controller):
        w = self._window(controller)
        gain = next(c for c in w.canvas.cell_items()
                    if isinstance(c, CellItem) and (c.cx, c.cy) == (0, 0)
                    and c.label)
        gain.setSelected(True)
        QApplication.processEvents()
        w.program_view.breakpoint_toggled.emit(28)  # add
        assert w.sim.breakpoints.find(0, 0, 0, BP_PC, 28) is not None
        assert 28 in w.program_view._bp_addrs
        assert "●" in w.program_view.table.item(28, 0).text()
        w.program_view.breakpoint_toggled.emit(28)  # toggle off
        assert w.sim.breakpoints.find(0, 0, 0, BP_PC, 28) is None

    def test_panel_enable_checkbox_disables(self, controller):
        w = self._window(controller)
        w.breakpoint_panel.add_breakpoint(0, 0, 0, BP_PC, 30)
        w.breakpoint_panel._table.item(0, 0).setCheckState(Qt.Unchecked)
        assert w.sim.breakpoints.breakpoints[0].enabled is False

    def test_remove_clears_canvas_mark(self, controller):
        w = self._window(controller)
        w.breakpoint_panel.add_breakpoint(0, 0, 0, BP_PC, 5)
        bp = w.sim.breakpoints.breakpoints[0]
        w.breakpoint_panel.remove_breakpoint(bp)
        items = {(getattr(c, "chip_id", 0) or 0, c.cx, c.cy): c
                 for c in w.canvas.cell_items()}
        assert not items[(0, 0, 0)].has_breakpoint

    def test_breakpoints_persist_across_reset(self, controller):
        w = self._window(controller)
        w.breakpoint_panel.add_breakpoint(0, 0, 0, BP_PC, 30)
        w.sim.reset()
        # the breakpoint itself survives; only the per-run hit list is cleared.
        assert w.sim.breakpoints.find(0, 0, 0, BP_PC, 30) is not None
        assert w.sim.breakpoint_hit_times() == []
