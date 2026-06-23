"""Multi-chip canvas: add chip, inter-chip connections, wire rendering (§3.2).

The model/build/DRC/IO already supported multiple chips; this covers the UI/
command layer added for the HF-modem demo (two daisy-chained kyttar_10x12 chips).
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

from tests.conftest import CHIP_YAML  # noqa: E402

from tests.conftest import CHIP_YAML as CT_PATH  # noqa: E402


@pytest.fixture(scope="module")
def qapp():
    return QApplication.instance() or QApplication([])


@pytest.fixture(scope="module")
def catalog():
    return BlockCatalog.from_gr_kyttar()


def _two_chip_ctrl(catalog):
    ctrl = AppController(catalog=catalog)
    ctrl.new_project("Multi", "kyttar_10x12")
    ctrl.add_chip("RX-2")
    return ctrl


class TestAddChip:
    def test_second_chip_offset(self, qapp, catalog):
        ctrl = _two_chip_ctrl(catalog)
        assert len(ctrl.project.chips) == 2
        c0, c1 = ctrl.project.chips
        # Chip 1 lands to the RIGHT of chip 0 (no overlap).
        assert c1.position_x > c0.position_x
        # 10 cells * 64px + 2-cell gap = 768.
        assert c1.position_x == 768.0

    def test_add_chip_undoable(self, qapp, catalog):
        ctrl = _two_chip_ctrl(catalog)
        ctrl.undo()
        assert len(ctrl.project.chips) == 1


class TestInterChip:
    def test_create_and_validate(self, qapp, catalog):
        ctrl = _two_chip_ctrl(catalog)
        ic = ctrl.add_inter_chip(0, "x16_out", 1, "x16_in")
        assert ic in ctrl.project.inter_chip_connections
        # wrong direction rejected
        with pytest.raises(ValueError):
            ctrl.add_inter_chip(0, "x16_in", 1, "x16_out")
        # same chip rejected
        with pytest.raises(ValueError):
            ctrl.add_inter_chip(0, "x16_out", 0, "x16_in")

    def test_undo_redo(self, qapp, catalog):
        ctrl = _two_chip_ctrl(catalog)
        ctrl.add_inter_chip(0, "x16_out", 1, "x16_in")
        ctrl.undo()
        assert not ctrl.project.inter_chip_connections
        ctrl.redo()
        assert len(ctrl.project.inter_chip_connections) == 1

    def test_roundtrip_through_kyt(self, qapp, catalog, tmp_path):
        from engine.io.project_io import load_project, save_project

        ctrl = _two_chip_ctrl(catalog)
        ctrl.add_inter_chip(0, "x16_out", 1, "x16_in")
        fp = tmp_path / "multi.kyt"
        save_project(ctrl.project, fp)
        reloaded = load_project(fp)
        assert len(reloaded.inter_chip_connections) == 1
        ic = reloaded.inter_chip_connections[0]
        assert (ic.from_chip, ic.from_port, ic.to_chip, ic.to_port) == (
            0, "x16_out", 1, "x16_in")


class TestCrossChipMove:
    def test_move_block_to_chip(self, qapp, catalog):
        ctrl = _two_chip_ctrl(catalog)
        ctrl.place_block("GainBlock", 0, 0, 0, library="lattrex.official")
        g = ctrl.project.blocks[0].name
        ctrl.move_block_to_chip(g, 1, 3, 2)
        pl = ctrl.project.block(g).placement
        assert pl.chip == 1
        # drop-point placement: the anchor cell lands at (3, 2).
        assert (pl.cells[0].x, pl.cells[0].y) == (3, 2)

    def test_cross_chip_move_undoable(self, qapp, catalog):
        ctrl = _two_chip_ctrl(catalog)
        ctrl.place_block("GainBlock", 0, 0, 0, library="lattrex.official")
        g = ctrl.project.blocks[0].name
        ctrl.move_block_to_chip(g, 1, 3, 2)
        ctrl.undo()
        pl = ctrl.project.block(g).placement
        assert pl.chip == 0 and (pl.cells[0].x, pl.cells[0].y) == (0, 0)

    def test_build_after_cross_chip_move(self, qapp, catalog):
        ctrl = _two_chip_ctrl(catalog)
        ctrl.place_block("GainBlock", 0, 0, 0, library="lattrex.official")
        g = ctrl.project.blocks[0].name
        ctrl.move_block_to_chip(g, 1, 3, 2)
        res = ctrl.build()
        assert res.ok
        # the gain now builds on chip 1.
        assert any(c.get("block") == g
                   for c in res.chips[1].cells.values())

    def test_cross_chip_move_removes_block_routes(self, qapp, catalog):
        """A block's routes reference cells on its old chip; moving chips breaks
        them, so they're removed (and restored on undo). They must NOT follow
        the block onto the new chip."""
        from model.connection import BlockEndpoint, ChipPortEndpoint

        ctrl = _two_chip_ctrl(catalog)
        ctrl.place_block("GainBlock", 0, 0, 0, library="lattrex.official")
        g = ctrl.project.blocks[0].name
        ctrl.add_route(BlockEndpoint(g, "out"), ChipPortEndpoint(0, "x16_out"),
                       [(x, 0) for x in range(10)])
        assert len(ctrl.project.connections) == 1
        ctrl.move_block_to_chip(g, 1, 3, 2)
        assert len(ctrl.project.connections) == 0  # route removed, not moved
        ctrl.undo()
        assert ctrl.project.block(g).placement.chip == 0
        assert len(ctrl.project.connections) == 1  # route restored

    def test_multicell_block_preserves_shape(self, qapp, catalog):
        ctrl = _two_chip_ctrl(catalog)
        ctrl.place_block("GardnerTimingRecovery", 0, 0, 0,
                         library="lattrex.official")
        g = ctrl.project.blocks[0].name
        before = [(c.x - ctrl.project.block(g).placement.cells[0].x,
                   c.y - ctrl.project.block(g).placement.cells[0].y)
                  for c in ctrl.project.block(g).placement.cells]
        ctrl.move_block_to_chip(g, 1, 4, 3)
        after_anchor = ctrl.project.block(g).placement.cells[0]
        after = [(c.x - after_anchor.x, c.y - after_anchor.y)
                 for c in ctrl.project.block(g).placement.cells]
        assert before == after  # shape (cell offsets) preserved across the move


class TestMultiChipSim:
    """End-to-end round-based multi-chip simulation (decoupled relay)."""

    def _chain_project(self, catalog):
        from model.connection import BlockEndpoint, ChipPortEndpoint

        ctrl = AppController(catalog=catalog)
        ctrl.new_project("M", "kyttar_10x12")
        ctrl.add_chip("RX-2")
        # chip0: gain at the input cell → x16_out
        ctrl.place_block("GainBlock", 0, 0, 0, library="lattrex.official")
        g0 = ctrl.project.blocks[0].name
        ctrl.add_route(BlockEndpoint(g0, "out"), ChipPortEndpoint(0, "x16_out"),
                       [(x, 0) for x in range(10)])
        # chip1: gain at the input cell → x16_out
        ctrl.place_block("GainBlock", 1, 0, 0, library="lattrex.official")
        g1 = ctrl.project.blocks[1].name
        ctrl.add_route(BlockEndpoint(g1, "out"), ChipPortEndpoint(1, "x16_out"),
                       [(x, 0) for x in range(10)])
        ctrl.add_inter_chip(0, "x16_out", 1, "x16_in")
        return ctrl

    def test_engine_relays_value_across_chips(self, qapp, catalog):
        # Two 0.5 gains chained across chips → 0.25× (matches the ref model's
        # decoupled relay; the data value at the handshake is identical to the
        # continuous-HOP_CNT hardware view).
        from engine.simulator import MultiChipSimEngine

        ctrl = self._chain_project(catalog)
        r = ctrl.build()
        assert r.ok
        ct_path = str(ctrl.registry.require("kyttar_10x12").path)
        eng = MultiChipSimEngine({0: ct_path, 1: ct_path})
        eng.connect(0, "x16_out", 1, "x16_in")
        eng.load(0, r.words(0), trace=True)
        eng.load(1, r.words(1), trace=True)
        e, ir = catalog.resolved_io("GainBlock")
        eng.configure_input_port(0, "x16_in", entry_addr=e, hop_count=30,
                                 data_addr=ir[0])
        eng.configure_input_port(1, "x16_in", entry_addr=e, hop_count=30,
                                 data_addr=ir[0])
        eng.inject(0, "x16_in", [0x4000, 0x2000])
        eng.run_until_output(1, "x16_out", 2, None, 2000)
        out = eng.capture(1, "x16_out")
        assert out[:2] == [0x1000, 0x800]  # 0.25× of 0x4000, 0x2000
        # cell-state overlay covers both chips.
        states = eng.cell_states()
        assert {k[0] for k in states} == {0, 1}

    def test_simcontroller_auto_selects_multichip(self, qapp, catalog):
        from ui.main_window import MainWindow

        ctrl = self._chain_project(catalog)
        w = MainWindow(controller=ctrl)
        w._after_project_loaded()
        assert w.sim.start()
        assert w.sim._multi  # auto-selected the multi-chip engine
        for _ in range(100):
            w.sim._run_batch()
            if not w.sim.running:
                break
        states = w.sim.engine.cell_states()
        assert {k[0] for k in states} == {0, 1}
        w.canvas.apply_cell_states(states)
        lit = {getattr(c, "chip_id", 0) for c in w.canvas.cell_items()
               if c.sim_state}
        assert lit == {0, 1}  # both chips light up, no chip-blind bleed


class TestOutputPortRouting:
    """Routes to non-default output ports (x1_out, south-facing) must build."""

    def test_x1_out_route_builds_and_faces_south(self, qapp, catalog):
        from model.connection import BlockEndpoint, ChipPortEndpoint

        ctrl = AppController(catalog=catalog)
        ctrl.new_project("X1", "kyttar_10x12")
        # gain on row 11 routed to x1_out (9,11), a SOUTH-facing port.
        ctrl.place_block("GainBlock", 0, 0, 11, library="lattrex.official",
                         params={"gain": 0.5})
        g = ctrl.project.blocks[0].name
        ctrl.add_route(BlockEndpoint(g, "out"), ChipPortEndpoint(0, "x1_out"),
                       [(x, 11) for x in range(10)])
        res = ctrl.build()
        assert res.ok, [str(e) for e in res.errors]  # was: "distance 32"
        # source hop = route length (10), not the Router's wandering trace (21).
        prog = ctrl.cell_program(0, 0, 11)
        for instr in prog["instructions"]:
            assert instr["hop"] == 10
        # the final routing cell exits SOUTH (matching x1_out), not north/east.
        assert ctrl.cell_program(0, 9, 11)["face"] == "south"

    def test_x1_out_flows_end_to_end(self, qapp, catalog):
        from engine.registry import ChipTypeRegistry
        from engine.simulator import SimulationEngine
        from model.connection import BlockEndpoint, ChipPortEndpoint

        reg = ChipTypeRegistry()
        reg.register_file(str(CT_PATH))
        ctrl = AppController(catalog=catalog)
        ctrl.new_project("X1", "kyttar_10x12")
        ctrl.place_block("GainBlock", 0, 0, 11, library="lattrex.official",
                         params={"gain": 0.5})
        g = ctrl.project.blocks[0].name
        ctrl.add_route(BlockEndpoint(g, "out"), ChipPortEndpoint(0, "x1_out"),
                       [(x, 11) for x in range(10)])
        res = ctrl.build()
        assert res.ok
        e, ir = catalog.resolved_io("GainBlock")
        sim = SimulationEngine(str(CT_PATH))
        sim.load(res.words(0))
        sim.configure_input_port("x1_in", entry_addr=e, hop_count=30,
                                 data_addr=ir[0])
        sim.inject("x1_in", [0x4000, 0x2000])
        sim.run_until_output("x1_out", 2)
        assert sim.capture("x1_out")[:2] == [0x2000, 0x1000]  # 0.5×


class TestInterChipHop:
    def test_hop_spans_boundary_to_next_chip_block(self, qapp, catalog):
        """A block routing out to x16_out, wired to chip1.x16_in, then routed to
        a chip-1 block: the source hop is continuous across the boundary
        (interconnect is not a hop). gain(0,0)→x16_out(9,0)=10, +1 to chip1
        block(1,0) = @11; dest/entry resolve to the chip-1 block."""
        from model.connection import BlockEndpoint, ChipPortEndpoint

        ctrl = _two_chip_ctrl(catalog)
        ctrl.place_block("GainBlock", 0, 0, 0, library="lattrex.official")
        g = ctrl.project.blocks[0].name
        ctrl.add_route(BlockEndpoint(g, "out"), ChipPortEndpoint(0, "x16_out"),
                       [(x, 0) for x in range(10)])
        ctrl.place_block("DCBlockerBlock", 1, 1, 0, library="lattrex.official")
        d = ctrl.project.blocks[1].name
        ctrl.add_route(ChipPortEndpoint(1, "x16_in"), BlockEndpoint(d, "in"),
                       [(0, 0), (1, 0)])
        ctrl.add_inter_chip(0, "x16_out", 1, "x16_in")
        assert ctrl.build().ok
        prog = ctrl.cell_program(0, 0, 0)
        jump = next(i for i in prog["instructions"] if i["kind"] == "JUMP")
        write = next(i for i in prog["instructions"] if i["kind"] == "WRITE")
        entry, in_regs = catalog.resolved_io("DCBlockerBlock")
        assert jump["hop"] == 11 and write["hop"] == 11
        assert jump["field"] == entry          # chip-1 block's entry
        assert write["field"] == in_regs[0]    # chip-1 block's input register

    def test_no_wire_keeps_port_hop(self, qapp, catalog):
        """Without an inter-chip wire, the block's hop stays at the port (@10),
        not chained to another chip."""
        from model.connection import BlockEndpoint, ChipPortEndpoint

        ctrl = _two_chip_ctrl(catalog)
        ctrl.place_block("GainBlock", 0, 0, 0, library="lattrex.official")
        g = ctrl.project.blocks[0].name
        ctrl.add_route(BlockEndpoint(g, "out"), ChipPortEndpoint(0, "x16_out"),
                       [(x, 0) for x in range(10)])
        assert ctrl.build().ok
        prog = ctrl.cell_program(0, 0, 0)
        jump = next(i for i in prog["instructions"] if i["kind"] == "JUMP")
        assert jump["hop"] == 10  # to the port, no inter-chip continuation


class TestCanvasRendering:
    def _window(self, catalog):
        from ui.main_window import MainWindow

        ctrl = _two_chip_ctrl(catalog)
        w = MainWindow(controller=ctrl)
        w._after_project_loaded()
        return w, ctrl

    def test_two_chip_outlines(self, qapp, catalog):
        from ui.canvas.chip_outline import ChipOutlineItem

        w, _ctrl = self._window(catalog)
        QApplication.processEvents()
        outlines = [it for it in w.canvas._scene.items()
                    if isinstance(it, ChipOutlineItem)]
        assert len(outlines) == 2

    def test_inter_chip_wire_rendered(self, qapp, catalog):
        from ui.canvas.inter_chip_wire_item import InterChipWireItem

        w, ctrl = self._window(catalog)
        ctrl.add_inter_chip(0, "x16_out", 1, "x16_in")
        QApplication.processEvents()
        wires = [it for it in w.canvas._scene.items()
                 if isinstance(it, InterChipWireItem)]
        assert len(wires) == 1
        assert wires[0].inter_chip is not None

    def test_routing_is_chip_aware(self, qapp, catalog):
        """Regression: with two chips, a block on chip 1 at the same LOCAL coords
        as a chip-0 route waypoint must NOT terminate the chip-0 route (the
        _cell_at lookup was chip-blind)."""
        from ui.canvas.cell_item import CellKind

        ctrl = AppController(catalog=catalog)
        ctrl.new_project("T", "kyttar_10x12")
        ctrl.place_block("GainBlock", 0, 0, 0, library="lattrex.official")
        ctrl.add_chip("RX-2")
        # chip 1 block at local (2, 0) — coincides with a chip-0 route waypoint.
        ctrl.place_block("DCBlockerBlock", 1, 2, 0, library="lattrex.official")
        from ui.main_window import MainWindow

        w = MainWindow(controller=ctrl)
        w._after_project_loaded()
        QApplication.processEvents()
        c = w.canvas
        # chip-aware lookup distinguishes the two chips' (2,0).
        assert c._cell_at(2, 0, 0).kind is CellKind.EMPTY
        assert c._cell_at(2, 0, 1).label == ctrl.project.blocks[1].name
        # Routing right on chip 0 passes through (2,0) without completing.
        c.start_route("gain", 0, 0, 0)
        assert c.add_waypoint(1, 0)
        assert c.add_waypoint(2, 0)  # was False (terminated) before the fix
        assert c.add_waypoint(3, 0)
        from ui.canvas.chip_canvas import Tool
        assert c._tool is Tool.ROUTE_DRAW  # still drawing, not completed

    def test_route_markers_chip_scoped(self, qapp, catalog):
        """Regression: a block on chip 1 must NOT blank a chip-0 route's marker
        at the same local coords (the route-marker block_cells set was global)."""
        from model.connection import BlockEndpoint, ChipPortEndpoint
        from ui.canvas.cell_item import CellKind

        ctrl = AppController(catalog=catalog)
        ctrl.new_project("T", "kyttar_10x12")
        ctrl.place_block("GainBlock", 0, 0, 0, library="lattrex.official")
        ctrl.add_chip("RX-2")
        ctrl.place_block("DCBlockerBlock", 1, 2, 0, library="lattrex.official")
        g = ctrl.project.blocks[0].name
        ctrl.add_route(BlockEndpoint(g, "out"), ChipPortEndpoint(0, "x16_out"),
                       [(x, 0) for x in range(10)])
        from ui.main_window import MainWindow

        w = MainWindow(controller=ctrl)
        w._after_project_loaded()
        QApplication.processEvents()
        markers = {(it.cx, it.cy) for it in w.canvas.cell_items()
                   if it.kind is CellKind.TRANSIT
                   and getattr(it, "chip_id", None) == 0}
        # (2,0) — coincident with chip 1's block — is still drawn (no void).
        assert (2, 0) in markers

    def test_delete_inter_chip_wire(self, qapp, catalog):
        from ui.canvas.inter_chip_wire_item import InterChipWireItem

        w, ctrl = self._window(catalog)
        ic = ctrl.add_inter_chip(0, "x16_out", 1, "x16_in")
        QApplication.processEvents()
        w._on_delete_inter_chip(ic)
        QApplication.processEvents()
        assert not ctrl.project.inter_chip_connections
        wires = [it for it in w.canvas._scene.items()
                 if isinstance(it, InterChipWireItem)]
        assert not wires


class TestInterChipSim:
    """End-to-end 2-chip simulation through the inter-chip relay (#189): a gain
    block on chip0 feeds chip1's gain over the x16_out->x16_in wire. The relay
    is a DECOUPLED VALUE relay (validated model) AND no-FIFO single-packet: the
    source output never accumulates a buffer."""

    CT = CHIP_YAML

    def _gain_bitstream(self, catalog):
        from engine.build import BuildEngine
        from engine.io.chip_type_io import load_chip_type
        from engine.port_config import input_port_config
        demo = Path(__file__).parent / "data" / "demo" / "gain_demo.kyt"
        ctrl = AppController(catalog=catalog)
        ctrl.open_project(str(demo))
        ct = load_chip_type(str(self.CT))
        res = BuildEngine(catalog, str(self.CT)).build(
            ctrl.project, {ctrl.project.chip_type: ct})
        _port, kw = input_port_config(ctrl.project, ctrl.registry, catalog)
        return list(res.words(0)), kw

    def test_gain_pipeline_through_relay_no_fifo(self, qapp, catalog):
        if not self.CT.exists():
            pytest.skip("chip-type yaml absent")
        from engine.simulator import MultiChipSimEngine, _chip_name
        gw, kw = self._gain_bitstream(catalog)
        eng = MultiChipSimEngine({0: str(self.CT), 1: str(self.CT)})
        eng.connect(0, "x16_out", 1, "x16_in")
        eng.load(0, gw)
        eng.load(1, gw)
        for cid in (0, 1):
            eng.configure_input_port(
                cid, "x16_in", entry_addr=kw["entry_addr"],
                hop_count=kw["hop_count"], data_addr=kw["data_addr"])
        eng.inject(0, "x16_in", [0x4000, 0x2000, 0x6000])
        c0 = _chip_name(0)
        max_buf = 0
        for _ in range(3000):
            eng.run(64, rounds=1)
            try:
                max_buf = max(max_buf, eng._sim.output_available(c0, "x16_out"))
            except Exception:  # noqa: BLE001
                pass
        out = [v & 0xFFFF for v in eng.capture(1, "x16_out")]
        # gain 0.5 on BOTH chips → 0.25x each input.
        assert out == [0x1000, 0x800, 0x1800]
        # NO FIFO on the wire: the source output port never accumulates a buffer.
        assert max_buf <= 1
