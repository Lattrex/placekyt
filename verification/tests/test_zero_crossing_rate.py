# SPDX-License-Identifier: GPL-3.0-or-later
"""Verify ZeroCrossingRateBlock — windowed zero-crossing rate of a real Q15 stream.

There is NO stock GNU Radio streaming counterpart (like Crc16Block), so the golden
is the independent numpy reference below, written from the block's PINNED contract:

  * sign of a Q15 word = bit 15; an EXACT ZERO is NON-NEGATIVE (the tie convention);
  * a crossing = consecutive samples whose sign bits differ;
  * the stream is preceded by ONE implicit zero sample (prev state init 0);
  * per non-overlapping window of N samples: count crossings over the N pairs
    ending at the window's samples — INCLUDING the inter-window boundary pair
    (state carries the last sample across windows) — and emit ONE Q15 word
    count << (15 - log2 N)  ==  count/N exactly, saturated to 0x7FFF at count==N
    (rate 1.0 is not Q15-representable).

Integer counts + an exact shift -> the gate is BIT-EXACT (Metric.EXACT, delay 0 on
the emitted stream). ``run_block_dut`` records None on non-emitting triggers, so
the emitted stream is ``outputs[N-1::N]``. Per INV-4 every gate is paired with
mutations proven to FAIL: wrong tie convention, missing boundary carry, off-by-one
window count, wrong window_size on-chip, +1 delay, inverted, empty.

Run:
    KYTTAR_GR_PYTHON=/usr/bin/python3 QT_QPA_PLATFORM=offscreen \
      <venv>/python -m pytest verification/tests/test_zero_crossing_rate.py -x -q
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
for p in (str(_PLACEKYT), str(_VERIFY), str(_RUNTIME)):
    if p not in sys.path:
        sys.path.insert(0, p)

from kyttar_verify import (  # noqa: E402
    run_block_dut, compare_against_grc, write_report, Metric)
from gr_kyttar.placement.blocks.zero_crossing_rate_block import (  # noqa: E402
    ZeroCrossingRateBlock)

CHIP_YAML = str(_PLACEKYT / "resources" / "chips" / "kyttar_10x12.yaml")
pytestmark = pytest.mark.skipif(
    not os.path.exists(CHIP_YAML), reason="chip yaml absent")


# --- the independent numpy golden ---------------------------------------------

def _sign_bit(w: int) -> int:
    """Sign of a Q15 word = bit 15. Exact zero -> 0 (non-negative) — the PINNED
    tie convention."""
    return (int(w) >> 15) & 1


def _golden_zcr(stim_q15, n: int) -> list[int]:
    """One uint16 Q15 word per COMPLETE window of n samples: crossing count over
    the n consecutive sign-bit pairs ending at the window's samples (implicit
    zero predecessor before the stream; state carries across windows), scaled
    count/n exactly (count << (15 - log2 n)), saturated to 0x7FFF at count==n."""
    shift = 15 - (int(n).bit_length() - 1)
    prev = 0                      # the implicit zero predecessor (non-negative)
    count = 0
    out = []
    for i, w in enumerate(stim_q15):
        w = int(w) & 0xFFFF
        if _sign_bit(w) != _sign_bit(prev):
            count += 1
        prev = w
        if (i + 1) % n == 0:
            out.append(0x7FFF if count == n else count << shift)
            count = 0
    return out


def _golden_floats(stim_q15, n: int) -> list[float]:
    """The golden as floats for compare_against_grc (round(v*32768) reproduces
    the exact word)."""
    return [(w - 0x10000 if w >= 0x8000 else w) / 32768.0
            for w in _golden_zcr(stim_q15, n)]


def _q15(f: float) -> int:
    return max(-32768, min(32767, int(round(f * 32768.0)))) & 0xFFFF


def _random(seed, count):
    rng = random.Random(seed)
    return [rng.randint(0, 0xFFFF) for _ in range(count)]


def _run_dut(stim, n, **kw):
    dut = run_block_dut("ZeroCrossingRateBlock", stim,
                        params={"window_size": n}, chip_yaml=CHIP_YAML,
                        in_port="sample", out_port="out", **kw)
    assert dut.ok, dut.reason
    return dut


def _emitted(dut, n):
    """The rate stream: one word on input indices n-1, 2n-1, ..."""
    return dut.outputs_q15[n - 1::n]


# --- emit-phase (rate) contract -----------------------------------------------

@pytest.mark.parametrize("n", [2, 4, 8])
def test_emit_phase(n):
    """Rate-REDUCING contract: a word egresses IFF the window completes (input
    index i with i % n == n-1); every other trigger produces nothing."""
    stim = _random(11, 4 * n)
    dut = _run_dut(stim, n)
    for i, w in enumerate(dut.outputs_q15):
        assert (w is not None) == (i % n == n - 1), \
            f"n={n} sample {i}: emitted={w is not None}, expected {i % n == n - 1}"


# --- bit-exact vs the numpy golden (random, >=3 seeds, window sweep) ----------

@pytest.mark.parametrize("n", [4, 16])
@pytest.mark.parametrize("seed", [1, 7, 42])
def test_bitexact_vs_golden(n, seed):
    stim = _random(seed, 6 * n)
    dut = _run_dut(stim, n)
    res = compare_against_grc(_emitted(dut, n), _golden_floats(stim, n),
                              metric=Metric.EXACT, delay=0)
    print(f"\nn={n} seed={seed}:", res.summary())
    assert res.passed, res.summary()


@pytest.mark.parametrize("n,windows", [(64, 4), (256, 2)])
def test_bitexact_large_windows(n, windows):
    """The dispatched window sizes 64 (the default) and 256, bit-exact."""
    stim = _random(5, windows * n)
    dut = _run_dut(stim, n)
    got = _emitted(dut, n)
    ref = _golden_zcr(stim, n)
    assert [w & 0xFFFF for w in got] == ref, f"n={n}: DUT != golden"


def test_block_reference_matches_independent_golden():
    """The block's own process_reference_q15 == this file's independent golden
    (two implementations of the pinned contract must agree)."""
    stim = _random(3, 96)
    for n in (2, 4, 8, 16, 32):
        blk = ZeroCrossingRateBlock("ref", window_size=n)
        assert blk.process_reference_q15(stim) == _golden_zcr(stim, n), f"n={n}"
        # float reference agrees after exact Q15 quantization
        floats = [(w - 0x10000 if w >= 0x8000 else w) / 32768.0 for w in stim]
        fq = [_q15(v) for v in blk.process_reference(floats)]
        assert fq == _golden_zcr(stim, n), f"n={n}: float reference disagrees"


# --- edge cases ---------------------------------------------------------------

def test_constant_positive_input_zero_rate():
    """A constant positive input never crosses: every window emits exactly 0."""
    n = 8
    stim = [_q15(0.5)] * (4 * n)
    dut = _run_dut(stim, n)
    assert [w & 0xFFFF for w in _emitted(dut, n)] == [0, 0, 0, 0]


def test_all_zero_input_zero_rate():
    """All-zero input: zero is NON-negative, sign never changes -> rate 0."""
    n = 8
    stim = [0x0000] * (2 * n)
    dut = _run_dut(stim, n)
    assert [w & 0xFFFF for w in _emitted(dut, n)] == [0, 0]


def test_constant_negative_input_seed_crossing_only():
    """A constant NEGATIVE input crosses exactly ONCE — against the implicit
    zero predecessor (non-negative) before the first sample. First window
    emits 1/N, every later window 0. Pins the implicit-zero-seed convention."""
    n = 8
    stim = [_q15(-0.5)] * (4 * n)
    dut = _run_dut(stim, n)
    assert [w & 0xFFFF for w in _emitted(dut, n)] == [1 << (15 - 3), 0, 0, 0]


def test_alternating_input_saturates_at_q15_rail():
    """A fully alternating input (starting negative) crosses on EVERY pair
    including the implicit-zero seed pair: count == N == rate 1.0, which is not
    Q15-representable -> the output pins at 0x7FFF (= 1 - 2^-15)."""
    n = 8
    stim = [_q15(-0.7) if i % 2 == 0 else _q15(0.7) for i in range(3 * n)]
    dut = _run_dut(stim, n)
    assert [w & 0xFFFF for w in _emitted(dut, n)] == [0x7FFF] * 3


def test_alternating_input_starting_positive():
    """Alternating starting POSITIVE: the seed pair (0, +) does not cross, so the
    first window counts N-1; the window-boundary pair then crosses, so every
    later window counts N -> 0x7FFF."""
    n = 8
    stim = [_q15(0.7) if i % 2 == 0 else _q15(-0.7) for i in range(3 * n)]
    dut = _run_dut(stim, n)
    assert [w & 0xFFFF for w in _emitted(dut, n)] == \
        [(n - 1) << (15 - 3), 0x7FFF, 0x7FFF]


def test_tie_convention_exact_zero_is_non_negative():
    """PINS the tie convention on-chip. Window [+, 0, 0, -]: (+ -> 0) is NOT a
    crossing (zero is non-negative, same sign as +), (0 -> 0) is not, (0 -> -)
    IS. With the seed pair (0 -> +) not crossing: count = 1 -> 1/4. A zero-is-
    negative convention would give 3/4 on the same window."""
    n = 4
    stim = [_q15(0.5), 0x0000, 0x0000, _q15(-0.5)]
    dut = _run_dut(stim, n)
    assert [w & 0xFFFF for w in _emitted(dut, n)] == [1 << 13]  # 1/4
    # and the golden agrees (the DUT gate above is against a hand-computed word)
    assert _golden_zcr(stim, n) == [1 << 13]


def test_window_boundary_carry():
    """The inter-window boundary pair is counted: window 1 ends POSITIVE, window
    2 is all NEGATIVE — window 2's ONLY crossing is the boundary pair itself,
    so it must emit exactly 1/N (state carries the last sample across windows).
    """
    n = 4
    stim = ([_q15(0.3)] * n) + ([_q15(-0.3)] * n)
    dut = _run_dut(stim, n)
    assert [w & 0xFFFF for w in _emitted(dut, n)] == [0, 1 << 13]


# --- parameter validation (the declared supported range raises loudly) --------

@pytest.mark.parametrize("bad", [0, 1, 3, 6, 100, 65536, -4])
def test_invalid_window_size_raises(bad):
    with pytest.raises(ValueError):
        ZeroCrossingRateBlock("bad", window_size=bad)


# --- MANDATORY mutation tests (INV-4): each corruption must FAIL the gate -----

def test_mutation_wrong_tie_convention_fails():
    """A golden that treats exact zero as NEGATIVE (sign(w)=1 for w<=0) must
    FAIL against the DUT on a stimulus containing exact zeros — proves the gate
    SEES the tie choice."""
    n = 4
    stim = [_q15(0.5), 0x0000, 0x0000, _q15(-0.5)]
    prev_s, count, mutant = 1, 0, []   # implicit zero seed is NEGATIVE here
    for i, w in enumerate(stim):
        v = w - 0x10000 if w >= 0x8000 else w
        s = 1 if v <= 0 else 0         # MUTANT: zero counted as negative
        if s != prev_s:
            count += 1
        prev_s = s
        if (i + 1) % n == 0:
            mutant.append(0x7FFF if count == n else count << (15 - 2))
            count = 0
    dut = _run_dut(stim, n)
    got = [w & 0xFFFF for w in _emitted(dut, n)]
    assert got == _golden_zcr(stim, n)  # the true gate passes on this stimulus
    assert got != mutant, "gate failed to detect a flipped tie convention!"


def _mutant_no_carry(stim, n, *, reset_to_zero):
    """Golden WITHOUT the inter-window carry. Two broken flavors:
    reset_to_zero=True  -> prev resets to 0 at each window start;
    reset_to_zero=False -> the boundary pair is SKIPPED (only the n-1 pairs
    internal to the window are counted)."""
    out = []
    for w0 in range(0, len(stim), n):
        win = [int(w) & 0xFFFF for w in stim[w0:w0 + n]]
        if reset_to_zero:
            prev, pairs = 0, win
        else:
            prev, pairs = win[0], win[1:]
        count = 0
        for w in pairs:
            if _sign_bit(w) != _sign_bit(prev):
                count += 1
            prev = w
        out.append(0x7FFF if count == n else count << (15 - (n.bit_length() - 1)))
    return out


def test_mutation_missing_boundary_carry_fails():
    """Both no-carry mutants (prev reset to 0 per window; boundary pair skipped)
    must FAIL, each on a stimulus that exposes it — proof the gate SEES the
    carried state, not merely a coincidentally-equal value."""
    n = 4
    p, m = _q15(0.3), _q15(-0.3)

    # (A) reset-to-zero mutant: window 1 ends NEGATIVE, window 2 all negative.
    # True (carry): window 2 has NO crossing. Mutant: phantom (0 -> -) crossing.
    stim_a = [p, p, m, m] + [m] * n
    dut = _run_dut(stim_a, n)
    got = [w & 0xFFFF for w in _emitted(dut, n)]
    assert got == _golden_zcr(stim_a, n)        # the true gate passes
    assert got != _mutant_no_carry(stim_a, n, reset_to_zero=True), \
        "gate failed to detect the reset-to-zero no-carry mutant!"

    # (B) skip-boundary-pair mutant: window 1 ends +, window 2 all negative.
    # True (carry): window 2's ONLY crossing is the boundary pair. Mutant: 0.
    stim_b = [p] * n + [m] * n
    dut = _run_dut(stim_b, n)
    got = [w & 0xFFFF for w in _emitted(dut, n)]
    assert got == _golden_zcr(stim_b, n)        # the true gate passes
    assert got != _mutant_no_carry(stim_b, n, reset_to_zero=False), \
        "gate failed to detect the skipped-boundary-pair mutant!"


def test_mutation_off_by_one_window_fails():
    """An off-by-one window count — a mutant that closes each window ONE SAMPLE
    EARLY (n-1 samples, same intended scaling) — must FAIL: it emits at the
    wrong phase AND yields different words on a random stimulus."""
    n = 8
    shift = 15 - 3
    stim = _random(13, 8 * n)
    # mutant golden: window closes after n-1 samples (countdown off by one)
    prev, count, mutant = 0, 0, []
    for i, w in enumerate(stim):
        w = int(w) & 0xFFFF
        if _sign_bit(w) != _sign_bit(prev):
            count += 1
        prev = w
        if (i + 1) % (n - 1) == 0:
            mutant.append(min(count << shift, 0x7FFF))
            count = 0
    dut = _run_dut(stim, n)
    got = [w & 0xFFFF for w in _emitted(dut, n)]
    assert got == _golden_zcr(stim, n)   # the true gate passes
    assert got != mutant[:len(got)], "gate failed to detect an off-by-one window!"
    # phase proof: the mutant requires a word at input index n-2; the DUT emits
    # none there (its first window closes at n-1).
    assert dut.outputs_q15[n - 2] is None


def test_mutation_wrong_window_size_onchip_fails():
    """A REAL on-chip mutant: a DUT built with window_size=4 compared against the
    window_size=8 golden must FAIL."""
    stim = _random(17, 32)
    dut = _run_dut(stim, 4)
    res = compare_against_grc(_emitted(dut, 4), _golden_floats(stim, 8),
                              metric=Metric.EXACT, delay=0)
    assert not res.passed, "gate failed to detect a wrong window_size!"


def test_mutation_one_sample_offset_fails():
    stim = _random(19, 48)
    n = 4
    dut = _run_dut(stim, n)
    shifted = [0x0000] + list(_emitted(dut, n))[:-1]
    res = compare_against_grc(shifted, _golden_floats(stim, n),
                              metric=Metric.EXACT, delay=0)
    assert not res.passed, "gate failed to detect a 1-window latency error!"


def test_mutation_inverted_output_fails():
    stim = _random(23, 48)
    n = 4
    dut = _run_dut(stim, n)
    inverted = [(-(w - 0x10000 if w >= 0x8000 else w)) & 0xFFFF
                for w in _emitted(dut, n)]
    res = compare_against_grc(inverted, _golden_floats(stim, n),
                              metric=Metric.EXACT, delay=0)
    # all-zero windows survive an inversion; the random stimulus guarantees
    # non-zero rates so the inversion is visible
    assert not res.passed, "gate failed to detect an inverted output!"


def test_empty_output_fails():
    res = compare_against_grc([], _golden_floats(_random(7, 32), 4),
                              metric=Metric.EXACT, delay=0)
    assert not res.passed


# --- dashboard report ---------------------------------------------------------

def test_emit_report():
    n = 16
    stim = _random(1, 8 * n)
    dut = _run_dut(stim, n)
    res = compare_against_grc(_emitted(dut, n), _golden_floats(stim, n),
                              metric=Metric.EXACT, delay=0)
    assert res.passed, res.summary()
    write_report("ZeroCrossingRateBlock", res, coverage={
        "edge": True, "random": 3, "param_sweep": 5, "bit_exact": True,
        "tie_convention": True, "boundary_carry": True, "saturation_rail": True,
        "mutation": True})
