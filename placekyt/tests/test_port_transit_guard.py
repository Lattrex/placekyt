# SPDX-License-Identifier: GPL-3.0-or-later
"""USED chip-port cell transit guard (bus_drc check (d), 2026-08-16).

The FLLBandEdge finding: an 8-wide block ring pinches both side channels against
the corner chip ports, and the router (the MAZE escalation was the shipping path;
the bus router's foreign-port penalty was only SOFT) wrapped the block's output
corridor THROUGH the x16_in port cell + its delivery broker — route "ok", build
"ok", chip silently dead (the injected words' landing is destroyed by the
transiting corridor's face programming; injections swallowed in 6 sim events).

These tests pin the closure at every level:
  * the full auto-route pipeline NEVER ships a corridor through a USED port cell
    (either it reroutes or the net is a NAMED failure) — the INV-4 pin that fails
    pre-fix (rep.ok was True with the corridor through (0, 0));
  * the bus router NAMES the port-transit hazard when it is what blocked a net;
  * ``check_bus`` / project DRC flag a HAND-LAID route through a used port cell,
    while a route through an UNUSED port cell and the port's own nets stay legal
    (the documented column-9 passage / direct-injection idioms).
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication  # noqa: E402

from engine.bus_drc import check_bus, check_port_transits  # noqa: E402
from engine.bus_router import route_all_bus  # noqa: E402
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
    def f(bt, lib, params=None):
        pm = catalog.port_map(bt, params=params, library=lib)
        return {p.name: (p.cell_id, p.direction) for p in pm.ports}
    return f


def _legacy_ring_layout(self):
    """The FLL's ORIGINAL 8-wide perimeter-RING ``default_layout`` (verbatim,
    pre-2026-08-17 serpentine re-fold): the geometry of the historical
    port-pinch finding. The guard under test is a ROUTER property, so the
    regression keeps pinning the once-shipped pinch layout even though the
    catalog block now folds ≤7 wide (which no longer pinches)."""
    need = self.cell_count + 1
    best = None
    for w in range(4, 9):
        for h in range(3, 9):
            p = 2 * (w + h) - 4
            if p >= need:
                key = (p, w * h, -w)
                if best is None or key < best[0]:
                    best = (key, w, h)
    w, h = best[1], best[2]
    slots = []
    for x in range(w):                      # top row, west -> east
        slots.append((x, 0, "east" if x < w - 1 else "south"))
    for y in range(1, h):                   # east column, downward
        slots.append((w - 1, y, "south" if y < h - 1 else "west"))
    for x in range(w - 2, -1, -1):          # bottom row, east -> west
        slots.append((x, h - 1, "west" if x > 0 else "north"))
    for y in range(h - 2, 0, -1):           # west column, upward
        slots.append((0, y, "north"))
    ids = ["phase", "sin_fold", "cos_fold", "table_sin", "table_cos",
           "rotate", "fanout"]
    ids += [f"ci{m}" for m in range(self._n_chain)]
    ids += [f"cq{m}" for m in range(self._n_chain)]
    ids += ["berr", "pi"]
    layout = {cid: slots[i] for i, cid in enumerate(ids)}
    for t, i in enumerate(range(len(ids), len(slots))):
        layout[f"transit_fb_{t}"] = slots[i]
    return layout


@pytest.fixture()
def legacy_fll_ring():
    """Swap the FLL back to its original 8-wide ring for the pinch tests (and
    restore after) — see :func:`_legacy_ring_layout`."""
    from gr_kyttar.placement.blocks.fll_band_edge_block import FLLBandEdgeBlock
    orig = FLLBandEdgeBlock.default_layout
    FLLBandEdgeBlock.default_layout = _legacy_ring_layout
    try:
        yield
    finally:
        FLLBandEdgeBlock.default_layout = orig


def _pinched_fll_project(catalog):
    """The FLLBandEdge pinch verbatim: the (legacy-layout) 8-wide ring at
    anchor (1, 1) leaves only row 0 + the corner port columns as channels, so
    the fanout→Costas corridor's only path wraps through a USED corner port
    cell. Callers MUST hold the ``legacy_fll_ring`` fixture."""
    ctrl = AppController(catalog=catalog)
    ctrl.new_project("fll_pinch", "kyttar_10x12")
    fll = ctrl.place_block("FLLBandEdgeBlock", 0, 1, 1,
                           library="lattrex.official",
                           params={"filter_size": 17, "bandwidth": 0.1})
    cos = ctrl.place_block("ComplexCostasLoopBlock", 0, 1, 6,
                           library="lattrex.official",
                           params={"loop_bw": 0.05, "order": 2})
    ctrl.add_logical_connection(ChipPortEndpoint(chip=0, port="x16_in"),
                                BlockEndpoint(block=fll, port="xi"),
                                name="in_xi")
    ctrl.add_logical_connection(ChipPortEndpoint(chip=0, port="x16_in"),
                                BlockEndpoint(block=fll, port="xq"),
                                name="in_xq")
    ctrl.add_logical_connection(BlockEndpoint(block=fll, port="yi_tap"),
                                BlockEndpoint(block=cos, port="xi"),
                                name="mid_i")
    ctrl.add_logical_connection(BlockEndpoint(block=cos, port="yi_tap"),
                                ChipPortEndpoint(chip=0, port="x16_out"),
                                name="chain_out")
    return ctrl


def _used_port_cells(ctrl, ct):
    cell_of = {p.name: (p.cell_x, p.cell_y) for p in ct.ports}
    used = set()
    for conn in ctrl.project.connections:
        for ep in (conn.source, conn.target):
            if isinstance(ep, ChipPortEndpoint) and ep.port in cell_of:
                used.add(cell_of[ep.port])
    return used


def test_pinched_corridor_never_ships_through_used_port(qapp, catalog, chip_type,
                                                        legacy_fll_ring):
    """THE INV-4 PIN (fails pre-fix): the full auto-route pipeline on the FLL
    pinch geometry must NOT return ok with a corridor through a used port cell.
    Pre-fix the maze escalation shipped mid_i as
    (7,1)…(1,0),(0,0),(0,1)…(0,6) — through x16_in AND its delivery broker —
    and rep.ok was True (the silent dead chip). Post-fix the hazardous nets are
    NAMED failures (this geometry has no port-free detour)."""
    ctrl = _pinched_fll_project(catalog)
    rep = ctrl.auto_route_all({"kyttar_10x12": chip_type})
    used = _used_port_cells(ctrl, chip_type)
    # No routed corridor may OCCUPY a used port cell it does not own.
    for conn in ctrl.project.connections:
        if not isinstance(conn.route, list) or not conn.route:
            continue
        own = set()
        for ep in (conn.source, conn.target):
            if isinstance(ep, ChipPortEndpoint):
                p = chip_type.port(ep.port)
                own.add((p.cell_x, p.cell_y))
        hit = [(p.x, p.y) for p in conn.route
               if (p.x, p.y) in used and (p.x, p.y) not in own]
        assert not hit, (
            f"net '{conn.name}' SHIPPED through used port cell(s) {hit} — "
            "the silent-dead-chip corridor the guard exists to forbid")
    # The pinch is genuinely unroutable without the port cell: sound NAMED failure.
    assert not rep.ok, "the pinched corridor routed 'ok' — it must be impossible"
    for r in rep.failed:
        assert r.reason, f"failed net '{r.name}' carries no reason (silent failure)"


def test_bus_router_names_the_port_transit_hazard(qapp, catalog, chip_type,
                                                  legacy_fll_ring):
    """The bus router itself (route_all_bus, the named-failure path) explains the
    pinch: the failing net's reason names the port-transit hazard, not a generic
    'no path' (the diagnostic relaxed-probe naming)."""
    ctrl = _pinched_fll_project(catalog)
    rep = route_all_bus(ctrl.project, {"kyttar_10x12": chip_type},
                        _port_cells(catalog))
    assert not rep.ok
    reasons = [r.reason or "" for r in rep.failed]
    assert any("port-transit hazard" in rs for rs in reasons), reasons


def _gain_wall_project(catalog, *, use_port):
    """A tiny hand-laid geometry: gA at (0,1) walled so its only corridor to the
    consumer's broker rides through the corner port cell (0,0). ``use_port``
    wires x16_in (making (0,0) a USED input port) or leaves it unused."""
    ctrl = AppController(catalog=catalog)
    ctrl.new_project("wall", "kyttar_10x12")
    gA = ctrl.place_block("GainBlock", 0, 0, 1, params={"gain": 0.5},
                          library="lattrex.official")
    gB = ctrl.place_block("GainBlock", 0, 3, 0, params={"gain": 1.0},
                          library="lattrex.official")
    for cell in ((1, 1), (0, 2)):
        ctrl.place_block("AGCBlock", 0, cell[0], cell[1],
                         library="lattrex.official")
    if use_port:
        ctrl.add_logical_connection(ChipPortEndpoint(chip=0, port="x16_in"),
                                    BlockEndpoint(block=gA, port="sample"),
                                    name="in")
    ctrl.add_logical_connection(BlockEndpoint(block=gA, port="out"),
                                BlockEndpoint(block=gB, port="sample"),
                                name="mid")
    return ctrl


def test_check_bus_flags_hand_laid_route_through_used_port(qapp, catalog,
                                                           chip_type):
    """A HAND-LAID route through a USED input port cell is a named
    ``port_transit`` violation (covering routes no router ever saw)."""
    ctrl = _gain_wall_project(catalog, use_port=True)
    routes = {"in": [(0, 0), (0, 1)],
              "mid": [(0, 1), (0, 0), (1, 0), (2, 0)]}
    viols = check_bus(ctrl.project, routes,
                      {"kyttar_10x12": chip_type})
    port_v = [v for v in viols if v.kind == "port_transit"]
    assert port_v, f"no port_transit violation raised: {[str(v) for v in viols]}"
    assert port_v[0].cell == (0, 0)
    assert port_v[0].nets == ("mid",), (
        "only the RIDING net may be implicated (the port's own nets are "
        f"innocent): {port_v[0].nets}")


def test_unused_port_cell_stays_a_legal_routing_cell(qapp, catalog, chip_type):
    """The same corridor over the SAME port cell is legal when the port is
    UNUSED (a plain routing cell — the documented passage case)."""
    ctrl = _gain_wall_project(catalog, use_port=False)
    routes = {"mid": [(0, 1), (0, 0), (1, 0), (2, 0)]}
    viols = check_port_transits(ctrl.project, routes,
                                {"kyttar_10x12": chip_type})
    assert viols == [], [str(v) for v in viols]


def test_own_port_endpoints_are_not_violations(qapp, catalog, chip_type):
    """The port's OWN nets legitimately start at the port cell (direct
    injection / delivery) — never flagged."""
    ctrl = _gain_wall_project(catalog, use_port=True)
    routes = {"in": [(0, 0), (0, 1)]}
    viols = check_port_transits(ctrl.project, routes,
                                {"kyttar_10x12": chip_type})
    assert viols == [], [str(v) for v in viols]


def test_project_drc_surfaces_port_transit_as_error(qapp, catalog, chip_type):
    """engine.drc.check_project (the DRC panel / build gate path) surfaces the
    hand-laid port transit as a hard ``port_transit`` ERROR."""
    from commands import SetConnectionRouteCommand  # noqa: PLC0415
    from engine.drc import check_project  # noqa: PLC0415

    ctrl = _gain_wall_project(catalog, use_port=True)
    SetConnectionRouteCommand(ctrl.project, "mid",
                              [(0, 1), (0, 0), (1, 0), (2, 0)]).execute()
    res = check_project(ctrl.project, {"kyttar_10x12": chip_type},
                        catalog=catalog)
    cats = [e.category for e in res.errors]
    assert "port_transit" in cats, cats
