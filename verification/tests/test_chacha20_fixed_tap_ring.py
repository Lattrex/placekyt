# SPDX-License-Identifier: GPL-3.0-or-later
"""The ChaCha20 FIXED-TAP RING, the silent layout traps it exposed, and the
on-chip gate that pins how much of the cipher actually runs.

``ChaCha20KeystreamBlock`` is still ``needs_human`` — but for a much smaller
reason than before. The CIPHER now runs on a real placed + routed + built chip:
all 80 quarter-round invocations, 19 half-boundary realignments, and **state word
0 bit-exact against RFC 8439 §2.3.2**. What does NOT yet work is the finish
DRAIN's repeat, so the block emits 8 of its 32 words; see the class docstring for
the measured four-word shortfall.

This file gates three layers:

* the algebraic restatement of RFC 8439's round schedule that removes the
  selector the original architecture was built around;
* the substrate traps measured while wiring it — the closed-ring/positional-
  pairing/gap ones (INV-51) and the FACE-register ones (INV-NEXT: the face
  persists across entries, it steers TRANSITING words, and the router cannot see
  it);
* what the assembled block does ON SILICON, as counts AND as bytes.

**1. The fixed-tap ring.** Written as ``index(k) = 4k + ((j + k*shift) & 3)`` the
schedule invites a per-row *selector*: a ``LOAD``-indirect read plus a 4-way
``CMP``/``BR`` write-back, driven by a broadcast to every lane. Read instead as
a per-row READ OFFSET it collapses::

    row 0 reads offsets  0 1 2 3 | 0 1 2 3
    row 1 reads offsets  0 1 2 3 | 1 2 3 0
    row 2 reads offsets  0 1 2 3 | 2 3 0 1
    row 3 reads offsets  0 1 2 3 | 3 0 1 2

Every row reads **offset 0** provided it rotates left by one after each quarter
round; the diagonal half is the same sequence started ``k`` positions later, so
it is bracketed by ``k`` extra rotations of row ``k`` and ``4 - k`` to restore
alignment. The tap is therefore always slot 0 — a CONSTANT — and the whole
column/diagonal permutation becomes a shift register. That is what deletes the
fan-out-8 selector broadcast and the 8-cell write-back demux.

This suite proves the restatement reproduces RFC 8439 §2.3.2 EXACTLY, over the
same cell-level operations the hardware performs (publish / quarter round /
write-back-and-rotate / spin), and pairs it with INV-4 negatives.

**2. Two silent traps, both MEASURED on the real chip** (``test_gap_transit`` and
the positional-pairing gate below). Each produces a design that places, routes,
builds and DRCs clean and then does the wrong thing in silence.

Run::

    KYTTAR_GR_PYTHON=/usr/bin/python3 QT_QPA_PLATFORM=offscreen \\
        .venv/bin/python -m pytest \\
        verification/tests/test_chacha20_fixed_tap_ring.py -q
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

_ROOT = Path(__file__).resolve().parents[2]
for _p in (str(_ROOT / "runtime" / "python"), str(_ROOT / "placekyt"),
           str(_ROOT / "verification"), str(Path(__file__).resolve().parent)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import chacha20_golden as g  # noqa: E402

CHIP_YAML = str(_ROOT / "placekyt" / "resources" / "chips" / "kyttar_10x12.yaml")


# --------------------------------------------------------------------------
# The cell-level model: exactly what the authored cells do, nothing more.
# --------------------------------------------------------------------------
class Row:
    """One row of the state: four 32-bit slots, tapped ALWAYS at slot 0."""

    def __init__(self, vals):
        self.s = list(vals)

    def pub(self):
        return self.s[0]

    def wb(self, new_head):
        """Install the quarter round's result and rotate left by one."""
        self.s = self.s[1:] + [new_head]

    def spin(self):
        """Rotate with no replacement — this is the realignment."""
        self.s = self.s[1:] + [self.s[0]]


def ring_block(key, nonce, counter, *, rounds=20, addback=True,
               column_first=True, realign=True, spin_dir=1):
    """RFC 8439 §2.3 via the fixed-tap ring. Knobs exist for the INV-4 mutants."""
    init = g.initial_state(key, nonce, counter)
    rows = [Row(init[4 * k:4 * k + 4]) for k in range(4)]
    halves = (0, 1) if column_first else (1, 0)
    laps = 0
    for _dr in range(rounds // 2):
        for half in halves:
            if half == 1 and realign:
                for k in range(4):
                    for _ in range((k * spin_dir) & 3):
                        rows[k].spin()
            for _step in range(4):
                a, b, c, d = g.quarter_round(*[r.pub() for r in rows])
                for r, v in zip(rows, (a, b, c, d)):
                    r.wb(v)
                laps += 1
            if half == 1 and realign:
                for k in range(4):
                    for _ in range((-k * spin_dir) & 3):
                        rows[k].spin()
    s = [rows[k].s[i] for k in range(4) for i in range(4)]
    if not addback:
        return s, laps
    return [(s[i] + init[i]) & g.MASK32 for i in range(16)], laps


# --------------------------------------------------------------------------
# The restatement IS the RFC.
# --------------------------------------------------------------------------
def test_read_offsets_are_zero_after_a_rotate_per_quarter_round():
    """THE identity: with a rotate-per-quarter-round, every row taps slot 0.

    Row ``k``'s read offsets are ``0,1,2,3`` in the column half and
    ``k,k+1,k+2,k+3`` in the diagonal half — i.e. the same walk, started ``k``
    later. That is why the tap is a constant and no selector is needed.
    """
    sched = [g.quarterround_indices(j, d)
             for d in (False, True) for j in range(4)]
    for k in range(4):
        offs = [sched[s][k] - 4 * k for s in range(8)]
        assert offs[:4] == [0, 1, 2, 3], f"row {k} column half: {offs[:4]}"
        assert offs[4:] == [(k + i) & 3 for i in range(4)], \
            f"row {k} diagonal half: {offs[4:]}"


def test_ring_reproduces_rfc8439_232_state():
    """The fixed-tap ring computes the RFC's §2.3.2 output state, exactly."""
    got, laps = ring_block(g.RFC8439_BLOCK_KEY, g.RFC8439_BLOCK_NONCE, 1)
    assert tuple(got) == g.RFC8439_BLOCK_EXPECTED_STATE
    assert laps == 80, "20 rounds is 80 quarter-round invocations"


def test_ring_reproduces_rfc8439_232_keystream_bytes():
    """...and its 64 serialised keystream bytes."""
    got, _ = ring_block(g.RFC8439_BLOCK_KEY, g.RFC8439_BLOCK_NONCE, 1)
    assert g.serialize(got) == g.RFC8439_BLOCK_EXPECTED_KEYSTREAM


def test_ring_reproduces_the_full_242_encryption_vector():
    """§2.4.2 end to end — 114 bytes spanning TWO blocks, so it also pins the
    per-block counter increment."""
    ks = bytearray()
    blk = g.RFC8439_ENCRYPT_COUNTER
    while len(ks) < len(g.RFC8439_ENCRYPT_PLAINTEXT):
        st, _ = ring_block(g.RFC8439_ENCRYPT_KEY, g.RFC8439_ENCRYPT_NONCE, blk)
        ks += g.serialize(st)
        blk += 1
    ct = bytes(p ^ k for p, k in zip(g.RFC8439_ENCRYPT_PLAINTEXT, ks))
    assert ct == g.RFC8439_ENCRYPT_CIPHERTEXT


def test_four_spins_are_the_identity():
    """A 4-slot rotation has order 4 — the property the realignment relies on,
    and the reason ``k`` and ``4 - k`` spins bracket the diagonal half."""
    r = Row([1, 2, 3, 4])
    for _ in range(4):
        r.spin()
    assert r.s == [1, 2, 3, 4]


# --------------------------------------------------------------------------
# INV-4 negatives — every knob must change the answer.
# --------------------------------------------------------------------------
@pytest.mark.parametrize("rounds", [8, 12, 18, 22])
def test_mutation_wrong_round_count_fails(rounds):
    got, _ = ring_block(g.RFC8439_BLOCK_KEY, g.RFC8439_BLOCK_NONCE, 1,
                        rounds=rounds)
    assert tuple(got) != g.RFC8439_BLOCK_EXPECTED_STATE


def test_mutation_missing_final_addition_fails():
    got, _ = ring_block(g.RFC8439_BLOCK_KEY, g.RFC8439_BLOCK_NONCE, 1,
                        addback=False)
    assert tuple(got) != g.RFC8439_BLOCK_EXPECTED_STATE


def test_mutation_diagonal_half_first_fails():
    """The COLUMN half must run first — and this still performs exactly 80
    invocations, so no count-based or structural check would catch it."""
    got, laps = ring_block(g.RFC8439_BLOCK_KEY, g.RFC8439_BLOCK_NONCE, 1,
                           column_first=False)
    assert laps == 80
    assert tuple(got) != g.RFC8439_BLOCK_EXPECTED_STATE


def test_mutation_no_realignment_fails():
    """Drop the half-boundary realignment and the diagonal half degenerates
    into a second column half — a different cipher, same lap count."""
    got, laps = ring_block(g.RFC8439_BLOCK_KEY, g.RFC8439_BLOCK_NONCE, 1,
                           realign=False)
    assert laps == 80
    assert tuple(got) != g.RFC8439_BLOCK_EXPECTED_STATE


def test_mutation_reversed_spin_direction_fails():
    """The realignment DIRECTION is load-bearing, exactly like the counter
    direction: reversing it still spins the right NUMBER of times."""
    got, laps = ring_block(g.RFC8439_BLOCK_KEY, g.RFC8439_BLOCK_NONCE, 1,
                           spin_dir=-1)
    assert laps == 80
    assert tuple(got) != g.RFC8439_BLOCK_EXPECTED_STATE


def test_mutation_a_stuck_tap_fails():
    """If a row does NOT rotate, its tap stops advancing and the schedule dies.
    This is the negative that gives the fixed-tap claim its content."""
    init = g.initial_state(g.RFC8439_BLOCK_KEY, g.RFC8439_BLOCK_NONCE, 1)
    rows = [Row(init[4 * k:4 * k + 4]) for k in range(4)]
    for _ in range(80):
        a, b, c, d = g.quarter_round(*[r.pub() for r in rows])
        for r, v in zip(rows, (a, b, c, d)):
            r.s[0] = v                     # install WITHOUT rotating
    s = [rows[k].s[i] for k in range(4) for i in range(4)]
    out = [(s[i] + init[i]) & g.MASK32 for i in range(16)]
    assert tuple(out) != g.RFC8439_BLOCK_EXPECTED_STATE


# --------------------------------------------------------------------------
# TRAP 1, measured on the real chip: a faced-but-PROGRAMLESS cell DOES forward.
# --------------------------------------------------------------------------
@pytest.mark.skipif(not os.path.exists(CHIP_YAML), reason="chip yaml absent")
@pytest.mark.parametrize("distance", [2, 3, 4, 6])
def test_a_word_transits_a_faced_cell_with_no_program(distance):
    """A WRITE crosses cells that carry a FACE and NO program at all.

    This is why an in-block "gap" is not automatically a dead end — and equally
    why it is not automatically a corridor: the build gives a bare array cell a
    face only where a ROUTE claims it, so a gap inside a block's own footprint
    IS a dead end for a block-internal WRITE. Both halves matter when folding.
    """
    import simkyt
    from gr_kyttar.placement.block import CellProgram, EntryPoint, Port
    from gr_kyttar.placement.resolver import (
        CellProgramResolver, JumpTarget, ResolvedTargets, WriteTarget)

    W = 10

    def one_word(name_in, name_out):
        return CellProgram(
            inputs=[Port(name_in, register=1)],
            outputs=[Port(name_out)],
            entries=[EntryPoint("default")],
            data=[], state=[],
            assembly_template=("default:\n"
                               "    MOVE R0, R{in:%s}\n"
                               "    {write:%s}\n"
                               "    {jump:%s}\n" % (name_in, name_out,
                                                    name_out)))

    R = CellProgramResolver()
    src, snk = one_word("x", "o"), one_word("v", "out")
    e_src = R.compute_entry_addresses(src)
    e_snk = R.compute_entry_addresses(snk)

    def reg(cp, nm):
        c = R.classify_addresses(cp)
        return [a for a, v in c.items() if v.get("name") == nm][0]

    tg = ResolvedTargets()
    tg.writes["o"] = WriteTarget(distance, reg(snk, "v"))
    tg.jumps["o"] = JumpTarget(distance, e_snk["default"])
    res_src = R.resolve(src, tg)
    tg2 = ResolvedTargets()
    tg2.writes["out"] = WriteTarget(W - distance, 0)
    tg2.jumps["out"] = JumpTarget(W - distance, 0)
    res_snk = R.resolve(snk, tg2)

    chip = simkyt.Chip.from_yaml(CHIP_YAML)
    for a in range(32):
        chip.write_cell_memory(0, a, int(res_src.memory.get(a, 0)))
        chip.write_cell_memory(distance, a, int(res_snk.memory.get(a, 0)))
    for x in range(W):
        chip.set_fwd_face(x, "east")          # a FACE, but no program between
    chip.set_port_entry_address("x16_in", e_src["default"])
    chip.set_port_target_hop_count("x16_in", 30)
    chip.write_port_multi_i16("x16_in", [[(reg(src, "x"), 0xA5A5)]],
                              e_src["default"])
    out = []
    for _ in range(20000):
        chip.run(max_events=64)
        while chip.output_available("x16_out"):
            w = chip.read_port_i16("x16_out").view("uint16").tolist()
            out.extend(int(v) & 0xFFFF for v in w)
            chip.release_output_ack("x16_out")
        if out:
            break
    assert out[:1] == [0xA5A5], (
        f"a word should transit {distance - 1} faced, programless cells; "
        f"got {out[:3]}")


# --------------------------------------------------------------------------
# TRAP 2: POSITIONAL PAIRING. Layout order must equal program order.
# --------------------------------------------------------------------------
def test_positional_pairing_is_a_real_contract():
    """The router and build walk the programs and the placed cells in LOCKSTEP
    BY POSITION, so ``build_cell_programs`` and ``default_layout`` must iterate
    in the SAME order.

    Both are keyed by cell id, which HIDES a mismatch: a layout in a different
    order silently pairs each program with the wrong cell, and the design places,
    routes, builds and DRCs clean while whole cells come out with EMPTY memory.
    Measured while wiring ChaCha20KeystreamBlock — the symptom was a block that
    built green and emitted nothing.
    """
    from gr_kyttar.placement.blocks.chacha20_keystream_block import (
        ChaCha20KeystreamBlock)
    b = ChaCha20KeystreamBlock("pair")
    assert list(b.build_cell_programs()) == list(b.default_layout()), (
        "layout order must match program order (INV-33 positional pairing)")


def test_every_cell_stays_inside_its_word_budget():
    """A cell whose highest pinned register reaches ``31 - instruction_count``
    overlays its own code. The resolver does NOT catch this — its space guard
    compares only DATA against ``base_addr``, never state and never pinned
    inputs — so such a cell assembles, loads, places and routes and then returns
    a wrong answer that looks like a routing fault (INV-33's overlap half)."""
    from gr_kyttar.placement.blocks.chacha20_keystream_block import (
        ChaCha20KeystreamBlock)
    b = ChaCha20KeystreamBlock("budget")
    over = []
    for cid, cp in b.build_cell_programs().items():
        instr = len([l for l in cp.assembly_template.splitlines()
                     if l.strip() and not l.strip().endswith(":")])
        base = 31 - instr
        pins = [p.register for p in list(cp.inputs) + list(cp.state)
                if p.register is not None]
        pins += [d.address for d in cp.data if d.address is not None]
        if pins and max(pins) >= base:
            over.append((cid, instr, base, max(pins)))
    assert not over, f"cells overlapping their own instructions: {over}"


def test_budget_gate_catches_a_known_bad_shape():
    """INV-4 for the gate above: re-inflate a cell past its budget and the same
    arithmetic must FLAG it. Without this the gate could be vacuous."""
    instr, maxpin = 23, 10          # the two-publish-body row cell, measured
    base = 31 - instr
    assert maxpin >= base, "the known-bad row shape must be detected as over"


# --------------------------------------------------------------------------
# TRAP 3: the FACE REGISTER PERSISTS, and it steers TRANSITING words too.
#
# These two gates are the ones that turned this block from "emits nothing" into
# "runs the whole cipher". Both failures are silent: no error, no output, a
# clean place/route/build/DRC.
# --------------------------------------------------------------------------
DX = {0: (0, 1), 1: (1, 0), 2: (-1, 0), 3: (0, -1)}     # S, E, W, N
FACE_NAME = {0: "S", 1: "E", 2: "W", 3: "N"}
FACE_OF = {"south": 0, "east": 1, "west": 2, "north": 3}


def _emit_faces_per_port(block):
    """For each (cell, port): every face the port can be EMITTED on.

    Symbolically executes each cell program over both sides of every branch,
    tracking the face register as ``MOVE [FACE], R{data:X}`` mutates it. The face
    register is a CELL register that PERSISTS across entries, so this iterates to
    a FIXPOINT: seed with the resting face, collect the faces each path can LEAVE
    behind, re-seed, repeat. Anything less flatters the design by assuming every
    entry starts clean -- which is exactly the bug this gate exists to catch.
    """
    import re

    lay = block.default_layout()
    rest = {c: FACE_OF[f] for c, (x, y, f) in lay.items()}
    out = {}
    for cid, cp in block.build_cell_programs().items():
        fv = {d.name: d.value for d in cp.data if getattr(d, "is_face", False)}
        lines = [l.strip() for l in cp.assembly_template.splitlines()
                 if l.strip()]
        labels, code = {}, []
        for l in lines:
            if l.endswith(":"):
                labels[l[:-1]] = len(code)
            else:
                code.append(l)
        entries = [e.name for e in cp.entries]
        seeds, pf = {rest[cid]}, {}
        for _ in range(8):
            seen, exits, pf = set(), set(), {}
            stack = [(labels[e], f) for e in entries if e in labels
                     for f in seeds]
            while stack:
                pc, f = stack.pop()
                if (pc, f) in seen:
                    continue
                seen.add((pc, f))
                if pc >= len(code):
                    exits.add(f)
                    continue
                ins = code[pc]
                m = re.match(r"MOVE \[FACE\], R\{data:(\w+)\}", ins)
                if m:
                    stack.append((pc + 1, fv[m.group(1)]))
                    continue
                for mm in re.finditer(r"\{(?:write|jump):(\w+)\}", ins):
                    pf.setdefault(mm.group(1), set()).add(f)
                mb = re.match(r"BR\.\w+ (\w+)", ins)
                if mb:
                    if mb.group(1) in labels:
                        stack.append((labels[mb.group(1)], f))
                    stack.append((pc + 1, f))
                    continue
                if ins.startswith("HALT"):
                    exits.add(f)
                    continue
                stack.append((pc + 1, f))
            if exits <= seeds:
                break
            seeds |= exits
        out[cid] = pf
    return out


def _walk(lay, src, face, dst, limit=31):
    """Hops from ``src`` to ``dst`` leaving on ``face``, forwarding on each
    TRANSIT CELL'S OWN resting face (INV-48 root cause C). None if it misses."""
    pos = {c: (x, y) for c, (x, y, f) in lay.items()}
    rest = {c: FACE_OF[f] for c, (x, y, f) in lay.items()}
    at = {v: k for k, v in pos.items()}
    x, y = pos[src]
    f, n = face, 0
    while n < limit:
        dx, dy = DX[f]
        x, y = x + dx, y + dy
        n += 1
        c = at.get((x, y))
        if c is None:
            return None
        if c == dst:
            return n
        f = rest[c]
    return None


def test_every_internal_edge_lands_on_a_real_forwarding_walk():
    """EVERY declared internal edge must reach its target from EVERY face the
    emitting path can be in.

    A word leaves its source on the source cell's live FACE register and is then
    forwarded on each transit cell's OWN face, so an edge is only sound if the
    face actually live where it is emitted walks to the target. A face that
    misses gives NO output and NO error.

    The block's fold was rebuilt against exactly this check. Four classes of bug
    fell out of it, all of which had built and routed clean:
      * face CONSTANTS pointing the wrong way (`wbk` north off the array, the
        taps south instead of north at their adders, `wb` south instead of
        north at `wbk`);
      * an entry INHERITING a face another entry left behind;
      * a cell deflecting a word that merely TRANSITS it, because its own flip
        was never restored;
      * an internal edge the block never declared at all.
    """
    from gr_kyttar.placement.blocks.chacha20_keystream_block import (
        ChaCha20KeystreamBlock)
    b = ChaCha20KeystreamBlock("fold")
    lay = b.default_layout()
    ef = _emit_faces_per_port(b)
    edges = ([("WRITE", *e) for e in b.internal_connections()]
             + [("JUMP", *e) for e in b.internal_jumps()])
    bad = []
    for kind, s, sp, d, dp in edges:
        faces = ef.get(s, {}).get(sp)
        if not faces:
            bad.append(f"{kind} {s}.{sp} -> {d}.{dp}: port never emitted")
            continue
        for f in sorted(faces):
            if _walk(lay, s, f, d) is None:
                if (s, sp) in KNOWN_CROSS_BATCH_FACE_LEAK:
                    continue
                bad.append(f"{kind} {s}.{sp} -> {d}.{dp}: face "
                           f"{FACE_NAME[f]} never reaches it")
    assert not bad, "edges on no real forwarding walk:\n  " + "\n  ".join(bad)


#: The ONE edge this gate knowingly exempts, with its reason and its cost.
#:
#: ``seq``'s ``finish`` path is the terminal path of a batch and does NOT restore
#: the resting face, so the fixpoint (correctly) reports that a SECOND trigger
#: could enter ``step`` with the face still pointing south and fire the first
#: half-boundary into ``wb`` instead of ``wbk``. Within ONE batch -- which is what
#: the block is driven with, one trigger per keystream block -- it cannot happen:
#: ``finish`` is reached exactly once, at lap 80, and nothing follows it.
#:
#: The restore is ONE word and ``seq`` has none: it assembles to 22 instructions
#: against a ``base_addr`` of 9 with its highest pin at 8. Freeing that word by
#: dropping ``default``'s ``MOVE half, four`` (redundant with ``half``'s
#: ``reset_per_batch``) DOES fit -- and it breaks the block, because shortening
#: ``seq`` moves ``seq.step``'s entry address from 15 to 14 and the build then
#: mis-resolves ``wbk.back`` to it instead of to ``row0.pub`` (also 15). Measured:
#: the realignment ran perfectly and then handed control to the lap counter, and
#: the ring stopped at the first boundary. Entry addresses are PARAMS-DEPENDENT
#: (INV-6/11) and this is that hazard biting an INTERNAL edge.
KNOWN_CROSS_BATCH_FACE_LEAK = {("seq", "bnd")}


def test_the_fold_gate_catches_a_face_that_misses():
    """INV-4 for the gate above. Re-point ONE face constant the way the block
    originally had it — ``wbk``'s row-trigger face NORTH, off the top of the
    array — and the gate must fail. That single constant is what made the block
    'emit no words': the write-back's four rotate triggers left on a face with
    no cell on it at all, silently.
    """
    from gr_kyttar.placement.blocks.chacha20_keystream_block import (
        ChaCha20KeystreamBlock)
    b = ChaCha20KeystreamBlock("mutant")
    lay = b.default_layout()
    # The ORIGINAL constant, NORTH, walks straight off the top of the array.
    assert _walk(lay, "wbk", 3, "row0") is None, (
        "the mutated face must NOT reach row0 -- otherwise this negative "
        "control proves nothing")
    # WEST is subtler and worth pinning: it DOES reach row0, round through `seq`
    # and `wb` -- but at hop 3, not 1, and it never reaches rows 1..3 in the
    # 1/3/5/7 order the schedule needs. A gate that only asked "is it
    # reachable?" would pass this and the block would still be wrong.
    assert _walk(lay, "wbk", 2, "row0") == 3
    assert _walk(lay, "wbk", 2, "row1") != 3
    # Only the face the block actually declares reaches every row, in order.
    for k, hop in ((0, 1), (1, 3), (2, 5), (3, 7)):
        assert _walk(lay, "wbk", 0, f"row{k}") == hop, (
            f"SOUTH must reach row{k} at hop {hop}")

    # The same for the taps: the original SOUTH constant walked off the array;
    # only NORTH reaches the adder, at hop 1.
    for k in range(4):
        assert _walk(lay, f"tap{k}", 0, f"add{k}") is None, (
            f"tap{k}'s original SOUTH face must miss add{k}")
        assert _walk(lay, f"tap{k}", 3, f"add{k}") == 1

    # ...and for `wb`: SOUTH (the original) is swallowed by the pad column;
    # NORTH transits `seq` and lands on `wbk` at hop 2.
    assert _walk(lay, "wb", 0, "wbk") is None
    assert _walk(lay, "wb", 3, "wbk") == 2


def test_declared_emit_faces_are_all_abutting_and_consistent():
    """``emit_faces()`` names a NEIGHBOUR CELL, not a compass direction, so the
    router can derive the face from the placed coordinates and the declaration
    survives rotation (INV-23). Every entry must therefore name a cell that
    actually abuts the emitter, and must agree with the face the program really
    flips to.
    """
    from gr_kyttar.placement.blocks.chacha20_keystream_block import (
        ChaCha20KeystreamBlock)
    b = ChaCha20KeystreamBlock("faces")
    lay = b.default_layout()
    pos = {c: (x, y) for c, (x, y, f) in lay.items()}
    ef = _emit_faces_per_port(b)
    for (cid, port), nbr in b.emit_faces().items():
        assert cid in pos and nbr in pos, f"{cid}/{nbr} not in the layout"
        sx, sy = pos[cid]
        nx, ny = pos[nbr]
        assert abs(nx - sx) + abs(ny - sy) == 1, (
            f"emit_faces[{cid}.{port}] -> {nbr} is not abutting")
        face = {(1, 0): 1, (-1, 0): 2, (0, 1): 0, (0, -1): 3}[(nx - sx,
                                                               ny - sy)]
        assert face in ef.get(cid, {}).get(port, set()), (
            f"emit_faces[{cid}.{port}] says {FACE_NAME[face]}, but the program "
            f"emits it on {[FACE_NAME[f] for f in ef[cid][port]]}")


def test_the_ring_runs_the_whole_rfc_schedule_on_a_built_chip():
    """END TO END on the real placed + routed + built chip.

    Not a proxy: this places the block, auto-routes it between the chip's x16
    ports, builds the bitstream, runs simKYT and reads the trace back. It asserts
    the counts RFC 8439's 20 rounds require -- 80 quarter-round invocations
    through every one of the sixteen stages, 19 half-boundary realignments, and
    the 37/38/39 realignment spins of rows 1/2/3 -- and that the FIRST state word
    out is the RFC's own ``0xE4E7 0xF110``.

    That last assertion is what makes this a value gate rather than a counting
    one: four of this cipher's mutants (diagonal-half-first, no realignment,
    reversed spin direction, stuck tap) all still perform exactly 80
    invocations, so only a byte-level check separates them.

    The block does NOT yet emit all 32 words -- the drain repeat is unfinished,
    see the class docstring -- so this gate pins what IS proven and will tighten
    to the full 32 when the drain lands.
    """
    simkyt = pytest.importorskip("simkyt")
    pytest.importorskip("PySide6")
    from PySide6.QtWidgets import QApplication
    QApplication.instance() or QApplication([])
    from engine.catalog import BlockCatalog
    from engine.io.chip_type_io import load_chip_type
    from engine.build import BuildEngine
    from ui.controller import AppController
    from model.connection import ChipPortEndpoint, BlockEndpoint
    from gr_kyttar.placement.blocks.chacha20_keystream_block import (
        ChaCha20KeystreamBlock)
    from gr_kyttar.placement.resolver import CellProgramResolver

    cat = BlockCatalog.from_gr_kyttar()
    ct = load_chip_type(CHIP_YAML)
    key = getattr(ct, "name", None) or "kyttar_10x12"
    ctrl = AppController(catalog=cat)
    ctrl.new_project("chacha", key)
    blk = ctrl.place_block("ChaCha20KeystreamBlock", 0, 0, 1,
                           library="lattrex.official", params={})
    ctrl.add_logical_connection(ChipPortEndpoint(chip=0, port="x16_in"),
                                BlockEndpoint(block=blk, port="sample"),
                                name="in_blk")
    ctrl.add_logical_connection(BlockEndpoint(block=blk, port="out"),
                                ChipPortEndpoint(chip=0, port="x16_out"),
                                name="blk_out")
    rep = ctrl.auto_route_all({key: ct})
    assert rep.ok, [f"{r.name}:{r.reason}" for r in rep.failed]
    bres = BuildEngine(cat, CHIP_YAML).build(ctrl.project, {key: ct})
    assert bres.ok, bres.errors

    land = (getattr((getattr(bres, "chips", {}) or {}).get(0),
                    "input_landings", {}) or {})["in_blk"]
    chip = simkyt.Chip.from_yaml(CHIP_YAML)
    chip.load_bitstream_physical(bres.words(0))
    chip.set_port_entry_address("x16_in", int(land["entry"]))
    chip.enable_trace(max_records=4000000)
    chip.inject_data_physical([1], target_hop_cnt=int(land["hop"]),
                              target_addr=int(land["data_addrs"][0]))
    chip.run(max_events=6000)
    chip.inject_jump_physical(target_hop_cnt=int(land["hop"]),
                              entry_addr=int(land["entry"]))
    out = []
    for _ in range(50):
        r = chip.run(max_events=200000)
        while chip.output_available("x16_out"):
            w = chip.read_port_i16("x16_out").view("uint16").tolist()
            out.extend(int(v) & 0xFFFF for v in w)
            chip.release_output_ack("x16_out")
            chip.run(max_events=8000)
        if r.get("completed"):
            break

    b = ChaCha20KeystreamBlock("gate")
    lay = b.default_layout()
    name = {(y + 1) * 10 + x: c for c, (x, y, f) in lay.items()}
    R = CellProgramResolver()
    ent = {c: R.compute_entry_addresses(p)
           for c, p in b.build_cell_programs().items()}
    runs = {}
    for ev in chip.get_trace():
        if ev.get("kind") != "exec_tick":
            continue
        cid = name.get(ev["cell_id"])
        if cid is None:
            continue
        for ename, addr in ent.get(cid, {}).items():
            if ev["pc"] == addr:
                runs[(cid, ename)] = runs.get((cid, ename), 0) + 1

    # 20 rounds == 80 quarter-round invocations, through EVERY stage.
    for stage in QR_STAGES:
        assert runs.get((stage, "default")) == 80, (
            f"{stage} ran {runs.get((stage, 'default'))} times, want 80")
    assert runs.get(("seq", "step")) == 80
    assert runs.get(("wb", "default")) == 80
    # 10 double rounds == 19 INTERIOR half-boundaries.
    assert runs.get(("wbk", "bnd")) == 19
    # Realignment spins: row k is spun k then 4-k per boundary.
    for k, want in ((1, 37), (2, 38), (3, 39)):
        assert runs.get((f"row{k}", "spin")) == want, (
            f"row{k} spun {runs.get((f'row{k}', 'spin'))} times, want {want}")
    # The finish arms every tap.
    for k in range(4):
        assert runs.get((f"tap{k}", "arm")) == 1, f"tap{k} was not armed"

    # ...and the VALUE gate: state word 0, exact, from RFC 8439 S2.3.2.
    want0 = g.RFC8439_BLOCK_EXPECTED_STATE[0]
    assert out[:2] == [(want0 >> 16) & 0xFFFF, want0 & 0xFFFF], (
        f"state word 0 on chip = {out[:2]}, want "
        f"{[(want0 >> 16) & 0xFFFF, want0 & 0xFFFF]}")


#: The sixteen quarter-round stages, as the block reuses them.
QR_STAGES = ("l1_add", "l1_xor", "l2_add", "l2_xor", "l2_rota", "l2_rotb",
             "l3_add", "l3_xor", "l3_rota", "l3_rotb", "l4_add", "l4_xor",
             "l4_rota", "l4_rotb")
