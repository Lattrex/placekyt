"""Data-vs-instruction classification + v2 resolved I/O (§3.3).

placeKYT builds via the v2 block path. v2 CellPrograms declare DataWord/StateVar,
so the Inspector can tell DATA words (coefficients — values that merely live in
memory) from executable instructions, even when a data word's bits match a
WRITE/JUMP opcode. The chip input port must be configured with the RESOLVED
entry + input register, not the static interface defaults.
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

from tests.conftest import CHIP_YAML as CT_PATH  # noqa: E402
pytestmark = pytest.mark.skipif(not CT_PATH.exists(), reason="chip yaml absent")


@pytest.fixture(scope="module")
def qapp():
    return QApplication.instance() or QApplication([])


@pytest.fixture(scope="module")
def catalog():
    return BlockCatalog.from_gr_kyttar()


def _decimator(catalog):
    ctrl = AppController(catalog=catalog)
    ctrl.new_project("Data", "kyttar_10x12")
    ctrl.place_block("DecimatorBlock", 0, 3, 2, library="lattrex.official")
    assert ctrl.build().ok
    return ctrl


class TestClassification:
    def test_coefficient_is_data_not_instruction(self, qapp, catalog):
        ctrl = _decimator(catalog)
        prog = ctrl.cell_program(0, 3, 2)
        classes = prog["classes"]
        # The coefficient c0 (Q15 ~1.0 = 0x7fff) is data, even though its bits
        # disassemble as a bogus JUMP.
        data_addrs = [a for a, c in classes.items() if c["role"] == "data"]
        assert data_addrs
        for a in data_addrs:
            assert classes[a]["name"]  # data words are named

    def test_data_words_excluded_from_handoff(self, qapp, catalog):
        ctrl = _decimator(catalog)
        prog = ctrl.cell_program(0, 3, 2)
        instr_addrs = {i["addr"] for i in prog["instructions"]}
        data_addrs = {a for a, c in prog["classes"].items()
                      if c["role"] in ("data", "state", "input", "output")}
        # No data/state address appears as a configurable handoff instruction.
        assert not (instr_addrs & data_addrs)

    def test_handoff_only_real_write_jump(self, qapp, catalog):
        ctrl = _decimator(catalog)
        prog = ctrl.cell_program(0, 3, 2)
        kinds = {i["kind"] for i in prog["instructions"]}
        assert kinds <= {"WRITE", "JUMP"}
        # The real WRITE/JUMP live at instruction addresses.
        for i in prog["instructions"]:
            assert prog["classes"][i["addr"]]["role"] == "instruction"


class TestWriteConfigOverride:
    def test_config_bit_set_on_override(self, qapp, catalog):
        ctrl = _decimator(catalog)
        prog = ctrl.cell_program(0, 3, 2)
        cid = prog["cell_id"]
        waddr = next(i["addr"] for i in prog["instructions"]
                     if i["kind"] == "WRITE")
        ctrl.set_instr_override("decimator", cid, waddr, dest=1, dest_config=True)
        prog2 = ctrl.cell_program(0, 3, 2)
        wi = next(i for i in prog2["instructions"] if i["addr"] == waddr)
        assert wi["field"] == 1 and wi["field_config"] is True
        # Clearing config reverts the bit.
        ctrl.set_instr_override("decimator", cid, waddr, dest_config=False)
        prog3 = ctrl.cell_program(0, 3, 2)
        wi3 = next(i for i in prog3["instructions"] if i["addr"] == waddr)
        assert wi3["field_config"] is False


class TestMultiCellBlocks:
    def test_dfe_builds(self, qapp, catalog):
        # The DFE uses STRING cell_ids ("ff0".."ffN"); the Router must index its
        # placed cells positionally, not by the (string) cell_programs key.
        ctrl = AppController(catalog=catalog)
        ctrl.new_project("DFE", "kyttar_10x12")
        ctrl.place_block("DFEEqualizerBlock", 0, 1, 1, library="lattrex.official")
        res = ctrl.build()
        assert res.ok, [str(e) for e in res.errors]
        programmed = sum(1 for c in res.chips[0].cells.values()
                         if not c["routing_only"])
        assert programmed >= 40  # full serpentine resolved

    def test_all_catalog_blocks_build(self, qapp, catalog):
        # Every catalog block must place + build via the v2 path (excluded
        # blocks — Viterbi, BlockInterleaver — are already out of the catalog).
        failures = []
        for spec in catalog.all():
            ctrl = AppController(catalog=catalog)
            ctrl.new_project("B", "kyttar_10x12")
            # Place each block at an origin that keeps its full footprint on the
            # 10x12 grid. Most blocks are small enough to sit at (1,1), but a
            # full-fabric-width block (e.g. the 10-wide CoherentRXBlock) has no
            # placement freedom and must sit at column 0. Derive the origin from
            # the block's default_layout footprint instead of hardcoding (1,1).
            ox = oy = 1
            try:
                layout = catalog.default_layout(spec.type_name,
                                                library=spec.library)
            except Exception:  # noqa: BLE001
                layout = {}
            if layout:
                max_dx = max(dx for (dx, _dy, *_f) in layout.values())
                max_dy = max(dy for (_dx, dy, *_f) in layout.values())
                ox = min(1, 10 - 1 - max_dx)
                oy = min(1, 12 - 1 - max_dy)
                ox = max(0, ox)
                oy = max(0, oy)
            try:
                ctrl.place_block(spec.type_name, 0, ox, oy, library=spec.library)
                res = ctrl.build()
                if not res.ok:
                    failures.append((spec.type_name,
                                     [str(e) for e in res.errors][:1]))
            except Exception as exc:  # noqa: BLE001
                failures.append((spec.type_name, str(exc)))
        assert not failures, failures


class TestAbutmentHops:
    def test_unrouted_block_exit_is_hop1(self, qapp, catalog):
        # A placed block with no outgoing route hands off @1 (abut to the next
        # cell), NOT the Router's sink-to-port fallback distance.
        ctrl = AppController(catalog=catalog)
        ctrl.new_project("Abut", "kyttar_10x12")
        ctrl.place_block("GainBlock", 0, 2, 0, library="lattrex.official")
        assert ctrl.build().ok
        prog = ctrl.cell_program(0, 2, 0)
        for instr in prog["instructions"]:
            assert instr["hop"] == 1

    def test_abutting_handoff_resolves_entry_and_dest(self, qapp, catalog):
        # Two abutting blocks (no explicit route): the source's exit JUMP entry
        # and WRITE dest auto-resolve to the downstream block's resolved values.
        for down in ("NCOBlock", "SquelchBlock", "DCBlockerBlock"):
            ctrl = AppController(catalog=catalog)
            ctrl.new_project("Abut", "kyttar_10x12")
            ctrl.place_block("GainBlock", 0, 0, 0, library="lattrex.official")
            ctrl.place_block(down, 0, 1, 0, library="lattrex.official")
            assert ctrl.build().ok
            prog = ctrl.cell_program(0, 0, 0)
            jump = next(i for i in prog["instructions"] if i["kind"] == "JUMP")
            write = next(i for i in prog["instructions"] if i["kind"] == "WRITE")
            entry, in_regs = catalog.resolved_io(down, library="lattrex.official")
            assert jump["hop"] == 1 and write["hop"] == 1
            assert jump["field"] == entry, down
            assert write["field"] == in_regs[0], down

    def test_multicell_internal_hops_follow_placement(self, qapp, catalog):
        # A 13-tap FIR is a 3-cell abutting chain: internal handoffs are @1.
        ctrl = AppController(catalog=catalog)
        ctrl.new_project("FIR", "kyttar_10x12")
        ctrl.place_block("FIRFilterBlock", 0, 0, 0, library="lattrex.official",
                         params={"coefficients": [0.1] * 13})
        assert ctrl.build().ok
        hops = set()
        for (x, y), c in ctrl._build_cache.chips[0].cells.items():
            if c.get("block") and not c["routing_only"]:
                for i in ctrl.cell_program(0, x, y)["instructions"]:
                    hops.add(i["hop"])
        # All chain cells abut → @1 (no @8 sink fallback leaking in).
        assert hops == {1}

    def test_multicell_fir_flows_correctly(self, qapp, catalog):
        # End-to-end: the multi-cell FIR's internal multi-signal chain
        # (sample/partial) routes to the right registers and produces output.
        from engine.registry import ChipTypeRegistry
        from engine.simulator import SimulationEngine
        from model.connection import BlockEndpoint, ChipPortEndpoint

        reg = ChipTypeRegistry()
        reg.register_file(str(CT_PATH))
        ctrl = AppController(catalog=catalog)
        ctrl.new_project("FIR", "kyttar_10x12")
        ctrl.place_block("FIRFilterBlock", 0, 0, 0, library="lattrex.official",
                         params={"coefficients": [0.1] * 13})
        b = ctrl.project.blocks[0].name
        # The multi-cell FIR FOLDS (INV-8): its output egresses the LAST cell,
        # whose position depends on the fold, so don't hardcode waypoints — wire
        # the logical net and let the auto-router source it from the real exit
        # cell (it resolves the PortMap WITH params, INV-11).
        ctrl.add_logical_connection(
            BlockEndpoint(b, "out"), ChipPortEndpoint(0, "x16_out"), name="out")
        ctrl.auto_route_all()
        assert ctrl.project.connection("out").is_routed, "FIR output did not route"
        res = ctrl.build()
        assert res.ok, [str(e) for e in res.errors]
        entry, in_regs = catalog.resolved_io(
            "FIRFilterBlock", {"coefficients": [0.1] * 13})
        sim = SimulationEngine(str(CT_PATH))
        sim.load(res.words(0))
        sim.configure_input_port("x16_in", entry_addr=entry, hop_count=30,
                                 data_addr=in_regs[0])
        sim.inject("x16_in", [0x4000] * 30)
        sim.run_until_output("x16_out", 20)
        out = sim.capture("x16_out")
        # steady state ≈ 13 * 0.1 * 0.5 = 0.65 → ~0x5333 (within a few LSBs).
        assert abs(out[-1] - 0x5333) < 0x40


class TestEditableParams:
    def test_dfe_only_step_size_editable(self, qapp, catalog):
        # forward_taps/feedback_taps change geometry (topology), forgetting_factor
        # changes nothing → not editable. Only step_size maps to a data word.
        editable = catalog.editable_params("DFEEqualizerBlock")
        assert "step_size" in editable
        assert "forward_taps" not in editable
        assert "feedback_taps" not in editable

    def test_gain_and_dcblocker_editable(self, qapp, catalog):
        assert "gain" in catalog.editable_params("GainBlock")
        assert "alpha" in catalog.editable_params("DCBlockerBlock")


class TestExcludedBlocks:
    def test_interleaver_absent(self, qapp, catalog):
        assert catalog.get("BlockInterleaverBlock") is None


class TestResolvedIO:
    def test_gain_resolved_entry_and_input(self, qapp, catalog):
        # v2 gain reads from R0 and entry is packed high (not the static
        # interface's R31 / entry 1).
        entry, in_regs = catalog.resolved_io("GainBlock")
        assert in_regs == (0,)
        assert entry > 1  # instructions packed at the top of memory

    def test_dfe_landing_entry_and_inputs(self, qapp, catalog):
        # The DFE lands on a STRING-keyed cell ("ff0") whose resolved entry is
        # 15 and inputs are R5–R7 — NOT the static interface (entry 1 / R31).
        entry, in_regs = catalog.resolved_io("DFEEqualizerBlock")
        assert entry == 15
        assert in_regs and in_regs[0] == 5

    def test_handoff_to_dfe_resolves_entry_and_input(self, qapp, catalog):
        from model.connection import BlockEndpoint

        ctrl = AppController(catalog=catalog)
        ctrl.new_project("Chain", "kyttar_10x12")
        ctrl.place_block("GainBlock", 0, 0, 0, library="lattrex.official")
        ctrl.place_block("DFEEqualizerBlock", 0, 1, 6, library="lattrex.official")
        g, d = (b.name for b in ctrl.project.blocks)
        ctrl.add_route(BlockEndpoint(g, "out"), BlockEndpoint(d, "in"),
                       [(0, 0), (0, 1), (1, 1), (1, 6)])
        assert ctrl.build().ok
        prog = ctrl.cell_program(0, 0, 0)
        jump = next(i for i in prog["instructions"] if i["kind"] == "JUMP")
        write = next(i for i in prog["instructions"] if i["kind"] == "WRITE")
        # The built words land at the DFE's resolved entry / input register.
        assert jump["field"] == 15
        assert write["field"] == 5

    def test_viterbi_absent(self, qapp, catalog):
        assert catalog.get("ViterbiK7DecoderBlock") is None


class TestParamsEditor:
    def test_param_edit_reresolves_data_word(self, qapp, catalog):
        from ui.panels.inspector_panel import InspectorPanel

        ctrl = AppController(catalog=catalog)
        ctrl.new_project("P", "kyttar_10x12")
        ctrl.place_block("GainBlock", 0, 0, 0, library="lattrex.official",
                         params={"gain": 0.5})
        gname = ctrl.project.blocks[0].name
        insp = InspectorPanel(controller=ctrl)
        insp.set_project(ctrl.project)
        insp.params_changed.connect(lambda b, p: ctrl.edit_params(b, p))
        insp.show_selection({"cell": (0, 0), "kind": "block", "block": gname,
                             "chip": 0, "cell_id": 0, "face": "east"})
        # Edit gain 0.5 → 0.25; the data word re-resolves to Q15 0.25 = 0x2000.
        edit = insp._param_edits["gain"]
        edit.setText("0.25")
        insp._on_param_edited("gain", edit, "float")
        from PySide6.QtWidgets import QApplication
        QApplication.processEvents()
        assert ctrl.project.block(gname).params["gain"] == 0.25
        prog = ctrl.cell_program(0, 0, 0)
        data_vals = [prog["memory"][a] for a, c in prog["classes"].items()
                     if c["role"] == "data"]
        assert 0x2000 in data_vals
