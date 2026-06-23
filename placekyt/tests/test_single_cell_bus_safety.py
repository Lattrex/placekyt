"""Single-cell bus-fed deadlock-safety (§5.3) — input-face != output-face + DRC.

A SINGLE-CELL block on the routing bus (cell_count==1, e.g. BPSKSlicerBlock) has ONE
cell that both RECEIVES its input (a broker WRITE+JUMP) and DRIVES its output
(WRITE+JUMP). If the input arrives on the SAME face the output drives, both contend on
one single-outstanding link → deadlock (the major, near-untestable-by-dynamic-sim
failure the user flagged). The auto-P&R now GUARANTEES input-face != output-face for
such blocks (abut-first placement + an adaptive router split), and a DRC ERRORS if any
bus-fed single-cell block still ends with input-face == output-face — a sound failure,
never a silent unsafe build.

This test pins both halves:
  1. In the production RX build, every cell_count==1 bus-fed block has input-face !=
     output-face (and the bus DRC reports no single-cell hazard).
  2. The DRC ERRORS on a deliberately-constructed in==out single-cell layout.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication  # noqa: E402

from engine.build import BuildEngine  # noqa: E402
from engine.bus_drc import _check_single_cell_inout, check_project_bus  # noqa: E402
from engine.catalog import BlockCatalog  # noqa: E402
from engine.io.chip_type_io import load_chip_type  # noqa: E402
from model.connection import BlockEndpoint, ChipPortEndpoint  # noqa: E402
from ui.controller import AppController  # noqa: E402

from tests.conftest import CHIP_YAML as CT_PATH  # noqa: E402
pytestmark = pytest.mark.skipif(not CT_PATH.exists(), reason="chip yaml absent")

_FWD_DELTA = {"south": (0, 1), "east": (1, 0), "west": (-1, 0), "north": (0, -1)}
_FACE_NAME = {(0, 1): "south", (1, 0): "east", (-1, 0): "west", (0, -1): "north"}


@pytest.fixture(scope="module")
def qapp():
    return QApplication.instance() or QApplication([])


@pytest.fixture(scope="module")
def catalog(qapp):
    return BlockCatalog.from_gr_kyttar()


@pytest.fixture(scope="module")
def chip_type():
    return load_chip_type(str(CT_PATH))


def _build_production_rx(catalog, chip_type):
    ctrl = AppController(catalog=catalog)
    ctrl.new_project("prodrx", "kyttar_10x12")
    lib = "lattrex.official"
    mf = ctrl.place_block("ComplexRRCMatchedFilterBlock", 0, 0, 0, library=lib)
    cos = ctrl.place_block("ComplexCostasLoopBlock", 0, 0, 0, library=lib)
    gar = ctrl.place_block("GardnerTimingRecovery", 0, 0, 0, library=lib)
    sli = ctrl.place_block("BPSKSlicerBlock", 0, 0, 0, library=lib)
    ctrl.add_route(ChipPortEndpoint(chip=0, port="x16_in"),
                   BlockEndpoint(block=mf, port="xi"), [])
    ctrl.add_route(ChipPortEndpoint(chip=0, port="x16_in"),
                   BlockEndpoint(block=mf, port="xq"), [])
    ctrl.add_route(BlockEndpoint(block=mf, port="yi"),
                   BlockEndpoint(block=cos, port="xi"), [])
    ctrl.add_route(BlockEndpoint(block=mf, port="yq"),
                   BlockEndpoint(block=cos, port="xq"), [])
    ctrl.add_route(BlockEndpoint(block=cos, port="yi_tap"),
                   BlockEndpoint(block=gar, port="xi"), [])
    ctrl.add_route(BlockEndpoint(block=gar, port="out"),
                   BlockEndpoint(block=sli, port="llr"), [])
    ctrl.add_route(BlockEndpoint(block=sli, port="out"),
                   ChipPortEndpoint(chip=0, port="x16_out"), [])
    ctrl.auto_place(0)
    rep = ctrl.auto_route_all({"kyttar_10x12": chip_type}, auto_orient=False,
                              use_bus="always")
    assert rep.ok, [(r.name, r.reason) for r in rep.failed]
    return ctrl


def _single_cell_bus_fed(project, chip_id=0):
    """Cells of single-cell blocks that are bus-fed (a brokered input net), and the
    block name — the cells subject to the deadlock rule."""
    out = {}
    one = {}
    for blk in project.blocks:
        pl = blk.placement
        if pl is not None and pl.chip == chip_id and len(pl.cells) == 1:
            one[(pl.cells[0].x, pl.cells[0].y)] = blk.name
    for conn in project.connections:
        if not conn.is_routed or not isinstance(conn.target, BlockEndpoint):
            continue
        blk = project.block(conn.target.block)
        if blk is None or len(blk.placement.cells) != 1:
            continue
        cell = (blk.placement.cells[0].x, blk.placement.cells[0].y)
        last = (conn.route[-1].x, conn.route[-1].y) if conn.route else cell
        if last == cell and isinstance(conn.source, ChipPortEndpoint):
            continue  # direct port injection — not bus-fed
        out[cell] = one.get(cell, conn.target.block)
    return out


def _arrival_and_output_face(project, cell):
    """(input arrival face name, output drive face name) for a single-cell block,
    from the routed geometry. Arrival = cell -> input net's final waypoint; output =
    cell -> output net's first waypoint."""
    arr = drv = None
    for conn in project.connections:
        if not conn.is_routed or not conn.route:
            continue
        pts = [(p.x, p.y) for p in conn.route]
        if isinstance(conn.target, BlockEndpoint):
            b = project.block(conn.target.block)
            if b is not None and len(b.placement.cells) == 1 \
                    and (b.placement.cells[0].x, b.placement.cells[0].y) == cell:
                last = pts[-1]
                if last != cell:
                    arr = _FACE_NAME.get((last[0] - cell[0], last[1] - cell[1]))
        if isinstance(conn.source, BlockEndpoint):
            b = project.block(conn.source.block)
            if b is not None and len(b.placement.cells) == 1 \
                    and (b.placement.cells[0].x, b.placement.cells[0].y) == cell:
                nxt = pts[1] if (len(pts) > 1 and pts[0] == cell) else \
                    (pts[0] if pts[0] != cell else None)
                if nxt is not None:
                    drv = _FACE_NAME.get((nxt[0] - cell[0], nxt[1] - cell[1]))
    return arr, drv


def test_production_rx_single_cell_input_neq_output(qapp, catalog, chip_type):
    """Every bus-fed single-cell block in the production RX has input-face !=
    output-face, and the bus DRC reports no single-cell hazard."""
    ctrl = _build_production_rx(catalog, chip_type)
    hazard_cells = _single_cell_bus_fed(ctrl.project)
    assert hazard_cells, "expected at least the BPSKSlicer single-cell block"
    for cell, name in hazard_cells.items():
        arr, drv = _arrival_and_output_face(ctrl.project, cell)
        assert arr is not None and drv is not None, \
            f"{name} at {cell}: could not resolve faces (arr={arr}, drv={drv})"
        assert arr != drv, (
            f"single-cell block {name} at {cell} has input-face == output-face "
            f"({arr}) — the §5.3 deadlock hazard")
    # The authoritative DRC agrees: no single-cell input==output violation.
    viols = [v for v in check_project_bus(ctrl.project, {"kyttar_10x12": chip_type},
                                          catalog)
             if v.kind == "single_cell_inout"]
    assert not viols, [str(v) for v in viols]
    # And it builds clean (no deadlock DRC error).
    bres = BuildEngine(catalog, str(CT_PATH)).build(
        ctrl.project, {"kyttar_10x12": chip_type})
    assert bres.ok, [str(e) for e in bres.errors]


def test_drc_errors_on_constructed_inout_layout(qapp, catalog, chip_type):
    """A deliberately-constructed single-cell block whose bus-fed input arrives on the
    SAME face its output drives MUST be flagged by the DRC (and block the build) — a
    sound failure, never a silent unsafe build."""
    ctrl = AppController(catalog=catalog)
    ctrl.new_project("haz", "kyttar_10x12")
    lib = "lattrex.official"
    # A single-cell GainBlock sink at (5, 5). We hand-lay both nets so the input
    # arrives from the EAST (a broker at (6,5)) AND the output drives EAST (to (6,5)) —
    # the in==out deadlock. A driver feeds the input from the east; the output egresses
    # east too. (Hand-laid routes are the build-from-design truth, so this exercises the
    # DRC exactly as a fragile manual/route would.)
    drv = ctrl.place_block("GainBlock", 0, 8, 5, params={"gain": 0.5}, library=lib)
    sink = ctrl.place_block("GainBlock", 0, 5, 5, params={"gain": 1.0}, library=lib)
    # Input net: driver (8,5) -> broker (6,5) abutting the sink's EAST face.
    ctrl.add_route(BlockEndpoint(block=drv, port="out"),
                   BlockEndpoint(block=sink, port="sample"),
                   [(8, 5), (7, 5), (6, 5)])
    # Output net: sink (5,5) -> EAST through (6,5) ... to the output port.
    ctrl.add_route(BlockEndpoint(block=sink, port="out"),
                   ChipPortEndpoint(chip=0, port="x16_out"),
                   [(5, 5), (6, 5), (6, 4), (6, 3), (6, 2), (6, 1), (6, 0),
                    (7, 0), (8, 0), (9, 0)])

    viols = [v for v in check_project_bus(ctrl.project, {"kyttar_10x12": chip_type},
                                          catalog)
             if v.kind == "single_cell_inout"]
    assert viols, "the in==out single-cell layout must be flagged"
    assert viols[0].cell == (5, 5)
    assert "deadlock" in viols[0].reason.lower()
    # The build is blocked by exactly that named hazard (not silently shipped).
    bres = BuildEngine(catalog, str(CT_PATH)).build(
        ctrl.project, {"kyttar_10x12": chip_type})
    assert not bres.ok
    assert any(e.category == "single_cell_inout_deadlock" for e in bres.errors), \
        [str(e) for e in bres.errors]


def test_check_single_cell_inout_direct_port_exempt(qapp, catalog, chip_type):
    """A single-cell block fed DIRECTLY by a chip input port at its own cell (no
    broker) is exempt — there is no shared-face deadlock to flag."""
    ctrl = AppController(catalog=catalog)
    ctrl.new_project("dp", "kyttar_10x12")
    lib = "lattrex.official"
    # Place a single-cell block ON the input port cell and route its input from the
    # port to its own cell, output out the same face — exempt (port injects at cell).
    g = ctrl.place_block("GainBlock", 0, 0, 0, params={"gain": 0.5}, library=lib)
    gc = ctrl.project.block(g).placement.cells[0]
    ctrl.add_route(ChipPortEndpoint(chip=0, port="x16_in"),
                   BlockEndpoint(block=g, port="sample"), [(gc.x, gc.y)])
    ctrl.add_route(BlockEndpoint(block=g, port="out"),
                   ChipPortEndpoint(chip=0, port="x16_out"),
                   [(gc.x, gc.y), (gc.x + 1, gc.y)])
    viols = [v for v in _check_single_cell_inout(ctrl.project)
             if v.kind == "single_cell_inout"]
    assert not viols, [str(v) for v in viols]
