# SPDX-License-Identifier: GPL-3.0-or-later
"""Verify BinArgmaxBlock — framewise argmax over a real Q15 stream.

There is NO stock GNU Radio streaming counterpart (like Crc16Block /
ZeroCrossingRateBlock), so the golden is the independent numpy reference below,
written from the block's PINNED contract:

  * per non-overlapping frame of n consecutive words, emit ONE raw integer word
    = the ZERO-BASED index (0..n-1) of the frame's maximum; a trailing partial
    frame is never emitted (rate-reducing n:1);
  * comparison is SIGNED Q15 (words view as int16; -32768 vs +32767 must order
    correctly — the on-chip compare is the overflow-corrected SLT branch);
  * tie: FIRST occurrence wins (strictly-greater update). This is exactly
    ``numpy.argmax``'s documented convention ("In case of multiple occurrences
    of the maximum values, the indices corresponding to the first occurrence
    are returned") — pinned by ``test_numpy_golden_first_occurrence_tie`` so
    the golden itself is proven to implement the contract;
  * state fully resets between frames (running max re-arms to -32768, counters
    reload) — adjacent frames are independent.

The output is a RAW INDEX WORD (an integer, not a Q15 sample), so the gates are
EXACT word-list equality (tol 0). ``run_block_dut`` records None on the n-1
non-emitting triggers of each frame; the emitted stream is ``outputs[n-1::n]``.
Per INV-4 every gate is paired with mutations proven to FAIL: >= tie flip
(last-occurrence), running max not reset between frames, index off-by-one,
frame counter closing one early, inverted comparison (argmin), on-chip wrong n,
+1 delay, empty.

Run:
    KYTTAR_GR_PYTHON=/usr/bin/python3 QT_QPA_PLATFORM=offscreen \
      <venv>/python -m pytest verification/tests/test_bin_argmax.py -x -q
"""
from __future__ import annotations

import os
import random
import sys
from pathlib import Path

import numpy as np
import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

_PLACEKYT = Path(__file__).resolve().parents[2] / "placekyt"
_VERIFY = Path(__file__).resolve().parents[1]
_RUNTIME = Path(__file__).resolve().parents[2] / "runtime" / "python"
for p in (str(_PLACEKYT), str(_VERIFY), str(_RUNTIME)):
    if p not in sys.path:
        sys.path.insert(0, p)

from kyttar_verify import (  # noqa: E402
    run_block_dut, compare_against_grc, write_report, Metric)
from gr_kyttar.placement.blocks.bin_argmax_block import BinArgmaxBlock  # noqa: E402

CHIP_YAML = str(_PLACEKYT / "resources" / "chips" / "kyttar_10x12.yaml")
pytestmark = pytest.mark.skipif(
    not os.path.exists(CHIP_YAML), reason="chip yaml absent")


# --- the independent numpy golden ---------------------------------------------

def _signed(w: int) -> int:
    w = int(w) & 0xFFFF
    return w - 0x10000 if w >= 0x8000 else w


def _golden_argmax(stim_q15, n: int) -> list[int]:
    """One index word per COMPLETE frame of n words: numpy.argmax over the
    frame's SIGNED int16 values (first-occurrence ties); trailing partial frame
    dropped."""
    signed = np.asarray([int(w) & 0xFFFF for w in stim_q15],
                        dtype=np.uint16).view(np.int16)
    return [int(np.argmax(signed[j * n:(j + 1) * n]))
            for j in range(len(signed) // n)]


def _golden_floats(stim_q15, n: int) -> list[float]:
    """The golden as floats for compare_against_grc (round(v*32768) reproduces
    the exact index word — indices are 0..n-1 <= 32767, always exact)."""
    return [idx / 32768.0 for idx in _golden_argmax(stim_q15, n)]


def _q15(f: float) -> int:
    return max(-32768, min(32767, int(round(f * 32768.0)))) & 0xFFFF


def _random(seed, count):
    rng = random.Random(seed)
    return [rng.randint(0, 0xFFFF) for _ in range(count)]


def _run_dut(stim, n, **kw):
    dut = run_block_dut("BinArgmaxBlock", stim, params={"n": n},
                        chip_yaml=CHIP_YAML, in_port="sample", out_port="out",
                        **kw)
    assert dut.ok, dut.reason
    return dut


def _emitted(dut, n):
    """The index stream: one word on input indices n-1, 2n-1, ..."""
    return [w & 0xFFFF for w in dut.outputs_q15[n - 1::n]]


# --- the golden's own tie convention (the contract the DUT is held to) --------

def test_numpy_golden_first_occurrence_tie():
    """PINS the golden: numpy.argmax returns the FIRST occurrence of the
    maximum (documented numpy behavior), which is exactly the block's pinned
    strictly-greater tie convention. If numpy ever changed this, the golden
    would no longer encode the contract — this test would catch it."""
    assert int(np.argmax([5, 5, 5])) == 0
    assert int(np.argmax([1, 9, 9, 1])) == 1
    assert int(np.argmax(np.asarray([-7, -7, -7], dtype=np.int16))) == 0
    # and the frame golden inherits it
    assert _golden_argmax([5, 5, 5, 5], 4) == [0]
    assert _golden_argmax([1, 9, 9, 1], 4) == [1]


# --- emit-phase (rate) contract -----------------------------------------------

@pytest.mark.parametrize("n", [1, 2, 4, 5])
def test_emit_phase(n):
    """Rate-REDUCING contract: an index word egresses IFF the frame completes
    (input index i with i % n == n-1); every other trigger produces nothing.
    n=1 is the degenerate 1:1 frame (emits on every trigger)."""
    stim = _random(11, 4 * n)
    dut = _run_dut(stim, n)
    for i, w in enumerate(dut.outputs_q15):
        assert (w is not None) == (i % n == n - 1), \
            f"n={n} sample {i}: emitted={w is not None}, expected {i % n == n - 1}"


# --- exact vs the numpy golden (random full-range, >=3 seeds, N sweep) --------

@pytest.mark.parametrize("n", [4, 16])
@pytest.mark.parametrize("seed", [1, 7, 42])
def test_exact_vs_golden(n, seed):
    """EXACT (tol 0) on full-range random words — every word pattern is legal
    input (signed compare), so the stimulus sweeps the whole 16-bit range."""
    frames = 6 if n == 4 else 4
    stim = _random(seed, frames * n)
    dut = _run_dut(stim, n)
    res = compare_against_grc(dut.outputs_q15[n - 1::n], _golden_floats(stim, n),
                              metric=Metric.EXACT, delay=0)
    print(f"\nn={n} seed={seed}:", res.summary())
    assert res.passed, res.summary()
    assert _emitted(dut, n) == _golden_argmax(stim, n)


@pytest.mark.parametrize("n,frames", [(64, 3), (128, 2), (256, 2)])
def test_exact_large_frames(n, frames):
    """The dispatched frame sizes 64/256 plus the manifest default 128, exact."""
    stim = _random(5, frames * n)
    dut = _run_dut(stim, n)
    assert _emitted(dut, n) == _golden_argmax(stim, n), f"n={n}: DUT != golden"


def test_block_reference_matches_independent_golden():
    """The block's own process_reference_q15 == this file's independent golden
    (two implementations of the pinned contract must agree), across a sweep
    including non-power-of-two n; the float reference agrees on exactly-
    representable Q15 values."""
    stim = _random(3, 132)
    for n in (1, 2, 4, 7, 16, 33):
        blk = BinArgmaxBlock("ref", n=n)
        assert blk.process_reference_q15(stim) == _golden_argmax(stim, n), f"n={n}"
        floats = [_signed(w) / 32768.0 for w in stim]
        assert list(blk.process_reference(floats)) == _golden_argmax(stim, n), \
            f"n={n}: float reference disagrees"


# --- tie frames (the pinned FIRST-occurrence convention, on-chip) -------------

def test_all_equal_frame_emits_index_zero():
    """All-equal frames emit index 0 — including the all-(-32768) frame, which
    equals the running-max re-arm sentinel (no strictly-greater update ever
    fires, and the argmax register re-arms to index 0), and the all-(+32767)
    rail frame."""
    n = 4
    stim = ([_q15(0.25)] * n + [_q15(-0.5)] * n
            + [0x8000] * n + [0x7FFF] * n + [0x0000] * n)
    dut = _run_dut(stim, n)
    assert _emitted(dut, n) == [0, 0, 0, 0, 0]
    assert _golden_argmax(stim, n) == [0, 0, 0, 0, 0]


def test_max_at_first_and_last_position():
    n = 4
    stim = ([_q15(0.9), _q15(0.1), _q15(0.2), _q15(0.3)]      # max first -> 0
            + [_q15(0.1), _q15(0.2), _q15(0.3), _q15(0.9)])   # max last -> 3
    dut = _run_dut(stim, n)
    assert _emitted(dut, n) == [0, 3]
    assert _golden_argmax(stim, n) == [0, 3]


def test_duplicate_maxima_first_wins():
    """Two equal maxima in one frame: the FIRST keeps the index (strictly-
    greater update). Duplicates at (1,3) -> 1; at (0,2) -> 0."""
    n = 4
    stim = ([_q15(0.1), _q15(0.7), _q15(0.2), _q15(0.7)]
            + [_q15(0.7), _q15(0.2), _q15(0.7), _q15(0.1)])
    dut = _run_dut(stim, n)
    assert _emitted(dut, n) == [1, 0]
    assert _golden_argmax(stim, n) == [1, 0]


# --- negative / signed-extreme frames (the SIGNED compare, on-chip) -----------

def test_all_negative_frame():
    n = 4
    stim = [_q15(-0.9), _q15(-0.1), _q15(-0.5), _q15(-0.2)]   # max = -0.1 at 1
    dut = _run_dut(stim, n)
    assert _emitted(dut, n) == [1]
    assert _golden_argmax(stim, n) == [1]


def test_signed_extremes_order_correctly():
    """PINS the overflow-corrected signed compare: -32768 and +32767 in one
    frame differ by more than 16 bits can hold, so a naive N-flag test after
    CMP would mis-order them. Both arrival orders on-chip."""
    n = 4
    stim = ([0x8000, 0x7FFF, 0x0000, 0x8000]     # +32767 at 1
            + [0x7FFF, 0x8000, 0x8000, 0x0000]   # +32767 at 0
            + [0x8000, 0x8001, 0x8000, 0x8000])  # -32767 beats -32768 -> 1
    dut = _run_dut(stim, n)
    assert _emitted(dut, n) == [1, 0, 1]
    assert _golden_argmax(stim, n) == [1, 0, 1]


def test_mixed_sign_frame():
    """Any positive beats any negative (signed, not magnitude): frame
    [-0.9, +0.1, -0.5, +0.05] -> index 1 even though |-0.9| is largest."""
    n = 4
    stim = [_q15(-0.9), _q15(0.1), _q15(-0.5), _q15(0.05)]
    dut = _run_dut(stim, n)
    assert _emitted(dut, n) == [1]
    assert _golden_argmax(stim, n) == [1]


# --- frame-boundary independence (state fully resets) -------------------------

def test_frame_boundary_independence():
    """A crafted adjacent pair: frame 1 carries the GLOBAL maximum (at pos 3);
    frame 2 is entirely smaller with its own maximum at pos 1. Correct per-frame
    reset emits [3, 1]; a running max carried across the boundary would never
    update in frame 2 and emit 0 for it."""
    n = 4
    stim = ([_q15(0.1), _q15(0.2), _q15(0.3), _q15(0.9)]
            + [_q15(0.2), _q15(0.5), _q15(0.1), _q15(0.4)])
    dut = _run_dut(stim, n)
    assert _emitted(dut, n) == [3, 1]
    assert _golden_argmax(stim, n) == [3, 1]


def test_n1_identity():
    """n=1: every frame is a single word, every emitted index is 0."""
    stim = _random(9, 6)
    dut = _run_dut(stim, 1)
    assert _emitted(dut, 1) == [0] * 6


# --- parameter validation (the declared supported range raises loudly) --------

@pytest.mark.parametrize("bad", [0, -1, 32769, 65536, 2.5])
def test_invalid_n_raises(bad):
    with pytest.raises(ValueError):
        BinArgmaxBlock("bad", n=bad)


def test_n_range_bounds_construct():
    """The declared bounds 1 and 32768 (2^15) construct and program-build; the
    32768 frame length encodes as the 0x8000 down-counter word and its indices
    0..32767 stay non-negative 16-bit (reference checked on a synthetic frame)."""
    BinArgmaxBlock("lo", n=1).build_cell_programs()
    blk = BinArgmaxBlock("hi", n=32768)
    blk.build_cell_programs()
    # reference on one full 32768-word frame: single maximum at position 32767
    frame = [0x0000] * 32767 + [0x0001]
    assert blk.process_reference_q15(frame) == [32767]
    # all-equal 32768-frame -> first occurrence -> 0
    assert blk.process_reference_q15([7] * 32768) == [0]


# --- MANDATORY mutation tests (INV-4): each corruption must FAIL the gate -----

def _mutant_last_occurrence(stim, n):
    """MUTANT: >= in place of > — the running max updates on EQUAL values too,
    so the LAST occurrence of the maximum keeps the index."""
    signed = [_signed(w) for w in stim]
    out = []
    for j in range(len(signed) // n):
        fr = signed[j * n:(j + 1) * n]
        m = max(fr)
        out.append(max(i for i, v in enumerate(fr) if v == m))
    return out


def test_mutation_tie_convention_flip_fails():
    """A >=-update mutant (last-occurrence ties) must FAIL on tie frames —
    proof the gate SEES the tie convention, not just the maximum's value."""
    n = 4
    stim = ([_q15(0.1), _q15(0.7), _q15(0.2), _q15(0.7)]
            + [_q15(0.5)] * n)
    dut = _run_dut(stim, n)
    got = _emitted(dut, n)
    assert got == _golden_argmax(stim, n)          # the true gate passes
    assert got != _mutant_last_occurrence(stim, n), \
        "gate failed to detect a flipped (>=) tie convention!"


def _mutant_no_reset(stim, n):
    """MUTANT: the running max is NOT re-armed between frames (carries across);
    the argmax register still reloads to 0 per frame — the concrete bug of a
    missing `maxv = -32768` re-arm."""
    signed = [_signed(w) for w in stim]
    out = []
    maxv = -32768
    for j in range(len(signed) // n):
        arg = 0
        for i, v in enumerate(signed[j * n:(j + 1) * n]):
            if v > maxv:
                maxv, arg = v, i
        out.append(arg)
    return out


def test_mutation_no_frame_reset_fails():
    """The no-re-arm mutant must FAIL: frame 1 holds the global maximum, so the
    mutant never updates in frame 2 and emits 0 where the true argmax is 1."""
    n = 4
    stim = ([_q15(0.1), _q15(0.2), _q15(0.3), _q15(0.9)]
            + [_q15(0.2), _q15(0.5), _q15(0.1), _q15(0.4)])
    dut = _run_dut(stim, n)
    got = _emitted(dut, n)
    assert got == _golden_argmax(stim, n)          # the true gate passes
    mut = _mutant_no_reset(stim, n)
    assert mut == [3, 0]                           # the mutant IS blind here
    assert got != mut, "gate failed to detect a missing inter-frame max reset!"


def test_mutation_index_off_by_one_fails():
    """A one-based (index+1) mutant must FAIL — the emitted word is pinned
    ZERO-based. Stimulus places every frame maximum away from a wrap-coincident
    position."""
    n = 4
    stim = ([_q15(0.1), _q15(0.9), _q15(0.2), _q15(0.3)]
            + [_q15(0.8), _q15(0.1), _q15(0.2), _q15(0.3)])
    dut = _run_dut(stim, n)
    got = _emitted(dut, n)
    assert got == _golden_argmax(stim, n)          # the true gate passes
    mutant = [(i + 1) for i in _golden_argmax(stim, n)]
    assert got != mutant, "gate failed to detect a one-based index!"


def test_mutation_counter_one_early_fails():
    """A frame counter that closes ONE SAMPLE EARLY (frames of n-1) must FAIL:
    it emits at the wrong phase AND yields different indices on a random
    stimulus. Phase proof: the mutant requires a word at input index n-2; the
    DUT emits none there."""
    n = 8
    stim = _random(13, 8 * n)
    dut = _run_dut(stim, n)
    got = _emitted(dut, n)
    assert got == _golden_argmax(stim, n)          # the true gate passes
    mutant = _golden_argmax(stim, n - 1)
    assert got != mutant[:len(got)], \
        "gate failed to detect a counter that closes one early!"
    assert dut.outputs_q15[n - 2] is None


def _mutant_argmin(stim, n):
    """MUTANT: inverted comparison — argMIN (first occurrence)."""
    signed = np.asarray([int(w) & 0xFFFF for w in stim],
                       dtype=np.uint16).view(np.int16)
    return [int(np.argmin(signed[j * n:(j + 1) * n]))
            for j in range(len(signed) // n)]


def test_mutation_inverted_comparison_fails():
    """An argmin mutant (inverted compare) must FAIL — stimulus frames place
    min and max at different positions."""
    n = 4
    stim = ([_q15(0.9), _q15(-0.9), _q15(0.1), _q15(0.2)]
            + [_q15(-0.5), _q15(0.1), _q15(0.6), _q15(-0.9)])
    dut = _run_dut(stim, n)
    got = _emitted(dut, n)
    assert got == _golden_argmax(stim, n)          # the true gate passes
    assert got != _mutant_argmin(stim, n), \
        "gate failed to detect an inverted (argmin) comparison!"


def test_mutation_wrong_n_onchip_fails():
    """A REAL on-chip mutant: a DUT built with n=4 compared against the n=8
    golden must FAIL."""
    stim = _random(17, 32)
    dut = _run_dut(stim, 4)
    res = compare_against_grc(dut.outputs_q15[3::4], _golden_floats(stim, 8),
                              metric=Metric.EXACT, delay=0)
    assert not res.passed, "gate failed to detect a wrong frame length!"


def test_mutation_one_frame_offset_fails():
    stim = _random(19, 48)
    n = 4
    dut = _run_dut(stim, n)
    shifted = [0] + list(dut.outputs_q15[n - 1::n])[:-1]
    res = compare_against_grc(shifted, _golden_floats(stim, n),
                              metric=Metric.EXACT, delay=0)
    # frame maxima land at varying positions, so a 1-frame shift is visible
    assert not res.passed, "gate failed to detect a 1-frame latency error!"


def test_empty_output_fails():
    res = compare_against_grc([], _golden_floats(_random(7, 32), 4),
                              metric=Metric.EXACT, delay=0)
    assert not res.passed


# --- dashboard report ---------------------------------------------------------

def test_emit_report():
    n = 16
    stim = _random(1, 8 * n)
    dut = _run_dut(stim, n)
    res = compare_against_grc(dut.outputs_q15[n - 1::n], _golden_floats(stim, n),
                              metric=Metric.EXACT, delay=0)
    assert res.passed, res.summary()
    assert _emitted(dut, n) == _golden_argmax(stim, n)
    write_report("BinArgmaxBlock", res, coverage={
        "edge": True, "random": 3, "param_sweep": 7, "bit_exact": True,
        "tie_first_occurrence": True, "signed_extremes": True,
        "frame_reset": True, "mutation": True})
