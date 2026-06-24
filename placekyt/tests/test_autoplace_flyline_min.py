# SPDX-License-Identifier: GPL-3.0-or-later
"""Flyline-minimising auto-orient (auto-P&R §8 / §4.3).

The placer chooses each block's D4 orientation to MINIMISE its total Manhattan
flyline to its real neighbours — the INPUT cell nearest its driver, the OUTPUT
cell nearest its consumer — instead of the old "output merely faces the travel
direction" heuristic. It also PREFERS the fold aspect that co-locates I/O on the
bus-facing edge when flyline ties.

These lock in:
  1. the orienter scores by neighbour flyline and picks the minimum (unit, on a
     synthetic PortMap so the geometry is controlled exactly);
  2. an io-colocated aspect wins a flyline TIE (the §4.3 cheap 1-D tap);
  3. a block with INTERNAL feedback is left as-authored (a D4 transform rotates
     its PortMap but not its hardcoded-face program → would break the loop);
  4. the real gain→FIR(20)→x16_out chain places without overlap, fits the array,
     ROUTES and BUILDS, with the FIR's input nearer its driver than its output is,
     and a total flyline no worse than the old output-only orientation produced.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication  # noqa: E402

from engine.autoplace import AutoPlacer  # noqa: E402
from engine.autoroute import suggest_flow_orientation  # noqa: E402
from engine.build import BuildEngine  # noqa: E402
from engine.catalog import BlockCatalog  # noqa: E402
from engine.io.chip_type_io import load_chip_type  # noqa: E402
from engine.portmap import PortInfo, PortMap, _derive_bus_edge  # noqa: E402
from model.connection import BlockEndpoint, ChipPortEndpoint  # noqa: E402
from model.enums import Face  # noqa: E402
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


# -- helpers -------------------------------------------------------------------

def _make_portmap(in_off, out_off, footprint, in_face=Face.WEST,
                  out_face=Face.EAST):
    """A synthetic PortMap with ONE input + ONE output at the given offsets."""
    ports = (
        PortInfo("in", "in", 0, in_off[0], in_off[1], in_face),
        PortInfo("out", "out", 1, out_off[0], out_off[1], out_face),
    )
    edge, colo = _derive_bus_edge(ports, footprint, PortMap.COLOCATION_SPAN)
    return PortMap("Synth", ports, footprint, edge, colo)


def _bare_placer(pm, *, driver_out=None, consumer_in=None, feedback=False,
                 width=10):
    """An AutoPlacer wired only enough to call ``_orient_for`` for ONE block 'b'."""
    placer = AutoPlacer.__new__(AutoPlacer)
    placer._takes_params = {}
    placer._port_map_provider = lambda bt, lib, p=None: pm
    placer._feedback_provider = (lambda bt, lib, p=None: True) if feedback else None
    placer._chip_port_resolver = None
    placer._driver_of = {"b": None}
    placer._in_port_of = {"b": driver_out} if driver_out is not None else {}
    placer._out_port_of = {"b": consumer_in} if consumer_in is not None else {}
    placer._width = width
    return placer


class _FakeBlk:
    type = "Synth"
    library = "x"
    params = None


def _io_cells(ctrl, catalog, name):
    b = ctrl.project.block(name)
    pm = catalog.port_map(b.type, params=b.params, library=b.library)
    ins = [(b.placement.cell(p.cell_id).x, b.placement.cell(p.cell_id).y)
           for p in pm.inputs() if b.placement.cell(p.cell_id)]
    outs = [(b.placement.cell(p.cell_id).x, b.placement.cell(p.cell_id).y)
            for p in pm.outputs() if b.placement.cell(p.cell_id)]
    return ins, outs


def _man(a, b):
    return abs(a[0] - b[0]) + abs(a[1] - b[1])


# -- (1) orienter minimises neighbour flyline ----------------------------------

def test_orient_puts_input_nearest_driver(qapp):
    """input WEST / output EAST on a 3x2 block; the driver sits to the block's
    RIGHT. Identity leaves the input at the far-left (far from the driver); a
    mirror_h brings the input to the right edge, nearer the driver — so the
    flyline orienter picks mirror_h, NOT identity."""
    pm = _make_portmap((0, 0), (2, 1), (2, 1))
    placer = _bare_placer(pm, driver_out=(5, 0))   # driver to the right of x=0
    kind = placer._orient_for(_FakeBlk(), True, "b", {}, 0, 0)
    assert kind == "mirror_h", f"expected mirror_h (input toward driver), got {kind}"
    # And it is genuinely the minimum: identity input (0,0) is dist 5 to the
    # driver, the chosen mirror_h input (2,0) is dist 3.
    assert _man((0, 0), (5, 0)) > _man((2, 0), (5, 0))


def test_orient_identity_when_already_minimal(qapp):
    """When identity already seats the input nearest the driver (driver to the
    LEFT), the orienter keeps identity — never transforms needlessly."""
    pm = _make_portmap((0, 0), (2, 1), (2, 1))
    placer = _bare_placer(pm, driver_out=(-3, 0))  # driver to the left
    kind = placer._orient_for(_FakeBlk(), True, "b", {}, 0, 0)
    assert kind is None


# -- (2) io-colocated aspect wins a flyline tie --------------------------------

def test_colocated_aspect_preferred_on_tie(qapp):
    """Two orientations give the SAME flyline but one co-locates I/O on the bus
    edge (the cheap 1-D tap, §4.3). The orienter prefers the colocated one.

    Construct a square 2x2 block whose input and output share one cell-column in
    one orientation (colocated) and sit apart in another, with NO driver/consumer
    so every orientation scores flyline 0 — the tie-break alone decides."""
    # input + output on the SAME west edge, adjacent -> identity is colocated.
    pm = _make_portmap((0, 0), (0, 1), (1, 1),
                       in_face=Face.WEST, out_face=Face.WEST)
    assert pm.io_colocated
    placer = _bare_placer(pm)                       # no driver, no consumer
    kind = placer._orient_for(_FakeBlk(), True, "b", {}, 3, 3)
    # Identity is colocated AND flyline-tied with everything -> identity wins.
    chosen = pm if kind is None else pm.transformed(kind)
    assert chosen.io_colocated, "a colocated orientation must win the tie"


# -- (3) internal-feedback blocks are left as-authored -------------------------

def test_feedback_block_not_reoriented(qapp):
    """A block flagged as having internal feedback is never reoriented — a D4
    transform would rotate its PortMap but not its hardcoded-face program."""
    pm = _make_portmap((0, 0), (2, 1), (2, 1))
    placer = _bare_placer(pm, driver_out=(5, 0), feedback=True)
    # Without feedback this same setup picks mirror_h; WITH feedback it stays put.
    assert placer._orient_for(_FakeBlk(), True, "b", {}, 0, 0) is None


def test_gardner_feedback_block_kept_identity(qapp, catalog, chip_type):
    """End-to-end: a chain containing Gardner (internal feedback) leaves Gardner
    as-authored under auto-place (rotating it breaks the RX loop — #270 BER)."""
    ctrl = AppController(catalog=catalog)
    ctrl.new_project("fb", "kyttar_10x12")
    lib = "lattrex.official"
    cos = ctrl.place_block("ComplexCostasLoopBlock", 0, 0, 0, library=lib)
    gar = ctrl.place_block("GardnerTimingRecovery", 0, 0, 0, library=lib)
    sli = ctrl.place_block("BPSKSlicerBlock", 0, 0, 0, library=lib)
    ctrl.add_logical_connection(
        ChipPortEndpoint(chip=0, port="x16_in"),
        BlockEndpoint(block=cos, port="xi"), name="in_cos")
    ctrl.add_logical_connection(
        BlockEndpoint(block=cos, port="yi_tap"),
        BlockEndpoint(block=gar, port="xi"), name="cos_gar")
    ctrl.add_logical_connection(
        BlockEndpoint(block=gar, port="out"),
        BlockEndpoint(block=sli, port="llr"), name="gar_sli")
    plan = ctrl.auto_place(0)
    # Gardner + Costas (both feedback) are NOT rotated; the slicer (feed-forward)
    # may be reoriented freely.
    assert plan.orientations.get(gar) is None
    assert plan.orientations.get(cos) is None


# -- (4) the real FIR chain: routes, builds, flyline not worse -----------------

def test_fir_chain_routes_builds_and_minimises_flyline(qapp, catalog, chip_type):
    """gain → FIR(20 taps) → x16_out: auto-place + auto-route + build all succeed
    with no overlap and on-grid, the FIR's input lands nearer its driver than its
    output does, and the total chain flyline is no worse than the old output-only
    orientation would have produced (strictly better when the old heuristic erred).
    """
    coeffs = [0.05] * 20

    def place(ctrl):
        g = ctrl.place_block("GainBlock", 0, 2, 2, library="lattrex.official")
        f = ctrl.place_block("FIRFilterBlock", 0, 5, 5,
                             library="lattrex.official",
                             params={"coefficients": coeffs})
        ctrl.add_logical_connection(
            ChipPortEndpoint(chip=0, port="x16_in"),
            BlockEndpoint(block=g, port="sample"), name="in_g")
        ctrl.add_logical_connection(
            BlockEndpoint(block=g, port="out"),
            BlockEndpoint(block=f, port="sample"), name="g_f")
        ctrl.add_logical_connection(
            BlockEndpoint(block=f, port="out"),
            ChipPortEndpoint(chip=0, port="x16_out"), name="f_out")
        return g, f

    ctrl = AppController(catalog=catalog)
    ctrl.new_project("fir", "kyttar_10x12")
    g, f = place(ctrl)
    plan = ctrl.auto_place(0)

    # On-grid + no overlap.
    W, H = 10, 12
    occ = {}
    for b in ctrl.project.blocks:
        for c in list(b.placement.cells) + list(b.placement.transit_cells):
            assert 0 <= c.x < W and 0 <= c.y < H, f"{b.name} off-grid"
            assert (c.x, c.y) not in occ, f"overlap at ({c.x},{c.y})"
            occ[(c.x, c.y)] = b.name

    # The FIR's input cell is nearer its driver (gain output) than its output cell.
    gi, go = _io_cells(ctrl, catalog, g)
    fi, fo = _io_cells(ctrl, catalog, f)
    x16in = ctrl._chip_port_cell(0, "x16_in")
    x16out = ctrl._chip_port_cell(0, "x16_out")
    dist_in_to_driver = _man(fi[0], go[0])
    dist_out_to_driver = _man(fo[0], go[0])
    assert dist_in_to_driver <= dist_out_to_driver, (
        f"FIR input {fi[0]} should be at least as near its driver {go[0]} as its "
        f"output {fo[0]} is (in={dist_in_to_driver}, out={dist_out_to_driver})")

    # Total chain flyline.
    total = (_man(x16in, gi[0]) + _man(go[0], fi[0]) + _man(fo[0], x16out))

    # Baseline: what the OLD output-only heuristic would have chosen for the FIR.
    # Recompute the FIR orientation with the bus-direction-only rule and measure
    # the resulting flyline at the SAME anchor the placer used.
    blk_f = ctrl.project.block(f)
    base_pm = catalog.port_map(blk_f.type, params=blk_f.params,
                               library=blk_f.library)
    old_kind = suggest_flow_orientation(base_pm, Face.EAST)  # going-right band
    old_pm = base_pm if old_kind is None else base_pm.transformed(old_kind)
    # Anchor = the FIR's current min corner (placement is identical up to its own
    # orientation; gain is the lead and unaffected).
    bb = blk_f.placement.bounding_box()
    ax, ay = bb[0], bb[1]
    oin = old_pm.inputs()[0]
    oout = old_pm.outputs()[0]
    old_in = (ax + oin.dx, ay + oin.dy)
    old_out = (ax + oout.dx, ay + oout.dy)
    old_total = (_man(x16in, gi[0]) + _man(go[0], old_in)
                 + _man(old_out, x16out))
    assert total <= old_total, (
        f"new flyline {total} worse than old output-only {old_total}")

    # And it ROUTES + BUILDS.
    rep = ctrl.auto_route_all({"kyttar_10x12": chip_type}, auto_orient=True)
    assert rep.ok, [(r.name, r.reason) for r in rep.failed]
    res = BuildEngine(catalog, str(CT_PATH)).build(
        ctrl.project, {"kyttar_10x12": chip_type})
    assert res.ok, [str(e) for e in res.errors]


if __name__ == "__main__":
    app = QApplication.instance() or QApplication([])
    cat = BlockCatalog.from_gr_kyttar()
    ct = load_chip_type(str(CT_PATH))
    test_orient_puts_input_nearest_driver(app)
    test_orient_identity_when_already_minimal(app)
    test_colocated_aspect_preferred_on_tie(app)
    test_feedback_block_not_reoriented(app)
    test_gardner_feedback_block_kept_identity(app, cat, ct)
    test_fir_chain_routes_builds_and_minimises_flyline(app, cat, ct)
    print("flyline-min auto-orient: ALL PASS")
