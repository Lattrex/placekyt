# SPDX-License-Identifier: GPL-3.0-or-later
"""Verify ChaCha20QRBlock against RFC 8439 (no GNU Radio counterpart).

``ChaCha20QRBlock`` computes ONE ChaCha20 quarter round (RFC 8439 §2.1) on four
32-bit words ``a, b, c, d``::

    a += b;  d ^= a;  d <<<= 16
    c += d;  b ^= c;  b <<<= 12
    a += b;  d ^= a;  d <<<= 8
    c += d;  b ^= c;  b <<<= 7

GNU Radio has no such block, so the golden is the published algorithm in
``chacha20_golden.py``, itself pinned by the RFC's OWN test vectors — §2.1.1
(the quarter round in isolation) and §2.2.1 (``QUARTERROUND(2,7,8,13)`` on a
full 16-word state, an INDEPENDENT operand set, so a transcription slip in one
vector cannot pass unnoticed). Those two anchors are asserted BEFORE any DUT
comparison: the golden is proven real first, then it gates the chip.

This is **exact modular integer arithmetic, not Q15 DSP**: every add wraps mod
2**32 and nothing saturates, which is gated explicitly
(``test_wrapping_not_saturating_*``). Words are RAW 16-bit values — raw
injection, EXACT integer equality, tolerance 0 (the XorBlock lesson).

The block is 8 words in / 8 words out: one input word per trigger, the result
frame bursting on the eighth, so ``run_block_dut_rate`` (which drains EVERY
word per trigger) is the driver — ``run_block_dut`` keeps only the last word
per trigger and cannot see a burst.

Run::

    KYTTAR_GR_PYTHON=/usr/bin/python3 QT_QPA_PLATFORM=offscreen \\
        .venv/bin/python -m pytest verification/tests/test_chacha20_qr.py -q
"""
from __future__ import annotations

import os
import random
import sys
from pathlib import Path

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

_PLACEKYT = Path(__file__).resolve().parents[2] / "placekyt"
_VERIFY = Path(__file__).resolve().parents[1]
_RUNTIME = Path(__file__).resolve().parents[2] / "runtime" / "python"
for _p in (str(_RUNTIME), str(_PLACEKYT), str(_VERIFY), str(Path(__file__).parent)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from kyttar_verify import (run_block_dut_rate, write_report,  # noqa: E402
                           CompareResult, Metric)
from gr_kyttar.placement.blocks.chacha20_qr_block import (  # noqa: E402
    FRAME, ChaCha20QRBlock)
from chacha20_golden import (  # noqa: E402
    RFC8439_QUARTERROUND_VECTOR, RFC8439_STATE_QUARTERROUND_INDICES,
    RFC8439_STATE_QUARTERROUND_VECTOR, frame_to_words, quarter_round,
    quarter_round_frame, rotl32, words_to_frame)

CHIP_YAML = str(_PLACEKYT / "resources" / "chips" / "kyttar_10x12.yaml")
_CHIP_OK = os.path.exists(CHIP_YAML)
pytestmark = pytest.mark.skipif(not _CHIP_OK, reason="chip yaml absent")

MASK32 = 0xFFFFFFFF
W = ChaCha20QRBlock.FRAME_WORDS          # 8 words per frame


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------

def _run_dut(frames, *, orient=None, block_type="ChaCha20QRBlock"):
    """Build + run the block on simKYT over a list of 8-word frames.

    Returns the per-frame output word lists.
    """
    stim = [int(w) & 0xFFFF for f in frames for w in f]
    dut = run_block_dut_rate(block_type, stim, chip_yaml=CHIP_YAML,
                             in_port="x", out_port="out", orient=orient)
    assert dut.ok, dut.reason
    flat = [int(w) & 0xFFFF for w in dut.outputs_q15]
    return [flat[i * W:(i + 1) * W] for i in range(len(flat) // W)], dut


def _golden_frames(frames):
    return [quarter_round_frame(f) for f in frames]


def _frame_errors(got, ref):
    """Word errors across aligned frame lists, plus any length gap."""
    n = min(len(got), len(ref))
    assert n > 0, "no frames compared"
    errs = sum(1 for i in range(n)
               for j in range(W) if got[i][j] != ref[i][j])
    errs += abs(len(got) - len(ref)) * W
    return errs, n * W


def _rand_frames(seed, n):
    rng = random.Random(seed)
    return [words_to_frame(*(rng.getrandbits(32) for _ in range(4)))
            for _ in range(n)]


# --------------------------------------------------------------------------
# The golden is REAL: the RFC's own vectors, asserted before any DUT compare
# --------------------------------------------------------------------------

def test_golden_matches_rfc8439_section_2_1_1():
    """RFC 8439 §2.1.1 — the quarter round in isolation, exact."""
    inp, exp = RFC8439_QUARTERROUND_VECTOR
    assert inp == (0x11111111, 0x01020304, 0x9B8D6F43, 0x01234567)
    assert exp == (0xEA2A92F4, 0xCB1CF8CE, 0x4581472E, 0x5881C4BB)
    assert quarter_round(*inp) == exp


def test_golden_matches_rfc8439_section_2_2_1():
    """RFC 8439 §2.2.1 — QUARTERROUND(2,7,8,13) on a full 16-word state. An
    INDEPENDENT operand set: it re-pins the same primitive on different data,
    so a mis-transcribed §2.1.1 could not slip through both."""
    state, expected = RFC8439_STATE_QUARTERROUND_VECTOR
    st = list(state)
    idx = RFC8439_STATE_QUARTERROUND_INDICES
    for k, v in zip(idx, quarter_round(*(st[i] for i in idx))):
        st[k] = v
    assert tuple(st) == expected


def test_golden_rotl32_is_a_rotation():
    """``rotl32`` is a true 32-bit rotation: bit-count preserving, and
    ``rotl32(x, n)`` composed to 32 is the identity."""
    rng = random.Random(0)
    for _ in range(200):
        x = rng.getrandbits(32)
        for n in (7, 8, 12, 16):
            r = rotl32(x, n)
            assert bin(r).count("1") == bin(x).count("1")
            assert rotl32(r, 32 - n) == x


def test_golden_frame_roundtrip():
    """The hi/lo frame view is a lossless recoding of four 32-bit words."""
    rng = random.Random(7)
    for _ in range(200):
        v = tuple(rng.getrandbits(32) for _ in range(4))
        assert frame_to_words(words_to_frame(*v)) == v


# --------------------------------------------------------------------------
# On-chip correctness
# --------------------------------------------------------------------------

def test_rfc8439_quarter_round_vector_on_chip():
    """THE gate: the RFC 8439 §2.1.1 vector, computed on the real placed and
    routed chip, word for word."""
    inp, exp = RFC8439_QUARTERROUND_VECTOR
    got, _ = _run_dut([words_to_frame(*inp)])
    assert len(got) == 1, f"expected 1 output frame, got {len(got)}"
    assert frame_to_words(got[0]) == exp, (
        f"on-chip {[hex(v) for v in frame_to_words(got[0])]} "
        f"!= RFC {[hex(v) for v in exp]}")


def test_rfc8439_state_quarter_round_vector_on_chip():
    """RFC 8439 §2.2.1's operands, on chip — the second independent anchor."""
    state, expected = RFC8439_STATE_QUARTERROUND_VECTOR
    idx = RFC8439_STATE_QUARTERROUND_INDICES
    got, _ = _run_dut([words_to_frame(*(state[i] for i in idx))])
    assert frame_to_words(got[0]) == tuple(expected[i] for i in idx)


@pytest.mark.parametrize("seed", [1, 2, 3, 12345])
def test_random_32bit_inputs_bit_exact(seed):
    """Random 32-bit operands (>=3 seeds): every output word exactly matches
    the golden. No tolerance — this is integer arithmetic."""
    frames = _rand_frames(seed, 4)
    got, _ = _run_dut(frames)
    errs, n = _frame_errors(got, _golden_frames(frames))
    assert errs == 0, f"seed {seed}: {errs}/{n} word errors"


def test_multiple_frames_back_to_back():
    """Frames are independent and the 8-word framing never drifts: six
    back-to-back frames all land exactly."""
    frames = _rand_frames(99, 6)
    got, _ = _run_dut(frames)
    assert len(got) == 6, f"expected 6 frames, got {len(got)}"
    errs, _ = _frame_errors(got, _golden_frames(frames))
    assert errs == 0, f"{errs} word errors across 6 frames"


def test_rate_is_eight_in_eight_out():
    """Exactly 8 words egress, and ONLY on every eighth trigger — the frame
    counter neither leaks partial frames nor double-emits."""
    frames = _rand_frames(5, 3)
    stim = [w for f in frames for w in f]
    dut = run_block_dut_rate("ChaCha20QRBlock", stim, chip_yaml=CHIP_YAML,
                             in_port="x", out_port="out")
    assert dut.ok, dut.reason
    counts = [len(t) for t in dut.per_trigger]
    assert len(counts) == len(stim)
    for i, c in enumerate(counts):
        expect = W if (i + 1) % W == 0 else 0
        assert c == expect, (f"trigger {i}: emitted {c} words, expected "
                             f"{expect} (counts={counts})")


@pytest.mark.parametrize("anchor", [(0, 0), (0, 1), (1, 0), (1, 1),
                                    (2, 1), (1, 2), (0, 2), (2, 2)],
                         ids=lambda a: f"{a[0]}x{a[1]}")
def test_correct_at_every_anchor(anchor):
    """The 8x3 fold is large, and corridor disjointness is known to be
    ANCHOR-dependent for big folds (the AGCCC / ComplexToMag precedent, where a
    block computes correctly at one placement and routes into the port cell at
    another). Assert the RFC vector is exact from every anchor the block fits,
    not just the harness default.
    """
    inp, exp = RFC8439_QUARTERROUND_VECTOR
    frame = words_to_frame(*inp)
    dut = run_block_dut_rate("ChaCha20QRBlock", frame, chip_yaml=CHIP_YAML,
                             in_port="x", out_port="out", place_xy=anchor)
    assert dut.ok, f"anchor {anchor}: {dut.reason}"
    got = [int(w) & 0xFFFF for w in dut.outputs_q15]
    assert got == list(quarter_round_frame(frame)), (
        f"anchor {anchor}: got {[hex(w) for w in got]}")


def test_trailing_partial_frame_not_emitted():
    """A trailing partial frame (< 8 words) produces NO output."""
    frames = _rand_frames(21, 2)
    stim = [w for f in frames for w in f] + [0x1234, 0x5678, 0x9ABC]
    dut = run_block_dut_rate("ChaCha20QRBlock", stim, chip_yaml=CHIP_YAML,
                             in_port="x", out_port="out")
    assert dut.ok, dut.reason
    assert len(dut.outputs_q15) == 2 * W, (
        f"partial frame leaked: {len(dut.outputs_q15)} words, expected {2 * W}")


# --------------------------------------------------------------------------
# Wrapping, NOT saturating — the property that separates this from Q15 DSP
# --------------------------------------------------------------------------

#: Operand sets that force a carry out of bit 31 (or bit 15 into bit 16).
_WRAP_CORNERS = [
    (0xFFFFFFFF, 0xFFFFFFFF, 0xFFFFFFFF, 0xFFFFFFFF),   # every add wraps
    (0xFFFFFFFF, 0x00000001, 0xFFFFFFFF, 0x00000001),   # a+b == 2**32 exactly
    (0x00000000, 0x00000000, 0x00000000, 0x00000000),   # all-zero
    (0x80000000, 0x80000000, 0x80000000, 0x80000000),   # sign-bit corner
    (0x0000FFFF, 0x00000001, 0x0000FFFF, 0x00000001),   # half-carry into hi
    (0xFFFF0000, 0x00010000, 0x7FFFFFFF, 0x80000000),   # hi-half wrap
    (0x7FFFFFFF, 0x00000001, 0x7FFFFFFF, 0x00000001),   # Q15 rail, must NOT clamp
]


@pytest.mark.parametrize("vals", _WRAP_CORNERS,
                         ids=[f"{v[0]:08x}" for v in _WRAP_CORNERS])
def test_wrapping_not_saturating_on_chip(vals):
    """Inputs near 2**32 must WRAP (mod 2**32), never saturate. A Q15-style
    saturating datapath would clamp at 0x7FFF/0x8000 and fail here."""
    got, _ = _run_dut([words_to_frame(*vals)])
    assert frame_to_words(got[0]) == quarter_round(*vals), (
        f"wrap corner {[hex(v) for v in vals]}: got "
        f"{[hex(v) for v in frame_to_words(got[0])]}")


def test_wrapping_corners_exercise_a_carry_at_every_add():
    """The wrap corners are not vacuous. Replay the quarter round on each
    corner and record which of the FOUR adds actually carried out of bit 31;
    every one of them must be exercised by some corner, and the low->high
    half-carry (the ``ADC``) must fire too.

    Without this the wrapping test above could pass on operands that never
    overflow, proving nothing about wrapping at all.
    """
    add_carried = [False] * 4
    half_carried = False

    for a, b, c, d in _WRAP_CORNERS:
        def _add(x, y, which):
            nonlocal half_carried
            if ((x & 0xFFFF) + (y & 0xFFFF)) > 0xFFFF:
                half_carried = True          # the ADC path (bit 15 -> bit 16)
            if x + y > MASK32:
                add_carried[which] = True    # wraps mod 2**32
            return (x + y) & MASK32

        a = _add(a, b, 0)
        d = rotl32(d ^ a, 16)
        c = _add(c, d, 1)
        b = rotl32(b ^ c, 12)
        a = _add(a, b, 2)
        d = rotl32(d ^ a, 8)
        c = _add(c, d, 3)
        b = rotl32(b ^ c, 7)

    assert all(add_carried), (
        f"adds never exercised at positions "
        f"{[i for i, v in enumerate(add_carried) if not v]} — the wrapping "
        "gate is partly vacuous")
    assert half_carried, "the low->high half-carry (ADC) is never exercised"


def test_no_q15_saturation_rail_appears():
    """No output word is pinned to the Q15 rails when the true answer is not:
    a saturating adder betrays itself as 0x7FFF/0x8000 words."""
    vals = (0x7FFFFFFF, 0x00000001, 0x7FFFFFFF, 0x00000001)
    got, _ = _run_dut([words_to_frame(*vals)])
    assert list(got[0]) == list(quarter_round_frame(words_to_frame(*vals)))


# --------------------------------------------------------------------------
# The block's own reference
# --------------------------------------------------------------------------

def test_process_reference_matches_golden():
    """``process_reference`` == the golden over random frames (the reference
    the block carries is itself real)."""
    frames = _rand_frames(2026, 5)
    stim = [w for f in frames for w in f]
    ref = [int(w) & 0xFFFF
           for w in ChaCha20QRBlock("r").process_reference(stim)]
    exp = [w for f in _golden_frames(frames) for w in f]
    assert ref == exp


def test_process_reference_drops_partial_frame():
    """A trailing partial frame produces no reference output either."""
    stim = [w for f in _rand_frames(3, 2) for w in f] + [1, 2, 3]
    ref = ChaCha20QRBlock("r").process_reference(stim)
    assert len(ref) == 2 * W


def test_process_reference_matches_dut():
    """The block's reference and the SILICON agree — the model shipped with
    the block is the model the chip implements."""
    frames = _rand_frames(808, 4)
    got, _ = _run_dut(frames)
    stim = [w for f in frames for w in f]
    ref = [int(w) & 0xFFFF
           for w in ChaCha20QRBlock("r").process_reference(stim)]
    assert [w for f in got for w in f] == ref


# --------------------------------------------------------------------------
# MANDATORY mutation gates (INV-4): every corruption MUST be caught
# --------------------------------------------------------------------------

def _mutant_qr(rot=(16, 12, 8, 7), *, add_as_xor=None, drop_carry=False,
               swap_hi_lo_rot16=False):
    """A deliberately BROKEN quarter round, parameterised by mutation.

    ``rot`` perturbs the four rotate constants; ``add_as_xor`` (an index in
    0..3) turns that add into a xor; ``drop_carry`` performs the 32-bit adds as
    two INDEPENDENT 16-bit adds (the classic multi-word bug — no ADC);
    ``swap_hi_lo_rot16`` reverses the hi/lo order of the free rotate-16 swap.
    """
    r0, r1, r2, r3 = rot

    def _add(x, y, which):
        if add_as_xor == which:
            return x ^ y
        if drop_carry:
            lo = ((x & 0xFFFF) + (y & 0xFFFF)) & 0xFFFF      # carry DISCARDED
            hi = ((x >> 16) + (y >> 16)) & 0xFFFF
            return (hi << 16) | lo
        return (x + y) & MASK32

    def _rot16(x):
        # The real rotate-16 IS the hi/lo swap; reversing the order is a no-op
        # rotation by 0, which is the mutation being modelled.
        return x if swap_hi_lo_rot16 else rotl32(x, r0)

    def f(a, b, c, d):
        a = _add(a, b, 0); d ^= a; d = _rot16(d)
        c = _add(c, d, 1); b ^= c; b = rotl32(b, r1)
        a = _add(a, b, 2); d ^= a; d = rotl32(d, r2)
        c = _add(c, d, 3); b ^= c; b = rotl32(b, r3)
        return a, b, c, d
    return f


def _mutation_frames():
    """A fixed stimulus set the mutation gates share (the RFC vector plus
    random frames, so a mutation cannot hide in one lucky operand)."""
    return ([words_to_frame(*RFC8439_QUARTERROUND_VECTOR[0])]
            + _rand_frames(4242, 4))


def _assert_mutant_detected(mutant, label):
    """The gate must FAIL on this mutant: its output must differ from the
    golden on the shared stimulus."""
    frames = _mutation_frames()
    ref = _golden_frames(frames)
    mut = [words_to_frame(*mutant(*frame_to_words(f))) for f in frames]
    errs, n = _frame_errors(mut, ref)
    assert errs > 0, f"MUTATION WENT UNDETECTED: {label}"


@pytest.mark.parametrize("idx,name,bad", [
    (0, "rot16", 15), (0, "rot16", 17),
    (1, "rot12", 11), (1, "rot12", 13),
    (2, "rot8", 7), (2, "rot8", 9),
    (3, "rot7", 6), (3, "rot7", 8),
])
def test_mutation_perturbed_rotate_constant_fails(idx, name, bad):
    """Each of the four rotate constants (16/12/8/7), perturbed by +-1
    INDEPENDENTLY, must be caught. This is the mutation the block's whole
    rotate construction exists to get right."""
    rot = [16, 12, 8, 7]
    rot[idx] = bad
    _assert_mutant_detected(_mutant_qr(tuple(rot)),
                            f"{name} -> {bad}")


@pytest.mark.parametrize("which", [0, 1, 2, 3])
def test_mutation_add_swapped_for_xor_fails(which):
    """Swapping any one of the four 32-bit ADDs for a XOR must be caught."""
    _assert_mutant_detected(_mutant_qr(add_as_xor=which),
                            f"add #{which} -> xor")


def test_mutation_dropped_carry_fails():
    """Performing the 32-bit adds as two INDEPENDENT 16-bit adds — dropping
    the carry from bit 15 into bit 16 (i.e. ADD instead of ADC on the high
    half) — must be caught. This is THE multi-word arithmetic bug."""
    _assert_mutant_detected(_mutant_qr(drop_carry=True), "dropped carry")


def test_mutation_reversed_rot16_hi_lo_swap_fails():
    """The free ``ROTL32(d, 16)`` is a hi/lo register swap; reversing the
    order (i.e. NOT swapping) must be caught."""
    _assert_mutant_detected(_mutant_qr(swap_hi_lo_rot16=True),
                            "rot16 hi/lo swap reversed")


def test_mutation_word_order_reversed_fails():
    """Emitting the frame's words in the wrong order must be caught (guards
    the egress slot order, including the accumulator-delivered slot 0)."""
    frames = _mutation_frames()
    ref = _golden_frames(frames)
    mut = [list(reversed(f)) for f in ref]
    errs, _ = _frame_errors(mut, ref)
    assert errs > 0, "a reversed frame word order went undetected!"


def test_mutation_hi_lo_swapped_in_frame_fails():
    """Swapping hi and lo within each 32-bit word of the emitted frame must be
    caught (guards the hi/lo convention end to end)."""
    frames = _mutation_frames()
    ref = _golden_frames(frames)
    mut = [[f[i ^ 1] for i in range(W)] for f in ref]
    errs, _ = _frame_errors(mut, ref)
    assert errs > 0, "a hi/lo swap went undetected!"


def test_mutation_one_frame_shift_fails():
    """A +1-frame shift of the output stream must FAIL (no free lag
    alignment, INV-2)."""
    frames = _rand_frames(31, 5)
    got, _ = _run_dut(frames)
    ref = _golden_frames(frames)
    shifted = [[0] * W] + got[:-1]
    errs, _ = _frame_errors(shifted, ref)
    assert errs > 0, "a one-frame shift went undetected!"


def test_mutation_identity_passthrough_fails():
    """A block that simply echoed its input frame would be caught."""
    frames = _mutation_frames()
    errs, _ = _frame_errors(frames, _golden_frames(frames))
    assert errs > 0, "an identity passthrough went undetected!"


def test_empty_output_fails():
    """An empty/short output cannot be certified against a non-empty
    reference (a length gap counts as error)."""
    ref = _golden_frames(_rand_frames(77, 3))
    errs, _ = _frame_errors([[0] * W], ref)
    assert errs > 0, "an (near-)empty output went undetected!"


# --------------------------------------------------------------------------
# INV-4 on SILICON: real on-chip mutants, each built/placed/routed and run
# --------------------------------------------------------------------------

def _run_mutated(patch):
    """Build + run the block on chip with ``patch`` applied to
    ``build_cell_programs``, then restore. Returns the flat output words."""
    from gr_kyttar.placement.blocks import chacha20_qr_block as _m

    orig = _m.ChaCha20QRBlock.build_cell_programs
    try:
        _m.ChaCha20QRBlock.build_cell_programs = patch(orig)
        frames = _rand_frames(5, 3)
        stim = [w for f in frames for w in f]
        dut = run_block_dut_rate("ChaCha20QRBlock", stim, chip_yaml=CHIP_YAML,
                                 in_port="x", out_port="out")
        assert dut.ok, dut.reason
        got = [int(w) & 0xFFFF for w in dut.outputs_q15]
        exp = [w for f in _golden_frames(frames) for w in f]
        return got, exp
    finally:
        _m.ChaCha20QRBlock.build_cell_programs = orig


def test_onchip_mutant_perturbed_rotate_constant_fails():
    """A REAL on-chip mutant with ``rot7`` built as ``rot6`` must be caught.

    The model-level mutation tests prove the golden discriminates; this proves
    the SILICON gate does — the mutant is placed, routed, built and run, and it
    still emits the right NUMBER of words, so it fails on values alone.
    """
    def patch(orig):
        def bcp(self):
            progs = orig(self)
            cls = type(self)
            progs["l4_rota"] = cls._rot_a("b", 6)                 # was 7
            progs["l4_rotb"] = cls._rot_b("b", 6, last=FRAME[0])
            return progs
        return bcp
    got, exp = _run_mutated(patch)
    assert len(got) == len(exp), "mutant changed the word COUNT, not the values"
    assert got != exp, "an ON-CHIP rot6 mutant went undetected by the gate!"


def test_onchip_mutant_dropped_carry_fails():
    """A REAL on-chip mutant that uses ``ADD`` instead of ``ADC`` on the high
    half — the dropped carry, THE multi-word arithmetic bug — must be caught."""
    def patch(orig):
        def bcp(self):
            progs = orig(self)
            progs["l1_add"] = type(self)._relay_cell("""\
    ADD R{in:a_lo}, R{in:b_lo}
    MOVE R{in:a_lo}, R0
    ADD R{in:a_hi}, R{in:b_hi}
    MOVE R{in:a_hi}, R0
""")
            return progs
        return bcp
    got, exp = _run_mutated(patch)
    assert len(got) == len(exp), "mutant changed the word COUNT, not the values"
    assert got != exp, "an ON-CHIP dropped-carry mutant went undetected!"


def test_onchip_mutant_removed_rot16_swap_fails():
    """A REAL on-chip mutant with the FREE ``ROTL32(d, 16)`` hi/lo swap removed
    must be caught. Because that rotate costs zero instructions, an incorrect
    one is invisible to every size/budget check — only a value gate sees it."""
    def patch(orig):
        def bcp(self):
            progs = orig(self)
            progs["l1_xor"] = type(self)._xor32("d", "a", rot16=False)
            return progs
        return bcp
    got, exp = _run_mutated(patch)
    assert len(got) == len(exp), "mutant changed the word COUNT, not the values"
    assert got != exp, "an ON-CHIP rot16-removed mutant went undetected!"


# --------------------------------------------------------------------------
# Structural guards
# --------------------------------------------------------------------------

def test_no_cell_overlaps_its_own_instructions():
    """INV-33's overlap half, as a STATIC gate: no data address, state
    register, or pinned INPUT register may land at or above
    ``31 - instr_count``, where the resolver lays instructions downward.

    The resolver's own space check compares only DATA against ``base_addr`` —
    it never checks state or inputs — so an over-budget cell assembles, loads,
    and runs WRONG. This block hit exactly that: an 8-word egress cell (8
    inputs + 24 instructions) put R7/R8 on top of its first two instruction
    words and silently dropped the LEADING word of every burst while the other
    seven came out bit-exact.
    """
    from gr_kyttar.placement.resolver import (CellProgramResolver,
                                              ResolvedTargets, WriteTarget,
                                              JumpTarget)
    res = CellProgramResolver()
    offenders = []
    for cid, p in ChaCha20QRBlock("g").build_cell_programs().items():
        tg = ResolvedTargets(
            writes={o.name: WriteTarget(1, 1) for o in p.outputs},
            jumps={o.name: JumpTarget(1, 1) for o in p.outputs})
        asm = res._substitute_registers(p.assembly_template, p,
                                        res._allocate_data(p.data),
                                        state_map={}, input_map={}, dummy=True)
        asm = res._substitute_write_jump(asm, tg, dummy=True)
        base = 31 - res._count_instructions(asm)
        used = ([d.address for d in p.data if d.address is not None]
                + [s.register for s in p.state if s.register is not None]
                + [i.register for i in p.inputs if i.register is not None])
        over = sorted(a for a in used if a >= base)
        if over:
            offenders.append((cid, base, over))
    assert not offenders, f"cells overlap their own instructions: {offenders}"


def test_overlap_gate_catches_the_known_bad_shape():
    """INV-4 for the gate above: re-inflate the pre-fix egress cell (all eight
    frame words held in registers) and assert the check FAILS on it."""
    from gr_kyttar.placement.block import CellProgram, EntryPoint, Port
    from gr_kyttar.placement.resolver import (CellProgramResolver,
                                              ResolvedTargets, WriteTarget,
                                              JumpTarget)
    body = "".join(f"    MOVE R0, R{{in:{s}}}\n    {{write:out}}\n"
                   "    {jump:out}\n" for s in FRAME)
    bad = CellProgram(
        inputs=[Port(w, register=1 + i) for i, w in enumerate(FRAME)],
        outputs=[Port("out")],
        entries=[EntryPoint("default")],
        data=[], state=[],
        assembly_template="default:\n" + body + "    HALT\n")
    res = CellProgramResolver()
    tg = ResolvedTargets(writes={"out": WriteTarget(1, 1)},
                         jumps={"out": JumpTarget(1, 1)})
    asm = res._substitute_registers(bad.assembly_template, bad,
                                    res._allocate_data(bad.data),
                                    state_map={}, input_map={}, dummy=True)
    asm = res._substitute_write_jump(asm, tg, dummy=True)
    base = 31 - res._count_instructions(asm)
    over = [i.register for i in bad.inputs if i.register >= base]
    assert over, ("the pre-fix 8-word egress cell no longer overlaps — the "
                  "overlap gate would not have caught the real bug")


def test_layout_is_folded_and_io_colocated():
    """Layout conventions: <=8 cells across, and the input and output ports
    co-locate on ONE bus-facing edge (else the block silently fails to route)."""
    from engine.catalog import BlockCatalog
    pm = BlockCatalog.from_gr_kyttar().port_map("ChaCha20QRBlock", {})
    w, h = pm.footprint
    assert w + 1 <= 8 and h + 1 <= 8, f"footprint {(w + 1, h + 1)} exceeds 8"
    assert pm.io_colocated, "input and output are not co-located on one edge"
    assert len(pm.outputs()) == 1, (
        f"expected ONE output port (an 8-word burst on one net), got "
        f"{[p.name for p in pm.outputs()]}")


def test_cell_programs_pair_positionally_with_layout():
    """INV-33 positional pairing: ``build_cell_programs()`` dict order MUST
    equal ``default_layout()`` order, or program A is assigned to cell B with
    no error at all."""
    blk = ChaCha20QRBlock("p")
    assert list(blk.build_cell_programs()) == list(blk.default_layout())
    assert len(blk.build_cell_programs()) == blk.cell_count


def test_every_declared_entry_is_reachable():
    """INV-39: every declared EntryPoint must be the target of an internal
    jump or the block's own external entry — an entry nothing jumps at is
    dead code that only the chip can reveal."""
    blk = ChaCha20QRBlock("p")
    targeted = {(dst, entry) for (_s, _p, dst, entry) in blk.internal_jumps()}
    for cid, prog in blk.build_cell_programs().items():
        for e in prog.entries:
            reachable = (cid, e.name) in targeted or cid == "in0"
            assert reachable, f"entry {cid}.{e.name} is unreachable"


# --------------------------------------------------------------------------
# INV-23 orientation invariance (full burst) and INV-19 saturation
# --------------------------------------------------------------------------

_D4 = [["cw"], ["cw", "cw"], ["cw", "cw", "cw"], ["mirror_h"], ["mirror_v"],
       ["mirror_h", "cw"], ["mirror_v", "cw"]]


@pytest.mark.parametrize("orient", _D4, ids=["+".join(o) for o in _D4])
def test_orientation_invariant_full_burst(orient):
    """INV-23: all 8 D4 orientations produce the IDENTICAL full burst stream.

    The shared orientation gate (test_orientation_invariance.py) covers the
    last-word-per-trigger view; this covers EVERY word of the 8-word frame,
    which is what the block actually emits.
    """
    frames = _rand_frames(64, 3)
    stim = [w for f in frames for w in f]
    ident = run_block_dut_rate("ChaCha20QRBlock", stim, chip_yaml=CHIP_YAML,
                               in_port="x", out_port="out")
    assert ident.ok, ident.reason
    rot = run_block_dut_rate("ChaCha20QRBlock", stim, chip_yaml=CHIP_YAML,
                             in_port="x", out_port="out", orient=orient)
    assert rot.ok, rot.reason
    assert rot.outputs_q15 == ident.outputs_q15, (
        f"orientation {orient} diverges from identity")
    # ...and the identity result is itself CORRECT, so an all-orientations
    # agreement on a wrong answer cannot pass.
    exp = [w for f in _golden_frames(frames) for w in f]
    assert [int(w) & 0xFFFF for w in ident.outputs_q15] == exp


def test_saturated_equals_per_sample():
    """INV-19: the whole burst enqueued back-to-back (no inter-sample
    quiescence) must produce the SAME stream as the per-sample drive.

    The block is feed-forward with no data-feedback loop and no reconvergent
    fan-in, so it needs no serialize-LOCK — but that is a claim the saturated
    drive has to actually demonstrate, not an assumption.
    """
    from kyttar_verify.dut_runner import run_block_dut_pipelined

    frames = _rand_frames(128, 3)
    stim = [w for f in frames for w in f]
    seq = run_block_dut_rate("ChaCha20QRBlock", stim, chip_yaml=CHIP_YAML,
                             in_port="x", out_port="out")
    assert seq.ok, seq.reason
    seq_out = list(seq.outputs_q15)
    assert len(seq_out) == len(frames) * W

    pipe = run_block_dut_pipelined("ChaCha20QRBlock", [(w,) for w in stim],
                                   chip_yaml=CHIP_YAML, in_ports=("x",),
                                   out_port="out")
    assert pipe.ok, f"saturated build/run failed: {pipe.reason}"
    n = len(seq_out)
    assert len(pipe.outputs_q15) >= n, (
        f"saturated produced {len(pipe.outputs_q15)} words, per-sample "
        f"produced {n} — the pipeline STALLED or mis-paced")
    assert list(pipe.outputs_q15[:n]) == seq_out, (
        "saturated output diverges from per-sample at index "
        + str(next(i for i in range(n) if pipe.outputs_q15[i] != seq_out[i])))


# --------------------------------------------------------------------------
# report
# --------------------------------------------------------------------------

def test_emit_report():
    """Emit the dashboard report reflecting a passing bit-exact verification."""
    frames = ([words_to_frame(*RFC8439_QUARTERROUND_VECTOR[0])]
              + [words_to_frame(*v) for v in _WRAP_CORNERS]
              + _rand_frames(1, 3) + _rand_frames(2, 3) + _rand_frames(3, 3))
    got, dut = _run_dut(frames)
    errs, n = _frame_errors(got, _golden_frames(frames))
    res = CompareResult(passed=(errs == 0), metric=Metric.EXACT,
                        n_compared=n, bit_errors=errs, delay_used=0)
    assert res.passed, res.summary()
    write_report("ChaCha20QRBlock", res, coverage={
        "golden": ("RFC 8439 §2.1 quarter round (verification/tests/"
                   "chacha20_golden.py); no GNU Radio counterpart"),
        "anchors": ("RFC 8439 §2.1.1 quarter-round vector AND §2.2.1 "
                    "QUARTERROUND(2,7,8,13) state vector — both exact ON CHIP"),
        "random": "3 seeds x 4 frames + 6 back-to-back (32-bit operands)",
        "edge": ("7 wrapping corners incl. 0xFFFFFFFF x4, a+b == 2**32, the "
                 "sign-bit corner and the Q15 rail — WRAPS, never saturates"),
        "rate": "8 words in / 8 words out; partial frame never emitted",
        "placements": ("RFC vector exact from all 8 anchors the 8x3 fold fits "
                       "(corridor disjointness is anchor-dependent for big "
                       "folds)"),
        "mutation": ("each rotate constant 16/12/8/7 perturbed +-1 "
                     "independently (8 cases) / each of the 4 adds swapped "
                     "for xor / DROPPED CARRY (16-bit adds, no ADC) / "
                     "rot16 hi-lo swap reversed / frame word order reversed / "
                     "hi-lo swapped / +1 frame shift / identity passthrough / "
                     "empty"),
        "mutation_onchip": ("3 REAL mutants built+placed+routed+run on simKYT, "
                            "each caught while still emitting the right word "
                            "COUNT: rot7 built as rot6; ADC->ADD (dropped "
                            "carry); the free rot16 hi/lo swap removed"),
        "structural": ("INV-33 no cell overlaps its own instructions (with an "
                       "INV-4 negative on the pre-fix 8-word egress cell); "
                       "positional pairing; every entry reachable; folded "
                       "layout, I/O co-located, ONE output port"),
        "cells": ChaCha20QRBlock("m").cell_count,
        "instr_measured": ("32-bit ADD=4 (ADD/MOVE/ADC/MOVE), XOR=4, "
                           "ROTL32(x,16)=0 (hi/lo swap folded into the relay), "
                           "ROTL32(x,n<16)=7 over 2 cells (4 ROL + 3 merge)"),
        "note": ("exact 32-bit modular integer arithmetic, NOT Q15; raw 16-bit "
                 "words, tolerance 0, delay 0"),
    })
