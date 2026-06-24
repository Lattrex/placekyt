# SPDX-License-Identifier: GPL-3.0-or-later
"""Verify FIRFilterBlock against GNU Radio's filter.fir_filter_fff.

FIR is the first block with a real coefficient set and the first that *scales*
with a parameter (the tap list sizes the filter). It exercises parts of the
harness that GainBlock did not: a params-dependent entry address (INV-6), a
derived tolerance that grows with tap count (each Q15 MAC contributes up to ~1
LSB), GNU Radio's reversed-tap convention, and — past the single-cell ceiling — a
MULTI-CELL wavefront whose output egresses from the block's LAST cell (INV-7/10).

SATURATION — COEFFICIENT HEADROOM (the correctness fix this suite gates)
-----------------------------------------------------------------------
GNU Radio's ``fir_filter_fff`` is FLOATING POINT and never overflows. The Kyttar
FIR runs Q15 fixed-point with a 16-bit accumulator (no guard bits). The cell ALU
WRAPS on signed overflow — which flips the sign on overload and produces garbage.
Production fixed-point FIRs (TI C5x/C6x, …) SATURATE (clamp to ±full-scale)
instead. The naive fixes all FAIL on a high-gain filter because the ALU's V flag
is NOT sticky: a sum can overflow a mid-chain MACQ and WRAP BACK into range by the
final op, so a final-only (or per-cell) clamp never sees the overflow and the
output ROLLS OVER (sign flip) — e.g. a 40-tap all-0.5 filter (gain 20) on a steady
0.9 input rolled to a sign-flipping ±0.x mess instead of pinning at +1.0. A
per-TAP clamp is correct but costs ~3 instructions/tap, collapsing TAPS_PER_CELL
to 1 (a 40-tap FIR → 40 cells) — unacceptable.

The correct AND dense fix is COEFFICIENT HEADROOM (accumulator scaling): pre-scale
the coefficients by ``2^-S`` (``S = max(0, ceil(log2 Σ|h|))``) so the running sum
can NEVER overflow internally (Σ|scaled h| ≤ 1 ⇒ in range at every tap/cell —
intermediate wrap is impossible), then restore the gain at the very END with ONE
SATURATING left shift by S (the only place a true overdrive overflows, where it
pins to ±full-scale). For a NORMALIZED filter (Σ|h| ≤ 1) S=0 — a no-op, identical
to a plain Q15 FIR and bit-exact with GR. The golden predictor for the DUT is
``FIRFilterBlock.process_reference_q15``: accumulate the scaled coeffs (wrapping,
but it never leaves range), then the saturating shift. NOT the float ideal.

Two reference tiers (per the verification plan / KNOWLEDGE_BASE):
  * **DSP equivalence:** a NORMALIZED filter (S=0) has no headroom shift, so the
    reference EQUALS GNU Radio's float output clipped to the Q15 range. The
    in-range tests assert DUT ≈ GR within the derived tolerance — proving the
    block is a real drop-in for the GR block, not just self-consistent.
  * **Bit-exact substrate:** the DUT must match the headroom Q15 reference EXACTLY
    (it models the hardware datapath: scaled wrapping accumulation, the final
    saturating shift, and the multi-cell accumulation order). The overload +
    scaling tests gate on this.

VERIFIED RANGE (this suite): 2 … 64 taps (the headline target). Single-cell for
≤MAX_SINGLE_CELL_TAPS=6 taps (or ≤4 when the headroom restore is present, S>0), a
chained partial-sum systolic wavefront at TAPS_PER_CELL=5 above (so a NORMALIZED
20-tap FIR is 4 cells). A high-gain (S>0) FIR caps its LAST cell to 3 taps to fit
the restore, so it may use one extra cell (e.g. a 40-tap all-0.5 FIR is 9 cells vs
8 normalized). Probing shows the same design routes to ~200 taps; well above that
the serpentine footprint leaves NO free routing corridor on the 10x12 array (the
genuine substrate wall — guarded below).

STIMULUS NOTE (INV-4/12): a multi-cell FIR is only exercised if the input is
LONGER than the filter — otherwise the deep cells never see a non-zero sample and
a deep-cell bug hides. The multi-cell sweeps drive ≥2·ntaps RANDOM samples with an
ASYMMETRIC tap set, and a deep-tap mutation proves the gate sees the deepest cell.

Run:
    KYTTAR_GR_PYTHON=/usr/bin/python3 QT_QPA_PLATFORM=offscreen \
      <venv>/python -m pytest verification/tests/test_fir_filter.py -x -q
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
    run_block_dut, run_gnuradio_ref, compare_against_grc, write_report, Metric)
from gr_kyttar.placement.blocks.fir_filter_block import FIRFilterBlock  # noqa: E402

CHIP_YAML = str(_PLACEKYT / "resources" / "chips" / "kyttar_10x12.yaml")
_GR_AVAILABLE = os.path.exists(os.environ.get("KYTTAR_GR_PYTHON", "/usr/bin/python3"))
pytestmark = pytest.mark.skipif(
    not (os.path.exists(CHIP_YAML) and _GR_AVAILABLE),
    reason="chip yaml or GNU Radio interpreter absent")

# Largest single-cell FIR for a NORMALIZED (S=0) filter (the rest fold to the
# multi-cell wavefront). A high-gain (S>0) single cell additionally carries the
# saturating-shift restore, dropping its ceiling to 4.
MAX_SINGLE_CELL_TAPS = FIRFilterBlock.MAX_SINGLE_CELL_TAPS  # 6
# Taps per cell in the multi-cell wavefront — the plain wrapping FIR's density.
# Intermediate cells forward an in-range scaled partial (no clamp/shift); only the
# LAST cell carries the headroom restore (and so caps to 3 taps when S>0).
TAPS_PER_CELL = FIRFilterBlock.TAPS_PER_CELL               # 5
# Headline scaling target — verified end to end.
MAX_VERIFIED_TAPS = 64
# A FIR this large overflows the 10x12 array's ROUTING capacity: TAPS_PER_CELL=5
# makes a 320-tap FIR 64 cells, whose serpentine footprint leaves no free
# corridor for the I/O ports. The genuine substrate wall — guarded below. (200
# taps / 40 cells still routes; the wall is placement-noisy in the 41..63-cell
# band, so the guard uses a tap count safely past it.)
ROUTING_WALL_TAPS = 320

EDGE = [0x0000, 0x4000, 0x2000, 0xC000, 0x7FFF, 0x8001, 0x6000, 0xA000,
        0x1000, 0x3000]


# --- helpers ------------------------------------------------------------------

def _s16(v):
    v = int(v) & 0xFFFF
    return v - 0x10000 if v >= 0x8000 else v


def _gr_fir(inputs_q15, taps):
    # GNU Radio's fir_filter_fff convolves with taps in latest-sample-first
    # order, the reverse of the Kyttar coefficient order — so reverse them here.
    return run_gnuradio_ref(
        input_q15=inputs_q15,
        gnuradio_script="""
from gnuradio import gr, filter as gr_filter, blocks
tb = gr.top_block()
src = blocks.vector_source_f(input_float, False)
fir = gr_filter.fir_filter_fff(1, taps)
sink = blocks.vector_sink_f()
tb.connect(src, fir); tb.connect(fir, sink)
tb.run()
output_float = list(sink.data())
""",
        extra_args={"taps": list(reversed(taps))})


def _sat_ref_floats(taps, inputs):
    """The block's bit-exact COEFFICIENT-HEADROOM Q15 reference, returned as floats
    so it feeds straight into ``compare_against_grc`` (which re-quantizes to Q15).
    Models scaled wrapping accumulation + the final saturating shift."""
    blk = FIRFilterBlock("ref", taps)
    return [_s16(w) / 32768.0 for w in blk.process_reference_q15(inputs)]


def _random_input(seed, n):
    rng = random.Random(seed)
    return [rng.randint(0, 0xFFFF) for _ in range(n)]


def _norm_taps(n, seed):
    """A realistic NORMALIZED random tap set — Σ|h| ≈ 0.95 (< 1) so the filter has
    no coefficient headroom (S=0): the Q15 output and the chained partial sums stay
    in range, the saturating shift is a no-op, and the block is bit-exact with a
    plain Q15 FIR / GNU Radio. This is the normal FIR case and gives deterministic
    cell counts (Σ < 1 never trips the ceil(log2 Σ) headroom). High-gain (S>0)
    behaviour is covered by the dedicated overload/headroom tests."""
    rng = random.Random(seed)
    t = [rng.uniform(0.0, 1.0) for _ in range(n)]
    s = sum(t)
    return [round(v / s * 0.95, 5) for v in t]


def _head_shift(taps):
    """The COEFFICIENT-HEADROOM shift S = max(0, ceil(log2 Σ|h|)) for a tap set."""
    import math
    s = sum(abs(c) for c in taps)
    return 0 if s <= 1.0 else int(math.ceil(math.log2(s)))


def _verify(taps, inputs):
    """DSP-equivalence check (in-range): DUT vs GNU Radio float within the derived
    Q15 tolerance. Valid only when no intermediate accumulation overflows (then
    saturating == float-clipped); used for the small/normalized in-range cases."""
    dut = run_block_dut("FIRFilterBlock", inputs,
                        params={"coefficients": taps}, chip_yaml=CHIP_YAML)
    assert dut.ok, f"build/run failed: {dut.reason}"
    ref = _gr_fir(inputs, taps)
    # FIR (decimation 1) emits one output per input aligned with GNU Radio's
    # output[n] — delay=0. Tolerance derived from the tap count: <=1 LSB per MAC.
    return dut, compare_against_grc(
        dut.outputs_q15, ref.floats, metric=Metric.AMPLITUDE,
        delay=0, op_count=len(taps))


def _verify_saturating(taps, inputs):
    """Bit-exact substrate check: DUT vs the COEFFICIENT-HEADROOM Q15 reference,
    EXACT. This is the predictor that models the hardware (scaled wrapping
    accumulation, the final saturating shift, and the multi-cell accumulation
    order), so it is exact in range AND when the final shift saturates."""
    dut = run_block_dut("FIRFilterBlock", inputs,
                        params={"coefficients": taps}, chip_yaml=CHIP_YAML)
    assert dut.ok, f"build/run failed: {dut.reason}"
    ref = _sat_ref_floats(taps, inputs)
    return dut, compare_against_grc(
        dut.outputs_q15, ref, metric=Metric.EXACT, delay=0)


# --- single-cell range (edge + random + sweep) --------------------------------
# All in-range (|Σ coeff·x| stays representable), so DUT must match GNU Radio.
TAP_SETS = [
    [0.5, 0.5],                        # 2-tap
    [0.2, 0.2, 0.2],                   # 3-tap averager (top of single cell)
    [0.3, -0.2, 0.5],                  # 3-tap ASYMMETRIC (catches tap-order bugs)
]


@pytest.mark.parametrize("taps", TAP_SETS, ids=lambda t: f"{len(t)}tap")
def test_fir_edge_vectors(taps):
    dut, res = _verify(taps, EDGE)
    print(f"\n{len(taps)}-tap edge:", res.summary(), "| hop", dut.hop_count)
    assert res.passed, res.summary()


@pytest.mark.parametrize("seed", [1, 7, 42])
def test_fir_random(seed):
    dut, res = _verify([0.2, 0.2, 0.2], _random_input(seed, 20))
    print(f"\nrandom seed={seed}:", res.summary())
    assert res.passed, res.summary()


def test_fir_single_cell_sweep():
    """The 1-cell range is correct and the derived tolerance grows with the tap
    count (more MACs, more rounding)."""
    for n in range(2, MAX_SINGLE_CELL_TAPS + 1):
        taps = [round(1.0 / n, 4)] * n
        dut, res = _verify(taps, EDGE)
        assert res.passed, f"{n}-tap: {res.summary()}"
        assert res.tolerance == n + 1, \
            f"{n}-tap tolerance should scale to {n + 1}, got {res.tolerance}"


# --- multi-cell scaling (the headline: 7 .. 64 taps) --------------------------
# Representative sizes spanning 2..13 cells (TAPS_PER_CELL=5: 7→2, 64→13 cells).
MULTICELL_SIZES = [7, 8, 11, 16, 20, 32, MAX_VERIFIED_TAPS]


@pytest.mark.parametrize("n", MULTICELL_SIZES, ids=lambda n: f"{n}tap")
def test_fir_multicell_scaling(n):
    """A multi-cell wavefront FIR matches the SATURATING Q15 reference EXACTLY.

    Driven with > 2*ntaps RANDOM full-range samples and a REALISTIC (asymmetric)
    tap set, so EVERY cell's delay segment is exercised with real data — a bug in
    any cell (not just the first) would show. Full-range random input + a
    chained partial-sum DOES overflow intermediate accumulators, so this gates on
    the saturating reference (the true hardware predictor), not the float ideal.
    (A short/uniform/positive stimulus hides such bugs; that is exactly how the
    prior 'passing' suite missed the multi-cell coefficient-ordering bug — INV-4.)
    """
    taps = _norm_taps(n, seed=100 + n)
    inputs = _random_input(seed=200 + n, n=2 * n + 16)
    dut, res = _verify_saturating(taps, inputs)
    print(f"\n{n}-tap multicell:", res.summary(), "| entry", dut.entry_addr)
    assert res.passed, res.summary()


def test_fir_20tap_is_4_cells_and_routes():
    """Budget guard: a NORMALIZED (S=0) 20-tap FIR is a COMPACT 4-cell wavefront
    (TAPS_PER_CELL=5) — NOT the ~10 cells the discarded per-tap-clamp scheme
    produced — and it places, routes, builds, and runs bit-exact against the
    headroom reference. COEFFICIENT HEADROOM adds NOTHING to a normalized filter
    (S=0), so this density is the plain wrapping FIR's. If a regression re-inflates
    the per-cell tap cost this flips."""
    taps = _norm_taps(20, seed=120)
    assert _head_shift(taps) == 0, "normalized taps should have no headroom (S=0)"
    assert FIRFilterBlock("c", taps).cell_count == 4, (
        "a normalized 20-tap FIR should fold to 4 cells (TAPS_PER_CELL=5); the "
        "budget regressed — did per-tap clamping creep back?")
    inputs = _random_input(seed=220, n=2 * 20 + 16)
    dut, res = _verify_saturating(taps, inputs)
    assert dut.ok, f"20-tap FIR did not build/route: {dut.reason}"
    assert res.passed, res.summary()


def test_fir_saturating_ref_matches_gnuradio_when_in_range():
    """Proves the headroom Q15 reference is REAL DSP (a GR drop-in), not merely
    self-consistent with the DUT: with NORMALIZED taps (Σ|h| < 1, so S=0, no
    headroom shift) and modest input, the reference must equal GNU Radio's float
    output clipped to Q15 — within the derived per-tap LSB floor. (If they
    disagreed here, the reference would be wrong DSP.)"""
    for n in (4, 8, 16):
        taps = _norm_taps(n, seed=300 + n)
        assert _head_shift(taps) == 0, "in-range GR match requires S=0 (no headroom)"
        # half-scale input keeps the running sum well inside range
        inputs = [v // 2 for v in _random_input(seed=400 + n, n=2 * n + 16)]
        ref_sat = _sat_ref_floats(taps, inputs)
        gr = _gr_fir(inputs, taps)
        sat_q15 = [int(round(v * 32768.0)) for v in ref_sat]
        res = compare_against_grc(
            [w & 0xFFFF for w in sat_q15], gr.floats,
            metric=Metric.AMPLITUDE, delay=0, op_count=n)
        assert res.passed, f"{n}-tap: headroom ref disagrees with GR: {res.summary()}"


# --- the OVERLOAD test: the block must SATURATE, not wrap ----------------------

# Each stimulus is a HIGH-GAIN filter (Σ|h| > 1 ⇒ headroom S > 0) driven near full
# scale, so the steady-state output is far over unity and the final saturating
# shift fires — the DUT visibly pins at ±full-scale. COEFFICIENT HEADROOM is what
# makes this WORK without rolling over: the scaled accumulator stays in range, and
# only the final shift overflows (and saturates). The 7-tap (S=2) is 2 cells; the
# 13-tap (S=2) is 3 cells (the high-gain last cell caps to 3 taps for the restore).
@pytest.mark.parametrize("taps,inp,cells", [
    ([0.9, 0.9],
     [0x7FFF, 0x7FFF, 0x7FFF, 0x7FFF, 0x8001, 0x8001, 0x7FFF, 0x7FFF], 1),
    ([0.5] * 7, [0x7FFF if i % 2 == 0 else 0x7000 for i in range(20)], 2),
    ([0.3] * 13, [0x7FFF] * 30, 3),
], ids=["single", "multi7", "multi13"])
def test_fir_overload_saturates(taps, inp, cells):
    """Drive a HIGH-GAIN filter PAST full scale so the final saturating shift
    fires. A correct production FIR SATURATES — the DUT must pin to ±full-scale,
    match the headroom reference EXACTLY, and its outputs must actually be at the
    rails (proof it saturated rather than happening to land in range). A WRAPPING
    accumulator (no headroom) would instead ROLL OVER / flip sign (see
    test_fir_overload_wrap_mutation_fails). Also asserts S > 0 (the headroom is
    genuinely engaged here)."""
    assert _head_shift(taps) > 0, "overload stimulus must be a high-gain (S>0) filter"
    dut, res = _verify_saturating(taps, inp)
    assert res.passed, f"DUT does not match headroom reference: {res.summary()}"
    assert FIRFilterBlock("c", taps).cell_count == cells, "footprint changed"
    sat = [_s16(w) for w in dut.outputs_q15]
    n_pinned = sum(1 for v in sat if v in (32767, -32768))
    assert n_pinned >= len(sat) // 2, (
        f"overload stimulus did not pin the DUT at the rails (got {sat}); the "
        "test no longer exercises saturation")


def test_fir_40tap_gain20_pins_no_rollover():
    """THE headline real-path test: a 40-tap, all-0.5 (gain-20, S=5) FIR — the exact
    case that ROLLED OVER before COEFFICIENT HEADROOM — driven with a STEADY 0.9
    input must PIN at +full-scale with NO sign-discontinuous jump.

    This is the full build → auto-route → simKYT path (run_block_dut, the same
    place_block + auto_route_all + BuildEngine.build + inject/run/read flow the
    GUI/SimServer use). Steady-state output of a gain-20 filter on 0.9 is +18 →
    saturates; a WRAPPING accumulator gave a sign-flipping ±0.x mess (e.g.
    [...0.9, -0.875...]). After headroom the DUT must (a) match the headroom Q15
    reference EXACTLY, (b) pin every steady-state sample at +0x7FFF, and (c) show
    NO sign flip between consecutive steady-state samples (the rollover signature).
    """
    taps = [0.5] * 40
    assert _head_shift(taps) == 5, "40x0.5 should need S=5 of headroom"
    blk = FIRFilterBlock("c", taps)
    # High-gain last cell caps at 3 taps, so this folds to 9 cells (vs 8 normalized).
    assert blk.cell_count == 9, f"40x0.5 (S=5) should be 9 cells, got {blk.cell_count}"
    inp = [int(round(0.9 * 32767)) & 0xFFFF] * 30
    dut, res = _verify_saturating(taps, inp)
    assert dut.ok, f"40-tap gain-20 FIR did not build/route: {dut.reason}"
    assert res.passed, f"DUT does not match headroom reference: {res.summary()}"
    sat = [_s16(w) for w in dut.outputs_q15]
    steady = sat[len(taps):]          # past the ramp-up transient
    assert all(v == 32767 for v in steady), (
        f"overdriven gain-20 FIR did not pin at +full-scale (steady tail={steady}); "
        "it is still ROLLING OVER")
    # No sign-discontinuous jump anywhere in steady state (the rollover signature).
    assert not any(steady[i] * steady[i + 1] < 0 for i in range(len(steady) - 1)), (
        "steady-state output sign-flips between samples — the filter is rolling "
        "over, not saturating")


# --- mandatory negative tests (prove the gate FAILS on a corrupted DUT) --------

def test_fir_overload_wrap_mutation_fails():
    """MUTATION (the heart of this fix, INV-4): a DUT WITHOUT coefficient headroom —
    the OLD, buggy behavior that accumulates the UNSCALED coeffs and WRAPS on
    overflow with no saturation — must FAIL the gate. We synthesize that
    fully-wrapping output for a high-gain overload case and compare it to the
    headroom reference. Where the headroom DUT pins at a rail, the wrapping output
    ROLLS OVER (flips sign / folds to a wrong small value), so the gate must REJECT
    it; if it passed, it would certify the overflow rollover bug.

    The stimulus is a high-gain 2-tap filter (Σ|h|=1.8 ⇒ S=1) driven at full scale,
    so the true sum is far over unity and wrap vs saturate genuinely diverge."""
    taps = [0.9, 0.9]
    inp = [0x7FFF, 0x7FFF, 0x7FFF, 0x7FFF, 0x8001, 0x8001, 0x7FFF, 0x7FFF]
    # The OLD no-headroom datapath: UNSCALED Q15 coeffs, every step WRAPS (incl. the
    # final), NO saturating shift. (q15 of 0.9 = 0x7333.)
    from gr_kyttar.placement.blocks._base import float_to_q15
    c = [float_to_q15(t) for t in taps]
    N = len(c)
    delay = [0] * N
    wrapped = []
    for s in inp:
        delay = delay[1:] + [_s16(int(s) & 0xFFFF)]     # newest at the end (d{N-1})
        acc = (_s16(delay[0]) * _s16(c[0])) >> 15        # priming MULQ
        for i in range(1, N):
            acc = _s16((acc + ((_s16(delay[i]) * _s16(c[i])) >> 15)) & 0xFFFF)  # WRAP
        wrapped.append(acc & 0xFFFF)
    ref = _sat_ref_floats(taps, inp)
    # Sanity: the headroom reference MUST actually pin at a rail here (else the
    # mutation is vacuous — wrap would equal a non-saturated reference).
    assert any(_s16(int(round(v * 32768.0))) in (32767, -32768) for v in ref), \
        "overload reference does not saturate — mutation stimulus is vacuous"
    res = compare_against_grc(wrapped, ref, metric=Metric.EXACT, delay=0)
    assert not res.passed, (
        "gate accepted a WRAPPING (no-headroom) FIR against the headroom reference "
        "— it would certify the overflow rollover bug!")


def test_fir_mutation_inverted_fails():
    dut, _ = _verify([0.2, 0.2, 0.2], EDGE)
    ref = _gr_fir(EDGE, [0.2, 0.2, 0.2])
    broken = [(0x10000 - (w or 0)) & 0xFFFF for w in dut.outputs_q15]
    res = compare_against_grc(broken, ref.floats, metric=Metric.AMPLITUDE,
                              delay=0, op_count=3)
    assert not res.passed, "gate failed to catch an inverted FIR output!"


def test_fir_mutation_wrong_taps_fails():
    """A FIR built with different taps must fail against the right reference."""
    dut = run_block_dut("FIRFilterBlock", EDGE,
                        params={"coefficients": [0.2, 0.2, 0.2]},
                        chip_yaml=CHIP_YAML)
    assert dut.ok, dut.reason
    ref_wrong = _gr_fir(EDGE, [0.9, 0.05, 0.05])   # different filter
    res = compare_against_grc(dut.outputs_q15, ref_wrong.floats,
                              metric=Metric.AMPLITUDE, delay=0, op_count=3)
    assert not res.passed, "gate failed to catch a wrong-taps mismatch!"


def test_fir_mutation_delay_offset_fails():
    dut, _ = _verify([0.2, 0.2, 0.2], EDGE)
    ref = _gr_fir(EDGE, [0.2, 0.2, 0.2])
    shifted = [0x0000] + list(dut.outputs_q15[:-1])
    res = compare_against_grc(shifted, ref.floats, metric=Metric.AMPLITUDE,
                              delay=0, op_count=3)
    assert not res.passed, "gate failed to catch a 1-sample FIR latency error!"


def test_fir_mutation_deep_cell_fails():
    """Prove the gate actually verifies the DEEPEST cell of a multi-cell FIR — the
    LAST cell of the wavefront chain, not just the first. Build a 32-tap (7-cell,
    TAPS_PER_CELL=5) DUT, drive it with long input, then compare against a
    saturating reference whose ONE perturbed tap lives in the LAST cell. (The
    wavefront assigns segments from the END of the tap array, so the last cell
    owns the FIRST coefficient indices — here taps 0..1; perturb tap 0.) If the
    gate passed, the deep cell's output would not depend on that tap → the
    multi-cell datapath would be unverified there. It must FAIL."""
    n = 32
    taps = _norm_taps(n, seed=132)
    inputs = _random_input(seed=232, n=2 * n + 16)
    dut = run_block_dut("FIRFilterBlock", inputs,
                        params={"coefficients": taps}, chip_yaml=CHIP_YAML)
    assert dut.ok, dut.reason
    assert FIRFilterBlock("c", taps).cell_count == 7, "deep-cell footprint changed"
    perturbed = list(taps)
    perturbed[0] += 0.15   # a tap owned by the LAST cell, well above the LSB floor
    ref_wrong = _sat_ref_floats(perturbed, inputs)
    res = compare_against_grc(dut.outputs_q15, ref_wrong, metric=Metric.EXACT,
                              delay=0)
    assert not res.passed, (
        "gate did not catch a perturbed DEEP-cell tap — the last cell of the "
        "multi-cell FIR is not actually being verified!")


# --- known-limit guard: the genuine substrate wall ----------------------------

def test_fir_routing_capacity_limit():
    """The block scales correctly to ~200 taps (40 cells); well above that the
    serpentine footprint leaves NO free routing corridor for the I/O ports on the
    10x12 array. This is a chip ROUTING-CAPACITY wall, not a block bug. Guarded as
    an executable expectation: if the array grows or placement improves so a
    320-tap (64-cell) FIR routes, this flips — extend MAX_VERIFIED_TAPS and this
    guard then. (The wall is placement-noisy in the 41..63-cell band; 64 cells
    fails reliably.)"""
    dut = run_block_dut("FIRFilterBlock", EDGE[:4],
                        params={"coefficients": [round(1.0 / ROUTING_WALL_TAPS, 6)]
                                * ROUTING_WALL_TAPS}, chip_yaml=CHIP_YAML)
    assert not dut.ok and "corridor" in dut.reason.lower(), (
        f"a {ROUTING_WALL_TAPS}-tap FIR now routes (reason={dut.reason!r}); the "
        "routing-capacity wall moved — extend MAX_VERIFIED_TAPS and this guard.")


def test_emit_report():
    """Record the verified result for the dashboard.

    The dashboard's quality column is the REAL Q15 error vs GNU Radio float, with
    the derived tolerance — NOT the bit-exact-vs-own-reference check (which is
    always 0/0 LSB and circular, telling the reader nothing about quantization).
    So the report is the DUT-vs-GR AMPLITUDE comparison in the in-range regime (a
    normalized multi-cell FIR where no intermediate sum overflows, so saturating
    == float-clipped and the only error IS the Q15 quantization noise). The
    bit-exact substrate, overload-saturation, and mutation checks are asserted in
    their own tests; this one publishes the meaningful quality number.
    """
    n = 16  # a multi-cell, normalized in-range case → genuine Q15 error vs GR
    taps = _norm_taps(n, seed=164)
    inputs = [v // 2 for v in _random_input(seed=264, n=2 * n + 16)]
    dut, res = _verify(taps, inputs)
    assert res.passed, res.summary()
    assert res.tolerance > 0, (
        "the dashboard quality must be a REAL vs-GR amplitude error with a "
        "derived (non-zero) tolerance, not the circular 0/0 bit-exact metric")
    write_report("FIRFilterBlock", res, coverage={
        "edge": True, "random": 3,
        "param_sweep": len(MULTICELL_SIZES) + (MAX_SINGLE_CELL_TAPS - 1),
        "mutation": True, "overload": True,
        "max_verified_taps": MAX_VERIFIED_TAPS})
