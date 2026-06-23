"""CP-SAT bus-sharing router tests (auto-P&R Phase 3, §7).

The heuristic BFS router keeps nets node-disjoint; CP-SAT lets nets SHARE a transit
cell (leaving it on one fwd_face) — the time-multiplexed bus model. These tests pin
the load-bearing win: a single-gap layout the heuristic CANNOT route (disjoint), but
CP-SAT can (both nets share the gap)."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication  # noqa: E402

from engine.autoroute import AutoRouter  # noqa: E402
from engine.catalog import BlockCatalog  # noqa: E402
from engine.io.chip_type_io import load_chip_type  # noqa: E402
from model.connection import BlockEndpoint  # noqa: E402
from ui.controller import AppController  # noqa: E402

from tests.conftest import CHIP_YAML as CT_PATH  # noqa: E402
ortools = pytest.importorskip("ortools")
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


def _port_cells(catalog):
    def f(bt, lib):
        pm = catalog.port_map(bt, library=lib)
        return {p.name: (p.cell_id, p.direction) for p in pm.ports}
    return f


def _wall_with_gap(ctrl, c1_row, c2_row):
    """Two producers (left rows 4, 6) → two consumers (right rows c1_row, c2_row),
    with a full vertical wall at col 4 except ONE gap at (4,5). Both nets must pass
    the gap. When the two consumers are at the SAME row the nets fan out to a common
    sink (sharing is sound); at DIFFERENT rows they need to demux (not sound for
    plain cells)."""
    p1 = ctrl.place_block("GainBlock", 0, 0, 4, library="lattrex.official")
    p2 = ctrl.place_block("GainBlock", 0, 0, 6, library="lattrex.official")
    c1 = ctrl.place_block("DCBlockerBlock", 0, 9, c1_row,
                          library="lattrex.official")
    if c2_row == c1_row:
        c2 = c1
    else:
        c2 = ctrl.place_block("DCBlockerBlock", 0, 9, c2_row,
                              library="lattrex.official")
    for row in range(0, 12):
        if row == 5:
            continue
        try:
            ctrl.place_block("AGCBlock", 0, 4, row, library="lattrex.official")
        except Exception:  # noqa: BLE001 — grid full edge case; ignore
            pass
    ctrl.add_logical_connection(
        BlockEndpoint(block=p1, port="out"),
        BlockEndpoint(block=c1, port="sample"), name="n1")
    ctrl.add_logical_connection(
        BlockEndpoint(block=p2, port="out"),
        BlockEndpoint(block=c2, port="sample"), name="n2")


def test_cpsat_routes_single_net(qapp, catalog, chip_type):
    from engine.cpsat_router import route_all_cpsat

    ctrl = AppController(catalog=catalog)
    ctrl.new_project("c", "kyttar_10x12")
    a = ctrl.place_block("GainBlock", 0, 1, 1, library="lattrex.official")
    b = ctrl.place_block("DCBlockerBlock", 0, 5, 1, library="lattrex.official")
    ctrl.add_logical_connection(
        BlockEndpoint(block=a, port="out"),
        BlockEndpoint(block=b, port="sample"), name="ab")
    rep = route_all_cpsat(ctrl.project, {"kyttar_10x12": chip_type},
                          _port_cells(catalog))
    assert rep.ok
    pts = rep.results[0].points
    assert pts[0] == (1, 1) and pts[-1] == (5, 1)


def test_heuristic_fails_cpsat_shares_common_sink_gap(qapp, catalog, chip_type):
    """THE WIN — two nets to the SAME consumer (fan-out) through a single gap.
    The heuristic (disjoint) can't route both; CP-SAT shares the gap cell because
    a shared transit cell legally fans both streams to one common sink."""
    from engine.cpsat_router import route_all_cpsat

    ctrl = AppController(catalog=catalog)
    ctrl.new_project("gap", "kyttar_10x12")
    _wall_with_gap(ctrl, c1_row=5, c2_row=5)   # both → the SAME consumer

    h = AutoRouter(ctrl.project, {"kyttar_10x12": chip_type},
                   _port_cells(catalog)).route_all()
    assert not h.ok, "heuristic unexpectedly routed the single-gap fan-out"

    c = route_all_cpsat(ctrl.project, {"kyttar_10x12": chip_type},
                        _port_cells(catalog))
    assert c.ok, [(r.name, r.reason) for r in c.failed]
    for r in c.results:
        assert (4, 5) in r.points          # both nets share the single gap


def test_cpsat_refuses_unsound_different_sink_share(qapp, catalog, chip_type):
    """SOUNDNESS — two nets with DIFFERENT sinks forced through one gap is
    correctly UNROUTABLE: a plain transit cell can't demux two streams to
    different destinations (it forwards everything on one face), so CP-SAT must
    NOT claim it routable (that would build but mis-compute)."""
    from engine.cpsat_router import route_all_cpsat

    ctrl = AppController(catalog=catalog)
    ctrl.new_project("diff", "kyttar_10x12")
    _wall_with_gap(ctrl, c1_row=4, c2_row=6)   # DIFFERENT consumers
    c = route_all_cpsat(ctrl.project, {"kyttar_10x12": chip_type},
                        _port_cells(catalog))
    assert not c.ok                            # proven infeasible, not fabricated
    assert c.failed and c.failed[0].reason


def test_controller_auto_route_escalates_to_cpsat(qapp, catalog, chip_type):
    """`use_cpsat="auto"` falls back to CP-SAT when the heuristic leaves a net
    unrouted, so the dense common-sink design routes through Route All."""
    ctrl = AppController(catalog=catalog)
    ctrl.new_project("esc", "kyttar_10x12")
    _wall_with_gap(ctrl, c1_row=5, c2_row=5)   # fan-out (sound to share)
    rep = ctrl.auto_route_all({"kyttar_10x12": chip_type}, auto_orient=False,
                              use_cpsat="auto")
    assert rep.ok, [(r.name, r.reason) for r in rep.failed]
    assert all(c.is_routed for c in ctrl.project.connections)


def test_cpsat_reports_unroutable_soundly(qapp, catalog, chip_type):
    """A net to an unplaced block is NAMED, not crashed (sound failure)."""
    from engine.cpsat_router import route_all_cpsat
    from model.block import Block

    ctrl = AppController(catalog=catalog)
    ctrl.new_project("snd", "kyttar_10x12")
    a = ctrl.place_block("GainBlock", 0, 1, 1, library="lattrex.official")
    ctrl.project.blocks.append(Block("ghost", "DCBlockerBlock",
                                      library="lattrex.official"))
    ctrl.add_logical_connection(
        BlockEndpoint(block=a, port="out"),
        BlockEndpoint(block="ghost", port="sample"), name="a_ghost")
    rep = route_all_cpsat(ctrl.project, {"kyttar_10x12": chip_type},
                          _port_cells(catalog))
    assert not rep.ok
    assert rep.failed[0].name == "a_ghost" and rep.failed[0].reason
