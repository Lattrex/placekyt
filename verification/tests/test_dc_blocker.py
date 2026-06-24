# SPDX-License-Identifier: GPL-3.0-or-later
"""Verify DCBlockerBlock against GNU Radio's filter.dc_blocker_ff.

GR's DC blocker is an LTI filter — a symmetric FIR. DCBlockerBlock therefore
REUSES the verified FIRFilterBlock datapath with the dc-blocker impulse response
as its coefficients. The parameters mirror GR's GRC ``dc_blocker_xx`` block
VERBATIM: ``length`` (GR's ``D``, the moving-averager delay-line length) and
``long_form`` (long vs short form).

The dc-blocker taps (a delayed unit impulse minus a unit-DC-gain cascade of
moving averagers) have ``Σ|h| ≈ 1.5..2``, so COEFFICIENT HEADROOM (INV-13)
engages with shift ``S=1``: the block scales coeffs by 1/2, accumulates without
overflow, and restores the gain with ONE saturating left shift — i.e. it
SATURATES on overload (no rollover), like every production fixed-point filter.

Two reference tiers (as for the FIR):
  * **DSP equivalence:** DUT vs GNU Radio ``dc_blocker_ff`` (float, clipped to
    Q15) within the HEADROOM-AWARE derived floor ``N·(2^(S-1)+1)+1`` LSB
    (``q15_quant_floor(N, head_shift=S)``). S=1 costs ~1 bit of coefficient
    precision, so the plain ``N+1`` floor is too tight — this widened floor is a
    DERIVED fixed-point worst case, NOT a loosened gate.
  * **Bit-exact substrate:** DUT vs ``FIRFilterBlock.process_reference_q15``
    (EXACT) — the predictor that models the hardware datapath (scaled wrapping
    accumulation + the final saturating shift + the multi-cell order), so it is
    exact in range AND when the shift saturates.

Run:
    KYTTAR_GR_PYTHON=/usr/bin/python3 QT_QPA_PLATFORM=offscreen \
      <venv>/python -m pytest verification/tests/test_dc_blocker.py -x -q
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
for p in (str(_PLACEKYT), str(_VERIFY), str(_RUNTIME)):
    if p not in sys.path:
        sys.path.insert(0, p)

from kyttar_verify import (  # noqa: E402
    run_block_dut, run_gnuradio_ref, compare_against_grc, write_report, Metric)
from kyttar_verify.compare import q15_quant_floor  # noqa: E402
from gr_kyttar.placement.blocks.fir_filter_block import FIRFilterBlock  # noqa: E402
from gr_kyttar.placement.blocks.dc_blocker_block import (  # noqa: E402
    DCBlockerBlock, _dc_blocker_taps)

CHIP_YAML = str(_PLACEKYT / "resources" / "chips" / "kyttar_10x12.yaml")
_GR_AVAILABLE = os.path.exists(os.environ.get("KYTTAR_GR_PYTHON", "/usr/bin/python3"))
pytestmark = pytest.mark.skipif(
    not (os.path.exists(CHIP_YAML) and _GR_AVAILABLE),
    reason="chip yaml or GNU Radio interpreter absent")

EDGE = [0x0000, 0x4000, 0x2000, 0xC000, 0x7FFF, 0x8001, 0x6000, 0xA000,
        0x1000, 0x3000, 0x5000, 0xB000]


# --- helpers ------------------------------------------------------------------

def _s16(v):
    v = int(v) & 0xFFFF
    return v - 0x10000 if v >= 0x8000 else v


def _gr_dc_blocker(inputs_q15, length, long_form):
    """GNU Radio filter.dc_blocker_ff golden reference."""
    return run_gnuradio_ref(
        input_q15=inputs_q15,
        gnuradio_script=f"""
from gnuradio import gr, blocks, filter as gr_filter
tb = gr.top_block()
src = blocks.vector_source_f(input_float, False)
dcb = gr_filter.dc_blocker_ff({int(length)}, {bool(long_form)})
sink = blocks.vector_sink_f()
tb.connect(src, dcb); tb.connect(dcb, sink)
tb.run()
output_float = list(sink.data())
""")


def _random_input(seed, n):
    rng = random.Random(seed)
    return [rng.randint(0, 0xFFFF) for _ in range(n)]


def _block(length, long_form):
    return DCBlockerBlock("ref", length=length, long_form=long_form)


def _verify_vs_gr(length, long_form, inputs):
    """DUT vs GNU Radio dc_blocker_ff within the HEADROOM-AWARE Q15 floor.

    dc_blocker is an LTI FIR: GR's output[n] and the DUT output[n] carry the same
    group delay, so they align at delay=0 (as for fir_filter)."""
    blk = _block(length, long_form)
    n_taps = blk.num_taps
    S = blk._head_shift
    dut = run_block_dut("DCBlockerBlock", inputs,
                        params={"length": length, "long_form": long_form},
                        chip_yaml=CHIP_YAML)
    assert dut.ok, f"build/run failed: {dut.reason}"
    ref = _gr_dc_blocker(inputs, length, long_form)
    res = compare_against_grc(
        dut.outputs_q15, ref.floats, metric=Metric.AMPLITUDE,
        delay=0, op_count=n_taps, head_shift=S)
    return dut, res, n_taps, S


def _verify_bitexact(length, long_form, inputs):
    """DUT vs the COEFFICIENT-HEADROOM Q15 reference, EXACT (models the hardware
    datapath incl. the saturating shift)."""
    blk = _block(length, long_form)
    dut = run_block_dut("DCBlockerBlock", inputs,
                        params={"length": length, "long_form": long_form},
                        chip_yaml=CHIP_YAML)
    assert dut.ok, f"build/run failed: {dut.reason}"
    ref = [_s16(w) / 32768.0 for w in blk.process_reference_q15(inputs)]
    return dut, compare_against_grc(dut.outputs_q15, ref, metric=Metric.EXACT,
                                    delay=0)


# --- the taps ARE GR's filter (cheap, no chip) --------------------------------

@pytest.mark.parametrize("length,long_form", [
    (2, False), (4, False), (4, True), (8, True), (16, True), (32, True)])
def test_taps_reproduce_gnuradio_impulse_response(length, long_form):
    """The computed dc-blocker taps must equal GR dc_blocker_ff's impulse response
    bit-for-bit (float) — proving the equivalence is REAL, not assumed."""
    taps = _dc_blocker_taps(length, long_form)
    # GR impulse response: feed a unit impulse padded long enough for the whole
    # kernel to emerge, read back the response window.
    n = len(taps)
    imp = [0.0] * 2 + [1.0] + [0.0] * (n + 4)
    imp_q15 = [int(round(v * 32767)) & 0xFFFF for v in imp]
    gr = _gr_dc_blocker(imp_q15, length, long_form)
    # the response starts at the impulse position (index 2); the input was scaled
    # by 32767/32768, so rescale GR's output back to a unit impulse.
    resp = [v * 32768.0 / 32767.0 for v in gr.floats[2:2 + n]]
    err = max(abs(a - b) for a, b in zip(taps, resp))
    assert err < 1e-4, (
        f"dc-blocker taps differ from GR impulse response by {err:.2e} "
        f"(length={length}, long_form={long_form})")
    assert abs(sum(taps)) < 1e-9, "dc-blocker taps must sum to 0 (a true DC notch)"


# --- DSP equivalence: DUT vs GNU Radio ---------------------------------------

def test_dc_blocker_edge_vectors():
    """Short-form single-cell DC blocker matches GR on edge vectors."""
    dut, res, n, S = _verify_vs_gr(2, False, EDGE)
    print(f"\nedge len=2 short (N={n},S={S}):", res.summary(), "| hop", dut.hop_count)
    assert res.passed, res.summary()


@pytest.mark.parametrize("seed", [1, 7, 42])
def test_dc_blocker_random(seed):
    """Half-scale random input keeps the chain in range — pure DSP equivalence."""
    n_taps = _block(4, True).num_taps
    inp = [v // 2 for v in _random_input(seed, 2 * n_taps + 20)]
    dut, res, n, S = _verify_vs_gr(4, True, inp)
    print(f"\nrandom seed={seed} len=4 long:", res.summary())
    assert res.passed, res.summary()


@pytest.mark.parametrize("length,long_form", [
    (2, False), (3, False), (4, False),     # short form: 3,5,7 taps (single cell)
    (4, True), (6, True), (8, True),        # long form: 13,21,29 taps (multi-cell)
    (16, True),                             # 61 taps, ~13 cells
])
def test_dc_blocker_param_sweep(length, long_form):
    """Parity must hold across the (length, long_form) parameter space — single
    cell through a deep multi-cell wavefront. Driven with > 2*ntaps full-scale
    random samples so every wavefront cell is exercised (INV-12)."""
    n_taps = _block(length, long_form).num_taps
    inp = _random_input(seed=500 + length + (1000 if long_form else 0),
                        n=2 * n_taps + 24)
    dut, res, n, S = _verify_vs_gr(length, long_form, inp)
    print(f"\nlen={length} long={long_form} (N={n},cells={_block(length,long_form).cell_count},S={S}):",
          res.summary())
    assert res.passed, res.summary()


def test_dc_blocker_gr_default_geometry_routes_and_matches():
    """The GR DEFAULT block (length=32, long_form=True → 125 taps ≈ 26 cells) must
    actually place, route, build, and run on simKYT — and match GR within the
    headroom-aware floor. This is the real default a GRC user drops in; the
    26-cell fold must stay ≤8 across (INV-9) and egress from the last cell."""
    length, long_form = 32, True
    blk = _block(length, long_form)
    assert blk.cell_count == 26, f"GR-default geometry changed: {blk.cell_count} cells"
    C, H = blk._fold_geometry()
    assert C <= FIRFilterBlock.MAX_CELLS_ACROSS, f"fold {C}x{H} too wide (INV-9)"
    n_taps = blk.num_taps
    inp = [v // 2 for v in _random_input(seed=3232, n=2 * n_taps + 30)]
    dut, res, n, S = _verify_vs_gr(length, long_form, inp)
    print(f"\nGR-default len=32 long ({n} taps, {blk.cell_count} cells, fold {C}x{H}):",
          res.summary())
    assert dut.ok, f"GR-default DC blocker did not build/route: {dut.reason}"
    assert res.passed, res.summary()


def test_headroom_tolerance_is_derived_not_loosened():
    """The DUT-vs-GR floor for the (headroom, S=1) dc-blocker is the widened
    ``N·(2^(S-1)+1)+1`` floor, NOT the plain ``N+1``. Assert S=1 and that the
    floor actually used is the headroom floor (so the gate is not secretly relying
    on a too-tight or hand-tuned bound)."""
    blk = _block(8, True)
    N, S = blk.num_taps, blk._head_shift
    assert S == 1, f"dc-blocker (Σ|h|≈1.9) should need S=1 of headroom, got S={S}"
    plain = q15_quant_floor(N)
    head = q15_quant_floor(N, head_shift=S)
    assert head == N * (2 ** (S - 1) + 1) + 1 == 2 * N + 1
    assert head > plain, "headroom floor must be WIDER than the plain N+1 floor"


# --- bit-exact substrate (incl. saturation) ----------------------------------

def test_dc_blocker_bitexact_multicell():
    """A multi-cell DC blocker matches the COEFFICIENT-HEADROOM Q15 reference
    EXACTLY under full-scale random input (which drives the chain into the
    saturating regime), proving the datapath — incl. the saturating shift — is
    bit-correct."""
    length, long_form = 8, True
    n_taps = _block(length, long_form).num_taps
    inp = _random_input(seed=909, n=2 * n_taps + 24)
    dut, res = _verify_bitexact(length, long_form, inp)
    print("\nbitexact len=8 long:", res.summary())
    assert res.passed, res.summary()


def test_dc_blocker_saturates_on_overload():
    """Full-scale input drives the dc-blocker (Σ|h|≈1.9) past unity, so the final
    saturating shift fires. The DUT must (a) match the headroom Q15 reference
    EXACTLY and (b) actually pin some outputs at ±full-scale — proof it SATURATES
    (clips) rather than wrapping/rolling over. Vacuity-guarded: the reference must
    contain a rail."""
    length, long_form = 4, False    # 7 taps, single cell, S=1
    blk = _block(length, long_form)
    # Worst-case sign-aligned full-scale window, tiled, so the convolution at the
    # aligned indices sums to Σ|h| ≈ 1.94 > 1 and the output pins at +full-scale.
    taps = _dc_blocker_taps(length, long_form)
    N = len(taps)
    pattern = [0x7FFF if t >= 0 else 0x8001 for t in reversed(taps)]
    inp = (pattern * 6)[: 4 * N]
    ref_q15 = blk.process_reference_q15(inp)
    assert any(_s16(w) in (32767, -32768) for w in ref_q15), \
        "overload reference does not saturate — stimulus is vacuous"
    dut, res = _verify_bitexact(length, long_form, inp)
    assert res.passed, f"DUT does not match the saturating reference: {res.summary()}"
    sat = [_s16(w) for w in dut.outputs_q15]
    assert any(v in (32767, -32768) for v in sat), \
        f"overload did not pin the DUT at a rail (got {sat})"


def test_dc_blocker_wrap_mutation_fails():
    """MUTATION (INV-4 / saturation): a DUT WITHOUT coefficient headroom — the
    UNSCALED coeffs accumulated with a WRAPPING accumulator and NO saturating
    shift — must FAIL against the saturating reference on the overload stimulus.
    Where the correct block pins at a rail, the wrapping output rolls over."""
    from gr_kyttar.placement.blocks._base import float_to_q15
    length, long_form = 4, False
    taps = _dc_blocker_taps(length, long_form)
    blk = _block(length, long_form)
    N = len(taps)
    pattern = [0x7FFF if t >= 0 else 0x8001 for t in reversed(taps)]
    inp = (pattern * 6)[: 4 * N]
    # OLD no-headroom datapath: UNSCALED Q15 coeffs, every step WRAPS, no shift.
    c = [float_to_q15(t) for t in taps]
    delay = [0] * N
    wrapped = []
    for s in inp:
        delay = delay[1:] + [_s16(int(s) & 0xFFFF)]
        acc = (_s16(delay[0]) * _s16(c[0])) >> 15
        for i in range(1, N):
            acc = _s16((acc + ((_s16(delay[i]) * _s16(c[i])) >> 15)) & 0xFFFF)
        wrapped.append(acc & 0xFFFF)
    ref = [_s16(w) / 32768.0 for w in blk.process_reference_q15(inp)]
    assert any(_s16(int(round(v * 32768.0))) in (32767, -32768) for v in ref), \
        "reference does not saturate — mutation is vacuous"
    res = compare_against_grc(wrapped, ref, metric=Metric.EXACT, delay=0)
    assert not res.passed, (
        "gate accepted a WRAPPING (no-headroom) dc-blocker — it would certify the "
        "overflow rollover bug!")


# --- mandatory negative tests (the gate must FAIL on a corrupted DUT) ---------

def test_dc_blocker_mutation_inverted_fails():
    dut, _res, n, S = _verify_vs_gr(2, False, EDGE)
    ref = _gr_dc_blocker(EDGE, 2, False)
    broken = [(0x10000 - (w or 0)) & 0xFFFF for w in dut.outputs_q15]
    res = compare_against_grc(broken, ref.floats, metric=Metric.AMPLITUDE,
                              delay=0, op_count=n, head_shift=S)
    assert not res.passed, "gate failed to catch an inverted DC-blocker output!"


def test_dc_blocker_mutation_wrong_length_fails():
    """A DC blocker built at the wrong length must fail against the right GR
    reference (different filter, same input)."""
    inp = _random_input(seed=11, n=80)
    dut = run_block_dut("DCBlockerBlock", inp,
                        params={"length": 4, "long_form": True},
                        chip_yaml=CHIP_YAML)
    assert dut.ok, dut.reason
    n = _block(4, True).num_taps
    S = _block(4, True)._head_shift
    ref_wrong = _gr_dc_blocker(inp, 8, True)   # a DIFFERENT length
    res = compare_against_grc(dut.outputs_q15, ref_wrong.floats,
                              metric=Metric.AMPLITUDE, delay=0,
                              op_count=n, head_shift=S)
    assert not res.passed, "gate failed to catch a wrong-length mismatch!"


def test_dc_blocker_mutation_delay_offset_fails():
    inp = [v // 2 for v in _random_input(seed=5, n=80)]
    dut, _res, n, S = _verify_vs_gr(4, True, inp)
    ref = _gr_dc_blocker(inp, 4, True)
    shifted = [0x0000] + list(dut.outputs_q15[:-1])
    res = compare_against_grc(shifted, ref.floats, metric=Metric.AMPLITUDE,
                              delay=0, op_count=n, head_shift=S)
    assert not res.passed, "gate failed to catch a 1-sample latency error!"


def test_dc_blocker_empty_output_fails():
    ref = _gr_dc_blocker(EDGE, 2, False)
    res = compare_against_grc([], ref.floats, metric=Metric.AMPLITUDE)
    assert not res.passed


def test_dc_blocker_mutation_deep_cell_fails():
    """Prove the DEEPEST cell of the multi-cell wavefront is actually verified.
    Build a real DC blocker (length=8 long → 29 taps, multi-cell), drive it with
    long input, and compare against a reference whose ONE perturbed tap lives in
    the LAST cell of the chain (the wavefront assigns segments from the END of the
    tap array, so the last cell owns the FIRST indices — perturb tap 0). The gate
    must FAIL."""
    length, long_form = 8, True
    blk = _block(length, long_form)
    taps = _dc_blocker_taps(length, long_form)
    n_taps = len(taps)
    assert blk.cell_count >= 3, "deep-cell test needs a multi-cell block"
    inp = _random_input(seed=313, n=2 * n_taps + 24)
    dut = run_block_dut("DCBlockerBlock", inp,
                        params={"length": length, "long_form": long_form},
                        chip_yaml=CHIP_YAML)
    assert dut.ok, dut.reason
    perturbed = list(taps)
    perturbed[0] += 0.15      # a tap owned by the LAST cell, well above the floor
    ref_wrong = [_s16(w) / 32768.0
                 for w in FIRFilterBlock("c", perturbed).process_reference_q15(inp)]
    res = compare_against_grc(dut.outputs_q15, ref_wrong, metric=Metric.EXACT,
                              delay=0)
    assert not res.passed, (
        "gate did not catch a perturbed DEEP-cell tap — the last cell of the "
        "multi-cell DC blocker is not actually verified!")


# --- report -------------------------------------------------------------------

def test_emit_report():
    """Publish the dashboard quality number: DUT-vs-GR amplitude error (with the
    headroom-aware derived tolerance) for the GR-default block geometry, in the
    in-range regime so the number is genuine Q15 quantization vs GR float."""
    length, long_form = 8, True
    n_taps = _block(length, long_form).num_taps
    inp = [v // 2 for v in _random_input(seed=777, n=2 * n_taps + 24)]
    dut, res, n, S = _verify_vs_gr(length, long_form, inp)
    assert res.passed, res.summary()
    assert res.tolerance == n * (2 ** (S - 1) + 1) + 1
    write_report("DCBlockerBlock", res, coverage={
        "edge": True, "random": 3, "param_sweep": 7, "mutation": True,
        "overload": True, "bit_exact": True})
