# SPDX-License-Identifier: GPL-3.0-or-later
"""Verify IIRBiquadBlock against GNU Radio's filter.iir_filter_ffd.

A biquad is the first RECURSIVE block: its output feeds back, so a single bad
sample propagates forever. Two things make Q15 hard here, and this suite gates
both:

FEEDBACK-COEFFICIENT RANGE — the half-and-double-MSUQ fix
---------------------------------------------------------
A biquad's feedback coefficient a1 = -2*cos(omega) can have |a1| up to ~2, which
Q15 (range [-1, +1)) cannot represent. The OLD block clamped a-coeffs to [-1, 1],
silently turning every sharp filter into a different, wrong filter. The fix
(IIRBiquadBlock): store each a-coeff HALVED (representable) and apply its MSUQ
(R0 -= (A*B)>>15, architecture_spec_v0.11 §4.12) TWICE — subtracting a/2 twice ==
subtracting a, with every intermediate product in range. No clamp, no overflow,
no new ISA. A stable biquad's output is itself bounded, so the Direct-Form-I
accumulator stays in range with no saturating shift.

PRECISION LIMIT (a documented known limit, like the FIR tap ceiling, INV-7)
---------------------------------------------------------------------------
GR's iir_filter_ffd uses DOUBLE-precision feedback taps; Q15 is coarser and the
quantization error in the recursive loop GROWS as the poles approach the unit
circle. Measured vs GR (butterworth-2): normalized cutoff 0.10-0.40 -> 3-16 LSB
(excellent); 0.05 -> ~53 LSB (marginal); 0.02 (poles within ~0.02 of |z|=1) ->
~160 LSB. So the block is production-accurate for the common gentle-to-moderate
range; very sharp poles are a GUARDED known limit (a test that flips if the
precision is ever improved), NOT a silent failure.

Two reference tiers (per the KNOWLEDGE_BASE):
  * Bit-exact substrate: the DUT must match IIRBiquadBlock.process_reference_q15
    EXACTLY (it models the hardware: MULQ b0, MACQ b1/b2, each feedback term as
    two wrapping MSUQ of the halved coeff). This holds at EVERY cutoff, sharp or
    gentle — the datapath is exactly the predictor.
  * DSP equivalence: in the production cutoff range the DUT matches GNU Radio's
    iir_filter_ffd within a derived tolerance — proving the block is a real
    drop-in for the GR block, not just self-consistent.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import numpy as np
import pytest

_HERE = Path(__file__).resolve().parent
_VERIF = _HERE.parent
_REPO = _VERIF.parent
_PLACEKYT = _REPO / "placekyt"
_RUNTIME = _REPO / "runtime" / "python"
for p in (str(_VERIF), str(_PLACEKYT), str(_RUNTIME)):
    if p not in sys.path:
        sys.path.insert(0, p)

_SYS_PY = os.environ.get("KYTTAR_GR_PYTHON", "/usr/bin/python3")


def _gr_available() -> bool:
    try:
        r = subprocess.run([_SYS_PY, "-c", "import gnuradio, scipy"],
                           capture_output=True, timeout=30)
        return r.returncode == 0
    except Exception:  # noqa: BLE001
        return False


from kyttar_verify import (  # noqa: E402
    run_block_dut, run_gnuradio_ref, compare_against_grc, write_report, Metric)
from gr_kyttar.placement.blocks.iir_biquad_block import IIRBiquadBlock  # noqa: E402

CHIP_YAML = str(_PLACEKYT / "resources" / "chips" / "kyttar_10x12.yaml")

pytestmark = pytest.mark.skipif(
    not (os.path.exists(CHIP_YAML) and _gr_available()),
    reason="chip yaml or GNU Radio/scipy unavailable")


# --- helpers ------------------------------------------------------------------

def _s16(v):
    v = int(v) & 0xFFFF
    return v - 0x10000 if v >= 0x8000 else v


# butterworth-2 coefficients per normalized cutoff, computed once via scipy in
# the GR subprocess (the verification venv has no scipy). {cutoff: (b, a)}.
_BUTTER_CACHE: dict | None = None


def _butter(cutoff: float):
    global _BUTTER_CACHE
    if _BUTTER_CACHE is None:
        cutoffs = [0.4, 0.25, 0.15, 0.1, 0.05, 0.02]
        code = ("import json;from scipy import signal;"
                f"cs={cutoffs!r};"
                "print(json.dumps({str(c):[list(map(float,signal.butter(2,c)[0])),"
                "list(map(float,signal.butter(2,c)[1]))] for c in cs}))")
        out = subprocess.check_output([_SYS_PY, "-c", code], timeout=60).decode()
        _BUTTER_CACHE = json.loads(out)
    b, a = _BUTTER_CACHE[str(cutoff)]
    return list(b), list(a)  # a = [1, a1, a2]


def _gr_iir(inputs_q15, b, a):
    """GNU Radio iir_filter_ffd(fftaps=b, fbtaps=a, oldstyle=False) golden."""
    return run_gnuradio_ref(
        input_q15=inputs_q15,
        gnuradio_script="""
from gnuradio import gr, filter as gr_filter, blocks
tb = gr.top_block()
src = blocks.vector_source_f(input_float, False)
iir = gr_filter.iir_filter_ffd(b, a, False)
sink = blocks.vector_sink_f()
tb.connect(src, iir); tb.connect(iir, sink)
tb.run()
output_float = list(sink.data())
""",
        extra_args={"b": list(b), "a": list(a)})


def _q15_ref_floats(b, a, inputs_q15):
    """The block's OWN bit-exact Q15 datapath reference, as floats for
    compare_against_grc (which re-quantizes to Q15)."""
    blk = IIRBiquadBlock("ref", b, [a[1], a[2]])
    return [_s16(w) / 32768.0 for w in blk.process_reference_q15(inputs_q15)]


def _stim(n=200, amp=0.5, f=0.013):
    x = (amp * np.sin(2 * np.pi * f * np.arange(n))).astype(np.float64)
    return [int(round(np.clip(v, -1, 0.999969) * 32768)) & 0xFFFF for v in x]


def _run(b, a, inputs):
    dut = run_block_dut("IIRBiquadBlock", inputs,
                        params={"b_coeffs": list(b), "a_coeffs": [a[1], a[2]]},
                        chip_yaml=CHIP_YAML)
    assert dut.ok, f"build/run failed: {dut.reason}"
    return dut


# Production cutoff range (gentle-to-moderate; the common SDR filter band).
PROD_CUTOFFS = [0.4, 0.25, 0.15, 0.1]
# Sharp poles — bit-exact still holds, but DSP equivalence is a documented limit.
SHARP_CUTOFFS = [0.05, 0.02]


# --- bit-exact substrate (holds at EVERY cutoff) ------------------------------

@pytest.mark.parametrize("cutoff", PROD_CUTOFFS + SHARP_CUTOFFS)
def test_iir_dut_matches_q15_reference_bit_exact(cutoff):
    """The on-chip datapath must equal the block's Q15 reference EXACTLY — the
    half-and-double-MSUQ feedback, in the modeled accumulation order — for every
    cutoff including the very sharp ones the old clamp corrupted."""
    b, a = _butter(cutoff)
    inp = _stim()
    dut = _run(b, a, inp)
    ref = _q15_ref_floats(b, a, inp)
    res = compare_against_grc(dut.outputs_q15, ref, metric=Metric.EXACT, delay=0)
    assert res.passed, (
        f"cutoff {cutoff}: DUT != Q15 reference (bit-exact substrate broken): "
        f"{res.summary}")


# --- DSP equivalence vs GNU Radio (production cutoff range) -------------------

@pytest.mark.parametrize("cutoff", PROD_CUTOFFS)
def test_iir_matches_gnuradio_production_range(cutoff):
    """In the production cutoff range the DUT matches GR's iir_filter_ffd within a
    derived tolerance — proving the block is a real drop-in for the GR biquad."""
    b, a = _butter(cutoff)
    inp = _stim()
    dut = _run(b, a, inp)
    ref = _gr_iir(inp, b, a)
    # A biquad is memoryless in latency terms vs GR's output[n] (delay=0). Derive
    # the tolerance from the op count (5 mul-adds + the recursive precision floor
    # of ~16 LSB at the sharp end of this range -> op_count scaled generously).
    res = compare_against_grc(dut.outputs_q15, ref.floats, metric=Metric.AMPLITUDE,
                              delay=0, op_count=20)
    assert res.passed, f"cutoff {cutoff}: DUT vs GR {res.summary}"
    if cutoff == PROD_CUTOFFS[-1]:
        write_report("IIRBiquadBlock", res, coverage={
            "bit_exact": True, "param_sweep": len(PROD_CUTOFFS),
            "known_limit": len(SHARP_CUTOFFS), "mutation": True})


# --- the half-and-double fix: sharp filters are no longer clamped to garbage ---

def test_iir_sharp_filter_not_clamped():
    """A sharp lowpass (|a1| ~ 1.78) used to be CLAMPED to a1=-1.0 -> a totally
    different filter. With the half-and-double-MSUQ store, the DUT now tracks the
    true filter. Assert the response is the correct sharp lowpass (the recursive
    energy stays bounded AND it differs sharply from the clamped-a1 filter)."""
    b, a = _butter(0.1)
    assert abs(a[1]) > 1.0, "this cutoff must exercise |a1| > 1 (the clamp case)"
    inp = _stim()
    dut = _run(b, a, inp)
    # The TRUE filter (bit-exact ref) vs the BROKEN clamped-a1 filter must differ
    # substantially — proving the fix changed behavior in the right direction.
    true_ref = np.array(_q15_ref_floats(b, a, inp))
    a_clamped = [1.0, max(-1.0, min(1.0, a[1])), max(-1.0, min(1.0, a[2]))]
    clamped_ref = np.array(_q15_ref_floats(b, a_clamped, inp))
    diff = np.abs(true_ref - clamped_ref).max()
    assert diff > 0.05, (
        "clamped vs true filter barely differ — test doesn't exercise the bug")
    dut_f = np.array([_s16(v) / 32768.0 for v in dut.outputs_q15
                      if v is not None])
    # DUT follows the TRUE filter, not the clamped one.
    assert np.abs(dut_f - true_ref[:len(dut_f)]).max() < 0.01
    assert np.abs(dut_f - clamped_ref[:len(dut_f)]).max() > 0.04


# --- documented precision limit (a guarded known-limit, INV-7 style) ----------

@pytest.mark.parametrize("cutoff", SHARP_CUTOFFS)
def test_iir_sharp_pole_is_a_known_precision_limit(cutoff):
    """Very sharp poles exceed Q15's recursive precision vs GR's double-precision
    iir_filter_ffd. The DUT is still BIT-EXACT with its own Q15 datapath (asserted
    above) — this just RECORDS that DSP-equivalence to GR degrades past the
    production range, as a guard that flips if precision is ever improved (e.g.
    accumulator guard bits). It must NOT silently pass as production-accurate."""
    b, a = _butter(cutoff)
    inp = _stim()
    dut = _run(b, a, inp)
    ref = _gr_iir(inp, b, a)
    dut_f = np.array([_s16(v) / 32768.0 for v in dut.outputs_q15
                      if v is not None])
    gr_f = np.array(ref.floats[:len(dut_f)])
    err_lsb = np.abs(dut_f - gr_f)[40:].max() * 32768
    # Guard: error is real (> the production ~16 LSB) but bounded (the loop is
    # STABLE, not diverging). If a future precision fix drops this below ~16 LSB,
    # this assert flips and the limit should be re-documented.
    assert 16 < err_lsb < 2000, (
        f"cutoff {cutoff}: sharp-pole error {err_lsb:.0f} LSB outside the "
        f"documented known-limit band (16, 2000)")


# --- MANDATORY mutation/negative tests (INV-4: a gate is worthless until it FAILS)

def test_iir_mutation_inverted_fails():
    """A sign-inverted DUT must FAIL the GR comparison."""
    b, a = _butter(0.15)
    inp = _stim()
    ref = _gr_iir(inp, b, a)
    broken = [(-v if v is not None else None) for v in ref.floats]
    res = compare_against_grc(
        [int(round(np.clip(v, -1, 0.999) * 32768)) & 0xFFFF for v in broken],
        ref.floats, metric=Metric.AMPLITUDE, delay=0, op_count=20)
    assert not res.passed, "inverted output passed — gate can't detect sign error"


def test_iir_mutation_clamped_a_coeffs_fails():
    """The OLD bug resurrected: clamping a-coeffs to [-1,1] must FAIL vs the true
    filter for a sharp pole. This is the regression guard for the actual fix."""
    b, a = _butter(0.1)
    inp = _stim()
    ref = _gr_iir(inp, b, a)
    a_clamped = [1.0, max(-1.0, min(1.0, a[1])), max(-1.0, min(1.0, a[2]))]
    clamped = _q15_ref_floats(b, a_clamped, inp)
    res = compare_against_grc(
        [int(round(np.clip(v, -1, 0.999) * 32768)) & 0xFFFF for v in clamped],
        ref.floats, metric=Metric.AMPLITUDE, delay=0, op_count=20)
    assert not res.passed, (
        "clamped-a1 filter passed vs GR — the gate cannot see the original bug")


def test_iir_mutation_delay_fails():
    """A +1 sample delay must FAIL when delay=0 is asserted (INV-2)."""
    b, a = _butter(0.15)
    inp = _stim()
    ref = _gr_iir(inp, b, a)
    shifted = [0.0] + list(ref.floats[:-1])
    res = compare_against_grc(
        [int(round(np.clip(v, -1, 0.999) * 32768)) & 0xFFFF for v in shifted],
        ref.floats, metric=Metric.AMPLITUDE, delay=0, op_count=20)
    assert not res.passed, "a +1 delay passed — gate can't see a latency bug"
