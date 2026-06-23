"""Build honors connection routes + per-instruction WRITE/JUMP overrides (§3.3).

The hop count and destination/entry address of a WRITE/JUMP are properties of
the INSTRUCTION, not the route (the route is passive). They auto-fill from the
routed distance + the downstream block interface, and the user may override any
of them per instruction; overrides live on the block's placement and round-trip
through the ``.kyt`` file.

Offscreen Qt (controller builds via the catalog/simkyt).
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
pytest.importorskip("PySide6")

from PySide6.QtWidgets import QApplication  # noqa: E402

from engine.catalog import BlockCatalog  # noqa: E402
from model.connection import BlockEndpoint  # noqa: E402
from ui.controller import AppController  # noqa: E402

from tests.conftest import CHIP_YAML as CT_PATH  # noqa: E402
pytestmark = pytest.mark.skipif(not CT_PATH.exists(), reason="chip yaml absent")


@pytest.fixture(scope="module")
def qapp():
    return QApplication.instance() or QApplication([])


@pytest.fixture(scope="module")
def catalog():
    return BlockCatalog.from_gr_kyttar()


@pytest.fixture(scope="module")
def chip_type():
    from engine.io.chip_type_io import load_chip_type
    return load_chip_type(str(CT_PATH))


def _two_gain_routed(catalog):
    ctrl = AppController(catalog=catalog)
    ctrl.new_project("Hops", "kyttar_10x12")
    ctrl.place_block("GainBlock", 0, 1, 1, library="lattrex.official")
    ctrl.place_block("GainBlock", 0, 1, 5, library="lattrex.official")
    g1, g2 = (b.name for b in ctrl.project.blocks)
    ctrl.add_route(BlockEndpoint(g1, "out"), BlockEndpoint(g2, "in"),
                   [(1, i) for i in range(1, 6)])
    return ctrl, g1, g2


def _src_cell_id(ctrl, block_name):
    """cell_id of the source block's cell sitting at (1, 1)."""
    info = ctrl.cell_program(0, 1, 1)
    return info["cell_id"]


def _write_instr(ctrl, x, y):
    """The first WRITE instruction's handoff metadata in a cell's program."""
    prog = ctrl.cell_program(0, x, y)
    for instr in prog["instructions"]:
        if instr["kind"] == "WRITE":
            return instr
    return None


class TestBuildHonorsRoutes:
    def test_hop_at_n_matches_routed_distance(self, qapp, catalog):
        ctrl, _g1, _g2 = _two_gain_routed(catalog)
        # block→block delivery is now ALWAYS BROKERED (AUTO_PNR_DESIGN §1.2): the user
        # draws the route ENDING ON the target's input cell (1,5); the build makes the
        # cell BEFORE it (1,4) a BROKER that relays the burst @1 into the input. So g1's
        # source WRITE addresses the BROKER at @3 (route [(1,1)..(1,4)] physical), not
        # @4 into the block. The broker's @1 relay covers the final hop.
        assert _write_instr(ctrl, 1, 1)["hop"] == 3

    def test_dest_autofills_from_downstream_interface(self, qapp, catalog):
        ctrl, _g1, g2 = _two_gain_routed(catalog)
        tb = ctrl.project.block(g2)
        # The WRITE dest auto-fills to the downstream block's RESOLVED input
        # register (v2 — gain reads R0, not the static interface's R31).
        _entry, in_regs = ctrl.catalog.resolved_io(tb.type, tb.params,
                                                   library=tb.library)
        assert _write_instr(ctrl, 1, 1)["field"] in in_regs


class TestInstrOverride:
    def test_hop_override_changes_at_n(self, qapp, catalog):
        ctrl, g1, _g2 = _two_gain_routed(catalog)
        cid = _src_cell_id(ctrl, g1)
        addr = _write_instr(ctrl, 1, 1)["addr"]
        ctrl.set_instr_override(g1, cid, addr, hop=6)
        assert _write_instr(ctrl, 1, 1)["hop"] == 6
        # clearing restores the auto distance — the BROKER hop (@3 to the cell before
        # the target input cell), since block→block delivery is always brokered.
        ctrl.set_instr_override(g1, cid, addr, hop=None)
        assert _write_instr(ctrl, 1, 1)["hop"] == 3

    def test_dest_override_changes_field(self, qapp, catalog):
        ctrl, g1, _g2 = _two_gain_routed(catalog)
        cid = _src_cell_id(ctrl, g1)
        addr = _write_instr(ctrl, 1, 1)["addr"]
        ctrl.set_instr_override(g1, cid, addr, dest=30)
        assert _write_instr(ctrl, 1, 1)["field"] == 30

    def test_override_is_undoable(self, qapp, catalog):
        ctrl, g1, _g2 = _two_gain_routed(catalog)
        cid = _src_cell_id(ctrl, g1)
        addr = _write_instr(ctrl, 1, 1)["addr"]
        ctrl.set_instr_override(g1, cid, addr, hop=6)
        assert ctrl.project.block(g1).placement.override(cid, addr).hop == 6
        ctrl.undo()
        assert ctrl.project.block(g1).placement.override(cid, addr) is None

    def test_override_roundtrips_through_yaml(self, qapp, catalog, tmp_path):
        from engine.io.project_io import load_project, save_project

        ctrl, g1, _g2 = _two_gain_routed(catalog)
        cid = _src_cell_id(ctrl, g1)
        addr = _write_instr(ctrl, 1, 1)["addr"]
        ctrl.set_instr_override(g1, cid, addr, hop=7, dest=29)
        fp = tmp_path / "hops.kyt"
        save_project(ctrl.project, fp)
        reloaded = load_project(fp)
        ov = reloaded.block(g1).placement.override(cid, addr)
        assert ov.hop == 7 and ov.dest == 29


class TestInspectorHandoffUI:
    def _window(self, catalog):
        from ui.main_window import MainWindow

        ctrl, _g1, _g2 = _two_gain_routed(catalog)
        w = MainWindow(controller=ctrl)
        w._after_project_loaded()
        return w, ctrl

    def test_block_cell_shows_handoff_editor(self, qapp, catalog):
        w, ctrl = self._window(catalog)
        cid = _src_cell_id(ctrl, ctrl.project.blocks[0].name)
        w.inspector.show_selection({
            "cell": (1, 1), "kind": "block cell",
            "block": ctrl.project.blocks[0].name, "chip": 0,
            "cell_id": cid, "face": "east"})
        QApplication.processEvents()
        # Editor rows = number of WRITE/JUMP in the cell (≥ 1).
        assert len(w.inspector._program._editors) >= 1

    def test_handler_sets_override(self, qapp, catalog):
        w, ctrl = self._window(catalog)
        g1 = ctrl.project.blocks[0].name
        cid = _src_cell_id(ctrl, g1)
        addr = _write_instr(ctrl, 1, 1)["addr"]
        w._on_instr_override(g1, cid, addr, "hop", 9)
        assert ctrl.project.block(g1).placement.override(cid, addr).hop == 9
        w._on_instr_override(g1, cid, addr, "hop", None)
        assert ctrl.project.block(g1).placement.override(cid, addr) is None

    def test_edit_preserves_selection(self, qapp, catalog):
        """An override edit rebuilds the scene; the Inspector must keep showing
        the same cell, not collapse to "No selection" (regression)."""
        from ui.canvas.cell_item import CellItem

        w, ctrl = self._window(catalog)
        g1 = ctrl.project.blocks[0].name
        cid = _src_cell_id(ctrl, g1)
        addr = _write_instr(ctrl, 1, 1)["addr"]
        # Select the source block cell at (1, 1).
        for it in w.canvas._scene.items():
            if (isinstance(it, CellItem) and (it.cx, it.cy) == (1, 1)
                    and it.kind.value == "block"):
                it.setSelected(True)
                break
        QApplication.processEvents()
        assert w.inspector._title.text() == "Cell (1, 1)"
        rows = len(w.inspector._program._editors)
        assert rows >= 1
        # Edit a handoff field → command → model change → render_scene().
        w._on_instr_override(g1, cid, addr, "hop", 6)
        QApplication.processEvents()
        # Selection (and the handoff editor) survive the rebuild.
        assert w.inspector._title.text() == "Cell (1, 1)"
        assert len(w.inspector._program._editors) == rows


class TestBrokeredDeliveryComputes:
    """block→block delivery is ALWAYS brokered: the user draws the route ENDING ON the
    target's input cell; the build makes the prior cell a BROKER that relays @1 into the
    input. These tests prove the brokered delivery is REAL (computes via simkyt), not
    just a renumbered hop, and that a route ending ON the target cell builds a broker at
    the cell before it."""

    def _gain_chain(self, catalog, chip_type):
        """x16_in → g1(gain) → g2(gain) → x16_out, auto-placed/routed, then the g1→g2
        route REDRAWN to END ON g2's input cell (the user's always-brokered case).
        g=0.5 each → out = 0.25·in. Returns (ctrl, g1, g2, g2_in_cell, broker_cell)."""
        from model.connection import ChipPortEndpoint
        ctrl = AppController(catalog=catalog)
        ctrl.new_project("brokered", "kyttar_10x12")
        g1 = ctrl.place_block("GainBlock", 0, 3, 3, library="lattrex.official")
        g2 = ctrl.place_block("GainBlock", 0, 6, 3, library="lattrex.official")
        ctrl.add_logical_connection(
            ChipPortEndpoint(chip=0, port="x16_in"),
            BlockEndpoint(block=g1, port="sample"), name="in_g1")
        ctrl.add_logical_connection(
            BlockEndpoint(block=g1, port="out"),
            BlockEndpoint(block=g2, port="in"), name="g1g2")
        ctrl.add_logical_connection(
            BlockEndpoint(block=g2, port="out"),
            ChipPortEndpoint(chip=0, port="x16_out"), name="g2_out")
        ctrl.auto_place(0)
        rep = ctrl.auto_route_all({"kyttar_10x12": chip_type}, auto_orient=True)
        assert rep.ok, [(r.name, r.reason) for r in rep.failed]
        # g2's placed input cell, and the auto-router's g1→g2 route.
        g2b = ctrl.project.block(g2)
        pm = catalog.port_map("GainBlock")
        incell_id = next(p.cell_id for p in pm.ports if p.direction == "in")
        g2c = g2b.placement.cell(incell_id)
        g2_in = (g2c.x, g2c.y)
        conn = next(c for c in ctrl.project.connections if c.name == "g1g2")
        pts = [(p.x, p.y) for p in conn.route]
        # REDRAW the route to END ON g2's input cell (extend the auto path by the final
        # hop into the cell) — exactly what the user did to Costas→Gardner.
        if pts[-1] != g2_in:
            pts = pts + [g2_in]
        ctrl.add_route(BlockEndpoint(g1, "out"), BlockEndpoint(g2, "in"), pts)
        broker = pts[-2]                       # the broker is the cell before g2's input
        return ctrl, g1, g2, g2_in, broker

    def test_broker_built_at_prior_cell(self, qapp, catalog, chip_type):
        """A hand-drawn route ending ON the target cell builds a BROKER at the prior
        cell — a programmed routing cell (kind 'broker'/_broker), NOT inside the block.
        The source addresses the BROKER (not one cell past it into the block); the
        broker's @1 relay covers the final hop into the input."""
        ctrl, _g1, g2, g2_in, broker = self._gain_chain(catalog, chip_type)
        bp = ctrl.cell_program(0, broker[0], broker[1])
        assert bp is not None
        # the prior cell carries a program (broker), distinct from a face-only transit.
        assert (bp.get("kind") == "broker") or (bp.get("block") or "").startswith("_broker")
        # the target cell is the block's own input cell, NOT the broker.
        gp = ctrl.cell_program(0, g2_in[0], g2_in[1])
        assert gp is not None and gp.get("block") == g2 and gp.get("kind") != "broker"

    def test_brokered_gain_chain_computes(self, qapp, catalog, chip_type):
        """The brokered g1→g2 delivery actually COMPUTES end to end: out = 0.25·in."""
        import simkyt
        from engine.build import BuildEngine

        ctrl, _g1, _g2, _gin, _brk = self._gain_chain(catalog, chip_type)
        res = BuildEngine(catalog, str(CT_PATH)).build(
            ctrl.project, {"kyttar_10x12": chip_type})
        assert res.ok, [str(e) for e in res.errors]

        entry, _in = catalog.resolved_io("GainBlock")
        chip = simkyt.Chip.from_yaml(str(CT_PATH))
        chip.load_bitstream_physical(res.words(0))
        chip.set_port_entry_address("x16_in", entry)

        def fq(f):
            return int(round(max(-1, min(0.999, f)) * 32768)) & 0xFFFF

        ins = [0.6, -0.4, 0.2, 0.8]
        outs = []
        for v in ins:
            chip.inject_data_physical([fq(v)], target_hop_cnt=30, target_addr=0)
            chip.run(max_events=4000)
            chip.inject_jump_physical(target_hop_cnt=30, entry_addr=entry)
            chip.run(max_events=40000)
            while chip.output_available("x16_out"):
                p = chip.read_port_i16("x16_out").tolist()
                outs.append(p[-1] / 32768.0)
                chip.release_output_ack("x16_out")
                chip.run(max_events=3000)
        assert len(outs) >= len(ins), f"only {len(outs)} outputs (broker didn't deliver?)"
        for i, v in enumerate(ins):
            assert abs(outs[i] - 0.25 * v) < 0.02, \
                f"sample {i}: got {outs[i]:.3f}, expected {0.25 * v:.3f} (gain·gain)"
