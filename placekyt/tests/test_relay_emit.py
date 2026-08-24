# SPDX-License-Identifier: GPL-3.0-or-later
"""§1.4 #3 — RELAY EMISSION for routes longer than the 5-bit hop field.

A WRITE/JUMP pair carries a 5-bit HOP_CNT, so one emission can address a cell at
most **31 hops** away (``@32`` is rejected by the assembler outright). Until now a
longer route was a NAMED failure: the router PLANNED relay cells but the build
never programmed them, so the net could not be built at all.

The fix splits an over-budget route into ≤31-hop SEGMENTS. The word is addressed
to LAND on an intermediate plain routing cell (HOP_CNT==31 ⇒ ``execute_locally``),
whose relay program flips to the route's continuation face and re-emits the
payload + trigger with a FRESH budget. This is the SAME land→flip→re-emit
primitive the CrossoverBlock demux already proves on-chip, specialised to one
track.

What is gated here:
  * the relay PROGRAM re-emits a landed burst on-chip (payload arrives);
  * a real >31-hop net BUILDS and RUNS, bit-exact against a hop-legal control
    (the relay is transparent — it must not perturb the data at all);
  * a MULTI-SEGMENT (>62-hop) net chains several relays and still runs;
  * the INV-4 mutations FAIL: relay omitted, stale hop count, mis-faced relay;
  * a relay may NEVER be placed on a block cell, a used chip-port cell, or a
    broker (the used-cell / port_transit hard failure classes, INV-32).
"""

from __future__ import annotations

import os

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication  # noqa: E402

from tests.conftest import CHIP_YAML as CT_PATH  # noqa: E402

pytestmark = pytest.mark.skipif(not CT_PATH.exists(), reason="chip yaml absent")

LIB = "lattrex.official"
GAIN = 0.5            # exactly representable in Q15 (the default gain_range=15)
VALS = [1000, -2000, 3000, 4321, -8192]
EXPECT = [v // 2 for v in VALS]


@pytest.fixture(scope="module")
def qapp():
    return QApplication.instance() or QApplication([])


@pytest.fixture(scope="module")
def env(qapp):
    from engine.catalog import BlockCatalog
    from engine.io.chip_type_io import load_chip_type
    ct = load_chip_type(str(CT_PATH))
    key = getattr(ct, "name", None) or "kyttar_10x12"
    return BlockCatalog.from_gr_kyttar(), ct, key


def _dedupe(pts):
    out = [pts[0]]
    for p in pts[1:]:
        if p != out[-1]:
            out.append(p)
    return out


def _long_path():
    """A hand-drawn serpentine from the gain at (1,1) to x16_out at (9,0).

    52 delivered hops — comfortably over the 31-hop field, needing ONE relay.
    Avoids the input port (0,0) and the gain's own cell."""
    pts = [(1, 1)]
    for y in range(2, 12):
        pts.append((1, y))
    for x in (2, 3):
        pts.append((x, 11))
    for y in range(10, -1, -1):
        pts.append((3, y))
    pts.append((4, 0))
    for y in range(1, 12):
        pts.append((4, y))
    for x in (5, 6):
        pts.append((x, 11))
    for y in range(10, -1, -1):
        pts.append((6, y))
    for x in (7, 8, 9):
        pts.append((x, 0))
    return _dedupe(pts)


def _very_long_path():
    """A near-full-chip serpentine: 96 delivered hops, needing THREE relays
    (>62, so it also proves relay CHAINING, not just a single re-launch)."""
    pts = [(1, 1)]
    for y in range(2, 12):
        pts.append((1, y))
    col, down = 2, False
    while col <= 8:
        pts.append((col, 11 if not down else 0))
        for y in (range(10, -1, -1) if not down else range(1, 12)):
            pts.append((col, y))
        down = not down
        col += 1
    last = pts[-1]
    if last[1] != 0:
        pts.append((9, last[1]))
        for y in range(last[1] - 1, -1, -1):
            pts.append((9, y))
    else:
        pts.append((9, 0))
    return _dedupe(pts)


def _build(env, path=None):
    """Place a gain, wire it to the ports, optionally force a long hand-drawn
    egress, route + build. Returns ``(ctrl, build_result)``."""
    from engine.build import BuildEngine
    from model.connection import BlockEndpoint, ChipPortEndpoint
    from ui.controller import AppController
    cat, ct, key = env
    ctrl = AppController(catalog=cat)
    ctrl.new_project("relay", key)
    g = ctrl.place_block("GainBlock", 0, 1, 1, params={"gain": GAIN},
                         library=LIB)
    ctrl.add_logical_connection(ChipPortEndpoint(chip=0, port="x16_in"),
                                BlockEndpoint(block=g, port="in"), name="n_in")
    if path is None:
        ctrl.add_logical_connection(BlockEndpoint(block=g, port="out"),
                                    ChipPortEndpoint(chip=0, port="x16_out"),
                                    name="n_out")
    else:
        ctrl.add_route(BlockEndpoint(block=g, port="out"),
                       ChipPortEndpoint(chip=0, port="x16_out"), path,
                       name="n_out")
    rep = ctrl.auto_route_all({key: ct})
    assert rep.ok, rep.reason
    res = BuildEngine(cat, str(CT_PATH)).build(ctrl.project, {key: ct})
    assert res.ok, "; ".join(str(e) for e in res.errors)
    return ctrl, res


def _run(env, res, patch=None):
    """Drive the built chip with VALS and collect the egress. ``patch(chip)``
    may corrupt cell memory first (the mutation hook)."""
    import simkyt
    cat, _ct, _key = env
    entry, _ins = cat.resolved_io("GainBlock", {"gain": GAIN}, library=LIB)
    ld = res.chips[0].input_landings["n_in"]
    hop, entry_i = int(ld["hop"]) & 0x1F, int(ld["entry"])
    addr = int(ld["data_addrs"][0])
    chip = simkyt.Chip.from_yaml(str(CT_PATH))
    chip.load_bitstream_physical(res.words(0))
    if patch is not None:
        patch(chip)
    chip.set_port_entry_address("x16_in", entry)
    got = []
    for v in VALS:
        chip.inject_data_physical([v & 0xFFFF], target_hop_cnt=hop,
                                  target_addr=addr)
        chip.run(max_events=20000)
        chip.inject_jump_physical(target_hop_cnt=hop, entry_addr=entry_i)
        chip.run(max_events=600000)
        while chip.output_available("x16_out"):
            got.extend(int(x) for x in chip.read_port_i16("x16_out").tolist())
            chip.release_output_ack("x16_out")
            chip.run(max_events=20000)
    return got


# --------------------------------------------------------------------------- #
# (1) The hardware ceiling this fix exists to lift.
# --------------------------------------------------------------------------- #

def test_hop_field_ceiling_is_31():
    """``@31`` is the furthest a single WRITE/JUMP can address; ``@32`` will not
    even assemble. This is the limit the relay splits a route around."""
    from engine.build import _relay_program, encode_hop_cnt, decode_hop_cnt
    assert encode_hop_cnt(31) == 0 and decode_hop_cnt(0) == 31
    _entry, mem = _relay_program(0, 31, 0, 0)
    assert mem, "a @31 relay must assemble"
    with pytest.raises(Exception):
        _relay_program(0, 32, 0, 0)


# --------------------------------------------------------------------------- #
# (2) The relay PROGRAM re-emits a landed burst (the COMPUTE proof).
# --------------------------------------------------------------------------- #

def test_relay_program_re_emits_landed_burst_on_chip(qapp):
    """Land a burst on a relay cell and assert it is forwarded to the neighbour
    the relay's exit face names — the primitive the whole fix rests on."""
    import simkyt
    from gr_kyttar.bitstream.generator import BitstreamGenerator
    from gr_kyttar.placement.cell_map import CellConfig, CellMap, Face
    from engine.build import _relay_program

    entry, mem = _relay_program(1, 1, 7, 0)      # exit EAST, @1 into R7
    cm = CellMap(width=12, height=12)
    relay = CellConfig(block_name="_relay")
    relay.memory.update(mem)
    relay.entry_addr = entry
    cm.set_cell(5, 5, relay)
    cm.set_cell(6, 5, CellConfig(fwd_face=Face.EAST, block_name="_sink"))

    gen = BitstreamGenerator(str(CT_PATH))
    gen.load_cell_map(cm)
    chip = simkyt.Chip.from_yaml(str(CT_PATH))
    chip.load_bitstream_physical(list(gen.generate().words))
    rid, sid = chip.cell_id_at(5, 5), chip.cell_id_at(6, 5)
    chip.write_cell_memory(rid, 0, 0xBEEF)
    chip.inject_jump(rid, entry)
    chip.run(max_events=4000)
    assert chip.read_cell_memory(sid, 7) == 0xBEEF


def test_relay_exit_face_steers_an_arriving_word(qapp):
    """The relay's programmed face — not the cell's static ``fwd_face`` — decides
    where a landed word is re-emitted, for a word ARRIVING over a face (the real
    corridor condition). Guards the face constant against being a dead word."""
    import simkyt
    from gr_kyttar.bitstream.generator import BitstreamGenerator
    from gr_kyttar.placement.cell_map import CellConfig, CellMap, Face
    from engine.build import _relay_program

    nb = {0: (5, 6), 1: (6, 5), 2: (4, 5)}       # S, E, W of the relay at (5,5)
    for prog_face in (0, 1, 2):
        entry, mem = _relay_program(prog_face, 1, 7, 0)
        s_entry, s_mem = _relay_program(0, 2, 0, entry)   # land 2 hops away
        cm = CellMap(width=12, height=12)
        src = CellConfig(block_name="_src")
        src.memory.update(s_mem)
        src.entry_addr = s_entry
        src.fwd_face = Face.SOUTH
        cm.set_cell(5, 3, src)
        cm.set_cell(5, 4, CellConfig(fwd_face=Face.SOUTH, block_name="_t"))
        relay = CellConfig(block_name="_relay")
        relay.memory.update(mem)
        relay.entry_addr = entry
        relay.fwd_face = Face.SOUTH              # static face DISAGREES for E/W
        cm.set_cell(5, 5, relay)
        for (x, y) in nb.values():
            cm.set_cell(x, y, CellConfig(fwd_face=Face.EAST, block_name="_s"))

        gen = BitstreamGenerator(str(CT_PATH))
        gen.load_cell_map(cm)
        chip = simkyt.Chip.from_yaml(str(CT_PATH))
        chip.load_bitstream_physical(list(gen.generate().words))
        sid = chip.cell_id_at(5, 3)
        chip.write_cell_memory(sid, 0, 0xBEEF)
        chip.inject_jump(sid, s_entry)
        chip.run(max_events=20000)
        landed = [f for f, (x, y) in nb.items()
                  if chip.read_cell_memory(chip.cell_id_at(x, y), 7) == 0xBEEF]
        assert landed == [prog_face], (
            f"relay programmed to face {prog_face} delivered to {landed}")


# --------------------------------------------------------------------------- #
# (3) A REAL over-budget net builds and RUNS — bit-exact vs a hop-legal control.
# --------------------------------------------------------------------------- #

def test_over_budget_net_runs_and_matches_short_route(env):
    """A 52-hop egress (impossible before — the field stops at 31) routes,
    builds, and delivers the SAME samples as the hop-legal auto-routed control.
    The relay must be perfectly transparent to the data."""
    path = _long_path()
    assert len(path) > 31, "the fixture path must exceed the hop field"

    _c_long, res_long = _build(env, path)
    assert res_long.chips[0].relay_cost >= 1, "the long net must consume a relay"
    long_out = _run(env, res_long)

    _c_short, res_short = _build(env, None)      # auto-routed, hop-legal
    assert res_short.chips[0].relay_cost == 0, "the short net needs no relay"
    short_out = _run(env, res_short)

    assert long_out == EXPECT, f"relayed net: {long_out} != {EXPECT}"
    assert long_out == short_out, "the relay perturbed the data"


def test_multi_segment_route_chains_relays(env):
    """A 96-hop route — over TWICE the ceiling — chains several relays and still
    delivers. Proves the split generalises beyond one re-launch, so the design
    has no practical hop ceiling beyond available array area."""
    path = _very_long_path()
    assert len(path) > 62
    _ctrl, res = _build(env, path)
    cells = res.chips[0].relay_cells["n_out"]
    assert len(cells) >= 2, f"a >62-hop route needs multiple relays, got {cells}"
    assert len(set(cells)) == len(cells), "relays must be distinct cells"
    assert _run(env, res) == EXPECT


def test_relay_cost_is_reported(env):
    """Relays consume array cells, so the build must SAY so (a hidden cost is a
    silent area regression)."""
    _ctrl, res = _build(env, _long_path())
    chip = res.chips[0]
    assert chip.relay_cells.get("n_out"), "relay cells must be reported per net"
    assert chip.relay_cost == sum(len(v) for v in chip.relay_cells.values())
    for cell in chip.relay_cells["n_out"]:
        assert cell in _long_path(), "a relay must sit ON the net's own route"


# --------------------------------------------------------------------------- #
# (4) INV-4 — the gate must FAIL on a corrupted relay.
# --------------------------------------------------------------------------- #

def test_mutation_relay_omitted_fails(env, monkeypatch):
    """Omitting the relay emission is the PRE-FIX behaviour: the source addresses
    a cell it cannot reach, so nothing is delivered. If this still produced the
    right answer, the test would not be testing the relay at all."""
    import engine.build as B
    monkeypatch.setattr(B, "_apply_relays", lambda *a, **k: {})
    _ctrl, res = _build(env, _long_path())
    assert res.chips[0].relay_cost == 0
    assert _run(env, res) != EXPECT, "a relay-less over-budget net must NOT work"


def test_mutation_stale_hop_count_fails(env, monkeypatch):
    """A relay that re-emits with a STALE (too-short) budget strands the word
    short of its destination."""
    import engine.build as B
    orig = B._relay_program
    monkeypatch.setattr(
        B, "_relay_program",
        lambda f, h, d, e: orig(f, max(1, int(h) - 3), d, e))
    _ctrl, res = _build(env, _long_path())
    assert _run(env, res) != EXPECT


def test_mutation_misfaced_relay_fails(env):
    """A relay that flips to the WRONG face re-emits off the corridor.

    Mutated IN the built chip's memory (the face constant at R1), which is the
    sharpest form: everything else about the build is identical. The reverse and
    perpendicular-away faces are used — an adjacent free cell can happen to
    forward a mis-faced word back onto the corridor on some geometries, so this
    asserts on the faces that genuinely leave the route."""
    _ctrl, res = _build(env, _long_path())
    relay = res.chips[0].relay_cells["n_out"][0]
    assert _run(env, res) == EXPECT, "baseline must be correct first"

    def mutate(face):
        def _patch(chip):
            chip.write_cell_memory(chip.cell_id_at(*relay), 1, face)
        return _patch

    for face in (2, 3):          # WEST / NORTH — back up the corridor
        got = _run(env, res, patch=mutate(face))
        assert got != EXPECT, f"a relay mis-faced to {face} still delivered"


# --------------------------------------------------------------------------- #
# (5) A relay may never sit on a cell that is already in use (INV-32).
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("kind", ["block", "used_port", "broker"])
def test_relay_never_placed_on_a_used_cell(kind):
    """The used-cell / port_transit hard failure classes. If every candidate cell
    on the corridor is a block cell, a USED chip-port cell, or an existing broker,
    the route must be a NAMED failure — never a relay overlaid on someone else's
    programming (a silent dead chip)."""
    from engine.bus_router import _plan_relays
    path = [(x, 0) for x in range(52)]
    occupied = set(path[1:-1])
    sets = {"block": (occupied, set(), set()),
            "used_port": (set(), occupied, set()),
            "broker": (set(), set(), occupied)}
    relays, why = _plan_relays(path, 52, *sets[kind])
    assert relays == [] and why, f"{kind} cells must not host a relay"
    assert "relay" in why


def test_relay_avoids_an_occupied_candidate_but_still_routes():
    """A SINGLE blocked candidate is stepped around (backward to the nearest free
    cell), not treated as a dead end — the route still gets its relay."""
    from engine.bus_router import _plan_relays
    path = [(x, 0) for x in range(52)]
    clean, why = _plan_relays(path, 52, set(), set(), set())
    assert why is None and clean == [(30, 0)]
    moved, why2 = _plan_relays(path, 52, {(30, 0)}, set(), set())
    assert why2 is None, "one blocked candidate must not fail the route"
    assert moved and moved != clean, "the relay must step off the used cell"
    assert moved[0][0] < 30, "it steps BACKWARD (shortening the segment)"


def test_planned_relay_segments_all_fit_the_hop_field():
    """Whatever the path, every emitted segment must be ≤31 hops — that is the
    entire point. Guards the segment arithmetic (including the trailing
    deliver/egress +1 absorbed by the LAST segment)."""
    from engine.bus_router import _plan_relays
    for n in (33, 52, 63, 96, 120):
        path = [(x, 0) for x in range(n)]
        distance = n            # n-1 waypoint hops + 1 egress
        relays, why = _plan_relays(path, distance, set(), set(), set())
        assert why is None, f"n={n}: {why}"
        idxs = [0] + [path.index(r) for r in relays] + [len(path) - 1]
        spans = [idxs[i + 1] - idxs[i] for i in range(len(idxs) - 1)]
        spans[-1] += distance - (len(path) - 1)
        assert all(s <= 31 for s in spans), f"n={n}: segments {spans}"


def test_short_route_gets_no_relay(env):
    """The common case must be untouched: a hop-legal net spends zero relay
    cells and takes the identical path it always did."""
    _ctrl, res = _build(env, None)
    assert res.chips[0].relay_cells == {}
    assert res.chips[0].relay_cost == 0
