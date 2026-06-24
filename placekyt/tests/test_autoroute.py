"""Auto-route tests (auto-P&R Phase 3, P3.1): the BFS corridor router that
materialises logical nets into drawn waypoint routes the build path consumes."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication  # noqa: E402

from engine.autoroute import AutoRouter  # noqa: E402
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


def _port_cells(catalog):
    def f(block_type, library):
        pm = catalog.port_map(block_type, library=library)
        return {p.name: (p.cell_id, p.direction) for p in pm.ports}
    return f


def _router(ctrl, catalog, chip_type):
    return AutoRouter(ctrl.project, {"kyttar_10x12": chip_type},
                      _port_cells(catalog))


# -- core routing ---------------------------------------------------------------

def test_routes_adjacent_blocks(qapp, catalog, chip_type):
    ctrl = AppController(catalog=catalog)
    ctrl.new_project("ar", "kyttar_10x12")
    a = ctrl.place_block("GainBlock", 0, 1, 1, library="lattrex.official")
    b = ctrl.place_block("DCBlockerBlock", 0, 5, 1, library="lattrex.official", params={"length": 2, "long_form": False})
    ctrl.add_logical_connection(
        BlockEndpoint(block=a, port="out"),
        BlockEndpoint(block=b, port="sample"), name="a_b")
    rep = _router(ctrl, catalog, chip_type).route_all()
    assert rep.ok
    r = rep.results[0]
    # corridor runs from the producer cell (1,1) to the consumer cell (5,1)
    assert r.points[0] == (1, 1) and r.points[-1] == (5, 1)
    # contiguous (each step is one cell)
    for (x0, y0), (x1, y1) in zip(r.points, r.points[1:]):
        assert abs(x1 - x0) + abs(y1 - y0) == 1


def test_full_chain_routes_and_builds(qapp, catalog, chip_type):
    """x16_in → Gain → DCBlocker → x16_out: every net auto-routes AND the design
    builds (auto-route integrates with the build-from-design path)."""
    from model.connection import RoutePoint

    ctrl = AppController(catalog=catalog)
    ctrl.new_project("chain", "kyttar_10x12")
    a = ctrl.place_block("GainBlock", 0, 1, 1, library="lattrex.official")
    b = ctrl.place_block("DCBlockerBlock", 0, 5, 1, library="lattrex.official", params={"length": 2, "long_form": False})
    ctrl.add_logical_connection(
        ChipPortEndpoint(chip=0, port="x16_in"),
        BlockEndpoint(block=a, port="sample"), name="in_a")
    ctrl.add_logical_connection(
        BlockEndpoint(block=a, port="out"),
        BlockEndpoint(block=b, port="sample"), name="a_b")
    ctrl.add_logical_connection(
        BlockEndpoint(block=b, port="out"),
        ChipPortEndpoint(chip=0, port="x16_out"), name="b_out")
    rep = _router(ctrl, catalog, chip_type).route_all()
    assert rep.ok, [(r.name, r.reason) for r in rep.failed]
    for r in rep.results:
        conn = ctrl.project.connection(r.name)
        conn.route = [RoutePoint(x, y) for (x, y) in r.points]
    res = BuildEngine(catalog, str(CT_PATH)).build(
        ctrl.project, {"kyttar_10x12": chip_type})
    assert res.ok, [str(e) for e in res.errors]


def test_nets_do_not_share_cells(qapp, catalog, chip_type):
    """Two nets routed in one pass take disjoint corridors (this cut keeps nets on
    separate cells — shared-bus multiplexing is a later increment)."""
    ctrl = AppController(catalog=catalog)
    ctrl.new_project("disj", "kyttar_10x12")
    a = ctrl.place_block("GainBlock", 0, 1, 1, library="lattrex.official")
    b = ctrl.place_block("DCBlockerBlock", 0, 5, 1, library="lattrex.official", params={"length": 2, "long_form": False})
    c = ctrl.place_block("GainBlock", 0, 1, 5, library="lattrex.official")
    d = ctrl.place_block("DCBlockerBlock", 0, 5, 5, library="lattrex.official", params={"length": 2, "long_form": False})
    ctrl.add_logical_connection(
        BlockEndpoint(block=a, port="out"),
        BlockEndpoint(block=b, port="sample"), name="ab")
    ctrl.add_logical_connection(
        BlockEndpoint(block=c, port="out"),
        BlockEndpoint(block=d, port="sample"), name="cd")
    rep = _router(ctrl, catalog, chip_type).route_all()
    assert rep.ok
    p1 = set(rep.results[0].points)
    p2 = set(rep.results[1].points)
    # the only legitimately shared cells would be block endpoints; here the two
    # rows are disjoint, so the corridors must not overlap at all
    assert p1.isdisjoint(p2)


# -- sound failure --------------------------------------------------------------

def test_unplaced_block_reported_not_fabricated(qapp, catalog, chip_type):
    ctrl = AppController(catalog=catalog)
    ctrl.new_project("unplaced", "kyttar_10x12")
    a = ctrl.place_block("GainBlock", 0, 1, 1, library="lattrex.official")
    # add a block but DON'T place it: craft a connection to a non-placed block
    from model.block import Block
    ctrl.project.blocks.append(Block("ghost", "DCBlockerBlock",
                                      library="lattrex.official"))
    ctrl.add_logical_connection(
        BlockEndpoint(block=a, port="out"),
        BlockEndpoint(block="ghost", port="sample"), name="a_ghost")
    rep = _router(ctrl, catalog, chip_type).route_all()
    assert not rep.ok
    bad = rep.failed[0]
    assert bad.name == "a_ghost" and bad.points is None and bad.reason


# -- controller integration (undoable Route All) --------------------------------

def test_controller_auto_route_all_is_undoable(qapp, catalog, chip_type):
    ctrl = AppController(catalog=catalog)
    ctrl.new_project("ctl", "kyttar_10x12")
    a = ctrl.place_block("GainBlock", 0, 1, 1, library="lattrex.official")
    b = ctrl.place_block("DCBlockerBlock", 0, 5, 1, library="lattrex.official", params={"length": 2, "long_form": False})
    ctrl.add_logical_connection(
        BlockEndpoint(block=a, port="out"),
        BlockEndpoint(block=b, port="sample"), name="ab")
    assert not ctrl.project.connection("ab").is_routed
    report = ctrl.auto_route_all({"kyttar_10x12": chip_type})
    assert report.ok
    assert ctrl.project.connection("ab").is_routed   # net now has a route
    ctrl.undo()
    assert not ctrl.project.connection("ab").is_routed  # back to a logical net


# -- auto-orient (P3.2) ---------------------------------------------------------

def test_suggest_flow_orientation_basics(catalog):
    from engine.autoroute import suggest_flow_orientation
    from model.enums import Face
    pm = catalog.port_map("GainBlock")          # output faces EAST by default
    assert suggest_flow_orientation(pm, Face.EAST) is None      # already correct
    assert suggest_flow_orientation(pm, Face.SOUTH) == "cw"
    assert suggest_flow_orientation(pm, Face.NORTH) == "ccw"
    assert suggest_flow_orientation(pm, Face.WEST) == "mirror_h"


def test_orient_for_flow_points_output_at_consumer(qapp, catalog, chip_type):
    # B placed SOUTH of A → A's output should be reoriented to face SOUTH (cw).
    ctrl = AppController(catalog=catalog)
    ctrl.new_project("orient", "kyttar_10x12")
    a = ctrl.place_block("GainBlock", 0, 3, 1, library="lattrex.official")
    b = ctrl.place_block("DCBlockerBlock", 0, 3, 5, library="lattrex.official", params={"length": 2, "long_form": False})
    ctrl.add_logical_connection(
        BlockEndpoint(block=a, port="out"),
        BlockEndpoint(block=b, port="sample"), name="ab")
    router = AutoRouter(
        ctrl.project, {"kyttar_10x12": chip_type}, _port_cells(catalog),
        port_map_provider=lambda bt, lib: catalog.port_map(bt, library=lib))
    suggestions = router.orient_for_flow()
    assert suggestions.get(a) == "cw"
    # the east-aligned consumer needs no reorientation
    assert b not in suggestions


def test_auto_route_all_with_orient_is_one_undo(qapp, catalog, chip_type):
    ctrl = AppController(catalog=catalog)
    ctrl.new_project("orient_route", "kyttar_10x12")
    a = ctrl.place_block("GainBlock", 0, 3, 1, library="lattrex.official")
    b = ctrl.place_block("DCBlockerBlock", 0, 3, 5, library="lattrex.official", params={"length": 2, "long_form": False})
    ctrl.add_logical_connection(
        BlockEndpoint(block=a, port="out"),
        BlockEndpoint(block=b, port="sample"), name="ab")
    before = ctrl.project.block(a).placement.cells[0].face
    report = ctrl.auto_route_all({"kyttar_10x12": chip_type}, auto_orient=True)
    assert report.ok
    after = ctrl.project.block(a).placement.cells[0].face
    assert after != before                       # block was reoriented
    assert ctrl.project.connection("ab").is_routed
    # ONE undo reverts BOTH the orientation and the route
    ctrl.undo()
    assert ctrl.project.block(a).placement.cells[0].face == before
    assert not ctrl.project.connection("ab").is_routed


def test_auto_orient_leaves_aligned_blocks_untouched(qapp, catalog, chip_type):
    # East-flow (B east of A): A's output already faces EAST → no reorientation.
    ctrl = AppController(catalog=catalog)
    ctrl.new_project("aligned", "kyttar_10x12")
    a = ctrl.place_block("GainBlock", 0, 1, 1, library="lattrex.official")
    b = ctrl.place_block("DCBlockerBlock", 0, 5, 1, library="lattrex.official", params={"length": 2, "long_form": False})
    ctrl.add_logical_connection(
        BlockEndpoint(block=a, port="out"),
        BlockEndpoint(block=b, port="sample"), name="ab")
    before = ctrl.project.block(a).placement.cells[0].face
    ctrl.auto_route_all({"kyttar_10x12": chip_type}, auto_orient=True)
    assert ctrl.project.block(a).placement.cells[0].face == before  # unchanged
