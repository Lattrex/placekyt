# SPDX-License-Identifier: GPL-3.0-or-later
"""Verify DecimatorBlock against GNU Radio's filter.fir_filter_fff(decim, taps).

A decimator IS an FIR plus an emit-every-M counter. GR's ``fir_filter_fff(M,
taps)`` produces the full FIR output sampled at phase 0 — ``y_full[0::M]``
(verified). DecimatorBlock SUBCLASSES the verified FIRFilterBlock: every cell of
the wavefront runs each sample (delay line / partial forwarding / COEFFICIENT-
HEADROOM saturation all inherited), and only the LAST cell's OUTPUT is gated by a
modulo-M counter, so it emits on input samples 0, M, 2M, … — aligned with GR at
delay 0.

Params mirror the GR decimating FIR: ``coefficients`` (GR ``taps``) and
``decimation`` (GR's first ``fir_filter_fff`` arg / the GRC ``decim``).

Reference tiers (as for the FIR / DC blocker):
  * **DSP equivalence:** DUT emitted stream vs GNU Radio float within the
    HEADROOM-AWARE floor ``q15_quant_floor(N, head_shift=S)`` (S>0 when the
    anti-alias taps have Σ|h|>1).
  * **Bit-exact substrate:** DUT vs ``process_reference_q15`` (the inherited FIR
    Q15 datapath decimated at phase 0), EXACT — models the saturating datapath.

KNOWN LIMIT (guarded): the mod-M counter shares the last cell with the saturating
restore, so Σ|h| > 4 (head_shift > 2) raises at construction — every realistic
anti-alias decimator (normalized, or up to ~4× gain) is covered.

Run:
    KYTTAR_GR_PYTHON=/usr/bin/python3 QT_QPA_PLATFORM=offscreen \
      <venv>/python -m pytest verification/tests/test_decimator.py -x -q
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
from gr_kyttar.placement.blocks.decimator_block import DecimatorBlock  # noqa: E402

CHIP_YAML = str(_PLACEKYT / "resources" / "chips" / "kyttar_10x12.yaml")
_GR_AVAILABLE = os.path.exists(os.environ.get("KYTTAR_GR_PYTHON", "/usr/bin/python3"))
pytestmark = pytest.mark.skipif(
    not (os.path.exists(CHIP_YAML) and _GR_AVAILABLE),
    reason="chip yaml or GNU Radio interpreter absent")


def _s16(v):
    v = int(v) & 0xFFFF
    return v - 0x10000 if v >= 0x8000 else v


def _gr_decimator(inputs_q15, taps, M):
    # fir_filter_fff convolves latest-sample-first → reverse the taps (as for the
    # FIR). The first arg is the decimation factor.
    return run_gnuradio_ref(
        input_q15=inputs_q15,
        gnuradio_script=f"""
from gnuradio import gr, filter as gr_filter, blocks
tb = gr.top_block()
src = blocks.vector_source_f(input_float, False)
fir = gr_filter.fir_filter_fff({int(M)}, list(reversed(taps)))
sink = blocks.vector_sink_f()
tb.connect(src, fir); tb.connect(fir, sink)
tb.run()
output_float = list(sink.data())
""", extra_args={"taps": list(taps)})


def _random_input(seed, n):
    rng = random.Random(seed)
    return [rng.randint(0, 0xFFFF) for _ in range(n)]


def _lp(n):
    """A realistic NORMALIZED anti-alias low-pass (Hann-windowed), Σ taps = 1.
    Σ|taps| may round just over 1 (sidelobes) → S=1, the common real case."""
    if n == 1:
        return [1.0]
    t = [0.54 - 0.46 * math.cos(2 * math.pi * i / (n - 1)) for i in range(n)]
    s = sum(t)
    return [round(v / s, 5) for v in t]


def _block(taps, M):
    return DecimatorBlock("ref", taps, decimation=M)


def _emitted(dut, M):
    """The decimated stream the block emits — one output per input at phase 0
    (samples 0, M, 2M, …). run_block_dut records None for the silent samples."""
    return dut.outputs_q15[::M]


def _verify_vs_gr(taps, M, inputs):
    blk = _block(taps, M)
    n_taps, S = blk.num_taps, blk._head_shift
    dut = run_block_dut("DecimatorBlock", inputs,
                        params={"coefficients": taps, "decimation": M},
                        chip_yaml=CHIP_YAML)
    assert dut.ok, f"build/run failed: {dut.reason}"
    ref = _gr_decimator(inputs, taps, M)
    res = compare_against_grc(_emitted(dut, M), ref.floats, metric=Metric.AMPLITUDE,
                              delay=0, op_count=n_taps, head_shift=S)
    return dut, res, n_taps, S


def _verify_bitexact(taps, M, inputs):
    blk = _block(taps, M)
    dut = run_block_dut("DecimatorBlock", inputs,
                        params={"coefficients": taps, "decimation": M},
                        chip_yaml=CHIP_YAML)
    assert dut.ok, f"build/run failed: {dut.reason}"
    ref = [_s16(w) / 32768.0 for w in blk.process_reference_q15(inputs)]
    return dut, compare_against_grc(_emitted(dut, M), ref, metric=Metric.EXACT,
                                    delay=0)


# --- decimation phase (the emit-every-M contract) -----------------------------

@pytest.mark.parametrize("M", [2, 3, 4])
def test_decimation_phase_is_every_Mth_sample(M):
    """The block emits EXACTLY on input samples 0, M, 2M, … (phase 0) — the GR
    fir_filter_fff convention. run_block_dut records None for the silent samples,
    so a non-None output must appear iff the input index is a multiple of M."""
    taps = _lp(4)
    inp = _random_input(seed=10 + M, n=6 * M + 3)
    dut = run_block_dut("DecimatorBlock", inp,
                        params={"coefficients": taps, "decimation": M},
                        chip_yaml=CHIP_YAML)
    assert dut.ok, dut.reason
    for i, w in enumerate(dut.outputs_q15):
        emitted = (w is not None)
        assert emitted == (i % M == 0), (
            f"sample {i}: emitted={emitted} but expected {i % M == 0} "
            f"(decimation phase wrong)")


# --- DSP equivalence vs GNU Radio --------------------------------------------

def test_decimator_edge_vectors():
    taps = _lp(3)
    inp = [0x0000, 0x4000, 0x7FFF, 0x8001, 0x2000, 0xC000, 0x6000, 0xA000,
           0x1000, 0x3000, 0x5000, 0xB000]
    dut, res, n, S = _verify_vs_gr(taps, 2, inp)
    print(f"\nedge M=2 (N={n},S={S}):", res.summary(), "| hop", dut.hop_count)
    assert res.passed, res.summary()


@pytest.mark.parametrize("seed", [1, 7, 42])
def test_decimator_random(seed):
    taps = _lp(5)
    inp = _random_input(seed, n=4 * 5 + 20)
    dut, res, n, S = _verify_vs_gr(taps, 3, inp)
    print(f"\nrandom seed={seed} M=3:", res.summary())
    assert res.passed, res.summary()


@pytest.mark.parametrize("n,M", [
    (2, 2), (4, 2), (3, 4),          # single cell
    (5, 2), (6, 3), (8, 2), (10, 4),  # multi-cell, S=0
    (16, 2),                          # deeper multi-cell
])
def test_decimator_param_sweep(n, M):
    """Parity across (tap count, decimation) — single cell through deep
    multi-cell. Driven with > 2*ntaps full-scale random samples so every cell of
    the wavefront is exercised (INV-12)."""
    taps = _lp(n)
    inp = _random_input(seed=300 + n + M, n=2 * n * M + 30)
    dut, res, nt, S = _verify_vs_gr(taps, M, inp)
    print(f"\nn={n} M={M} (cells={_block(taps,M).cell_count},S={S}):", res.summary())
    assert res.passed, res.summary()


def test_decimator_headroom_filter_matches():
    """An anti-alias filter with Σ|h|>1 (head_shift S=1) — the common real case —
    still matches GR within the headroom-aware floor and is bit-exact with the
    saturating reference (the doubling restore coexists with the mod-M counter)."""
    taps = [0.4, 0.4, 0.4]          # Σ|h|=1.2 → S=1
    blk = _block(taps, 2)
    assert blk._head_shift == 1, f"expected S=1, got {blk._head_shift}"
    inp = _random_input(seed=55, n=80)
    dut, res, n, S = _verify_vs_gr(taps, 2, inp)
    assert res.passed, res.summary()
    _dut2, be = _verify_bitexact(taps, 2, inp)
    assert be.passed, f"not bit-exact with saturating reference: {be.summary()}"


def test_decimator_bitexact_multicell():
    """A multi-cell decimator matches the Q15 saturating reference EXACTLY under
    full-scale random input (proves the gated wavefront datapath is bit-correct)."""
    taps = _lp(8)
    inp = _random_input(seed=808, n=2 * 8 * 3 + 30)
    dut, res = _verify_bitexact(taps, 3, inp)
    print("\nbitexact n=8 M=3:", res.summary())
    assert res.passed, res.summary()


# --- mandatory negative tests -------------------------------------------------

def test_decimator_mutation_inverted_fails():
    taps = _lp(3)
    inp = _random_input(seed=2, n=60)
    dut, _res, n, S = _verify_vs_gr(taps, 2, inp)
    ref = _gr_decimator(inp, taps, 2)
    broken = [(0x10000 - (w or 0)) & 0xFFFF for w in _emitted(dut, 2)]
    res = compare_against_grc(broken, ref.floats, metric=Metric.AMPLITUDE,
                              delay=0, op_count=n, head_shift=S)
    assert not res.passed, "gate failed to catch an inverted decimator output!"


def test_decimator_mutation_wrong_decimation_fails():
    """A decimator built at M=2 must FAIL against a GR reference decimated by M=3
    (different output stream)."""
    taps = _lp(4)
    inp = _random_input(seed=9, n=120)
    dut = run_block_dut("DecimatorBlock", inp,
                        params={"coefficients": taps, "decimation": 2},
                        chip_yaml=CHIP_YAML)
    assert dut.ok, dut.reason
    n, S = _block(taps, 2).num_taps, _block(taps, 2)._head_shift
    ref_wrong = _gr_decimator(inp, taps, 3)
    res = compare_against_grc(_emitted(dut, 2), ref_wrong.floats,
                              metric=Metric.AMPLITUDE, delay=0,
                              op_count=n, head_shift=S)
    assert not res.passed, "gate failed to catch a wrong-decimation mismatch!"


def test_decimator_mutation_wrong_taps_fails():
    taps = _lp(4)
    inp = _random_input(seed=3, n=80)
    dut = run_block_dut("DecimatorBlock", inp,
                        params={"coefficients": taps, "decimation": 2},
                        chip_yaml=CHIP_YAML)
    assert dut.ok, dut.reason
    n, S = _block(taps, 2).num_taps, _block(taps, 2)._head_shift
    ref_wrong = _gr_decimator(inp, [0.9, 0.05, 0.03, 0.02], 2)
    res = compare_against_grc(_emitted(dut, 2), ref_wrong.floats,
                              metric=Metric.AMPLITUDE, delay=0,
                              op_count=n, head_shift=S)
    assert not res.passed, "gate failed to catch a wrong-taps mismatch!"


def test_decimator_mutation_delay_offset_fails():
    taps = _lp(4)
    inp = _random_input(seed=5, n=80)
    dut, _res, n, S = _verify_vs_gr(taps, 2, inp)
    ref = _gr_decimator(inp, taps, 2)
    shifted = [0x0000] + list(_emitted(dut, 2)[:-1])
    res = compare_against_grc(shifted, ref.floats, metric=Metric.AMPLITUDE,
                              delay=0, op_count=n, head_shift=S)
    assert not res.passed, "gate failed to catch a 1-sample (decimated) latency error!"


def test_decimator_empty_output_fails():
    ref = _gr_decimator([0x4000] * 20, _lp(3), 2)
    res = compare_against_grc([], ref.floats, metric=Metric.AMPLITUDE)
    assert not res.passed


def test_decimator_mutation_deep_cell_fails():
    """Prove the DEEPEST cell of the multi-cell wavefront is verified. Build an
    8-tap (multi-cell) decimator, drive it long, and compare against a saturating
    reference whose ONE perturbed tap lives in the LAST cell (segments are
    assigned from the END of the tap array, so the last cell owns the FIRST
    indices — perturb tap 0). Must FAIL."""
    taps = _lp(8)
    M = 2
    blk = _block(taps, M)
    assert blk.cell_count >= 2, "deep-cell test needs a multi-cell decimator"
    inp = _random_input(seed=313, n=2 * 8 * M + 30)
    dut = run_block_dut("DecimatorBlock", inp,
                        params={"coefficients": taps, "decimation": M},
                        chip_yaml=CHIP_YAML)
    assert dut.ok, dut.reason
    perturbed = list(taps)
    perturbed[0] += 0.2
    ref_blk = DecimatorBlock("c", perturbed, decimation=M)
    ref_wrong = [_s16(w) / 32768.0 for w in ref_blk.process_reference_q15(inp)]
    res = compare_against_grc(_emitted(dut, M), ref_wrong, metric=Metric.EXACT,
                              delay=0)
    assert not res.passed, (
        "gate did not catch a perturbed DEEP-cell tap — the last cell of the "
        "multi-cell decimator is not actually verified!")


# --- known-limit guard --------------------------------------------------------

def test_decimator_excess_headroom_raises():
    """Σ|h| > 4 (head_shift > 2) needs more restore than fits beside the mod-M
    counter on one cell; the block raises a clear error rather than silently
    failing to build. If the budget is ever extended this guard flips."""
    with pytest.raises(ValueError, match="headroom"):
        DecimatorBlock("x", [0.9] * 7, decimation=2)   # Σ|h|=6.3 → S=3


# --- report -------------------------------------------------------------------

def test_emit_report():
    taps = _lp(8)
    inp = [v // 2 for v in _random_input(seed=777, n=2 * 8 * 2 + 30)]
    dut, res, n, S = _verify_vs_gr(taps, 2, inp)
    assert res.passed, res.summary()
    write_report("DecimatorBlock", res, coverage={
        "edge": True, "random": 3, "param_sweep": 8, "mutation": True,
        "phase": True, "bit_exact": True})
