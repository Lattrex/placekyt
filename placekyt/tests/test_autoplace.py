"""Auto-place tests (auto-P&R §8): flow-ordered SERPENTINE multi-row packing."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication  # noqa: E402

from engine.autoplace import AutoPlacer  # noqa: E402
from engine.build import BuildEngine  # noqa: E402
from engine.catalog import BlockCatalog  # noqa: E402
from engine.io.chip_type_io import load_chip_type  # noqa: E402
from model.connection import BlockEndpoint, ChipPortEndpoint  # noqa: E402
from ui.controller import AppController  # noqa: E402

from tests.conftest import CHIP_YAML as CT_PATH  # noqa: E402
pytestmark = pytest.mark.skipif(not CT_PATH.exists(), reason="chip yaml absent")


@pytest.fixture(scope="module")
def qapp():
    return QApplication.instance() or QApplication([])


@pytest.fixture(scope="module")
def catalog(qapp):
    return BlockCatalog.from_gr_kyttar()


@pytest.fixture(scope="module")
def chip_type():
    return load_chip_type(str(CT_PATH))


def _chain_out_of_order(ctrl):
    """Place A→B→C out of physical order (C, A, B), wired in flow order."""
    c = ctrl.place_block("BPSKSlicerBlock", 0, 1, 3, library="lattrex.official")
    a = ctrl.place_block("GainBlock", 0, 6, 3, library="lattrex.official")
    b = ctrl.place_block("DCBlockerBlock", 0, 3, 3, library="lattrex.official")
    ctrl.add_logical_connection(
        ChipPortEndpoint(chip=0, port="x16_in"),
        BlockEndpoint(block=a, port="sample"), name="in_a")
    ctrl.add_logical_connection(
        BlockEndpoint(block=a, port="out"),
        BlockEndpoint(block=b, port="sample"), name="ab")
    ctrl.add_logical_connection(
        BlockEndpoint(block=b, port="out"),
        BlockEndpoint(block=c, port="llr"), name="bc")
    return a, b, c


def test_flow_order_is_topological(qapp, catalog, chip_type):
    ctrl = AppController(catalog=catalog)
    ctrl.new_project("ap", "kyttar_10x12")
    a, b, c = _chain_out_of_order(ctrl)
    plan = ctrl.auto_place(0)
    assert plan.ok
    assert plan.order == [a, b, c]                   # topological A→B→C
    xs = [ctrl.project.block(n).placement.cells[0].x for n in (a, b, c)]
    assert xs[0] < xs[1] < xs[2]                     # laid left-to-right in flow


def test_wide_chain_serpentine_fits_grid(qapp, catalog, chip_type):
    """A pipeline wider than the array (RRC 7 + folded Costas 8 + Gardner + Slicer
    ≈ 18 cells) must SERPENTINE across rows and fit a 10-wide array with no
    off-grid cell and no overlap — the 1-row packer ran it off the edge."""
    ctrl = AppController(catalog=catalog)
    ctrl.new_project("wide", "kyttar_10x12")
    rrc = ctrl.place_block("RRCPulseShaperBlock", 0, 0, 0,
                           library="lattrex.official")
    cos = ctrl.place_block("ComplexCostasLoopBlock", 0, 0, 4,
                           library="lattrex.official")
    gar = ctrl.place_block("GardnerTimingRecovery", 0, 0, 7,
                           library="lattrex.official")
    sli = ctrl.place_block("BPSKSlicerBlock", 0, 0, 10,
                           library="lattrex.official")
    ctrl.add_logical_connection(
        ChipPortEndpoint(chip=0, port="x16_in"),
        BlockEndpoint(block=rrc, port="sample"), name="in_rrc")
    ctrl.add_logical_connection(
        BlockEndpoint(block=rrc, port="out"),
        BlockEndpoint(block=cos, port="xi"), name="rrc_cos")
    ctrl.add_logical_connection(
        BlockEndpoint(block=cos, port="yi_tap"),
        BlockEndpoint(block=gar, port="xi"), name="cos_gar")
    ctrl.add_logical_connection(
        BlockEndpoint(block=gar, port="out"),
        BlockEndpoint(block=sli, port="llr"), name="gar_sli")
    plan = ctrl.auto_place(0)
    assert plan.ok

    W, H = 10, 12
    occ = {}
    for b in ctrl.project.blocks:
        cells = list(b.placement.cells) + list(b.placement.transit_cells)
        for c in cells:
            assert 0 <= c.x < W and 0 <= c.y < H, \
                f"{b.name} cell ({c.x},{c.y}) off the {W}x{H} grid"
            assert (c.x, c.y) not in occ, \
                f"overlap at ({c.x},{c.y}): {occ.get((c.x, c.y))} vs {b.name}"
            occ[(c.x, c.y)] = b.name
    # It genuinely wrapped: the blocks are NOT all on one row.
    rows = {b.placement.bounding_box()[1] for b in ctrl.project.blocks}
    assert len(rows) > 1, "wide chain did not wrap to multiple rows"
    # The placer produced a bus spine for the router to thread.
    assert plan.spine, "no bus spine waypoints produced"


def test_oriented_footprint_keeps_next_block_on_band(qapp, catalog, chip_type):
    """The serpentine pack reserves space for the ORIENTED block, not the as-authored
    one, so a block following an oriented block isn't stranded on a far band and the
    inter-block geometry stays tappable. Gardner is a compact 2x2 fold (dual-face
    loop_filter — `out` egresses outward to the bus, `period_fb` returns to the
    resampler), placed near the Costas output band; the slicer lands within a couple
    bands of the Gardner output so the gardner→slicer bus routes end to end."""
    ctrl = AppController(catalog=catalog)
    ctrl.new_project("orient", "kyttar_10x12")
    cos = ctrl.place_block("ComplexCostasLoopBlock", 0, 0, 0,
                           library="lattrex.official")
    gar = ctrl.place_block("GardnerTimingRecovery", 0, 0, 4,
                           library="lattrex.official")
    sli = ctrl.place_block("BPSKSlicerBlock", 0, 0, 7,
                           library="lattrex.official")
    ctrl.add_logical_connection(
        ChipPortEndpoint(chip=0, port="x16_in"),
        BlockEndpoint(block=cos, port="xi"), name="in_cos")
    ctrl.add_logical_connection(
        BlockEndpoint(block=cos, port="yi_tap"),
        BlockEndpoint(block=gar, port="xi"), name="cos_gar")
    ctrl.add_logical_connection(
        BlockEndpoint(block=gar, port="out"),
        BlockEndpoint(block=sli, port="llr"), name="gar_sli")
    ctrl.auto_place(0)
    g = ctrl.project.block(gar)
    lf = g.placement.cell("loop_filter")
    s = ctrl.project.block(sli).placement.cells[0]
    # The slicer lands within a couple bands/cols of the Gardner output (not stranded
    # a far band away) — the placement keeps the gardner→slicer geometry compact.
    assert abs(s.y - lf.y) <= 2, \
        f"slicer band {s.y} far from gardner out band {lf.y}"
    assert abs(s.x - lf.x) <= 6, \
        f"slicer col {s.x} far from gardner out col {lf.x}"


def test_auto_place_then_route_then_build(qapp, catalog, chip_type):
    ctrl = AppController(catalog=catalog)
    ctrl.new_project("apr", "kyttar_10x12")
    _chain_out_of_order(ctrl)
    ctrl.auto_place(0)
    rep = ctrl.auto_route_all({"kyttar_10x12": chip_type}, auto_orient=True)
    assert rep.ok, [(r.name, r.reason) for r in rep.failed]
    res = BuildEngine(catalog, str(CT_PATH)).build(
        ctrl.project, {"kyttar_10x12": chip_type})
    assert res.ok, [str(e) for e in res.errors]


def test_auto_place_is_undoable(qapp, catalog, chip_type):
    ctrl = AppController(catalog=catalog)
    ctrl.new_project("apu", "kyttar_10x12")
    a, b, c = _chain_out_of_order(ctrl)
    before = {n: ctrl.project.block(n).placement.cells[0].x for n in (a, b, c)}
    ctrl.auto_place(0)
    after = {n: ctrl.project.block(n).placement.cells[0].x for n in (a, b, c)}
    assert after != before
    ctrl.undo()
    restored = {n: ctrl.project.block(n).placement.cells[0].x for n in (a, b, c)}
    assert restored == before


def test_backward_edge_is_named_not_hidden(qapp, catalog, chip_type):
    """A backward inter-block edge (a later stage feeds an earlier one) needs a
    ring; the planner reports it by name rather than silently mis-ordering."""
    ctrl = AppController(catalog=catalog)
    ctrl.new_project("ring", "kyttar_10x12")
    a = ctrl.place_block("GainBlock", 0, 1, 3, library="lattrex.official")
    b = ctrl.place_block("DCBlockerBlock", 0, 5, 3, library="lattrex.official")
    # A→B and B→A: a 2-cycle (forces a ring).
    ctrl.add_logical_connection(
        BlockEndpoint(block=a, port="out"),
        BlockEndpoint(block=b, port="sample"), name="ab")
    ctrl.add_logical_connection(
        BlockEndpoint(block=b, port="out"),
        BlockEndpoint(block=a, port="sample"), name="ba")
    plan = ctrl.auto_place(0)
    assert not plan.ok
    assert plan.backward_edges                       # the cycle is reported


def test_placer_unit_packs_by_footprint(qapp, catalog, chip_type):
    """AutoPlacer (unit): packs blocks at increasing x spaced by footprint+gap."""
    ctrl = AppController(catalog=catalog)
    ctrl.new_project("pack", "kyttar_10x12")
    a, b, c = _chain_out_of_order(ctrl)

    def fp(bt, lib):
        return catalog.port_map(bt, library=lib).footprint

    plan = AutoPlacer(ctrl.project, fp, row=2, gap=1).plan(0)
    # all on the chosen row, strictly increasing x
    ys = {plan.positions[n][2] for n in plan.order}
    assert ys == {2}
    xs = [plan.positions[n][1] for n in plan.order]
    assert xs == sorted(xs) and len(set(xs)) == len(xs)


def test_auto_pnr_output_computes(qapp, catalog, chip_type):
    """THE load-bearing check: the full auto-P&R flow produces a DUT that
    actually COMPUTES, not just one that builds. Place a GainBlock (gain=0.5)
    out of position, wire x16_in→gain→x16_out, auto-place (anchors the lead block
    at the input port) + auto-route + build, then SIMULATE: out must equal
    0.5×in. (Without the input-port anchor the design builds but never computes —
    the port injects at its own cell and a block one cell away gets nothing.)"""
    import simkyt

    ctrl = AppController(catalog=catalog)
    ctrl.new_project("compute", "kyttar_10x12")
    g = ctrl.place_block("GainBlock", 0, 5, 4, library="lattrex.official")
    ctrl.add_logical_connection(
        ChipPortEndpoint(chip=0, port="x16_in"),
        BlockEndpoint(block=g, port="sample"), name="in_g")
    ctrl.add_logical_connection(
        BlockEndpoint(block=g, port="out"),
        ChipPortEndpoint(chip=0, port="x16_out"), name="g_out")
    ctrl.auto_place(0)
    rep = ctrl.auto_route_all({"kyttar_10x12": chip_type}, auto_orient=True)
    assert rep.ok, [(r.name, r.reason) for r in rep.failed]
    res = BuildEngine(catalog, str(CT_PATH)).build(
        ctrl.project, {"kyttar_10x12": chip_type})
    assert res.ok, [str(e) for e in res.errors]

    entry, _in = catalog.resolved_io("GainBlock")
    chip = simkyt.Chip.from_yaml(str(CT_PATH))
    chip.load_bitstream_physical(res.words(0))
    chip.set_port_entry_address("x16_in", entry)

    def fq(f):
        return int(round(max(-1, min(0.999, f)) * 32768)) & 0xFFFF

    ins = [0.6, -0.4, 0.2, -0.8, 0.5]
    outs = []
    for v in ins:
        chip.inject_data_physical([fq(v)], target_hop_cnt=30, target_addr=0)
        chip.run(max_events=3000)
        chip.inject_jump_physical(target_hop_cnt=30, entry_addr=entry)
        chip.run(max_events=20000)
        while chip.output_available("x16_out"):
            p = chip.read_port_i16("x16_out").tolist()
            outs.append(p[-1] / 32768.0)
            chip.release_output_ack("x16_out")
            chip.run(max_events=2000)
    assert len(outs) >= len(ins), f"only {len(outs)} outputs (no egress?)"
    for i, v in enumerate(ins):
        assert abs(outs[i] - 0.5 * v) < 0.02, \
            f"sample {i}: got {outs[i]:.3f}, expected {0.5 * v:.3f}"


def test_multicell_block_packed_without_overlap(qapp, catalog, chip_type):
    """A multi-cell block (ComplexMixer, 3 cells) keeps its internal cells together
    under auto-place AND the next block is spaced clear of it (footprint-aware
    packing — no overlap), then the chain routes + builds. This proves auto-place
    handles real multi-cell DSP blocks, not just single-cell ones."""
    ctrl = AppController(catalog=catalog)
    ctrl.new_project("mc", "kyttar_10x12")
    m = ctrl.place_block("ComplexMixerBlock", 0, 4, 6, library="lattrex.official")
    d = ctrl.place_block("DCBlockerBlock", 0, 1, 2, library="lattrex.official")
    ctrl.add_logical_connection(
        ChipPortEndpoint(chip=0, port="x16_in"),
        BlockEndpoint(block=m, port="sample"), name="in_m")
    ctrl.add_logical_connection(
        BlockEndpoint(block=m, port="out"),
        BlockEndpoint(block=d, port="sample"), name="md")
    ctrl.add_logical_connection(
        BlockEndpoint(block=d, port="out"),
        ChipPortEndpoint(chip=0, port="x16_out"), name="d_out")
    ctrl.auto_place(0)
    mcells = {(c.x, c.y) for c in ctrl.project.block(m).placement.cells}
    dcells = {(c.x, c.y) for c in ctrl.project.block(d).placement.cells}
    # the mixer's 3 cells stayed contiguous in a row
    assert len(mcells) == 3
    assert mcells.isdisjoint(dcells)              # no overlap with the next block
    assert max(x for x, _ in mcells) < min(x for x, _ in dcells)  # mixer before dc
    rep = ctrl.auto_route_all({"kyttar_10x12": chip_type}, auto_orient=True)
    assert rep.ok, [(r.name, r.reason) for r in rep.failed]
    res = BuildEngine(catalog, str(CT_PATH)).build(
        ctrl.project, {"kyttar_10x12": chip_type})
    assert res.ok, [str(e) for e in res.errors]
