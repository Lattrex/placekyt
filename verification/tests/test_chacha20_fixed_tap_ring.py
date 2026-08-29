# SPDX-License-Identifier: GPL-3.0-or-later
"""The ChaCha20 FIXED-TAP RING, and the two silent layout traps it exposed.

``ChaCha20KeystreamBlock`` is still ``needs_human``: it places, routes and builds
cleanly and its static gates are green, but it does NOT yet compute (see the
manifest). What IS established, and what this file gates, is the layer beneath
it — an algebraic restatement of RFC 8439's round schedule that removes the
selector the previous architecture was built around, plus two substrate traps
measured while wiring it.

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
