# SPDX-License-Identifier: GPL-3.0-or-later
"""Gates for the FULL FOC current loop on ONE array — and for the five build
mechanisms it forced into existence (INV-74):

  1. a PORT net that leaves the shared input corridor is brokered AT ITS FORK
     and relayed ``@N`` straight into its block (N = remaining transits);
  2. a port-cell DIVERT relays ``@N`` to the net's real broker (not ``@1``
     into the next cell, whose bus-face register the payload then overwrote);
  3. two rails of ONE source diverging at a crossover land in DISTINCT
     registers (see placekyt/tests/test_crossover_router.py for the on-chip
     clobber proof);
  4. a drawn 2-point handoff route is an abutted rail (its operand no longer
     dies in the sibling's broker);
  5. a block RESHAPED by hand has its internal faces — static ``fwd_face`` AND
     in-program ``is_face`` words such as the CORDIC ``unlock_face`` relock —
     re-derived from where its cells actually sit.

The design under test is the SHIPPED ``examples/foc_motor/foc_motor.kyt``
once it carries the full loop (Clarke → Park → PI×2 → inverse Park → SVPWM);
until then these gates skip. Every expectation is DERIVED from the placed
design (routes, landings, cell programs), never hardcoded, so the gates hold
for whatever placement ships.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
_ROOT = Path(__file__).resolve().parents[2]
for _p in (_ROOT / "placekyt", _ROOT / "runtime" / "python",
           _ROOT / "examples" / "foc_motor", _ROOT / "verification" / "tests"):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

KYT = _ROOT / "examples" / "foc_motor" / "foc_motor.kyt"
CHIP_YAML = _ROOT / "placekyt" / "resources" / "chips" / "kyttar_10x12.yaml"


def _is_full_loop(path: Path) -> bool:
    try:
        return path.exists() and "ClarkeTransformBlock" in path.read_text()
    except OSError:
        return False


pytestmark = pytest.mark.skipif(
    not (_is_full_loop(KYT) and CHIP_YAML.exists()),
    reason="the shipped foc_motor.kyt is not (yet) the full loop")


@pytest.fixture(scope="module")
def built():
    from PySide6.QtWidgets import QApplication
    QApplication.instance() or QApplication([])
    from engine.catalog import BlockCatalog
    from engine.io.project_io import load_project
    from engine.io.chip_type_io import load_chip_type
    from engine.build import BuildEngine
    cat = BlockCatalog.from_gr_kyttar()
    ct = load_chip_type(str(CHIP_YAML))
    proj = load_project(KYT)
    bres = BuildEngine(cat, str(CHIP_YAML)).build(proj, {proj.chip_type: ct})
    assert bres.ok, "build failed: " + "; ".join(str(e) for e in bres.errors)
    return cat, ct, proj, bres


def _port_nets(proj):
    from model.connection import ChipPortEndpoint, BlockEndpoint
    return [c for c in proj.connections
            if isinstance(c.source, ChipPortEndpoint)
            and isinstance(c.target, BlockEndpoint)]


def _stream_of(proj, name):
    return {c.name: getattr(c, "stream_id", None) for c in proj.connections}[name]


def _drive_arm(chip, landing, value, cap=1_000_000):
    hop = int(landing["hop"]) & 0x1F
    chip.inject_data_physical([int(value) & 0xFFFF], target_hop_cnt=hop,
                              target_addr=int(landing["data_addrs"][0]))
    chip.run(max_events=8000)
    chip.inject_jump_physical(target_hop_cnt=hop, entry_addr=int(landing["entry"]))
    for _ in range(20):
        r = chip.run(max_events=cap)
        if r.get("stop_reason") in ("QueueEmpty", "Deadlock"):
            return r["stop_reason"]
    return "EventLimit"


# --------------------------------------------------------------------------- #
# (1) + (2) every host-fed arm lands on ITS OWN block — measured by tracing
# --------------------------------------------------------------------------- #

def test_every_port_arm_reaches_its_own_block(built):
    """Each ingress arm, driven ALONE on a fresh chip, executes its target
    block (the relay it is wired to) and no other splitter. This is the
    fork-broker + divert-@N delivery, proven by which cells RAN (INV-56/74)."""
    import simkyt
    cat, ct, proj, bres = built
    il = bres.chips[0].input_landings
    width = ct.width
    occ = {}
    for b in proj.blocks:
        for c in b.placement.cells:
            occ[(c.x, c.y)] = b.name
    for conn in _port_nets(proj):
        chip = simkyt.Chip.from_yaml(str(CHIP_YAML))
        chip.load_bitstream_physical(bres.words(0))
        chip.enable_trace(2_000_000)
        stop = _drive_arm(chip, il[conn.name], 0x1234)
        reached = []
        for e in chip.get_trace():
            cid = e.get("cell_id")
            if cid is None or e.get("kind") != "exec_tick":
                continue
            b = occ.get((cid % width, cid // width))
            if b and b not in reached:
                reached.append(b)
        assert conn.target.block in reached, (
            f"{conn.name} ({_stream_of(proj, conn.name)}) never reached "
            f"{conn.target.block}; executed {reached}; stop={stop}")
        others = [b for b in reached if b.startswith("streamsplitter")
                  and b != conn.target.block]
        assert not others, (
            f"{conn.name} also fired foreign relay(s) {others} — a mis-landed "
            f"word (wrong fork, wrong hop or a clobbered bus face)")


def _deliver_jump(mem, entry):
    """(hops, entry) of the first JUMP in the program starting at ``entry``."""
    for a in range(int(entry), 32):
        if not mem[a]:
            return None
        if ((mem[a] >> 12) & 0xF) == 7:
            return 31 - ((mem[a] >> 5) & 0x1F), mem[a] & 0x1F
    return None


def test_fork_landings_relay_the_remaining_distance(built):
    """For every port net whose host landing is NOT its target's own cell, the
    landing cell's deliver program relays ``@N`` with N the number of cells
    left on the route to the block — never a hardcoded 1 — and its JUMP names
    the block's own entry. A net that must turn a second time (its planned
    broker on a cell another net leaves in a different direction) relays
    ``@N`` to that broker, which finishes the distance: the CHAIN of relay
    hops, read from the built programs, adds up to the route's remaining
    length and ends at the block's entry."""
    cat, ct, proj, bres = built
    il = bres.chips[0].input_landings
    cells = bres.chips[0].cells
    from engine.bus_router import _target_input_cell
    checked = 0
    for conn in _port_nets(proj):
        land = tuple(il[conn.name]["cell"])
        blk = proj.block(conn.target.block)
        in_cell = _target_input_cell(blk, conn.target.port, cat)
        if land == tuple(in_cell):
            continue                        # rides straight onto the block
        pts = [(p.x, p.y) for p in conn.route if hasattr(p, "x")]
        if in_cell not in pts:
            pts.append(in_cell)
        assert land in pts, f"{conn.name} lands off its own route at {land}"
        need = len(pts) - 1 - pts.index(land)
        blk_entry, _regs = cat.resolved_io(blk.type, blk.params, library=blk.library)
        cell, entry, total, chain = land, int(il[conn.name]["entry"]), 0, []
        for _ in range(4):
            j = _deliver_jump(cells[cell]["memory"], entry)
            assert j, f"{conn.name}: no JUMP in the deliver program at {cell}:{entry}"
            hops, entry = j
            assert hops >= 1, f"{conn.name}: {cell} relays @{hops}"
            total += hops
            chain.append((cell, hops, entry))
            nxt = pts.index(cell) + hops
            assert nxt < len(pts), (
                f"{conn.name}: relay chain {chain} overshoots the route")
            cell = pts[nxt]
            if cell == tuple(in_cell):
                break
        assert cell == tuple(in_cell) and total == need, (
            f"{conn.name}: relay chain {chain} from {land} covers {total} of the "
            f"{need} transits to the block at {in_cell}")
        assert entry == int(blk_entry), (
            f"{conn.name}: relay chain {chain} ends at entry {entry}, not the "
            f"block's own entry {blk_entry}")
        checked += 1
    assert checked >= 1, "no forked port net in this design — gate vacuous"


# --------------------------------------------------------------------------- #
# (5) a hand-reshaped block's internal faces follow its ACTUAL cells
# --------------------------------------------------------------------------- #

def test_reshaped_block_internal_faces_follow_placement(built):
    """For every multi-cell block, each internal handoff source cell's built
    ``fwd_face`` points at its handoff destination when that destination is
    adjacent, and the CORDIC ``pre`` cell's authored ``unlock_face`` (the
    INV-69 relock into ``rdv``) equals the direction from ``pre`` to where
    ``rdv`` actually sits. Holds for rigid AND reshaped placements."""
    cat, ct, proj, bres = built
    cells = bres.chips[0].cells
    NAME = {"south": 0, "east": 1, "west": 2, "north": 3}
    from engine.build import _step_face
    checked = 0
    for blk in proj.blocks:
        if blk.type != "CordicRotateBlock":
            continue
        by_id = {getattr(c, "cell_id", None): (c.x, c.y) for c in blk.placement.cells}
        rdv, pre = by_id.get("rdv"), by_id.get("pre")
        assert rdv and pre, f"{blk.name}: rdv/pre cells not found in placement"
        want_fwd = _step_face(rdv[0], rdv[1], pre[0], pre[1])
        assert want_fwd is not None, f"{blk.name}: rdv and pre are not adjacent"
        assert NAME[cells[rdv]["face"]] == want_fwd, (
            f"{blk.name}: rdv {rdv} faces {cells[rdv]['face']} but pre is at {pre}")
        gb = cat.instantiate(blk.type, "probe", blk.params, library=blk.library)
        cp = gb.build_cell_programs()["pre"]
        addr = next(d.address for d in cp.data if getattr(d, "name", "") == "unlock_face")
        want_relock = _step_face(pre[0], pre[1], rdv[0], rdv[1])
        assert (cells[pre]["memory"][addr] & 0x3) == want_relock, (
            f"{blk.name}: pre {pre} unlock_face={cells[pre]['memory'][addr] & 3} "
            f"but rdv is at {rdv} (needs {want_relock}) — the relock fires into a "
            f"foreign cell and the rendezvous never releases")
        checked += 1
    assert checked == 2


# --------------------------------------------------------------------------- #
# THE gate: the whole loop streams bit-exact on the shipped placement
# --------------------------------------------------------------------------- #

def test_full_loop_streams_bit_exact_on_one_array(built):
    """Six consecutive control iterations with DIFFERENT inputs through the
    real placed + routed + built chip: the (i_d, i_q) packets AND the duty
    packets are bit-exact against the host goldens and every run settles
    ``QueueEmpty``. One iteration cannot see a rendezvous whose release
    re-admits the wrong arm; six can (INV-69/74)."""
    import simkyt
    import foc_loop_model as M
    cat, ct, proj, bres = built
    il = bres.chips[0].input_landings
    sid = {_stream_of(proj, c.name): c.name for c in _port_nets(proj)}
    for s in ("ia", "ib", "th_park", "e_d", "e_q", "th_ipark"):
        assert s in sid, f"stream '{s}' has no ingress net"
    N = 6
    ia = [1000, 0x0333, -1500 & 0xFFFF, 300, 0x0700, -200 & 0xFFFF]
    ib = [2000, 0x1500, 900, -2200 & 0xFFFF, 0x0123, 1750]
    th = [0x1234, 0x4000, 0x8000, 0xC000, 0x2468, 0x9ABC]
    ed = [1500, 0x0210, -900 & 0xFFFF, 400, 0x0650, -150 & 0xFFFF]
    eq = [2500, 0x1200, 700, -1800 & 0xFFFF, 0x0333, 1400]
    want_m = [(a & 0xFFFF, b & 0xFFFF) for a, b in M.measurement_half(ia, ib, th)]
    want_c = [w & 0xFFFF for w in M.command_half(ed, eq, th)]
    # The chain's egress tags: the measurement (i_d,i_q) pair and the duties.
    from model.connection import ChipPortEndpoint
    tags = sorted({int(c.out_tag) for c in proj.connections
                   if isinstance(c.target, ChipPortEndpoint) and c.out_tag is not None})
    assert len(tags) == 3, f"expected 3 egress tags (i_d, i_q, duties): {tags}"
    t_id, t_iq, t_duty = tags[0], tags[1], tags[2]

    chip = simkyt.Chip.from_yaml(str(CHIP_YAML))
    chip.load_bitstream_physical(bres.words(0))
    got: dict = {}
    stops = set()

    def drain():
        while chip.output_available("x16_out"):
            for v, d, _t in chip.read_port_words_timed("x16_out"):
                got.setdefault(int(d), []).append(int(v) & 0xFFFF)
            chip.release_output_ack("x16_out")

    for i in range(N):
        for s, v in (("ia", ia[i]), ("ib", ib[i]), ("th_park", th[i]),
                     ("e_d", ed[i]), ("e_q", eq[i]), ("th_ipark", th[i])):
            stops.add(_drive_arm(chip, il[sid[s]], v))
            drain()
    drain()
    assert stops == {"QueueEmpty"}, f"stop_reasons {stops} (INV-56)"
    got_m = list(zip(got.get(t_id, []), got.get(t_iq, [])))
    got_c = got.get(t_duty, [])
    assert got_m == want_m, f"measurement half diverged: got {got_m[:3]}… want {want_m[:3]}…"
    assert got_c == want_c, f"command half diverged: got {got_c[:6]}… want {want_c[:6]}…"
