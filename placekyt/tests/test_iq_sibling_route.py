"""Manually routing one rail of an I/Q complex pair routes its sibling too.

An I/Q complex pair is two LOGICAL nets that share ONE physical path (same
source-output cell + target-input cell) — e.g. ComplexRRCMatchedFilter.yi→Costas.xi
and .yq→Costas.xq. The auto-router routes both with the identical path. When the
user draws the route on ONE rail by hand, the sibling must get it too — otherwise
the sibling stays a fly line and DRC errors "no physical route" on a link that
visually looks connected (the manual-edit bug the user hit on net4).
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import sys

from PySide6.QtWidgets import QApplication  # noqa: E402

from engine.catalog import BlockCatalog  # noqa: E402
from ui.controller import AppController  # noqa: E402

from tests.conftest import CHIP_YAML as CT  # noqa: E402
from tests.conftest import EXAMPLES_DIR  # noqa: E402
GRC = EXAMPLES_DIR / "coherent_bpsk_rx_mf_demo.grc"
pytestmark = pytest.mark.skipif(
    not (GRC.exists() and CT.exists()), reason=".grc / chip yaml absent")


@pytest.fixture(scope="module")
def qapp():
    return QApplication.instance() or QApplication([])


@pytest.fixture(scope="module")
def catalog(qapp):
    return BlockCatalog.from_gr_kyttar()


def test_routing_one_iq_rail_routes_the_sibling(qapp, catalog):
    ctrl = AppController(catalog=catalog)
    ctrl.import_grc(str(GRC), chip_type="kyttar_10x12")
    ctrl.auto_place(0)
    ctrl.auto_orient_for_flow()
    prj = ctrl.project

    def find(port):
        return next(c for c in prj.connections
                    if getattr(c.source, "block", "") == "complexrrcmatchedfilter"
                    and c.source.port == port)

    yi = find("yi")   # MF.yi -> Costas.xi (one rail)
    yq = find("yq")   # MF.yq -> Costas.xq (the I/Q sibling, same physical cells)
    assert not yi.is_routed and not yq.is_routed

    # Draw a route on the yi rail; the yq sibling must route with the same path.
    ctrl.add_route(yi.source, yi.target, [(1, 1), (2, 1), (6, 3)])
    yi2 = prj.connection(yi.name)
    yq2 = prj.connection(yq.name)
    assert yi2.is_routed, "drawn rail must be routed"
    assert yq2.is_routed, "I/Q sibling must be routed by the same draw (no fly line)"
    assert [(p.x, p.y) for p in yi2.route] == [(p.x, p.y) for p in yq2.route], \
        "sibling shares the identical physical path"

    # Undo restores BOTH to unrouted (one composite undo step).
    ctrl.commands.undo()
    assert not prj.connection(yi.name).is_routed
    assert not prj.connection(yq.name).is_routed


def test_non_iq_route_has_no_spurious_siblings(qapp, catalog):
    """A net whose endpoints don't share cells with another unrouted net routes
    alone — sibling propagation must not over-match."""
    ctrl = AppController(catalog=catalog)
    ctrl.import_grc(str(GRC), chip_type="kyttar_10x12")
    ctrl.auto_place(0)
    ctrl.auto_orient_for_flow()
    prj = ctrl.project
    # gardner.out -> slicer.llr is a lone block→block net (no I/Q twin).
    g = next(c for c in prj.connections
             if getattr(c.source, "block", "") == "gardnertimingrecovery"
             and c.source.port == "out")
    before = sum(1 for c in prj.connections if c.is_routed)
    ctrl.add_route(g.source, g.target, [(3, 1), (3, 2)])
    after = sum(1 for c in prj.connections if c.is_routed)
    assert after - before == 1, "a lone net must route exactly one connection"


@pytest.mark.skipif(not CT.exists(), reason="chip yaml absent")
def test_manual_move_then_route_complex_egress_co_routes_yq(qapp, catalog):
    """Reproduce the user's EXACT GUI sequence, SELF-CONTAINED (no saved .kyt):
    place a complex-output block (FrequencyModulator, pipeline_lock) whose yi/yq rails
    both go to x16_out, auto-route, then MANUALLY MOVE the block one row (which unroutes
    its nets), then MANUALLY ROUTE the yi output. The yq rail MUST co-route — both ride
    the one emit-cell corridor and the port de-interleaves by tag. Before the fix, yq
    stayed a fly line + a "no physical route" DRC on a link that looked connected: the
    egress target is a ChipPort (not a block cell), so the cell-pair sibling match missed
    it. The rail match is by yi<->yq PORT relationship (via _iq_sibling), NOT out_tag —
    a raw/hand-wired net's out_tag is None, so a tag-based match would miss it.

    Exercises the SAME add_route -> _route_siblings path the GUI's manual route uses (NOT
    auto_route_all — the earlier auto-only fix is why this looked "not fixed"). Built
    in-code so the regression needs no external .kyt."""
    from engine.io.chip_type_io import load_chip_type
    from model.connection import BlockEndpoint, ChipPortEndpoint
    from commands.placement_cmds import MoveBlockCommand

    key = "kyttar_10x12"
    ct = load_chip_type(str(CT))
    ctrl = AppController(catalog=catalog)
    ctrl.new_project("egress", key)
    fm = ctrl.place_block(
        "FrequencyModulatorBlock", 0, 2, 2, library="lattrex.official",
        params={"sensitivity": 1.5707963267948966, "pipeline_lock": True})
    add = ctrl.add_route
    add(ChipPortEndpoint(chip=0, port="x16_in"), BlockEndpoint(block=fm, port="x"), [])
    add(BlockEndpoint(block=fm, port="yi"), ChipPortEndpoint(chip=0, port="x16_out"), [])
    add(BlockEndpoint(block=fm, port="yq"), ChipPortEndpoint(chip=0, port="x16_out"), [])
    ctrl.auto_route_all({key: ct}, auto_orient=True, use_bus="always")

    def _fm_net(port):
        return next((c for c in ctrl.project.connections
                     if getattr(c.source, "block", "") == fm
                     and getattr(c.source, "port", "") == port), None)

    yi, yq = _fm_net("yi"), _fm_net("yq")
    assert yi is not None and yq is not None
    assert yi.is_routed and yq.is_routed, "auto-route seeds both egress rails routed"
    yi_path = [(p.x, p.y) for p in yi.route]

    # MANUAL MOVE the FM one row down (the user's 'moved' step) — unroutes both rails.
    MoveBlockCommand(ctrl.project, fm, 0, 1).execute()
    yi, yq = _fm_net("yi"), _fm_net("yq")
    assert not yq.is_routed, "the move leaves the egress rails unrouted (fly lines)"

    # MANUAL ROUTE of the yi output only, on the shifted path — must co-route yq.
    shifted = [(x, y + 1) for (x, y) in yi_path]
    ctrl.add_route(yi.source, yi.target, shifted)
    assert _fm_net("yi").is_routed, "the drawn yi rail must be routed"
    assert _fm_net("yq").is_routed, (
        "yq complex-egress sibling must route with the yi draw — no orphan fly line")


if __name__ == "__main__":
    app = QApplication.instance() or QApplication([])
    cat = BlockCatalog.from_gr_kyttar()
    test_routing_one_iq_rail_routes_the_sibling(app, cat)
    print("I/Q sibling route: PASS")
    test_non_iq_route_has_no_spurious_siblings(app, cat)
    print("no spurious siblings: PASS")
