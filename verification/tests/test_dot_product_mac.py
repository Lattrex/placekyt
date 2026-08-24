# SPDX-License-Identifier: GPL-3.0-or-later
"""Verify DotProductMACBlock — fixed-coefficient K-vector dot product (correlator
pattern), bit-exact against its numpy integer golden.

There is NO stock GNU Radio counterpart (the numpy-golden pattern, like
Crc16Block): the golden is ``process_reference_q15``, a bit-exact integer model
of the pinned scale schedule + MACQ accumulation, and a float reference pins the
accuracy against the ideal ``y = bias + sum c[i]*x[i]``.

The block consumes a FRESH K-element vector per output (K consecutive samples =
one vector -> one output; NO delay line, NO sample aging — the correlator
pattern, not the FIR pattern). Scale schedule (pinned):

    S = max(0, ceil(log2(sum|c| + |b|)));  store round(v * 2^-S * 32768)
    POST-ROUNDING GUARD: if sum|q| > 32767 after rounding, bump S, requantize.

Coverage: schedule cases S=0..3 incl. sum|c| exactly 1 and the rounding-trip
guard edge; K sweep 2..7 on-chip bit-exact (raw); both output modes (restored
saturation proven on an overdriven case, rails 0x7FFF/0x8000); float-accuracy
within the derived tolerance; rate correctness (floor(n/K), trailing partial
dropped); no-intermediate-wrap invariant over random sets; orientation samples;
saturated == per-sample; the mandatory INV-4 mutations (guard REMOVED on a
crafted wrapping set, wrong S, swapped coefficients, bias not preloaded,
+1 shift, inverted, empty) proven to FAIL; out-of-range params RAISE.

Run::

    KYTTAR_GR_PYTHON=/usr/bin/python3 QT_QPA_PLATFORM=offscreen \\
        .venv/bin/python -m pytest verification/tests/test_dot_product_mac.py -q
"""
from __future__ import annotations

import math
import os
import random
import sys
from pathlib import Path

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

_PLACEKYT = Path(__file__).resolve().parents[2] / "placekyt"
_VERIFY = Path(__file__).resolve().parents[1]
_RUNTIME = Path(__file__).resolve().parents[2] / "runtime" / "python"
for p in (str(_RUNTIME), str(_PLACEKYT), str(_VERIFY)):
    if p not in sys.path:
        sys.path.insert(0, p)

from kyttar_verify import run_block_dut, write_report, CompareResult, Metric  # noqa: E402
from gr_kyttar.placement.blocks.dot_product_mac_block import (  # noqa: E402
    DotProductMACBlock, scale_schedule, _clip_q15, _s16)

CHIP_YAML = str(_PLACEKYT / "resources" / "chips" / "kyttar_10x12.yaml")
_CHIP_OK = os.path.exists(CHIP_YAML)
pytestmark = pytest.mark.skipif(not _CHIP_OK, reason="chip yaml absent")


def _fq(v: float) -> int:
    q = int(round(v * 32768.0))
    return max(-32768, min(32767, q)) & 0xFFFF


def _rand_words(seed: int, n: int, lim: float = 0.95) -> list[int]:
    rng = random.Random(seed)
    return [_fq(rng.uniform(-lim, lim)) for _ in range(n)]


def _run_dut(words, params):
    dut = run_block_dut("DotProductMACBlock", words, params=params,
                        in_port="sample", chip_yaml=CHIP_YAML)
    assert dut.ok, dut.reason
    return dut


def _emitted(dut):
    """The on-chip output stream: the non-None words (an output lands on the
    sample completing each K-vector; accumulating samples read None)."""
    return [int(w) & 0xFFFF for w in dut.outputs_q15 if w is not None]


# Asymmetric coefficient sets by S (sum|c| + |b| in (2^(S-1), 2^S]); asymmetry
# so a swapped/reordered-coefficient mutant cannot coincide (INV-12).
_SETS = {
    0: ([0.3, -0.2, 0.2, 0.15], 0.05),          # tot 0.90 -> S=0
    1: ([0.5, 0.4, -0.3], 0.1),                 # tot 1.3  -> S=1
    2: ([0.9, -0.7, 0.8, 0.6], 0.2),            # tot 3.2  -> S=2
    3: ([2.0, -1.5, 1.6, 1.2, 0.9], -0.5),      # tot 7.7  -> S=3
}


# --- the scale schedule (pure reference) --------------------------------------

@pytest.mark.parametrize("S_want", [0, 1, 2, 3])
def test_scale_schedule_S_cases(S_want):
    """S = max(0, ceil(log2(sum|c| + |b|))) across S=0..3; the stored words are
    round(v * 2^-S * 32768) and the guarded magnitude sum is <= 32767."""
    coeffs, bias = _SETS[S_want]
    S, cq, bq = scale_schedule(coeffs, bias)
    assert S == S_want, f"S={S} expected {S_want} for {coeffs}+{bias}"
    for c, q in zip(coeffs, cq):
        assert q == _clip_q15(round(c * (2.0 ** -S) * 32768.0))
    assert bq == _clip_q15(round(bias * (2.0 ** -S) * 32768.0))
    assert sum(abs(q) for q in cq) + abs(bq) <= 32767


def test_scale_schedule_sum_exactly_one_trips_guard():
    """sum|c| == 1.0 exactly: ceil(log2 1) = 0, but each 0.25 quantizes to 8192
    and sum|q| = 32768 > 32767 — the POST-ROUNDING GUARD must bump S to 1."""
    S, cq, bq = scale_schedule([0.25, 0.25, 0.25, 0.25], 0.0)
    assert S == 1, f"guard did not bump S (got {S})"
    assert cq == [4096, 4096, 4096, 4096] and bq == 0
    assert sum(abs(q) for q in cq) <= 32767


def test_scale_schedule_rounding_trips_guard():
    """The rounding-trip edge: float sum < 1 (so the log2 formula says S=0) but
    every coefficient rounds UP and sum|q| crosses 32767 — the guard must bump.
    This is the load-bearing case the guard-removal mutation exercises."""
    c = 8191.51 / 32768.0                       # rounds up to 8192
    coeffs = [c, c, c, c, 0.9 / 32768.0]        # unguarded sum|q| = 32769
    tot = sum(abs(v) for v in coeffs)
    assert tot < 1.0 and math.ceil(math.log2(tot)) == 0   # formula alone: S=0
    S, cq, bq = scale_schedule(coeffs, 0.0)
    assert S == 1, f"post-rounding guard did not bump S (got {S})"
    assert sum(abs(q) for q in cq) <= 32767


def test_no_intermediate_wrap_over_random_sets():
    """With the guard, the partial sum is inside int16 at EVERY step — the
    no-wrap invariant, asserted over random coefficient sets/vectors including
    full-scale +/-1.0 inputs."""
    rng = random.Random(20260823)
    for _ in range(200):
        K = rng.randint(2, 7)
        coeffs = [rng.uniform(-2.0, 2.0) for _ in range(K)]
        bias = rng.uniform(-1.0, 1.0)
        S, cq, bq = scale_schedule(coeffs, bias)
        xs = [rng.choice([-32768, 32767, _s16(_fq(rng.uniform(-1, 1)))])
              for _ in range(K)]
        acc = bq
        for q, x in zip(cq, xs):
            acc += (q * x) >> 15
            assert -32768 <= acc <= 32767, (
                f"partial sum {acc} left int16 (coeffs={coeffs}, bias={bias})")


# --- on-chip bit-exact (raw mode) ---------------------------------------------

@pytest.mark.parametrize("k", [2, 3, 4, 5, 6, 7])
def test_k_sweep_bit_exact_raw(k):
    """K sweep 2..7 (raw): random asymmetric coefficients, random input with a
    trailing partial vector — on-chip output bit-exact vs the integer golden,
    and exactly floor(n/K) outputs on the K-th samples."""
    rng = random.Random(500 + k)
    coeffs = [rng.uniform(-0.9, 0.9) for _ in range(k)]
    bias = rng.uniform(-0.3, 0.3)
    params = {"coefficients": coeffs, "bias": bias, "k": k}
    blk = DotProductMACBlock("r", **params)
    xs = _rand_words(600 + k, 3 * k + (k - 1))      # 3 vectors + a partial tail
    dut = _run_dut(xs, params)
    ref = blk.process_reference_q15(xs)
    got = _emitted(dut)
    assert got == ref, f"k={k}: dut={got} ref={ref} (S={blk.scale_shift})"
    assert len(got) == 3, f"k={k}: expected floor(n/K)=3 outputs, got {len(got)}"
    emit_pos = [i for i, w in enumerate(dut.outputs_q15) if w is not None]
    assert emit_pos == [k - 1, 2 * k - 1, 3 * k - 1], (
        f"k={k}: outputs must land on the K-th samples, got {emit_pos}")


@pytest.mark.parametrize("S", [0, 1, 2, 3])
def test_S_sweep_bit_exact_raw(S):
    """sum|c| near and above 1 (S=0,1,2,3): the raw on-chip word equals the
    integer golden bit-for-bit; the derived S matches the schedule."""
    coeffs, bias = _SETS[S]
    params = {"coefficients": coeffs, "bias": bias, "k": len(coeffs)}
    blk = DotProductMACBlock("r", **params)
    assert blk.scale_shift == S
    xs = _rand_words(700 + S, 4 * len(coeffs))
    dut = _run_dut(xs, params)
    assert _emitted(dut) == blk.process_reference_q15(xs)


@pytest.mark.parametrize("rseed", [1, 7, 42, 1234])
def test_random_seeds_bit_exact_raw(rseed):
    """Random stimulus (>= 3 seeds) incl. full-scale edge words, K=4, S=2."""
    coeffs, bias = _SETS[2]
    params = {"coefficients": coeffs, "bias": bias, "k": 4}
    blk = DotProductMACBlock("r", **params)
    xs = [0x7FFF, 0x8000, 0x8001, 0x0000] + _rand_words(rseed, 16, lim=1.0)
    dut = _run_dut(xs, params)
    assert _emitted(dut) == blk.process_reference_q15(xs)


def test_guard_edge_set_bit_exact_on_chip():
    """The rounding-trip guard set runs bit-exact on-chip at the BUMPED S (the
    guarded schedule is what is actually stored)."""
    c = 8191.51 / 32768.0
    coeffs = [c, c, c, c, 0.9 / 32768.0]
    params = {"coefficients": coeffs, "bias": 0.0, "k": 5}
    blk = DotProductMACBlock("r", **params)
    assert blk.scale_shift == 1
    xs = [0x8000] * 5 + [0x7FFF] * 5 + _rand_words(9, 10, lim=1.0)
    dut = _run_dut(xs, params)
    assert _emitted(dut) == blk.process_reference_q15(xs)


# --- restored mode ------------------------------------------------------------

@pytest.mark.parametrize("k", [2, 3, 4, 5, 6, 7])
def test_k_sweep_bit_exact_restored(k):
    """K sweep 2..7 in RESTORED mode (S>0 -> the 2-cell mac->restore chain):
    on-chip output bit-exact vs the golden's clamp(acc << S)."""
    rng = random.Random(800 + k)
    raw = [rng.uniform(-0.9, 0.9) for _ in range(k)]
    scale = 1.6 / sum(abs(v) for v in raw)      # sum|c| = 1.6 -> S >= 1
    coeffs = [v * scale for v in raw]
    bias = rng.uniform(-0.4, 0.4)
    params = {"coefficients": coeffs, "bias": bias, "k": k, "mode": "restored"}
    blk = DotProductMACBlock("r", **params)
    assert blk.scale_shift >= 1 and blk.cell_count == 2
    xs = _rand_words(900 + k, 3 * k)
    dut = _run_dut(xs, params)
    got = _emitted(dut)
    ref = blk.process_reference_q15(xs)
    assert got == ref, f"k={k}: dut={got} ref={ref} (S={blk.scale_shift})"


def test_restored_saturation_pins_rails():
    """Overdriven restored case: |y| > 1 must pin EXACTLY at 0x7FFF / 0x8000
    (the saturating left shift), while an in-range vector passes through."""
    coeffs, bias = _SETS[2]                       # S=2
    params = {"coefficients": coeffs, "bias": bias, "k": 4, "mode": "restored"}
    blk = DotProductMACBlock("r", **params)
    xs = [_fq(v) for v in (0.9, -0.9, 0.9, 0.9,     # y ~ +2.9 -> rail +
                           -0.9, 0.9, -0.9, -0.9,   # y ~ -2.5 -> rail -
                           0.1, -0.05, 0.02, 0.1)]  # in range
    dut = _run_dut(xs, params)
    got = _emitted(dut)
    assert got == blk.process_reference_q15(xs)
    assert got[0] == 0x7FFF, f"positive overdrive not pinned: {got}"
    assert got[1] == 0x8000, f"negative overdrive not pinned: {got}"
    assert got[2] not in (0x7FFF, 0x8000), f"in-range vector clipped: {got}"


def test_restored_S0_equals_raw_single_cell():
    """restored with S == 0 is the identity restore: single cell, output
    identical to raw mode word-for-word."""
    coeffs, bias = _SETS[0]
    xs = _rand_words(31, 12)
    outs = {}
    for mode in ("raw", "restored"):
        params = {"coefficients": coeffs, "bias": bias, "k": 4, "mode": mode}
        blk = DotProductMACBlock("r", **params)
        assert blk.scale_shift == 0 and blk.cell_count == 1
        outs[mode] = _emitted(_run_dut(xs, params))
    assert outs["raw"] == outs["restored"]


# --- scale-shift metadata (the downstream-consumer contract) ------------------

def test_scale_shift_exposed_and_raw_word_scaled_by_it():
    """The derived S is exposed (scale_shift + quantized words), and the raw
    word really is y / 2^S: restoring dut_raw << S (saturating) reproduces the
    restored-mode on-chip output — the exact contract a downstream consumer
    relies on when folding S into its own shift immediates."""
    coeffs, bias = _SETS[2]
    base = {"coefficients": coeffs, "bias": bias, "k": 4}
    blk = DotProductMACBlock("r", **base)
    assert blk.scale_shift == 2
    assert len(blk.quantized_coefficients) == 4
    assert isinstance(blk.quantized_bias, int)
    xs = _rand_words(77, 16, lim=1.0)
    raw = [_s16(w) for w in _emitted(_run_dut(xs, base))]
    restored = _emitted(_run_dut(xs, {**base, "mode": "restored"}))
    folded = [_clip_q15(v << blk.scale_shift) & 0xFFFF for v in raw]
    assert folded == restored, (
        f"raw<<S != restored: raw={raw} folded={folded} restored={restored}")


# --- float accuracy (derived tolerance) ---------------------------------------

@pytest.mark.parametrize("S", [0, 1, 2])
def test_float_accuracy_within_derived_tolerance(S):
    """DUT vs the float reference within the DERIVED tolerance (not tuned):
    (K+1) stored words rounding <= 0.5 LSB each (|x| <= 1) + K truncating
    products <= 1 LSB each => raw-word error <= 1.5K + 0.5 LSB of the scaled
    domain. Restored mode multiplies by 2^S (+0.5 clamp)."""
    coeffs, bias = _SETS[S]
    K = len(coeffs)
    params = {"coefficients": coeffs, "bias": bias, "k": K}
    blk = DotProductMACBlock("r", **params)
    xs = _rand_words(40 + S, 6 * K)
    dut_words = [_s16(w) for w in _emitted(_run_dut(xs, params))]
    xf = [_s16(w) / 32768.0 for w in xs]
    ref = blk.process_reference(xf)             # raw-mode float ref: y / 2^S
    tol_lsb = 1.5 * K + 0.5
    for got, want in zip(dut_words, ref):
        err = abs(got - want * 32768.0)
        assert err <= tol_lsb, (
            f"S={S}: raw word err {err:.2f} LSB > derived {tol_lsb}")


# --- rate contract ------------------------------------------------------------

def test_trailing_partial_vector_not_emitted():
    """floor(n/K) outputs; a trailing partial vector emits nothing (and stays
    pending in the accumulator, exactly like the golden's per-call contract)."""
    coeffs, bias = _SETS[0]
    params = {"coefficients": coeffs, "bias": bias, "k": 4}
    blk = DotProductMACBlock("r", **params)
    xs = _rand_words(55, 4 * 3 + 3)             # 3 vectors + 3-sample partial
    dut = _run_dut(xs, params)
    got = _emitted(dut)
    assert len(got) == 3 and got == blk.process_reference_q15(xs)


def test_fresh_vector_no_sample_aging():
    """The correlator contract (NOT an FIR): each sample contributes to exactly
    ONE output. Feeding [v1, v2] must give [dot(v1), dot(v2)] — the second
    output must NOT see any v1 sample (no delay line). Distinguishable from an
    FIR by construction: an FIR's 2nd..K-th outputs would mix v1 into v2."""
    coeffs, bias = _SETS[1]
    K = len(coeffs)
    params = {"coefficients": coeffs, "bias": bias, "k": K}
    blk = DotProductMACBlock("r", **params)
    v1 = _rand_words(61, K)
    v2 = _rand_words(62, K)
    both = _emitted(_run_dut(v1 + v2, params))
    solo1 = blk.process_reference_q15(v1)
    solo2 = blk.process_reference_q15(v2)       # fresh accumulator == on-chip re-arm
    assert both == solo1 + solo2, (
        f"vector independence violated: {both} != {solo1}+{solo2}")


# --- saturated (pipelined) == per-sample --------------------------------------

@pytest.mark.parametrize("mode", ["raw", "restored"])
def test_saturated_equals_per_sample(mode):
    """Back-to-back saturated drive equals the per-sample stream for both
    modes (feed-forward, no feedback / no reconvergent fan-in; the 2-cell
    restored chain is a straight linear handoff). The block is also in the
    global test_pipeline_saturation RATE_1IN registry."""
    from kyttar_verify.dut_runner import (  # noqa: PLC0415
        run_block_dut_rate, run_block_dut_pipelined)
    coeffs, bias = _SETS[2]
    params = {"coefficients": coeffs, "bias": bias, "k": 4, "mode": mode}
    xs = _rand_words(88, 20, lim=1.0)
    seq = run_block_dut_rate("DotProductMACBlock", xs, params=params,
                             chip_yaml=CHIP_YAML, in_port="sample",
                             out_port="out")
    assert seq.ok, seq.reason
    pipe = run_block_dut_pipelined("DotProductMACBlock", [(w,) for w in xs],
                                   params=params, chip_yaml=CHIP_YAML,
                                   in_ports=("sample",), out_port="out")
    assert pipe.ok, pipe.reason
    n = len(seq.outputs_q15)
    assert n == 5 and list(pipe.outputs_q15[:n]) == list(seq.outputs_q15)


# --- orientation samples (full 8-orientation gate lives in
#     test_orientation_invariance.py; these are the block-local spot checks) ---

@pytest.mark.parametrize("mode", ["raw", "restored"])
@pytest.mark.parametrize("orient", [["cw"], ["cw", "cw"], ["mirror_h"]])
def test_orientation_samples(mode, orient):
    coeffs, bias = _SETS[2]
    params = {"coefficients": coeffs, "bias": bias, "k": 4, "mode": mode}
    xs = _rand_words(99, 12)
    base = _run_dut(xs, params)
    rot = run_block_dut("DotProductMACBlock", xs, params=params,
                        in_port="sample", chip_yaml=CHIP_YAML, orient=orient)
    assert rot.ok, rot.reason
    assert rot.outputs_q15 == base.outputs_q15, f"orient {orient} diverges"


# --- MANDATORY mutation gates (INV-4) -----------------------------------------

def _mutant_reference(coeff_q, bias_q, k, xs, S, mode="raw", preload_bias=True,
                      wrap=True):
    """A corrupted-schedule reference: accumulate the GIVEN quantized words
    (whatever mutation produced them) with 16-bit wrap semantics."""
    out = []
    acc = bias_q if preload_bias else 0
    i = 0
    for w in xs:
        x = _s16(int(w) & 0xFFFF)
        acc = acc + ((coeff_q[i] * x) >> 15)
        if wrap:
            acc = _s16(acc)
        i += 1
        if i == k:
            v = acc if (mode == "raw" or S == 0) else _clip_q15(acc << S)
            out.append(v & 0xFFFF)
            acc = bias_q if preload_bias else 0
            i = 0
    return out


def test_mutation_guard_removed_fails():
    """REMOVE the post-rounding guard (the crafted rounding-trip set quantized
    at the UNGUARDED S=0): sum|q| = 32769, and a full-scale input vector wraps
    the 16-bit accumulator — the mutant's output is sign-flipped garbage that
    MUST disagree with the guarded DUT/golden. Proves the guard is load-bearing."""
    c = 8191.51 / 32768.0
    coeffs = [c, c, c, c, 0.9 / 32768.0]
    params = {"coefficients": coeffs, "bias": 0.0, "k": 5}
    blk = DotProductMACBlock("r", **params)
    assert blk.scale_shift == 1                       # the guard bumped S
    # The unguarded schedule: S from the log2 formula alone, no requantize bump.
    S_bad = max(0, math.ceil(math.log2(sum(abs(v) for v in coeffs))))
    assert S_bad == 0
    cq_bad = [_clip_q15(round(v * 32768.0)) for v in coeffs]
    assert sum(abs(q) for q in cq_bad) > 32767        # the guard's whole point
    xs = [0x8000] * 5                                 # full-scale -1.0 vector
    # The mutant wraps: the exact partial sum leaves int16 on this vector.
    exact = 0
    wrapped = False
    for q in cq_bad:
        exact += (q * -32768) >> 15
        wrapped |= not (-32768 <= exact <= 32767)
    assert wrapped, "crafted set failed to wrap the unguarded accumulator"
    mut = _mutant_reference(cq_bad, 0, 5, xs, S_bad)
    dut = _emitted(_run_dut(xs, params))
    assert dut == blk.process_reference_q15(xs)       # guarded DUT == golden
    # Compare on the SAME scale (mutant is at S=0, DUT word at S=1).
    mut_val = _s16(mut[0])
    dut_val = _s16(dut[0]) << blk.scale_shift
    assert mut_val != dut_val, "guard-removed mutant went undetected!"
    assert mut_val > 0 > dut_val, (
        f"expected the wrap to sign-flip the mutant (mut={mut_val}, "
        f"true={dut_val})")


def test_mutation_wrong_S_fails():
    """Coefficients quantized at the WRONG S (S+1) must disagree with the DUT."""
    coeffs, bias = _SETS[1]
    params = {"coefficients": coeffs, "bias": bias, "k": 3}
    blk = DotProductMACBlock("r", **params)
    S_bad = blk.scale_shift + 1
    cq_bad = [_clip_q15(round(v * (2.0 ** -S_bad) * 32768.0)) for v in coeffs]
    bq_bad = _clip_q15(round(bias * (2.0 ** -S_bad) * 32768.0))
    xs = _rand_words(3, 12)
    mut = _mutant_reference(cq_bad, bq_bad, 3, xs, S_bad)
    dut = _emitted(_run_dut(xs, params))
    assert mut != dut, "a wrong-S mutant went undetected by the gate!"


def test_mutation_swapped_coefficients_fails():
    """REVERSED coefficient order (asymmetric set) must disagree — proves every
    coefficient position, including the deepest, is actually exercised (INV-12)."""
    coeffs, bias = _SETS[3]
    params = {"coefficients": coeffs, "bias": bias, "k": 5}
    blk = DotProductMACBlock("r", **params)
    xs = _rand_words(5, 20)
    mut = _mutant_reference(list(reversed(blk.quantized_coefficients)),
                            blk.quantized_bias, 5, xs, blk.scale_shift)
    dut = _emitted(_run_dut(xs, params))
    assert dut == blk.process_reference_q15(xs)
    assert mut != dut, "a swapped-coefficient mutant went undetected!"


def test_mutation_bias_not_preloaded_fails():
    """An accumulator that does NOT preload the scaled bias must disagree with
    the DUT whenever bias != 0."""
    coeffs, bias = _SETS[2]
    params = {"coefficients": coeffs, "bias": bias, "k": 4}
    blk = DotProductMACBlock("r", **params)
    xs = _rand_words(13, 16)
    mut = _mutant_reference(blk.quantized_coefficients, blk.quantized_bias,
                            4, xs, blk.scale_shift, preload_bias=False)
    dut = _emitted(_run_dut(xs, params))
    assert mut != dut, "a bias-not-preloaded mutant went undetected!"


def test_mutation_one_output_shift_fails():
    """A +1-output shift of the stream must FAIL (no free lag alignment)."""
    coeffs, bias = _SETS[0]
    params = {"coefficients": coeffs, "bias": bias, "k": 4}
    blk = DotProductMACBlock("r", **params)
    xs = _rand_words(17, 20)
    dut = _emitted(_run_dut(xs, params))
    shifted = [0] + dut[:-1]
    assert shifted != blk.process_reference_q15(xs), \
        "a one-output shift went undetected!"


def test_mutation_inverted_output_fails():
    coeffs, bias = _SETS[0]
    params = {"coefficients": coeffs, "bias": bias, "k": 4}
    blk = DotProductMACBlock("r", **params)
    xs = _rand_words(19, 16)
    dut = _emitted(_run_dut(xs, params))
    inverted = [(-_s16(w)) & 0xFFFF for w in dut]
    assert inverted != blk.process_reference_q15(xs), \
        "an inverted-output mutant went undetected!"


def test_mutation_empty_output_fails():
    coeffs, bias = _SETS[0]
    ref = DotProductMACBlock("r", coefficients=coeffs, bias=bias,
                             k=4).process_reference_q15(_rand_words(23, 12))
    assert len(ref) > 0
    assert [] != ref, "an empty output went undetected!"


# --- range guards (documented limits RAISE, never clamp) ----------------------

@pytest.mark.parametrize("bad_k", [0, 1, 8, 16])
def test_out_of_range_k_raises(bad_k):
    with pytest.raises(ValueError):
        DotProductMACBlock("x", coefficients=[0.1] * max(bad_k, 1), k=bad_k)


def test_k_len_mismatch_raises():
    with pytest.raises(ValueError):
        DotProductMACBlock("x", coefficients=[0.1, 0.2, 0.3], k=4)


def test_bad_mode_raises():
    with pytest.raises(ValueError):
        DotProductMACBlock("x", coefficients=[0.1, 0.2], k=2, mode="scaled")


def test_shift_over_15_raises():
    """sum|c| + |b| > 2^15 exceeds the 4-bit shift immediate — RAISES."""
    with pytest.raises(ValueError):
        DotProductMACBlock("x", coefficients=[20000.0, 20000.0], k=2)


# --- report -------------------------------------------------------------------

def test_emit_report():
    """Emit the dashboard report reflecting a passing bit-exact verification."""
    coeffs, bias = _SETS[2]
    params = {"coefficients": coeffs, "bias": bias, "k": 4}
    blk = DotProductMACBlock("r", **params)
    xs = _rand_words(1, 240, lim=1.0)
    dut = _emitted(_run_dut(xs, params))
    ref = blk.process_reference_q15(xs)
    n = len(ref)
    errs = sum(1 for a, b in zip(dut, ref) if a != b) + abs(len(dut) - n)
    res = CompareResult(passed=(errs == 0 and n > 0), metric=Metric.DECISION,
                        n_compared=n, bit_errors=errs, delay_used=0)
    assert res.passed, res.summary()
    write_report("DotProductMACBlock", res, coverage={
        "gr_equiv": "(none — numpy integer golden; correlator/weighted-sum "
                    "primitive, FIR-family param names)",
        "edge": "full-scale 0x7FFF/0x8000/0x8001 words; sum|c| exactly 1.0; "
                "the post-rounding-guard rounding-trip set; overdriven rails",
        "random": 4,
        "k_sweep": "2..7 bit-exact on-chip, BOTH modes (raw single-cell / "
                   "restored 2-cell mac->restore)",
        "s_sweep": "S=0,1,2,3 (sum|c|+|b| 0.9 .. 7.7) bit-exact",
        "modes": "raw (emit y/2^S, S exposed as scale_shift) / restored "
                 "(saturating <<S; rails pinned 0x7FFF/0x8000 on overdrive; "
                 "S=0 restored == raw, single cell)",
        "rate": "K:1 rate-reducing; floor(n/K) outputs on the K-th samples; "
                "trailing partial dropped; fresh-vector (no sample aging)",
        "saturation": "pipelined == per-sample, both modes (+ RATE_1IN in the "
                      "global gate)",
        "orientation": "cw / cw+cw / mirror_h spot checks both modes (full 8 "
                       "in test_orientation_invariance.py)",
        "mutation": "guard REMOVED (crafted wrap, sign-flip) / wrong S / "
                    "swapped coefficients / bias not preloaded / +1 shift / "
                    "inverted / empty",
        "float_accuracy": "<= 1.5K+0.5 LSB of the scaled domain (derived)",
        "hw_range": "k in 2..7 (one-cell coeff+bias+code discipline); "
                    "S <= 15 (4-bit shift immediate); len==k; all RAISE",
        "note": "scale schedule pinned: S=max(0,ceil(log2(sum|c|+|b|))), "
                "store round(v*2^-S*32768), post-rounding guard bumps S; "
                "acc preloads scaled bias; intermediate wrap impossible",
    })
