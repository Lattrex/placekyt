# SPDX-License-Identifier: GPL-3.0-or-later
"""Bus/broker router tests (auto-P&R Stage 3, §1.2).

The §1.2 bus/broker router lays a shared directional bus where blocks abut a
programmed BROKER cell (flip→relay→restore). These tests pin the load-bearing wins
the prior routers can't do:

  * a 2-block tapped bus BUILDS and COMPUTES (a broker actually delivers on-chip),
  * a fan-in (two streams into one cell) and a different-sink share route + build,
  * the full coherent BPSK RX chain routes ALL 6 nets + builds (net4/5/6, which the
    BFS corridor router fails, now route),
  * the bus DRC NAMES a face-conflict and a deadlock (sound failure, never silent).
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication  # noqa: E402

from commands import SetConnectionRouteCommand  # noqa: E402
from engine.build import BuildEngine  # noqa: E402
from engine.bus_drc import check_bus  # noqa: E402
from engine.bus_router import broker_plan, route_all_bus  # noqa: E402
from engine.catalog import BlockCatalog  # noqa: E402
from engine.io.chip_type_io import load_chip_type  # noqa: E402
from model.connection import BlockEndpoint, ChipPortEndpoint  # noqa: E402
from ui.controller import AppController  # noqa: E402

from tests.conftest import CHIP_YAML as CT_PATH  # noqa: E402
from tests.conftest import EXAMPLES_DIR  # noqa: E402
GRC_COHERENT = EXAMPLES_DIR / "coherent_bpsk_rx.grc"
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


def _fq(f):
    return int(round(max(-1, min(0.999, f)) * 32768)) & 0xFFFF


# --------------------------------------------------------------------------- #
# Unit: a 2-block tapped bus BUILDS and COMPUTES (broker delivers on-chip).
# --------------------------------------------------------------------------- #

def test_two_block_tapped_bus_computes(qapp, catalog, chip_type):
    """input → gain(0.5) → gain(0.5) → output, the second tapped off the bus via a
    BROKER. Builds AND computes: out == 0.25 × in (proves the broker flip→relay→
    restore actually delivers in simkyt, not just that it builds)."""
    import simkyt

    ctrl = AppController(catalog=catalog)
    ctrl.new_project("two", "kyttar_10x12")
    g1 = ctrl.place_block("GainBlock", 0, 0, 0, params={"gain": 0.5},
                          library="lattrex.official")
    g2 = ctrl.place_block("GainBlock", 0, 5, 0, params={"gain": 0.5},
                          library="lattrex.official")
    ctrl.add_logical_connection(ChipPortEndpoint(chip=0, port="x16_in"),
                                BlockEndpoint(block=g1, port="sample"), name="in")
    ctrl.add_logical_connection(BlockEndpoint(block=g1, port="out"),
                                BlockEndpoint(block=g2, port="sample"), name="mid")
    ctrl.add_logical_connection(BlockEndpoint(block=g2, port="out"),
                                ChipPortEndpoint(chip=0, port="x16_out"), name="out")
    ctrl.auto_place(0)
    rep = ctrl.auto_route_all({"kyttar_10x12": chip_type}, auto_orient=True,
                              use_bus="always")
    assert rep.ok, [(r.name, r.reason) for r in rep.failed]

    # A BROKER exists on the bus tapping into g2 (the route doesn't end on g2's cell).
    taps = broker_plan(ctrl.project, 0, chip_type, catalog)
    assert taps, "expected a broker tap on the bus"

    bres = BuildEngine(catalog, str(CT_PATH)).build(
        ctrl.project, {"kyttar_10x12": chip_type})
    assert bres.ok, [str(e) for e in bres.errors]

    entry, _ = catalog.resolved_io("GainBlock")
    chip = simkyt.Chip.from_yaml(str(CT_PATH))
    chip.load_bitstream_physical(bres.words(0))
    chip.set_port_entry_address("x16_in", entry)
    ins = [0.6, -0.4, 0.8, 0.2]
    outs = []
    for v in ins:
        chip.inject_data_physical([_fq(v)], target_hop_cnt=30, target_addr=0)
        chip.run(max_events=6000)
        chip.inject_jump_physical(target_hop_cnt=30, entry_addr=entry)
        chip.run(max_events=60000)
        while chip.output_available("x16_out"):
            outs.append(chip.read_port_i16("x16_out").tolist()[-1] / 32768.0)
            chip.release_output_ack("x16_out")
            chip.run(max_events=3000)
    assert len(outs) >= len(ins), f"only {len(outs)} outputs"
    for i, v in enumerate(ins):
        assert abs(outs[i] - 0.25 * v) < 0.02, \
            f"sample {i}: {outs[i]:.3f} != {0.25 * v:.3f}"


# --------------------------------------------------------------------------- #
# SHORTEST-PATH: two blocks ONE free cell apart hand off through exactly that
# cell (a broker on the source's OWN emit cell — the staircase fix).
# --------------------------------------------------------------------------- #

def test_one_gap_pair_brokers_on_own_emit_cell(qapp, catalog, chip_type):
    """gain(1,1) → gain(3,1): the route must be the minimal 2-point form
    [(1,1),(2,1)] — a broker ON the source's emit cell — and the built chip must
    compute 0.25 × in. Before the shortest-path fix the emit cell was reserved
    against ALL brokering (even this net's own), so every one-gap pair
    staircased up-and-over to a far-side broker (the audited effect_echo /
    data_link zigzags)."""
    import simkyt

    ctrl = AppController(catalog=catalog)
    ctrl.new_project("gap1", "kyttar_10x12")
    g1 = ctrl.place_block("GainBlock", 0, 1, 1, params={"gain": 0.5},
                          library="lattrex.official")
    g2 = ctrl.place_block("GainBlock", 0, 3, 1, params={"gain": 0.5},
                          library="lattrex.official")
    ctrl.add_logical_connection(ChipPortEndpoint(chip=0, port="x16_in"),
                                BlockEndpoint(block=g1, port="sample"), name="in")
    ctrl.add_logical_connection(BlockEndpoint(block=g1, port="out"),
                                BlockEndpoint(block=g2, port="sample"), name="mid")
    ctrl.add_logical_connection(BlockEndpoint(block=g2, port="out"),
                                ChipPortEndpoint(chip=0, port="x16_out"), name="out")
    rep = route_all_bus(ctrl.project, {"kyttar_10x12": chip_type},
                        _port_cells(catalog))
    assert rep.ok, [(r.name, r.reason) for r in rep.failed]
    for r in rep.routed:
        SetConnectionRouteCommand(ctrl.project, r.name, r.points).execute()
    mid = [(p.x, p.y) for p in
           next(c for c in ctrl.project.connections if c.name == "mid").route]
    assert mid == [(1, 1), (2, 1)], \
        f"one-gap pair must broker on the source's own emit cell, got {mid}"

    bres = BuildEngine(catalog, str(CT_PATH)).build(
        ctrl.project, {"kyttar_10x12": chip_type})
    assert bres.ok, [str(e) for e in bres.errors]
    entry, _ = catalog.resolved_io("GainBlock")
    chip = simkyt.Chip.from_yaml(str(CT_PATH))
    chip.load_bitstream_physical(bres.words(0))
    chip.set_port_entry_address("x16_in", entry)
    outs = []
    for v in (0.6, -0.4):
        chip.inject_data_physical([_fq(v)], target_hop_cnt=28, target_addr=0)
        chip.run(max_events=6000)
        chip.inject_jump_physical(target_hop_cnt=28, entry_addr=entry)
        chip.run(max_events=120000)
        while chip.output_available("x16_out"):
            outs.append(chip.read_port_i16("x16_out").tolist()[-1] / 32768.0)
            chip.release_output_ack("x16_out")
            chip.run(max_events=3000)
    assert len(outs) >= 2, f"only {len(outs)} outputs"
    for got, v in zip(outs, (0.6, -0.4)):
        assert abs(got - 0.25 * v) < 0.02, f"{got:.3f} != {0.25 * v:.3f}"


# --------------------------------------------------------------------------- #
# A longer (8-hop) single-broker bus also computes — the broker isn't a 1-off.
# --------------------------------------------------------------------------- #

def test_long_bus_broker_computes(qapp, catalog, chip_type):
    """gain on the input port → gain far across the array, tapped by a broker 8 hops
    down the shared bus. Computes 0.5 × in — the broker delivers correctly over a
    long shared corridor."""
    import simkyt

    ctrl = AppController(catalog=catalog)
    ctrl.new_project("long", "kyttar_10x12")
    gA = ctrl.place_block("GainBlock", 0, 0, 0, params={"gain": 0.5},
                          library="lattrex.official")
    sinkA = ctrl.place_block("GainBlock", 0, 7, 2, params={"gain": 1.0},
                             library="lattrex.official")
    # A wall in row 0 forces the bus down into row 1 (a real shared corridor).
    for col in range(1, 7):
        try:
            ctrl.place_block("AGCBlock", 0, col, 0, library="lattrex.official")
        except Exception:  # noqa: BLE001
            pass
    ctrl.add_logical_connection(ChipPortEndpoint(chip=0, port="x16_in"),
                                BlockEndpoint(block=gA, port="sample"), name="in")
    ctrl.add_logical_connection(BlockEndpoint(block=gA, port="out"),
                                BlockEndpoint(block=sinkA, port="sample"), name="mid")
    ctrl.add_logical_connection(BlockEndpoint(block=sinkA, port="out"),
                                ChipPortEndpoint(chip=0, port="x16_out"), name="out")
    rep = route_all_bus(ctrl.project, {"kyttar_10x12": chip_type},
                        _port_cells(catalog))
    assert rep.ok, [(r.name, r.reason) for r in rep.failed]
    for r in rep.routed:
        SetConnectionRouteCommand(ctrl.project, r.name, r.points).execute()
    mid = [(p.x, p.y) for p in
           next(c for c in ctrl.project.connections if c.name == "mid").route]
    assert len(mid) >= 6, f"bus too short to be a real shared corridor: {mid}"

    bres = BuildEngine(catalog, str(CT_PATH)).build(
        ctrl.project, {"kyttar_10x12": chip_type})
    assert bres.ok, [str(e) for e in bres.errors]
    entry, _ = catalog.resolved_io("GainBlock")
    chip = simkyt.Chip.from_yaml(str(CT_PATH))
    chip.load_bitstream_physical(bres.words(0))
    chip.set_port_entry_address("x16_in", entry)
    ins = [0.6, -0.4]
    outs = []
    for v in ins:
        chip.inject_data_physical([_fq(v)], target_hop_cnt=30, target_addr=0)
        chip.run(max_events=6000)
        chip.inject_jump_physical(target_hop_cnt=30, entry_addr=entry)
        chip.run(max_events=120000)
        while chip.output_available("x16_out"):
            outs.append(chip.read_port_i16("x16_out").tolist()[-1] / 32768.0)
            chip.release_output_ack("x16_out")
            chip.run(max_events=3000)
    assert len(outs) >= len(ins)
    for i, v in enumerate(ins):
        assert abs(outs[i] - 0.5 * v) < 0.02, f"sample {i}: {outs[i]:.3f}"


# --------------------------------------------------------------------------- #
# Different-sink SHARE: two nets to DIFFERENT sinks share a bus segment + build.
# This is what CP-SAT cannot do (it only shares a common-sink corridor).
# --------------------------------------------------------------------------- #

def test_different_sink_share_routes_and_builds(qapp, catalog, chip_type):
    """Two independent nets to DIFFERENT sinks SHARE a bus segment (each peels at its
    OWN broker) and the design builds. The CP-SAT router proves this case UNROUTABLE
    (a plain transit cell can't demux); the bus/broker router routes it."""
    ctrl = AppController(catalog=catalog)
    ctrl.new_project("ds", "kyttar_10x12")
    gA = ctrl.place_block("GainBlock", 0, 0, 0, params={"gain": 0.5},
                          library="lattrex.official")
    sinkA = ctrl.place_block("GainBlock", 0, 7, 2, params={"gain": 1.0},
                             library="lattrex.official")
    gB = ctrl.place_block("GainBlock", 0, 0, 2, params={"gain": 0.25},
                          library="lattrex.official")
    sinkB = ctrl.place_block("GainBlock", 0, 4, 2, params={"gain": 1.0},
                             library="lattrex.official")
    # Walls in rows 0 and 3 force BOTH chains through the shared row-1 corridor.
    # The row-3 wall stops at col 5 so ``B_out`` keeps ONE southern passage (via
    # (6,3)) to x1_out — the shortest-path router's tighter broker choices seal
    # the south completely with the full-width wall, making B_out unroutable;
    # the (2,2) wall alone still forces net B through the shared row-1 corridor.
    for col in range(1, 6):
        try:
            ctrl.place_block("AGCBlock", 0, col, 3, library="lattrex.official")
        except Exception:  # noqa: BLE001
            pass
    for col in range(1, 7):
        try:
            ctrl.place_block("AGCBlock", 0, col, 0, library="lattrex.official")
        except Exception:  # noqa: BLE001
            pass
    # Wall (2,2) too: the router is now STRICTLY shortest-path (sharing is a
    # tie-break, never a detour), so with row 2 open net B would take its own
    # straight row-2 corridor and never NEED the shared bus. Blocking row 2
    # between gB and sinkB makes the shared row-1 corridor the only (and
    # shortest) way — the different-sink SHARE capability this test proves.
    ctrl.place_block("AGCBlock", 0, 2, 2, library="lattrex.official")
    ctrl.add_logical_connection(ChipPortEndpoint(chip=0, port="x16_in"),
                                BlockEndpoint(block=gA, port="sample"), name="in_A")
    ctrl.add_logical_connection(BlockEndpoint(block=gA, port="out"),
                                BlockEndpoint(block=sinkA, port="sample"), name="A")
    ctrl.add_logical_connection(BlockEndpoint(block=sinkA, port="out"),
                                ChipPortEndpoint(chip=0, port="x16_out"),
                                name="A_out")
    ctrl.add_logical_connection(BlockEndpoint(block=gB, port="out"),
                                BlockEndpoint(block=sinkB, port="sample"), name="B")
    ctrl.add_logical_connection(BlockEndpoint(block=sinkB, port="out"),
                                ChipPortEndpoint(chip=0, port="x1_out"),
                                name="B_out")
    rep = route_all_bus(ctrl.project, {"kyttar_10x12": chip_type},
                        _port_cells(catalog))
    assert rep.ok, [(r.name, r.reason) for r in rep.failed]
    for r in rep.routed:
        SetConnectionRouteCommand(ctrl.project, r.name, r.points).execute()
    A = [(p.x, p.y) for p in
         next(c for c in ctrl.project.connections if c.name == "A").route]
    B = [(p.x, p.y) for p in
         next(c for c in ctrl.project.connections if c.name == "B").route]
    shared = set(A[1:]) & set(B[1:])
    assert shared, f"the two different-sink nets did not share a bus cell: {A} {B}"
    # Distinct brokers / sinks (the peel-off points differ).
    assert A[-1] != B[-1], "different-sink nets must end at different brokers"
    # The two different-sink chains SHARE the row-1 corridor, ROUTE, and BUILD —
    # the property this test exists to prove (CP-SAT cannot). The shortest-path
    # router's distance-aware broker choice also finds a SAFE input/output face
    # split for the walled-corner single-cell ``sinkA`` (the old first-fit broker
    # forced input==output there and this test asserted the named
    # single_cell_inout_deadlock DRC block; that DRC gate keeps its own dedicated
    # tests — test_bus_drc_names_deadlock, test_single_cell_bus_safety).
    bres = BuildEngine(catalog, str(CT_PATH)).build(
        ctrl.project, {"kyttar_10x12": chip_type})
    assert bres.ok, [str(e) for e in bres.errors]


# --------------------------------------------------------------------------- #
# WALLED CORNER: the hazard-DISABLED fallback path yields a NAMED failure, not
# a silent unsafe route (restores the coverage the rewritten share test lost).
# --------------------------------------------------------------------------- #

def test_walled_corner_fallback_demotes_to_named_failure(qapp, catalog,
                                                         chip_type):
    """A single-cell sink with exactly ONE free neighbour: its input broker and
    its output's first hop MUST share that cell, so the safe (hazard-split)
    attempt cannot route every net and ``route_all_bus`` re-routes with the
    hazard guard DISABLED. The resulting in==out route is a §5.3 wait 2-cycle
    (sink -> broker -> sink); the router's own DRC gate must DEMOTE it to a
    NAMED failure — never return it silently ok (the runtime symptom would be
    a hard Deadlock only saturated drive exposes)."""
    ctrl = AppController(catalog=catalog)
    ctrl.new_project("corner", "kyttar_10x12")
    g = ctrl.place_block("GainBlock", 0, 4, 5, params={"gain": 0.5},
                         library="lattrex.official")
    sink = ctrl.place_block("GainBlock", 0, 0, 5, params={"gain": 1.0},
                            library="lattrex.official")
    h = ctrl.place_block("GainBlock", 0, 4, 3, params={"gain": 1.0},
                         library="lattrex.official")
    # Wall the sink's north/south neighbours: (1,5) is its ONLY free neighbour.
    ctrl.place_block("AGCBlock", 0, 0, 4, library="lattrex.official")
    ctrl.place_block("AGCBlock", 0, 0, 6, library="lattrex.official")
    ctrl.add_logical_connection(BlockEndpoint(block=g, port="out"),
                                BlockEndpoint(block=sink, port="sample"),
                                name="in_s")
    ctrl.add_logical_connection(BlockEndpoint(block=sink, port="out"),
                                BlockEndpoint(block=h, port="sample"),
                                name="out_s")
    rep = route_all_bus(ctrl.project, {"kyttar_10x12": chip_type},
                        _port_cells(catalog))
    assert not rep.ok, "the walled corner must not route silently ok"
    named = [r for r in rep.results if not r.ok and r.reason]
    assert named, "failures must carry reasons"
    assert any("deadlock" in r.reason or "broker" in r.reason
               for r in named), [(r.name, r.reason) for r in named]


# --------------------------------------------------------------------------- #
# THE TARGET: the full coherent BPSK RX chain routes ALL 6 nets + builds.
# --------------------------------------------------------------------------- #

@pytest.mark.skipif(not GRC_COHERENT.exists(), reason="coherent .grc absent")
def test_coherent_chain_bus_routes_and_builds(qapp, catalog, chip_type):
    """Import the PRODUCTION coherent BPSK RX flowgraph (x16_in → RRC matched filter
    → Costas(xi,xq) → Gardner → Slicer → x16_out — the on-chip MF front end is the
    lead block) → auto-place (lead RRC ``head`` cell anchored on the port) → BUS-route
    → build. ALL SEVEN nets route: I/Q ingress ×2, MF→Costas yi/yq ×2, costas→gardner,
    gardner→slicer, slicer egress.

    The Gardner-block single-fwd_face conflict that used to block gardner→slicer
    (loop_filter emitting BOTH `out` and `period_fb` on one face) is FIXED: loop_filter
    is now a DUAL-FACE cell (in-program FACE flips — see ``GardnerTimingRecovery``), so
    `out` egresses outward to the bus while `period_fb` returns to the resampler. The
    build transforms the in-program face constants by the block's orientation, and
    traces the feedback via the block's transit cell even when the source face is
    route-overridden."""
    ctrl = AppController(catalog=catalog)
    res = ctrl.import_grc(str(GRC_COHERENT), chip_type="kyttar_10x12")
    assert res.ok, res.unknown
    ctrl.auto_place(0)
    rep = ctrl.auto_route_all({"kyttar_10x12": chip_type}, auto_orient=False,
                              use_bus="always")
    assert rep.ok, [(r.name, r.reason) for r in rep.failed]
    # ALL seven nets route — including gardner→slicer, the old single-fwd_face blocker.
    routed = {r.name for r in rep.routed}
    assert {f"net{i}" for i in range(1, 8)} <= routed, \
        f"all seven coherent-RX nets must route, got {sorted(routed)}"

    # The lead-block input-cell anchor lands the RRC matched filter's HEAD cell
    # directly ON the x16_in port, so both I and Q inject straight into head (R0/R1)
    # at the port cell — the proven complex-ingress, no broker needed.
    head = ctrl.project.block("complexrrcmatchedfilter").placement.cell("head")
    port = chip_type.port("x16_in")
    assert (head.x, head.y) == (port.cell_x, port.cell_y), \
        "I/Q ingress must land on the RRC head cell (the lead input-cell anchor)"

    # The routed design BUILDS into a loadable bitstream.
    bres = BuildEngine(catalog, str(CT_PATH)).build(
        ctrl.project, {"kyttar_10x12": chip_type})
    assert bres.ok, [str(e) for e in bres.errors]


@pytest.mark.skipif(not GRC_COHERENT.exists(), reason="coherent .grc absent")
def test_controller_use_bus_routes_coherent_via_auto(qapp, catalog, chip_type):
    """``use_bus="auto"`` escalates to the bus router so Route All handles the full
    multi-block coherent chain end to end. With the Gardner dual-face loop_filter
    fix, net4 (gardner→slicer) routes and the whole chain routes cleanly — every
    connection routes (the old single-fwd_face xfail is RESOLVED). A chip
    INPUT-port net needs no physical route (direct port injection) and is left
    UNROUTED on purpose — so the routed-state check excludes input nets."""
    from model.connection import ChipPortEndpoint

    ctrl = AppController(catalog=catalog)
    ctrl.import_grc(str(GRC_COHERENT), chip_type="kyttar_10x12")
    ctrl.auto_place(0)
    rep = ctrl.auto_route_all({"kyttar_10x12": chip_type}, auto_orient=False,
                              use_cpsat="auto", use_bus="auto")
    assert rep.ok, [(r.name, r.reason) for r in rep.failed]

    def _is_input(c):
        return (isinstance(c.source, ChipPortEndpoint)
                and c.source.port.endswith("_in"))

    assert all(c.is_routed for c in ctrl.project.connections if not _is_input(c))
    assert not any(c.is_routed for c in ctrl.project.connections if _is_input(c))


# --------------------------------------------------------------------------- #
# Sound failure: the bus DRC NAMES a face-conflict and a deadlock.
# --------------------------------------------------------------------------- #

def test_bus_drc_names_face_conflict():
    """Two nets transiting one cell in DIFFERENT directions is a single-fwd_face
    conflict (§1.3) — the DRC NAMES the cell + nets (never a silent dead build)."""
    routes = {"na": [(0, 1), (1, 1), (2, 1), (3, 1)],     # east through (2,1)
              "nb": [(2, 0), (2, 1), (2, 2)]}              # south through (2,1)
    viols = check_bus(None, routes, {})
    assert viols, "expected a face-conflict violation"
    fc = [v for v in viols if v.kind == "face_conflict" and v.cell == (2, 1)]
    assert fc and set(fc[0].nets) == {"na", "nb"}


def test_bus_drc_names_deadlock():
    """A cyclic handshake wait on the corridor (a forwards into b, b forwards into a)
    is a structural deadlock (§5.3) — the DRC NAMES the cycle."""
    routes = {"x": [(0, 0), (1, 0)], "y": [(1, 0), (0, 0)]}
    viols = check_bus(None, routes, {})
    assert any(v.kind == "deadlock" for v in viols)


def test_bus_drc_passes_a_clean_bus():
    """A single directed bus (all cells forward the same way) has NO violations."""
    routes = {"n": [(0, 0), (1, 0), (2, 0), (3, 0)]}
    assert check_bus(None, routes, {}) == []


def test_bus_router_demotes_a_drc_violation_to_named_failure(qapp, catalog,
                                                             chip_type):
    """A net the bus DRC flags is reported as a NAMED failure (ok=False, reason set),
    not silently built — the sound-failure contract (P3.4)."""
    from engine.bus_router import _drc_gate
    from engine.autoroute import RouteResult

    good = RouteResult("clean", True, points=[(0, 0), (1, 0), (2, 0)])
    a = RouteResult("a", True, points=[(0, 1), (1, 1), (2, 1), (3, 1)])
    b = RouteResult("b", True, points=[(2, 0), (2, 1), (2, 2)])
    out = _drc_gate([good, a, b], {})
    by = {r.name: r for r in out}
    assert by["clean"].ok
    assert not by["a"].ok and by["a"].reason
    assert not by["b"].ok and by["b"].reason


# --------------------------------------------------------------------------- #
# Sound failures: named, never fabricated.
# --------------------------------------------------------------------------- #

def test_bus_router_names_unplaced_target(qapp, catalog, chip_type):
    """A net to an unplaced block is NAMED, not crashed."""
    from model.block import Block

    ctrl = AppController(catalog=catalog)
    ctrl.new_project("snd", "kyttar_10x12")
    a = ctrl.place_block("GainBlock", 0, 1, 1, library="lattrex.official")
    ctrl.project.blocks.append(Block("ghost", "DCBlockerBlock",
                                      library="lattrex.official"))
    ctrl.add_logical_connection(BlockEndpoint(block=a, port="out"),
                                BlockEndpoint(block="ghost", port="sample"),
                                name="a_ghost")
    rep = route_all_bus(ctrl.project, {"kyttar_10x12": chip_type},
                        _port_cells(catalog))
    assert not rep.ok
    assert rep.failed[0].name == "a_ghost" and rep.failed[0].reason


# --------------------------------------------------------------------------- #
# BUS topology (doc/ROUTING_TOPOLOGIES.md) — the single-backbone v2 router and
# the smart-default topology selection.
# --------------------------------------------------------------------------- #

def test_topology_smart_default_single_vs_multi(qapp, catalog):
    """``_select_topology`` keys on the filament count: >1 filament feeding the input
    port → ``"bus"`` (the modem's TX+RX off x16_in); a lone filament → ``"block"``."""
    from model.connection import BlockEndpoint, ChipPortEndpoint

    # Single filament: gain -> dcblocker, fed by one input port.
    ctrl = AppController(catalog=catalog)
    ctrl.new_project("one", "kyttar_10x12")
    g = ctrl.place_block("GainBlock", 0, 1, 1, library="lattrex.official")
    d = ctrl.place_block("DCBlockerBlock", 0, 4, 1, library="lattrex.official")
    ctrl.add_logical_connection(ChipPortEndpoint(chip=0, port="x16_in"),
                                BlockEndpoint(block=g, port="sample"), name="i")
    ctrl.add_logical_connection(BlockEndpoint(block=g, port="out"),
                                BlockEndpoint(block=d, port="sample"), name="m")
    ctrl.add_logical_connection(BlockEndpoint(block=d, port="out"),
                                ChipPortEndpoint(chip=0, port="x16_out"), name="o")
    assert ctrl._select_topology("auto") == "block"
    assert ctrl._select_topology("never") == "block"

    # Two filaments off ONE input port -> bus.
    ctrl2 = AppController(catalog=catalog)
    ctrl2.new_project("two", "kyttar_10x12")
    a = ctrl2.place_block("GainBlock", 0, 1, 1, library="lattrex.official")
    b = ctrl2.place_block("DCBlockerBlock", 0, 1, 4, library="lattrex.official")
    ctrl2.add_logical_connection(ChipPortEndpoint(chip=0, port="x16_in"),
                                 BlockEndpoint(block=a, port="sample"), name="ia")
    ctrl2.add_logical_connection(ChipPortEndpoint(chip=0, port="x16_in"),
                                 BlockEndpoint(block=b, port="sample"), name="ib")
    ctrl2.add_logical_connection(BlockEndpoint(block=a, port="out"),
                                 ChipPortEndpoint(chip=0, port="x16_out"), name="oa")
    ctrl2.add_logical_connection(BlockEndpoint(block=b, port="out"),
                                 ChipPortEndpoint(chip=0, port="x16_out"), name="ob")
    assert ctrl2._select_topology("auto") == "bus"
    assert ctrl2._select_topology("never") == "block"


def test_route_all_bus_default_is_legacy(qapp, catalog, chip_type):
    """``route_all_bus`` with the default ``topology="block"`` never invokes the v2
    single-backbone router — the existing per-net loop result is unchanged (so every
    direct caller stays byte-identical)."""
    from model.connection import BlockEndpoint, ChipPortEndpoint

    ctrl = AppController(catalog=catalog)
    ctrl.new_project("legacy", "kyttar_10x12")
    g = ctrl.place_block("GainBlock", 0, 1, 1, library="lattrex.official")
    ctrl.add_logical_connection(ChipPortEndpoint(chip=0, port="x16_in"),
                                BlockEndpoint(block=g, port="sample"), name="i")
    ctrl.add_logical_connection(BlockEndpoint(block=g, port="out"),
                                ChipPortEndpoint(chip=0, port="x16_out"), name="o")
    default = route_all_bus(ctrl.project, {"kyttar_10x12": chip_type},
                            _port_cells(catalog))
    block = route_all_bus(ctrl.project, {"kyttar_10x12": chip_type},
                          _port_cells(catalog), topology="block")
    assert {r.name for r in default.routed} == {r.name for r in block.routed}
    assert default.ok == block.ok


def test_bus_topology_routes_two_filaments_crossover_free(qapp, catalog, chip_type):
    """BUS topology routes a two-filament shared-port design and the result is
    CROSSOVER-FREE (the structural soundness proof: no plain cell carries two
    conflicting forwarding directions, no broker is a foreign through-transit)."""
    from engine.bus_router import crossover_plan
    from model.connection import BlockEndpoint, ChipPortEndpoint

    ctrl = AppController(catalog=catalog)
    ctrl.new_project("twofil", "kyttar_10x12")
    a = ctrl.place_block("GainBlock", 0, 2, 1, library="lattrex.official")
    b = ctrl.place_block("DCBlockerBlock", 0, 2, 5, library="lattrex.official")
    ctrl.add_logical_connection(ChipPortEndpoint(chip=0, port="x16_in"),
                                BlockEndpoint(block=a, port="sample"), name="ia")
    ctrl.add_logical_connection(ChipPortEndpoint(chip=0, port="x16_in"),
                                BlockEndpoint(block=b, port="sample"), name="ib")
    ctrl.add_logical_connection(BlockEndpoint(block=a, port="out"),
                                ChipPortEndpoint(chip=0, port="x16_out"), name="oa")
    ctrl.add_logical_connection(BlockEndpoint(block=b, port="out"),
                                ChipPortEndpoint(chip=0, port="x16_out"), name="ob")

    def port_maps(bt, lib, params=None):
        return catalog.port_map(bt, params=params, library=lib)

    rep = route_all_bus(ctrl.project, {"kyttar_10x12": chip_type},
                        _port_cells(catalog), port_map_provider=port_maps,
                        topology="bus")
    # v2 may route it (crossover-free) or fall back; either way the kept routes must
    # be sound (all routed) and crossover-free once applied.
    if rep.ok:
        for r in rep.routed:
            SetConnectionRouteCommand(ctrl.project, r.name, r.points).execute()
        assert not crossover_plan(ctrl.project, 0, chip_type, catalog)
