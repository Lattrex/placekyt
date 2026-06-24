"""GUI simulation tests: trace-derived cell-state overlay + Run/Reset (§3.2).

Offscreen Qt. The QTimer animation is driven synchronously via ``sim._tick()``
so tests don't depend on the event loop.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
pytest.importorskip("PySide6")

from PySide6.QtCore import QRectF  # noqa: E402
from PySide6.QtGui import QColor, QImage, QPainter  # noqa: E402
from PySide6.QtWidgets import QApplication, QGraphicsScene  # noqa: E402

from engine.catalog import BlockCatalog  # noqa: E402
from engine.simulator import (  # noqa: E402
    CELL_ACTIVE,
    CELL_EXECUTING,
    SimulationEngine,
)
from ui.canvas import CELL_PX, CellItem, CellKind, ChipCanvas  # noqa: E402
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


def _run_to_done(sim, max_ticks=40):
    sim.start()
    for _ in range(max_ticks):
        sim._tick()
        if not sim.running:
            break


# --------------------------------------------------------------------------- #
# Engine-level trace derivation (no Qt)
# --------------------------------------------------------------------------- #


class TestCellStateDerivation:
    def test_executing_block_detected(self, controller, catalog):
        controller.open_project(DEMO)
        result = controller.build()
        sim = SimulationEngine(CT_PATH)
        sim.load(result.words(0), trace=True)
        entry, in_regs = catalog.resolved_io("GainBlock")
        sim.configure_input_port("x16_in", entry_addr=entry,
                                 hop_count=30, data_addr=in_regs[0])
        sim.inject("x16_in", [0x4000, 0x2000])
        sim.run_until_output("x16_out", 2)
        states = sim.cell_states(10)
        assert states.get((0, 0)) == CELL_EXECUTING  # the gain block
        assert any(v == CELL_ACTIVE for v in states.values())  # routing cells

    def test_no_trace_yields_empty(self):
        sim = SimulationEngine(CT_PATH)  # trace not enabled
        assert sim.cell_states(10) == {}


# --------------------------------------------------------------------------- #
# CellItem overlay
# --------------------------------------------------------------------------- #


class TestCellItemOverlay:
    def test_sim_state_overrides_fill(self, qapp):
        item = CellItem(0, 0, kind=CellKind.BLOCK)
        base = item._fill_color()
        item.set_sim_state("executing")
        assert item._fill_color() != base
        item.set_sim_state(None)
        assert item._fill_color() == base

    def test_paints_each_sim_state(self, qapp):
        for state in ("executing", "active", "idle", "halted"):
            item = CellItem(0, 0, kind=CellKind.BLOCK, label="g")
            item.set_sim_state(state)
            scene = QGraphicsScene()
            scene.addItem(item)
            img = QImage(CELL_PX, CELL_PX, QImage.Format_ARGB32)
            img.fill(QColor("black"))
            p = QPainter(img)
            scene.render(p, QRectF(img.rect()), item.boundingRect())
            p.end()


# --------------------------------------------------------------------------- #
# Canvas overlay application
# --------------------------------------------------------------------------- #


class TestCanvasOverlay:
    def _canvas(self, controller):
        controller.open_project(DEMO)
        canvas = ChipCanvas()
        canvas.set_project(controller.project, controller.chip_types())
        return canvas

    def test_apply_and_clear(self, controller):
        canvas = self._canvas(controller)
        # Overlay is keyed by (chip_id, x, y) — chip 0 here.
        canvas.apply_cell_states({(0, 0, 0): "executing", (0, 1, 0): "active"})
        by_pos = {(c.cx, c.cy): c.sim_state for c in canvas.cell_items()}
        assert by_pos[(0, 0)] == "executing"
        assert by_pos[(1, 0)] == "active"
        assert by_pos[(5, 5)] is None  # not in the map → cleared
        canvas.clear_sim_states()
        assert all(c.sim_state is None for c in canvas.cell_items())

    def test_overlay_is_chip_scoped(self, controller):
        # With two chips, a chip-0 overlay must NOT bleed onto chip 1.
        controller.open_project(DEMO)
        controller.add_chip("RX-2")
        from ui.canvas.chip_canvas import ChipCanvas

        canvas = ChipCanvas()
        canvas.set_project(controller.project, controller.chip_types())
        canvas.apply_cell_states({(0, 0, 0): "executing"})
        states = {(getattr(c, "chip_id", 0), c.cx, c.cy): c.sim_state
                  for c in canvas.cell_items()}
        assert states[(0, 0, 0)] == "executing"
        assert states[(1, 0, 0)] is None  # chip 1's (0,0) is untouched


# --------------------------------------------------------------------------- #
# MainWindow Run / Reset
# --------------------------------------------------------------------------- #


class TestSimMenu:
    def _window(self, controller):
        w = MainWindow(controller=controller)
        controller.open_project(DEMO)
        w._after_project_loaded()
        return w

    def test_run_overlays_living_chip(self, controller):
        w = self._window(controller)
        _run_to_done(w.sim)
        exec_cells = [(c.cx, c.cy) for c in w.canvas.cell_items()
                      if c.sim_state == "executing"]
        assert (0, 0) in exec_cells  # gain block lit up
        assert "Sim:" in w._status_sim.text()

    def test_reset_clears_overlay(self, controller):
        w = self._window(controller)
        _run_to_done(w.sim)
        w._reset_simulation()
        assert all(c.sim_state is None for c in w.canvas.cell_items())
        assert w._status_canvas.text() == "Canvas: Edit"

    def test_handshake_flash_lights_faces(self, controller):
        # Data transfers flash cell exit faces AND chip ports.
        w = self._window(controller)
        cells, ports = [], []
        w.sim.handshakes.connect(
            lambda hs: (cells.extend(hs["cells"]), ports.extend(hs["ports"])))
        _run_to_done(w.sim)
        assert cells  # cell transfers reported
        assert all(t[3] in ("S", "E", "W", "N") for t in cells)
        # the gain cell flashed its East edge (writes east to the next cell)…
        assert any(t[:4] == (0, 0, 0, "E") for t in cells)
        # …and the routing cells along the row flashed too (not just the gain).
        assert any(t[:3] == (0, 5, 0) for t in cells)
        # the x16 ports flashed as data entered/left.
        assert any(p[1] in ("x16_in", "x16_out") for p in ports)

    def test_flash_lands_on_visible_overlay_cell(self, controller):
        # A routing waypoint has TWO stacked CellItems: the base grid cell (Z=0)
        # and an opaque route-overlay TRANSIT cell (Z=2) on top. The flash MUST
        # land on the visible (highest-Z) one, else it lights a hidden cell and
        # the user sees nothing change on the routing cells (the reported bug).
        from ui.canvas.cell_item import CellItem, CellKind

        w = self._window(controller)
        at_5_0 = [c for c in w.canvas.cell_items()
                  if isinstance(c, CellItem) and (c.cx, c.cy) == (5, 0)]
        # Pre-condition: there really are two stacked items here.
        assert len(at_5_0) >= 2
        top = max(at_5_0, key=lambda c: c.zValue())
        assert top.kind is CellKind.TRANSIT  # the route overlay is on top
        w.canvas.apply_handshakes({"cells": [(0, 5, 0, "E")], "ports": []})
        # The visible top item is lit; the hidden base cell is NOT.
        assert top._flash.get("E") == 1.0
        base = min(at_5_0, key=lambda c: c.zValue())
        assert not base._flash

    def test_flash_decays_and_clears(self, controller):
        from ui.canvas.cell_item import CellItem

        w = self._window(controller)
        cell = next(c for c in w.canvas.cell_items()
                    if isinstance(c, CellItem) and (c.cx, c.cy) == (0, 0))
        w.canvas.apply_handshakes({"cells": [(0, 0, 0, "E")], "ports": []})
        assert cell._flash  # lit
        for _ in range(12):  # decay is 0.12/step → ~9 steps to clear
            w.canvas._decay_flashes()
        assert not cell._flash  # fully decayed

    def test_port_flashes_on_data_flow(self, controller):
        w = self._window(controller)
        port = next(p for p in w.canvas.port_items() if p.name == "x16_out")
        w.canvas.apply_handshakes({"cells": [], "ports": [(0, "x16_out")]})
        assert port._flash > 0  # the port lit up
        for _ in range(12):  # decay is 0.12/step → ~9 steps to clear
            w.canvas._decay_flashes()
        assert port._flash == 0

    def test_run_blocked_by_drc_errors(self, controller, qapp, monkeypatch):
        # Overlap two blocks → build fails → sim.start returns False.
        from PySide6.QtWidgets import QMessageBox

        monkeypatch.setattr(QMessageBox, "exec", lambda self: None)
        controller.new_project("Bad", "kyttar_10x12")
        controller.place_block("GainBlock", 0, 3, 3, library="lattrex.official")
        controller.place_block("DCBlockerBlock", 0, 3, 3, library="lattrex.official", params={"length": 2, "long_form": False})
        w = MainWindow(controller=controller)
        w._after_project_loaded()
        assert w.sim.start() is False
        assert w._status_sim.text().startswith("Sim: error")


class TestSimPolish:
    """Speed slider, step, pause/resume, metrics."""

    def _window(self, controller):
        w = MainWindow(controller=controller)
        controller.open_project(DEMO)
        w._after_project_loaded()
        return w

    def test_speed_slider_sets_batch(self, controller):
        from ui.sim_controller import SPEED_BATCHES

        w = self._window(controller)
        w.speed_slider.setValue(0)
        assert w.sim._batch == SPEED_BATCHES[0]
        w.speed_slider.setValue(len(SPEED_BATCHES) - 1)
        assert w.sim._batch == SPEED_BATCHES[-1]

    def test_set_speed_index_clamps(self, controller):
        from ui.sim_controller import SPEED_BATCHES

        w = self._window(controller)
        w.sim.set_speed_index(999)
        assert w.sim._batch == SPEED_BATCHES[-1]
        w.sim.set_speed_index(-5)
        assert w.sim._batch == SPEED_BATCHES[0]

    def test_slow_speed_sets_long_interval_and_one_flash_per_tick(self, controller):
        # The slow end of the slider must be genuine slow-motion: a long tick
        # interval AND one per-word flash step per tick (so individual
        # transactions are visible), propagated to the canvas.
        from ui.sim_controller import SPEED_STEPS

        w = self._window(controller)
        w.speed_slider.setValue(0)                       # slowest
        batch, tick_ms, flash = SPEED_STEPS[0]
        assert w.sim._batch == batch
        assert w.sim._timer.interval() == tick_ms
        assert tick_ms >= 200                            # clearly slow-motion
        assert w.sim._flash_per_tick == flash == 1
        assert w.canvas._flash_per_tick == 1             # propagated to canvas
        # Fast end uses adaptive catch-up (flash rate 0).
        w.speed_slider.setValue(len(SPEED_STEPS) - 1)
        assert w.sim._flash_per_tick == 0
        assert w.canvas._flash_per_tick == 0

    def test_step_advances_and_counts_events(self, controller):
        w = self._window(controller)
        w.sim.set_speed_index(0)  # 1 event/step so it doesn't finish instantly
        w._step_simulation()
        assert w.sim.total_events >= 1

    def test_step_event_advances_one(self, controller):
        w = self._window(controller)
        w.sim.start()
        w.sim.pause()
        before = w.sim.total_events
        w.sim.step("event")
        assert w.sim.total_events == before + 1

    def _trace_count(self, w, kind):
        return sum(1 for e in w.sim.engine.chip.get_trace()
                   if e.get("kind") == kind)

    def test_step_instruction_lands_on_exec(self, controller):
        w = self._window(controller)
        w.sim.start()
        w.sim.pause()
        before = self._trace_count(w, "exec_tick")
        w.sim.step("instruction")
        assert self._trace_count(w, "exec_tick") > before

    def test_step_handshake_lands_on_transfer(self, controller):
        w = self._window(controller)
        w.sim.start()
        w.sim.pause()
        before = self._trace_count(w, "output_ready")
        w.sim.step("handshake")
        assert self._trace_count(w, "output_ready") > before

    def test_step_mode_selector_exists(self, controller):
        w = self._window(controller)
        modes = [w.step_mode.itemData(i) for i in range(w.step_mode.count())]
        assert modes == ["event", "instruction", "handshake"]

    def test_trace_not_rebuilt_every_frame(self, controller):
        # The TraceModel must NOT be rebuilt on every animation frame — doing so
        # starves the flash-decay/paint loop (transit-cell flashes never render)
        # and stacks up Transaction-Log relayouts (multi-second hangs). It is
        # rebuilt only on terminal/pause/step events.
        w = self._window(controller)
        w.sim.set_speed_index(0)  # 1 event/frame → many frames before done
        calls = []
        orig = w.sim._rebuild_trace
        w.sim._rebuild_trace = lambda: (calls.append(1), orig())[1]
        w.sim.start()
        # Drive several mid-run frames; none of them may rebuild the trace.
        for _ in range(5):
            if not w.sim.running:
                break
            w.sim._tick()
            if w.sim.running:  # not the terminal frame
                assert not calls, "trace rebuilt mid-run (starves paint loop)"
        w.sim.stop()

    def test_flash_survives_live_frames(self, controller):
        # With the per-frame rebuild gone, a cell's handshake flash is still set
        # (and paintable) across the frames that follow it — the basis for the
        # red glow marching through transit cells being visible.
        from ui.canvas.cell_item import CellItem

        w = self._window(controller)
        w.sim.set_speed_index(0)
        w.sim.start()
        # Step frames until a row-0 transit cell lights, then confirm the flash
        # persists across the following frames (decay fires on the event loop,
        # not synchronously here, so it won't have been zeroed). The input port
        # is handshake-paced (no FIFO), so the first transfer may take a number
        # of small frames to reach the transit cells — step until it does.
        lit = []
        for _ in range(400):
            if not w.sim.running:
                break
            w.sim._tick()
            lit = [c for c in w.canvas.cell_items()
                   if isinstance(c, CellItem) and c.cx > 0 and c.cy == 0
                   and c._flash]
            if lit:
                break
        assert lit, "no transit cell carried a flash across frames"
        w.sim.stop()

    def test_pause_resume_toggle(self, controller):
        w = self._window(controller)
        w.sim.set_speed_index(0)  # slow so it stays running
        w.sim.start()
        assert w.sim.running and not w.sim.paused
        w.sim.toggle_pause()
        assert w.sim.paused
        w.sim.toggle_pause()
        assert not w.sim.paused
        w.sim.stop()

    def test_run_label_reflects_state(self, controller):
        w = self._window(controller)
        w.sim.set_speed_index(0)
        w._run_simulation()  # start
        assert w.act_run.text() == "Pause"
        w._run_simulation()  # pause
        assert w.act_run.text() == "Run"
        w.sim.stop()

    def test_metrics_signal_updates_status(self, controller):
        w = self._window(controller)
        w.sim.set_speed_index(2)
        w._step_simulation()
        # status shows an event count after a step
        assert "events" in w._status_sim.text()

    def test_reset_clears_event_count(self, controller):
        w = self._window(controller)
        w._step_simulation()
        w._reset_simulation()
        assert w.sim.total_events == 0


class TestStimulus:
    """Stimulus = a BITSTREAM of raw WRITE+DATA+JUMP words injected verbatim."""

    INS = [0x1000, 0x2000, 0x3000, 0x4000]

    def _window(self, controller):
        w = MainWindow(controller=controller)
        controller.open_project(DEMO)
        w._after_project_loaded()
        return w

    def _stim_words(self, w):
        """Wrap INS into a bitstream using the gain design's input-port cfg."""
        from engine.port_config import input_port_config, values_to_bitstream
        cfg = input_port_config(w.controller.project, w.controller.registry,
                                w.controller.catalog)
        _port, kw = cfg
        return values_to_bitstream(self.INS, kw)

    def test_loaded_stimulus_feeds_input_port(self, controller):
        w = self._window(controller)
        words = self._stim_words(w)
        w.sim.set_stimulus(words, "stimulus.kbs")
        assert w.sim.stimulus_name == "stimulus.kbs"
        # A loaded bitstream stimulus is used verbatim (not the ramp).
        assert w.sim._stimulus_words(None) == words

    def test_clear_falls_back_to_ramp(self, controller):
        from ui.sim_controller import _default_ramp

        w = self._window(controller)
        w.sim.set_stimulus(self._stim_words(w), "stimulus.kbs")
        w.sim.set_stimulus(None, None)
        assert w.sim.stimulus_name is None
        # Cleared → the default ramp, wrapped into bursts via the port config.
        from engine.port_config import input_port_config, values_to_bitstream
        cfg = input_port_config(w.controller.project, w.controller.registry,
                                w.controller.catalog)
        assert w.sim._stimulus_words(cfg) == values_to_bitstream(
            _default_ramp(), cfg[1])

    def test_stimulus_drives_correct_output(self, controller):
        w = self._window(controller)
        w.sim.set_stimulus(self._stim_words(w), "stimulus.kbs")
        assert w.sim.start()
        for _ in range(120):
            w.sim._run_batch()
            if not w.sim.running:
                break
        out = list(w.output_panel._samples)
        # gain 0.5: out ≈ (in * 16384) >> 15 for each injected sample.
        assert out[: len(self.INS)] == [(v * 16384) >> 15 for v in self.INS]

    def test_output_panel_shows_captured_values(self, controller):
        w = self._window(controller)
        w.sim.set_stimulus(self._stim_words(w), "stimulus.kbs")
        w._run_simulation()  # starts
        for _ in range(120):
            w.sim._run_batch()
            if not w.sim.running:
                break
        from PySide6.QtWidgets import QApplication

        QApplication.processEvents()
        assert "Output" in w._docks
        assert "x16_out" in w.output_panel._title.text()
        assert len(w.output_panel._samples) >= 1
        assert w.output_panel.table.rowCount() == len(w.output_panel._samples)


class TestTransactionLog:
    """The Transaction Log (debug step 1): full trace, detail toggle, cursor."""

    def _window(self, controller):
        w = MainWindow(controller=controller)
        controller.open_project(DEMO)
        w._after_project_loaded()
        return w

    def _run(self, w):
        w._run_simulation()
        for _ in range(60):
            w.sim._run_batch()
            if not w.sim.running:
                break
        from PySide6.QtWidgets import QApplication
        QApplication.processEvents()

    def test_trace_model_populated(self, controller):
        w = self._window(controller)
        self._run(w)
        assert len(w.sim.trace_model.transactions) > 0

    def test_detail_toggle_shows_full_transactions(self, controller):
        w = self._window(controller)
        self._run(w)
        p = w.output_panel
        p._detail.setChecked(True)
        from PySide6.QtWidgets import QApplication
        QApplication.processEvents()
        assert p._txn_table.rowCount() == len(w.sim.trace_model.transactions)
        # the full stream includes the WRITE+DATA+JUMP kinds, not just payloads.
        kinds = {p._txn_table.item(r, 3).text()
                 for r in range(p._txn_table.rowCount())}
        assert "instr_arrival" in kinds
        assert "data_arrival" in kinds

    def test_row_click_moves_cursor(self, controller):
        w = self._window(controller)
        self._run(w)
        p = w.output_panel
        p._detail.setChecked(True)
        got = []
        p.cursor_requested.connect(lambda ns: got.append(ns))
        p._on_row_clicked(0, 0)
        assert got and got[0] == w.sim.trace_model.transactions[0].time_ns
        # the shared cursor actually moved.
        assert w.sim.trace_model.cursor_ns == got[0]

    def test_payload_view_is_default(self, controller):
        w = self._window(controller)
        self._run(w)
        # Default (detail off) shows the payload table, not the transaction one.
        assert not w.output_panel._detail.isChecked()
        assert w.output_panel._stack.currentIndex() == 0

    def test_instruction_word_decoded(self, controller):
        w = self._window(controller)
        self._run(w)
        p = w.output_panel
        p._detail.setChecked(True)
        from PySide6.QtWidgets import QApplication
        QApplication.processEvents()
        # an instr_arrival row shows the decoded mnemonic + raw hex.
        details = [p._txn_table.item(r, 4).text()
                   for r in range(p._txn_table.rowCount())
                   if p._txn_table.item(r, 3).text() == "instr_arrival"]
        assert details
        assert any(("Write" in d or "Jump" in d) and "0x" in d for d in details)

    def test_cell_filter_isolates_one_cell(self, controller):
        w = self._window(controller)
        self._run(w)
        p = w.output_panel
        p._detail.setChecked(True)
        p.filter_to_cell(0, 0, 0)
        from PySide6.QtWidgets import QApplication
        QApplication.processEvents()
        cells = {p._txn_table.item(r, 2).text()
                 for r in range(p._txn_table.rowCount())}
        assert cells == {"(0,0)"}
        assert p._txn_table.rowCount() < len(w.sim.trace_model.transactions)


class TestCellInspectorLiveMode:
    """Debug step 2: live PC + register overlay in the program view."""

    GAIN_SEL = {"cell": (0, 0), "chip": 0, "kind": "block",
                "block": "GainBlock", "face": "E"}

    def _window(self, controller):
        w = MainWindow(controller=controller)
        controller.open_project(DEMO)
        w._after_project_loaded()
        return w

    def _run(self, w):
        w.sim.set_speed_index(6)
        w.sim.start()
        for _ in range(60):
            w.sim._run_batch()
            if not w.sim.running:
                break

    def test_has_run_tracks_trace(self, controller):
        w = self._window(controller)
        assert not w.sim.has_run()
        self._run(w)
        assert w.sim.has_run()
        w.sim.reset()
        assert not w.sim.has_run()

    def test_live_state_reads_real_registers(self, controller):
        # The live read returns all 32 registers from the engine RAM, including
        # self-computed values (R0 accumulator) — not just external writes.
        w = self._window(controller)
        self._run(w)
        st = w.sim.cell_live_state(0, 0, 0)
        assert st["live"] is True
        assert len(st["registers"]) == 32
        assert st["pc"] is not None  # the gain cell executed

    def test_transit_cell_has_no_pc(self, controller):
        # Routing cells never execute, so there is no PC to highlight.
        w = self._window(controller)
        self._run(w)
        st = w.sim.cell_live_state(0, 5, 0)
        assert st["pc"] is None

    def test_program_view_shows_pc_marker(self, controller):
        w = self._window(controller)
        self._run(w)
        w._on_selection_changed(self.GAIN_SEL)
        pv = w.program_view
        assert pv._live is True
        assert pv._pc is not None
        assert pv.table.item(pv._pc, 0).text() == f"▶R{pv._pc}"
        # a non-PC row keeps the plain register label.
        other = (pv._pc + 1) % 32
        assert pv.table.item(other, 0).text() == f"R{other}"

    def test_pc_advances_on_instruction_step(self, controller):
        # Single-stepping in instruction mode moves the PC — the core "watch
        # each instruction execute" gesture.
        w = self._window(controller)
        controller_pcs = []
        w.sim.reset()
        w._on_selection_changed(self.GAIN_SEL)
        w.sim.start()
        w.sim.pause()
        for _ in range(4):
            w.sim.step("instruction")
            controller_pcs.append(w.sim.cell_live_state(0, 0, 0)["pc"])
        # PCs are not all identical — the program counter moved as we stepped.
        assert len(set(controller_pcs)) > 1

    def test_log_autoscrolls_to_latest_on_step(self, controller):
        # While single-stepping, the Transaction Log follows the newest rows so
        # the user sees what just happened without scrolling manually.
        from PySide6.QtWidgets import QApplication

        w = self._window(controller)
        p = w.output_panel
        p._detail.setChecked(True)
        p._txn_table.setFixedHeight(120)  # force a scrollbar
        w.sim.start()
        w.sim.pause()
        for _ in range(6):
            w.sim.step("instruction")
            QApplication.processEvents()  # let the deferred scroll run
            if not w.sim.running:
                break
        sb = p._txn_table.verticalScrollBar()
        assert sb.maximum() > 0  # there's something to scroll
        assert sb.value() == sb.maximum()  # parked at the bottom (latest)

    def test_reset_clears_live_overlay(self, controller):
        w = self._window(controller)
        self._run(w)
        w._on_selection_changed(self.GAIN_SEL)
        assert w.program_view._live is True
        w.sim.reset()
        w.inspector.update_live_state(w.sim)
        assert w.program_view._live is False
        assert w.program_view._pc is None

    def test_changed_register_flagged(self, controller):
        # Stepping such that a register changes flags it for the live colour.
        w = self._window(controller)
        w._on_selection_changed(self.GAIN_SEL)
        w.sim.start()
        w.sim.pause()
        pv = w.program_view
        # Two instruction steps far enough apart to change R0 across a sample.
        for _ in range(8):
            w.sim.step("instruction")
            if not w.sim.running:
                break
        # After several steps the view has a non-empty live register set.
        assert pv._live and pv._live_regs


class TestGnuradioServer:
    """SimController hosts the chip for a GNURadio flowgraph (live IPC bridge)."""

    def _window(self, controller):
        w = MainWindow(controller=controller)
        controller.open_project(DEMO)
        w._after_project_loaded()
        return w

    def test_server_starts_and_stops(self, controller):
        w = self._window(controller)
        bound = w.sim.start_gnuradio_server()
        assert bound is not None and bound > 0
        assert w.sim.gr_server_running
        w.sim.stop_gnuradio_server()
        assert not w.sim.gr_server_running

    def test_live_trace_is_bounded_and_cycles(self, controller):
        # Live streaming: the TraceModel window stays bounded (fixes the growing
        # Stop-lag) AND keeps CYCLING fresh data (fixes "burst then frozen").
        # The chip's max_records is a HARD CAP (stops recording when full), so
        # SimController drains + clears it each refresh and keeps the rolling
        # window itself — the trace timestamps must ADVANCE every wave.
        import numpy as np

        from ui.sim_controller import _LIVE_TRACE_MAX

        w = self._window(controller)
        w.sim.start_gnuradio_server()
        eng = w.sim.engine
        prev_len = None
        prev_tmax = None
        for _ in range(5):
            eng.chip.write_port(
                "x16_in", np.random.uniform(-0.8, 0.8, 500).astype(np.float32))
            eng.chip.run_until_output("x16_out", 500, 500 * 500)
            eng.chip.read_port("x16_out")
            w.sim.refresh_debug_from_chip(force=True)
            txns = w.sim.trace_model.transactions
            n = len(txns)
            # bounded window …
            assert n <= _LIVE_TRACE_MAX
            if prev_len is not None:
                assert n == prev_len  # flat once saturated (not growing)
            prev_len = n
            # … and CYCLING: the newest timestamp advances every wave.
            tmax = txns[-1].time_ns
            if prev_tmax is not None:
                assert tmax > prev_tmax, "trace frozen — not cycling fresh data"
            prev_tmax = tmax
            # chip trace drained each refresh so it keeps recording.
            assert len(eng.chip.get_trace()) == 0
        w.sim.stop_gnuradio_server()

    def test_rerun_resumes_after_reset(self, controller):
        # Run/Stop/Run: the second run (reset RPC) must clear the GUI window so
        # the fresh chip's (low-timestamp) events aren't sorted behind / trimmed
        # by the previous run's, which would freeze the views (the reported bug).
        import numpy as np

        w = self._window(controller)
        w.sim.start_gnuradio_server()
        eng = w.sim.engine

        def stream():
            eng.chip.write_port(
                "x16_in", np.random.uniform(-0.8, 0.8, 800).astype(np.float32))
            eng.chip.run_until_output("x16_out", 800, 800 * 500)
            eng.chip.read_port("x16_out")
            w.sim.refresh_debug_from_chip(force=True)
            return w.sim.trace_model.transactions[-1].time_ns

        stream()
        run1_tmax = stream()
        # simulate the client's reset-on-rerun RPC
        new = w.sim._rehost_server_chip_threadsafe()
        w.sim._gr_server.set_chip(new)
        # run 2 starts fresh — its first window should NOT be the stale run-1 max
        run2_first = stream()
        run2_second = stream()
        assert run2_first < run1_tmax       # window was cleared (fresh time)
        assert run2_second > run2_first     # and it advances again (not frozen)
        w.sim.stop_gnuradio_server()

    def test_live_window_configurable(self, controller):
        w = self._window(controller)
        assert w.sim.live_window == 20000  # new default
        w.sim.set_live_window(3000)
        assert w.sim.live_window == 3000
        w.sim.set_live_window(50)          # clamped to a sane floor
        assert w.sim.live_window >= 100

    def test_server_streams_and_refreshes_debug(self, controller):
        import json
        import socket
        import struct

        import numpy as np
        from PySide6.QtCore import QEventLoop, Qt

        w = self._window(controller)
        calls = []
        w.sim.server_activity.connect(lambda: calls.append(1),
                                      Qt.QueuedConnection)
        bound = w.sim.start_gnuradio_server()
        hdr = struct.Struct(">I")

        def send(c, h, p=None):
            h = dict(h)
            a = None
            if p is not None:
                a = np.ascontiguousarray(p, dtype="<f4")
                h["n"] = int(a.size)
            else:
                h.setdefault("n", 0)
            hb = json.dumps(h).encode()
            c.sendall(hdr.pack(len(hb)))
            c.sendall(hb)
            if a is not None and a.size:
                c.sendall(a.tobytes())

        def recv(c):
            def rx(n):
                b = b""
                while len(b) < n:
                    b += c.recv(n - len(b))
                return b
            hl = hdr.unpack(rx(4))[0]
            h = json.loads(rx(hl))
            n = int(h.get("n", 0))
            return h, (np.frombuffer(rx(n * 4), dtype="<f4") if n else None)

        c = socket.socket()
        c.connect(("127.0.0.1", bound))
        send(c, {"op": "write_port", "port": "x16_in"},
             np.array([0.6, 0.4], dtype=np.float32))
        recv(c)
        send(c, {"op": "run_until_output", "port": "x16_out",
                 "count": 2, "max_events": 20000})
        recv(c)
        send(c, {"op": "read_port", "port": "x16_out"})
        _h, out = recv(c)
        c.close()
        # chip applied gain 0.5
        assert np.allclose(out, [0.3, 0.2], atol=1e-3)
        # the activity signal reaches the GUI thread when events are pumped
        from PySide6.QtWidgets import QApplication
        for _ in range(20):
            QApplication.processEvents(QEventLoop.AllEvents, 50)
        assert calls  # debug refresh was triggered
        w.sim.stop_gnuradio_server()


class TestSramDemoPlainRun:
    """The SRAM demo runs end-to-end via PLAIN Run: opening the .kyt auto-loads
    its .kbs bitstream stimulus (6 writes then 6 reads); Run injects it; the
    writes store, the reads push back out x16_out. No special demo action."""

    KYT = Path(__file__).parent / "data" / "demo" / "sram_panel_demo.kyt"

    def test_plain_run_writes_then_reads_out_x16(self, controller):
        import pytest
        if not self.KYT.exists():
            pytest.skip("SRAM demo .kyt absent")
        from engine.sram_demo import DEMO_WORDS

        w = MainWindow(controller=controller)
        controller.open_project(str(self.KYT))
        w._after_project_loaded()
        # the project's default_stimulus .kbs auto-loaded
        assert w.sim.stimulus_name == "sram_panel_demo.kbs"
        out = []
        w.sim.output.connect(lambda d: out.append(d))
        assert w.sim.start()                      # plain Run
        for _ in range(8000):
            if not w.sim._running:
                break
            w.sim._run_batch()
        dev = w.sim.panel_device(0)
        assert dev.writes_committed == len(DEMO_WORDS)
        assert dev.reads_issued == len(DEMO_WORDS)
        assert all(dev.mem.get(a) == v for a, v in enumerate(DEMO_WORDS))
        x16 = [d for d in out if d.get("port") == "x16_out" and d.get("samples")]
        assert x16, "no x16_out output captured"
        assert [s & 0xFFFF for s in x16[-1]["samples"]] == list(DEMO_WORDS)


class TestLiveCellFace:
    """A cell that re-points at runtime (MOVE [FACE], e.g. the crossover) shows
    its LIVE output face in the canvas during simulation."""

    KYT = Path(__file__).parent / "data" / "demo" / "sram_panel_demo.kyt"

    def test_crossover_face_updates_live(self, controller):
        import pytest
        if not self.KYT.exists():
            pytest.skip("SRAM demo .kyt absent")
        w = MainWindow(controller=controller)
        controller.open_project(str(self.KYT))
        w._after_project_loaded()
        seen = []
        w.sim.cell_faces.connect(lambda f: seen.append(f.get((0, 8, 6))))
        w.sim.start()
        for _ in range(8000):
            if not w.sim._running:
                break
            w.sim._run_batch()
        # The crossover relays to the controller (south) AND out to x16_out
        # (east) at different times → both live faces observed.
        observed = {f for f in seen if f}
        assert "south" in observed and "east" in observed


class TestDisasmAutoLoad:
    """The Disassembly panel auto-loads the stimulus being run (#195)."""

    KYT = Path(__file__).parent / "data" / "demo" / "sram_panel_demo.kyt"

    def test_run_auto_loads_stimulus(self, controller):
        import pytest
        if not self.KYT.exists():
            pytest.skip("SRAM demo .kyt absent")
        w = MainWindow(controller=controller)
        controller.open_project(str(self.KYT))
        w._after_project_loaded()
        # Empty before a run.
        assert w.disassembly_panel._view.toPlainText() == ""
        assert w.sim.start()
        # On Run the stimulus bitstream is auto-loaded into the panel.
        text = w.disassembly_panel._view.toPlainText()
        assert w.disassembly_panel._source == "sram_panel_demo.kbs"
        assert len(text.splitlines()) == 36          # 6 writes + 6 reads bursts
        assert "WRITE @15, 4" in text and "DW   0xCAFE" in text
        w.sim.stop()


class TestDisasmHighlight:
    """The Disassembly panel highlights each injected word during a run (#196)."""

    KYT = Path(__file__).parent / "data" / "demo" / "sram_panel_demo.kyt"

    def test_highlight_marks_injected_line(self, controller):
        from ui.panels.disassembly_panel import DisassemblyPanel
        p = DisassemblyPanel()
        p.show_words([0x6204, 0xCAFE, 0x720F, 0x6204, 0x1234, 0x720F], source="t")
        assert p._view.extraSelections() == []
        p.highlight_injected(3)               # count=3 -> line index 2
        sels = p._view.extraSelections()
        assert len(sels) == 1
        assert sels[0].cursor.block().blockNumber() == 2
        p.show_words([0x0000], source="x")    # re-render clears the highlight
        assert p._view.extraSelections() == []

    def test_run_advances_injection_progress(self, controller):
        import pytest
        if not self.KYT.exists():
            pytest.skip("SRAM demo .kyt absent")
        w = MainWindow(controller=controller)
        controller.open_project(str(self.KYT))
        w._after_project_loaded()
        counts = []
        w.sim.injection_progress.connect(counts.append)
        w.sim.start()
        for _ in range(8000):
            if not w.sim._running:
                break
            w.sim._run_batch()
        assert max(counts) == 36              # all 36 stimulus words injected
        w.sim.stop()


class TestDisasmBreakpoint:
    """Stimulus-line breakpoints: pause the run when a word is injected (#197)."""

    KYT = Path(__file__).parent / "data" / "demo" / "sram_panel_demo.kyt"

    def test_panel_toggle_emits_and_marks(self, controller):
        from ui.panels.disassembly_panel import DisassemblyPanel
        p = DisassemblyPanel()
        p.show_words([0x6204, 0xCAFE, 0x720F], source="t")
        toggled = []
        p.breakpoint_toggled.connect(lambda line, on: toggled.append((line, on)))
        p.set_breakpoints({1})
        # a breakpoint line renders a marker (one extra selection).
        assert len(p._view.extraSelections()) == 1
        # clearing emits an off-toggle and removes the marker.
        p._on_clear_breakpoints()
        assert toggled == [(1, False)]
        assert p._view.extraSelections() == []

    def test_breakpoint_pauses_run_at_injection(self, controller):
        import pytest
        if not self.KYT.exists():
            pytest.skip("SRAM demo .kyt absent")
        w = MainWindow(controller=controller)
        controller.open_project(str(self.KYT))
        w._after_project_loaded()
        w.sim.toggle_injection_breakpoint(7)      # pause when word 7 injects
        hits = []
        w.sim.injection_breakpoint_hit.connect(hits.append)
        w.sim.set_speed_index(0)                  # gradual injection
        w.sim.start()
        for _ in range(20000):
            if not w.sim._running or w.sim._paused:
                break
            w.sim._run_batch()
        assert w.sim._paused
        assert hits == [7]
        # The paused injection count is exactly word 7 + 1.
        assert w.sim.engine.input_injection_count("x16_in") == 8
        # Clearing + resuming runs to completion.
        w.sim.clear_injection_breakpoints()
        w.sim.resume()
        for _ in range(20000):
            if not w.sim._running:
                break
            w.sim._run_batch()
        from engine.sram_demo import DEMO_WORDS
        dev = w.sim.panel_device(0)
        assert dev.writes_committed == len(DEMO_WORDS)
        assert dev.reads_issued == len(DEMO_WORDS)
