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


_FSK4_MOVED = EXAMPLES_DIR / "fsk4_modem" / "fsk4_modem.moved.kyt"


@pytest.mark.skipif(not _FSK4_MOVED.exists(),
                    reason="fsk4 modem moved .kyt absent")
def test_routing_complex_egress_yi_routes_yq_sibling(qapp, catalog):
    """COMPLEX EGRESS to the chip OUTPUT port: the FrequencyModulator emits yi
    (out_tag 10) and yq (out_tag 11) from its ONE emit cell to x16_out; the port
    de-interleaves by tag, so both rails ride ONE corridor. Manually routing the yi
    rail (net5) MUST co-route the yq rail (net11) — else yq stays a fly line + a "no
    physical route" DRC error, which is EXACTLY the bug the user hit after moving the
    FM and routing its output. The target is a ChipPort (not a block cell), so the
    old cell-pair sibling match missed it. Pinned to the user's real saved placement."""
    from engine.io.project_io import load_project

    prj = load_project(str(_FSK4_MOVED))
    ctrl = AppController(catalog=catalog)
    ctrl.project = prj
    net5 = prj.connection("net5")     # frequencymodulator.yi -> x16_out, out_tag 10
    net11 = prj.connection("net11")   # frequencymodulator.yq -> x16_out, out_tag 11
    assert net5 is not None and net11 is not None
    assert not net11.is_routed, "yq starts unrouted (the fly line)"
    pts = [(p.x, p.y) for p in net5.route] if not isinstance(net5.route, str) \
        else [(0, 0)]
    # Re-draw the route on the yi rail (the user's manual 'route the FM output').
    ctrl.add_route(net5.source, net5.target, pts)
    assert prj.connection("net5").is_routed, "the drawn yi rail must be routed"
    assert prj.connection("net11").is_routed, (
        "yq complex-egress sibling must route with the same draw — no orphan fly line")


if __name__ == "__main__":
    app = QApplication.instance() or QApplication([])
    cat = BlockCatalog.from_gr_kyttar()
    test_routing_one_iq_rail_routes_the_sibling(app, cat)
    print("I/Q sibling route: PASS")
    test_non_iq_route_has_no_spurious_siblings(app, cat)
    print("no spurious siblings: PASS")
