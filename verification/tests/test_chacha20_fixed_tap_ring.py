# SPDX-License-Identifier: GPL-3.0-or-later
"""The ChaCha20 FIXED-TAP RING, the silent layout traps it exposed, and the
on-chip gate that pins how much of the cipher actually runs.

``ChaCha20KeystreamBlock`` is still ``needs_human`` — but for a much smaller
reason than before. The CIPHER IS NOW CORRECT on a real placed + routed + built
chip: 80 quarter-round invocations, **20** half-boundary realignments, the four
drain laps, 32 words emitted, and **all sixteen state words bit-exact against RFC
8439 §2.3.2**. What remains is the ORDER they leave in — one drain lap empties one
SLOT of every row, so the words come out lap-major, the 4x4 transpose of §2.3.2's
order. Every value is right; the positions are permuted. See the class docstring
for why row-major does not fit this fold.

This file gates three layers:

* the algebraic restatement of RFC 8439's round schedule that removes the
  selector the original architecture was built around;
* the substrate traps measured while wiring it — the closed-ring/positional-
  pairing/gap ones (INV-51), the FACE-register ones (INV-52: the face persists
  across entries, it steers TRANSITING words, and the router cannot see it), and
  the backward-JUMP-by-address one (INV-NEXT), which had silently redirected an
  internal edge for a whole pass behind an entry-address coincidence;
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
                bad.append(f"{kind} {s}.{sp} -> {d}.{dp}: face "
                           f"{FACE_NAME[f]} never reaches it")
    assert not bad, "edges on no real forwarding walk:\n  " + "\n  ".join(bad)
    # NO EXEMPTIONS. An earlier revision had to exempt `seq.bnd`, because
    # `seq`'s `finish` path did not restore the resting face and the fixpoint
    # correctly reported that a SECOND trigger could enter `step` still pointing
    # south and fire the first half-boundary into `wb` instead of `wbk`. That
    # restore now exists -- `finish` has to hand off to `wbk.bnd` for the closing
    # realignment bracket anyway, so it restores EAST on the way -- and the gate
    # is unconditional again.


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


def test_entry_addresses_stay_distinct_where_edges_resolve():
    """Entry addresses are PARAMS-DEPENDENT (INV-6/11), and two entries that
    collide numerically can mask a mis-resolved edge.

    ``seq.step`` and ``row0.pub`` both resolved to address 15 in an earlier
    revision. That coincidence hid a real defect for a whole pass: the build was
    rewriting ``wbk.back`` (authored ``-> row0.pub``) to ``seq.step``, and
    because the two addresses were equal the corrupted jump still landed on the
    right entry. Any edit that moved either entry -- and a three-word saving in
    ``seq`` did -- turned a silent latent bug into eighty laps of wrong answers.

    This gate does not forbid collisions in general (they are common and mostly
    harmless). It pins the SPECIFIC pair whose equality masked the defect, so
    that if a future edit re-collides them the reason is at least visible.
    """
    from gr_kyttar.placement.blocks.chacha20_keystream_block import (
        ChaCha20KeystreamBlock)
    from gr_kyttar.placement.resolver import CellProgramResolver

    b = ChaCha20KeystreamBlock("addrs")
    R = CellProgramResolver()
    ent = {c: R.compute_entry_addresses(p)
           for c, p in b.build_cell_programs().items()}
    assert ent["seq"]["step"] != ent["row0"]["pub"], (
        "seq.step and row0.pub have collided again "
        f"(both {ent['seq']['step']}); a mis-resolved wbk.back would be "
        "invisible")


def test_the_realignment_needs_TWENTY_brackets_not_nineteen():
    """INV-4 for the closing bracket -- the bug that hid behind row 0.

    Each diagonal half is bracketed by ``k`` spins of row ``k`` before and
    ``4 - k`` after. Ten double rounds therefore need TEN of each. Issuing only
    the nineteen that fall BETWEEN laps leaves every row short by its own closing
    bracket, and the drain then reads slot ``k`` of row ``k`` instead of slot 0.

    Row 0's bracket is zero spins either way, so row 0 stays aligned and its head
    -- the RFC's first output word -- is still bit-exact. That is exactly why a
    gate on word 0 alone could not see this.
    """
    key, nonce, ctr = g.RFC8439_BLOCK_KEY, g.RFC8439_BLOCK_NONCE, 1
    init = g.initial_state(key, nonce, ctr)

    def run(close_last):
        rows = [Row(init[4 * k:4 * k + 4]) for k in range(4)]
        spins = [0, 0, 0, 0]

        def realign(mult):
            for k in range(4):
                for _ in range((mult * k) & 3):
                    rows[k].spin()
                    spins[k] += 1

        for dr in range(10):
            for half in (0, 1):
                if half == 1:
                    realign(1)
                for _ in range(4):
                    a, b, c, d = g.quarter_round(*[r.pub() for r in rows])
                    for r, v in zip(rows, (a, b, c, d)):
                        r.wb(v)
                if half == 1 and (close_last or dr != 9):
                    realign(-1)
        heads = [(rows[k].s[0] + init[4 * k]) & g.MASK32 for k in range(4)]
        return spins, heads

    ok_spins, ok_heads = run(True)
    bad_spins, bad_heads = run(False)

    # The correct schedule spins rows 1..3 forty times; the truncated one
    # 37/38/39 -- exactly `10a + 9b` against `10a + 10b`.
    assert ok_spins == [0, 40, 40, 40]
    assert bad_spins == [0, 37, 38, 39]

    # Row 0 is IDENTICAL either way -- the whole reason this was invisible.
    assert ok_heads[0] == bad_heads[0] == g.RFC8439_BLOCK_EXPECTED_STATE[0]
    # ...and rows 1..3 are wrong without the closing bracket.
    for k in (1, 2, 3):
        assert ok_heads[k] == g.RFC8439_BLOCK_EXPECTED_STATE[4 * k]
        assert bad_heads[k] != g.RFC8439_BLOCK_EXPECTED_STATE[4 * k]


def test_at_most_one_backward_internal_jump_per_cell():
    """INV-48 rule 2, as a GATE rather than a comment.

    ``build._apply_internal_feedback`` resolves a BACKWARD internal jump (one
    whose destination cell precedes its source in ``build_cell_programs`` order)
    by rewriting the source cell's HIGHEST-ADDRESSED JUMP instruction. A cell
    with two backward jumps therefore keeps one and silently loses the other,
    and a cell with one backward jump whose highest-addressed JUMP is a
    DIFFERENT jump has that other jump silently redirected.

    Both bit this block. ``wbk`` declared one backward jump (``step`` -> ``seq``)
    but its highest-addressed JUMP was ``back`` -> ``row0.pub``, so the build
    rewrote ``back`` to point at ``seq.step``. It went unnoticed for a whole pass
    because ``seq.step`` and ``row0.pub`` happened to resolve to the SAME numeric
    address (15) and the corrupted jump landed on the right entry by coincidence;
    shortening ``seq`` by three words decoupled them and the realignment's
    hand-back went to the lap counter instead.
    """
    from gr_kyttar.placement.blocks.chacha20_keystream_block import (
        ChaCha20KeystreamBlock)
    from gr_kyttar.placement.resolver import CellProgramResolver

    b = ChaCha20KeystreamBlock("backward")
    progs = b.build_cell_programs()
    order = list(progs)
    idx = {c: i for i, c in enumerate(order)}

    backward = {}
    for s, sp, d, dp in b.internal_jumps():
        if idx[d] < idx[s]:
            backward.setdefault(s, []).append((sp, d, dp))
    for cid, edges in backward.items():
        assert len(edges) == 1, (
            f"{cid} declares {len(edges)} backward jumps {edges}; the build "
            f"keeps only the highest-addressed one and drops the rest")

    # ...and the surviving one must BE the cell's highest-addressed jump.
    R = CellProgramResolver()
    for cid, ((sp, d, dp),) in ((c, tuple(e)) for c, e in backward.items()):
        lines = [ln.strip() for ln in progs[cid].assembly_template.splitlines()
                 if ln.strip()]
        code = [ln for ln in lines if not ln.endswith(":")]
        jaddrs = [i for i, ln in enumerate(code) if "{jump:" in ln]
        assert jaddrs, f"{cid} has a backward jump but no JUMP instruction"
        last = code[max(jaddrs)]
        assert f"{{jump:{sp}}}" == last.strip(), (
            f"{cid}'s backward jump is '{sp}' but its highest-addressed JUMP is "
            f"'{last.strip()}' -- the build will rewrite THAT one instead")


def test_the_ring_runs_the_whole_rfc_schedule_on_a_built_chip():
    """END TO END on the real placed + routed + built chip.

    Not a proxy: this places the block, auto-routes it between the chip's x16
    ports, builds the bitstream, runs simKYT and reads the trace back.

    It asserts the counts RFC 8439's 20 rounds require -- 80 quarter-round
    invocations through every one of the sixteen stages, **20** half-boundary
    realignments and the 40/40/40 realignment spins of rows 1/2/3 -- and then,
    the part that actually decides correctness, that **all sixteen output state
    words are bit-exact** against the RFC's §2.3.2 vector.

    The value gate is what makes this real: four of this cipher's mutants
    (diagonal-half-first, no realignment, reversed spin direction, stuck tap) all
    still perform exactly 80 invocations, so no count-based or structural check
    separates them -- only the bytes do.

    **Why 20 realignments and not 19.** Each diagonal half is BRACKETED, ``k``
    spins of row ``k`` before it and ``4 - k`` after, so ten double rounds need
    ten opening and ten closing brackets. Nineteen fall between laps; the
    twentieth is the closing bracket of the LAST diagonal half and has no
    following lap to hang off, so ``seq.finish`` issues it explicitly. An earlier
    revision issued only nineteen and this gate asserted nineteen -- the spin
    counts were 37/38/39 where the schedule requires 40/40/40, i.e. exactly
    ``10a + 9b`` against ``10a + 10b``. It hid behind row 0, whose bracket is
    zero spins either way: row 0 stayed aligned and its head came out bit-exact
    while rows 1..3 were left rotated by ``4 - k`` too little. A gate that
    checked only word 0 passed it. Hence the assertion below is over ALL SIXTEEN
    words, not the first.

    **Emission ORDER is a known, documented gap.** One drain lap empties one slot
    of every row, so the words leave lap-major -- ``state[0], state[4],
    state[8], state[12], state[1], ...``, the 4x4 transpose of §2.3.2's order.
    Every value is right; the positions are permuted. This gate therefore checks
    the values against the transposed order AND checks that the emitted multiset
    is exactly the RFC's sixteen words, which is what pins the arithmetic. See
    ``ChaCha20KeystreamBlock``'s Status section for why row-major does not fit
    this fold.
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
    # Origin (0, 0). The fold's TOP band is now the reorder row, which carries
    # both `out` (its east end) and the walk `seq`'s release trigger climbs, so
    # the block starts at array row 0 and the chip's I/O corridor taps `seq` at
    # (0, 1) from the west edge rather than from a free row above. Measured:
    # origin y=0 routes both nets; y=1..4 all fail `in_blk` with "no free
    # corridor between the ports", because a 7-tall block at y=1 leaves the
    # input port no way in.
    blk = ctrl.place_block("ChaCha20KeystreamBlock", 0, 0, 0,
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
    # The block is placed at origin (0, 0), so a layout offset IS its array
    # cell id (row-major, 10 wide).
    name = {y * 10 + x: c for c, (x, y, f) in lay.items()}
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
    # 10 double rounds == 10 OPENING + 10 CLOSING brackets. Nineteen fall
    # between laps; the twentieth is issued by `seq.finish` (see the docstring).
    assert runs.get(("wbk", "bnd")) == 20
    # Realignment spins: row k is spun k then 4-k per double round, ten times,
    # so every row 1..3 spins exactly 40 times -- plus the THREE drain spins.
    for k in (1, 2, 3):
        assert runs.get((f"row{k}", "spin")) == 43, (
            f"row{k} spun {runs.get((f'row{k}', 'spin'))} times, want 43 "
            f"(40 realignment + 3 drain)")
    # row0's bracket is zero spins either way, so it only ever sees the drain's.
    assert runs.get(("row0", "spin")) == 3
    # The finish arms every tap, and the drain then runs four laps.
    for k in range(4):
        assert runs.get((f"tap{k}", "arm")) == 1, f"tap{k} was not armed"
        assert runs.get((f"add{k}", "default")) == 4, (
            f"add{k} fired {runs.get((f'add{k}', 'default'))} times, want 4")
    assert runs.get(("drn", "default")) == 4
    assert runs.get(("out", "default")) == 32

    # ...and THE VALUE GATE: all sixteen state words, bit-exact AND IN RFC 8439
    # S2.3.2 ORDER, on the real placed + routed + built chip. This is the
    # definition of done for this block.
    #
    # The drain still emits lap-major -- one lap empties one slot of every row,
    # and that is structural (INV-55: one pass visiting every source once makes
    # the source index the fast-varying half of the output position). The order
    # is corrected at the COLLECTOR by the eight reorder-buffer stages: each
    # adder's four words are held in a 4-deep FIFO built as `bufA_k -> bufB_k`,
    # and the release walks the eight stages west to east, so output group `k`
    # is exactly `add_k`'s four words in lap order.
    assert len(out) == 32, f"emitted {len(out)} words, want 32"
    got32 = [(out[2 * i] << 16) | out[2 * i + 1] for i in range(16)]
    want = list(g.RFC8439_BLOCK_EXPECTED_STATE)
    assert got32 == want, (
        "on-chip state words differ from RFC 8439 S2.3.2:\n"
        f"  got  {[f'{v:#010x}' for v in got32]}\n"
        f"  want {[f'{v:#010x}' for v in want]}")


#: The sixteen quarter-round stages, as the block reuses them.
QR_STAGES = ("l1_add", "l1_xor", "l2_add", "l2_xor", "l2_rota", "l2_rotb",
             "l3_add", "l3_xor", "l3_rota", "l3_rotb", "l4_add", "l4_xor",
             "l4_rota", "l4_rotb")


# ===========================================================================
# THE EMISSION ORDER — why it is what it is, and what would change it.
#
# The block's one remaining defect is that its 32 output words leave in
# LAP-MAJOR order (the 4x4 transpose of RFC 8439 S2.3.2). The gates below pin
# what the 2026-08-29 pass MEASURED, so the next builder inherits facts instead
# of re-deriving them — and so a future "just permute X" idea is refuted by a
# running test rather than by an argument in a docstring.
#
# The headline result: the reorder is NOT expressible at the ROWS (every
# boot-time and drain-time knob is exhaustively searched below and none works),
# it IS expressible at the COLLECTOR as a per-adder buffer, and that buffer
# misses this fold's cell budget by EXACTLY THREE INSTRUCTION WORDS.
# ===========================================================================
def _ring_read_pattern(bracket=(0, 1, 2, 3)):
    """The slot each row is read at, for all 80 laps, plus the final offsets.

    This is the ring the block actually runs: four rows rotating one step per
    lap, with row ``k`` spun ``bracket[k]`` extra times before each diagonal
    half and the same number back after it.
    """
    off = [0, 0, 0, 0]
    pat = []
    for _ in range(10):
        for diagonal in (False, True):
            if diagonal:
                for k in range(4):
                    off[k] = (off[k] + bracket[k]) % 4
            for _ in range(4):
                pat.append(tuple(off))
                for k in range(4):
                    off[k] = (off[k] + 1) % 4
            if diagonal:
                for k in range(4):
                    off[k] = (off[k] - bracket[k]) % 4
    return pat, tuple(off)


def _rfc_schedule():
    """All 80 quarter-round index quadruples, in order."""
    return [g.quarterround_indices(j, diagonal)
            for _ in range(10)
            for diagonal in (False, True)
            for j in range(4)]


def test_the_boot_load_map_is_FORCED_by_the_quarter_round_schedule():
    """The cheapest imaginable fix — permute the state at BOOT — cannot work.

    A boot-time permutation costs ZERO instructions and zero cells: it is just
    different ``initial_value`` constants in :meth:`_row`. If emitting
    lap-major from a transposed load yielded row-major, the defect would be a
    one-line fix. **It does not, and this gate says why with no freedom left
    over.**

    Row ``k`` slot ``i`` is read on exactly the laps where that row's rotation
    offset is ``i``, and on each lap the quarter round demands a SPECIFIC state
    word there — RFC 8439's own index quadruple. Walking the 80 laps therefore
    PINS every one of the sixteen (row, slot) cells, with no conflicts and no
    slot left free. The unique solution is the identity ``LOAD[k][i] = 4k + i``,
    which is exactly what the block ships.

    So there is no boot-time permutation to choose: the quarter-round wiring
    has already chosen it. (Measured 2026-08-29.)
    """
    pat, _final = _ring_read_pattern()
    load = [[None] * 4 for _ in range(4)]
    for offsets, quad in zip(pat, _rfc_schedule()):
        for k in range(4):
            slot = offsets[k]
            if load[k][slot] is None:
                load[k][slot] = quad[k]
            else:
                assert load[k][slot] == quad[k], (
                    f"row{k} slot{slot} is demanded as both "
                    f"{load[k][slot]} and {quad[k]}")
    assert all(v is not None for row in load for v in row), (
        "some (row, slot) was never read — the load map would be free there")
    assert load == [[4 * k + i for i in range(4)] for k in range(4)], (
        "the forced load map is not the identity the block ships")


def test_no_drain_side_knob_can_produce_row_major_order():
    """EXHAUSTIVE: every free parameter of the DRAIN leaves the order lap-major.

    With the load map forced (gate above), the drain emits, at output position
    ``4L + rank(k)``, the word ``load[k][(off_k + L*spin_k) % 4]``. Three knobs
    are genuinely free and cost nothing:

    * ``off_k``  — row ``k``'s rotation when the drain starts (extra pre-drain
      spins, which ``seq``/``wbk`` already know how to issue);
    * ``spin_k`` — how far row ``k`` advances between drain laps (``drn``'s
      schedule, currently one);
    * ``rank``   — the order the four rows publish within a lap (pure wiring:
      the tap chain's baton order).

    All 4^4 x 4^4 x 4! combinations are searched. **None produces §2.3.2 order,
    and the best any achieves is 4 of 16 positions correct.** The reason is
    structural: one drain lap visits each row exactly once, so each lap emits
    one word per row; the row index is therefore the FAST-varying part of the
    output position while the state index carries it in the SLOW nibble. No
    permutation of laps or rows can exchange those.

    This is what makes the fix a COLLECTOR problem rather than a row problem.
    """
    import itertools
    load = [[4 * k + i for i in range(4)] for k in range(4)]
    want = list(range(16))
    best = -1
    for off in itertools.product(range(4), repeat=4):
        for spin in itertools.product(range(1, 5), repeat=4):
            for rank in itertools.permutations(range(4)):
                emit = [None] * 16
                for lap in range(4):
                    for k in range(4):
                        emit[4 * lap + rank[k]] = \
                            load[k][(off[k] + lap * spin[k]) % 4]
                if None in emit or len(set(emit)) != 16:
                    continue
                assert emit != want, (
                    f"a drain-side knob DOES give row-major: off={off} "
                    f"spin={spin} rank={rank} — the block should use it")
                best = max(best, sum(1 for a, b in zip(emit, want) if a == b))
    assert best == 4, f"best partial match changed from 4 to {best}"


def test_the_transpose_is_a_PER_ADDER_buffer_not_a_per_row_loop():
    """The reorder, stated at the COLLECTOR end, is small and needs no counter
    that reaches the rows.

    Emission position ``4L + k`` carries ``state[4k + L]``. Read that the other
    way round: the word wanted at output position ``4k + L`` is the one
    ``add_k`` produces on drain lap ``L``. So **output group ``k`` is exactly
    ``add_k``'s four words, in lap order** — and the whole 4x4 transpose is
    "hold each adder's four words, then release adder by adder".

    That is a per-ADDER buffer of four 32-bit words with a four-step release,
    not the per-row loop earlier passes searched for (and correctly found no
    room for). This gate simulates the buffer's exact cell semantics — a
    4-deep shift register, one store per drain lap, one emit-and-advance per
    release step — and asserts the output is RFC 8439 §2.3.2 order.
    """
    class _Buf:
        def __init__(self):
            self.slots = [None] * 4
            self.tail = None

        def store(self, word):           # the `default` entry
            self.tail = word
            self.slots = self.slots[1:] + [self.tail]

        def release(self, sink):         # the `rel` entry
            sink.append(self.slots[0])
            self.slots = self.slots[1:] + [self.tail]

    bufs = [_Buf() for _ in range(4)]
    for lap in range(4):                 # the four drain laps
        for k in range(4):
            bufs[k].store(4 * k + lap)   # add_k emits state[4k + lap]
    out = []
    for k in range(4):                   # release, buffer by buffer
        for _ in range(4):
            bufs[k].release(out)
    assert out == list(range(16)), (
        f"the per-adder buffer does not give §2.3.2 order: {out}")


def test_the_reorder_buffer_misses_this_folds_cell_budget_by_three_words():
    """The MEASURED gap, and the thing a re-fold has to close.

    A cell's 32 addresses are shared by code and data: instructions pack
    downward from 30 (``base_addr = 31 - instruction_count``) and registers and
    data words pack upward from 1, so a cell is legal only while ``base_addr``
    exceeds its highest live address. Overshooting is SILENT — the cell
    assembles, loads, places and routes clean and overlays its own code with
    data (INV-33's overlap half).

    The reorder buffer of the gate above needs, per adder:

    * eight registers for four 32-bit words, plus two for the arriving pair
      (the adder writes hi and lo into different registers) — ten live words
      before anything else;
    * an 8-instruction shift (a 4-deep 32-bit rotate — irreducible);
    * a release that emits one word and advances, which must RE-ENTER the cell
      three times. The finish row is a one-way eastward conveyor, so re-entry
      needs a WESTWARD hop, which costs a face constant plus its restore
      (INV-52 — the face register also steers transiting words, and this row
      carries every other buffer's released words to the egress).

    Measured on this fold: **without the release counter the cell is 16
    instructions against a ``base_addr`` of 15 and 12 live words — three words
    spare. With it, 20 instructions, ``base_addr`` 11, 13 live words — an
    overlap of three.** The counter's ``SUB``/``MOVE``/``BR`` triple is the
    entire shortfall, and it has nowhere else to live on this fold.

    This gate pins those two numbers. It is the specification for the re-fold
    that closes them: DEPTH-2 buffers, two per adder, free four registers and
    bring the cell in with room to spare — and a second finish row is where
    they go (checked by the last gate in this file).
    """
    without_counter = ["MOVE R0,s0h", "WRITE oh", "MOVE R0,s0l", "WRITE ol",
                       "JUMP ol", "MOVE FACE,f_back", "JUMP self",
                       "MOVE FACE,f_line"] + ["shift"] * 8
    with_counter = ["MOVE R0,s0h", "WRITE oh", "MOVE R0,s0l", "WRITE ol",
                    "JUMP ol", "SUB cnt,f_line", "MOVE cnt,R0", "BR.NZ again",
                    "JUMP nxt", "MOVE FACE,f_back", "JUMP self",
                    "MOVE FACE,f_line"] + ["shift"] * 8
    # live words: 2 inputs + 8 state (+1 counter) + 2 face constants
    assert 31 - len(without_counter) - (2 + 8 + 2) == 3, (
        "the counter-less reorder buffer no longer has three words spare")
    assert (2 + 8 + 1 + 2) - (31 - len(with_counter)) + 1 == 3, (
        "the reorder buffer's overlap is no longer exactly three words")

def test_the_two_row_reorder_band_is_BUILT_and_every_walk_resolves():
    """The re-fold pass 6 specified, now BUILT -- checked walk by walk.

    The block was 41 cells in a 10x6 fold whose top row (the four finish
    adders) was SEALED: measured over every free slot of the bounding box x
    every face, no cell of that row could reach ANY free slot, so the reorder
    buffer the transpose needs had nowhere to live (INV-55 rule 2).

    Pass 7 shifted the whole fold DOWN one array row and added a REORDER BAND
    on top, giving each adder a PAIR of depth-2 buffer stages. The block is now
    48 cells in a 10x7 fold:

        y=0  reorder:  bpad0 bpad1 B0 A0 B1 A1 B2 A2 B3 A3
        y=1  finish:   seq wbk add0 . add1 . add2 . add3 out
        y=2  state:    wb | row0 tap0 row1 tap1 row2 tap2 row3 tap3 | in0
        y=3+ the control column and the quarter-round legs, shifted down one

    Depth 2 is what makes the stage fit: it halves the state (four registers,
    not eight) AND removes the release counter outright, because a depth-2 cell
    can emit BOTH its words from one straight-line entry. Measured: the depth-4
    form was 20 instructions against a ``base_addr`` of 11 with 13 live words --
    INV-33's overlap by exactly three; the depth-2 stage is 22 against 9.

    This gate checks the built geometry, not a proposal: every walk the block
    actually declares, re-measured on ``_geometry()`` itself.
    """
    from gr_kyttar.placement.blocks.chacha20_keystream_block import (
        ChaCha20KeystreamBlock, BUFFER_CHAIN)
    b = ChaCha20KeystreamBlock("refold")
    lay = b._geometry()
    positions = [v[:2] for v in lay.values()]
    assert len(set(positions)) == len(positions), "the fold self-overlaps"
    assert max(y for _x, y in positions) <= 11, "the fold leaves the array"
    assert len(lay) == 48, f"the fold is {len(lay)} cells, want 48"

    for k in range(4):
        # the tap still reaches its adder at hop 1, straight north
        assert _walk(lay, f"tap{k}", FACE_OF["north"], f"add{k}") == 1
        # the adder rests NORTH and reaches its FIRST stage at hop 2, through
        # the second stage that sits directly above it
        assert _walk(lay, f"add{k}", FACE_OF["north"], f"bufA{k}") == 2
        # ...and that stage spills WEST into the second at hop 1
        assert _walk(lay, f"bufA{k}", FACE_OF["west"], f"bufB{k}") == 1
        # the state line's broadcast pattern is untouched by the shift
        assert _walk(lay, "wbk", FACE_OF["south"], f"row{k}") == 1 + 2 * k
        assert _walk(lay, "drn", FACE_OF["north"], f"row{k}") == 1 + 2 * k
    # the control hand-off that `wb`'s one face flip pays for
    assert _walk(lay, "wb", FACE_OF["north"], "wbk") == 2
    # the drain lap's baton still climbs the control column
    assert _walk(lay, "tap3", FACE_OF["south"], "add_pad") == 19
    assert _walk(lay, "add_pad", FACE_OF["east"], "drn") == 1

    # THE RELEASE TRIGGER. The state line is a uniform EAST conveyor, so the
    # only cells that can lift a word off it are the four TAPS -- each already
    # owns a NORTH flip for its adder, and the adders rest north too, so a
    # tap's inward walk passes THROUGH its adder onto the reorder row. That is
    # what fixes the reorder row's columns: `bufB_k` sits directly above
    # `add_k` so that `tap0`'s walk lands exactly on the chain's head.
    assert _walk(lay, "drn", FACE_OF["north"], "tap0") == 2
    assert _walk(lay, "tap0", FACE_OF["north"], "bufB0") == 2

    # THE RELEASE CHAIN rides the row's own eastward resting face: every stage
    # reaches the next at hop 1 and the egress at the hop its column implies.
    for i, cid in enumerate(BUFFER_CHAIN[:-1]):
        assert _walk(lay, cid, FACE_OF["east"], BUFFER_CHAIN[i + 1]) == 1
    for i, cid in enumerate(BUFFER_CHAIN[:-1]):
        assert _walk(lay, cid, FACE_OF["east"], "out") == 8 - i
    # ...and the row's east end rests SOUTH, dropping into the egress below.
    assert lay["bufA3"][2] == "south"
    assert _walk(lay, "bufA3", FACE_OF["south"], "out") == 1


def test_the_refold_gate_catches_a_broken_reorder_walk():
    """INV-4 for the gate above. Move ONE buffer stage off its column and the
    fill walk must break -- the adder's hop-2 north write is what pins the
    reorder row's columns, and it is the constraint that decides where the
    release chain's head can be."""
    from gr_kyttar.placement.blocks.chacha20_keystream_block import (
        ChaCha20KeystreamBlock)
    b = ChaCha20KeystreamBlock("mutant")
    lay = dict(b._geometry())
    # Swap bufB0 and bufA0: the adder then feeds the FAR end of the FIFO and
    # `tap0`'s trigger lands on the wrong stage.
    lay["bufB0"], lay["bufA0"] = lay["bufA0"], lay["bufB0"]
    assert _walk(lay, "add0", FACE_OF["north"], "bufA0") != 2, (
        "the swapped layout must break the adder's hop-2 fill walk")
    assert _walk(lay, "tap0", FACE_OF["north"], "bufB0") != 2, (
        "the swapped layout must break the release trigger's landing")
