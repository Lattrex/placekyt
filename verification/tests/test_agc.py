# SPDX-License-Identifier: GPL-3.0-or-later
"""Verify AGCBlock against GNU Radio ``analog.agc_ff``.

AGC is a recursive (feedback) loop, so unlike a feed-forward block this uses an
explicit Q15 loop tolerance and trims the loop transient (head_shift) before the
amplitude comparison — the standard treatment for recursive blocks (see the IIR
biquad). The block mirrors ``agc_ff`` VERBATIM (params: rate, reference, gain,
max_gain) with the exact update law:

    out   = in * gain
    gain += rate * (reference - |out|)
    if max_gain > 0: gain = min(gain, max_gain)   (and a 0 floor)

LIMITATION (documented, not a bug): the on-chip gain register is Q15 [-1, 1), so
this block is GRC-faithful in the ATTENUATING regime (gain <= 1 — strong signal
driven down to the reference). True amplification (gain > 1, weak signal pulled
UP) needs a gain register with integer headroom (e.g. Q8.7) and is out of scope
for the single-cell Q15 block. Tests therefore drive a strong signal with
max_gain bounded to 1.0 so the loop stays in range — the regime the chip block
implements.

Run:
    KYTTAR_GR_PYTHON=/usr/bin/python3 QT_QPA_PLATFORM=offscreen \
      <venv>/python -m pytest verification/tests/test_agc.py -x -q
"""

from __future__ import annotations

import math
import os
import sys
from pathlib import Path

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

_PLACEKYT = Path(__file__).resolve().parents[2] / "placekyt"
_VERIFY = Path(__file__).resolve().parents[1]
for p in (str(_PLACEKYT), str(_VERIFY)):
    if p not in sys.path:
        sys.path.insert(0, p)

from kyttar_verify import (  # noqa: E402
    run_block_dut, run_gnuradio_ref, compare_against_grc, write_report, Metric)

CHIP_YAML = str(_PLACEKYT / "resources" / "chips" / "kyttar_10x12.yaml")

_GR_AVAILABLE = os.path.exists(os.environ.get("KYTTAR_GR_PYTHON", "/usr/bin/python3"))
pytestmark = pytest.mark.skipif(
    not (os.path.exists(CHIP_YAML) and _GR_AVAILABLE),
    reason="chip yaml or GNU Radio interpreter absent")

# Loop tolerance + transient trim for this recursive block.
_TOL_LSB = 80          # Q15 loop floor (observed ~39 LSB; margin for param sweep)
_TRIM = 40             # drop the loop start-up transient before comparing
# Gain bound that keeps the loop in Q15 range (the attenuating regime).
_MAXG = 0.999


def _fq(f):
    return int(round(max(-1.0, min(0.999, f)) * 32767)) & 0xFFFF


def _signal(n=300, amp=0.85, freq=0.04):
    return [amp * math.sin(2 * math.pi * freq * i) for i in range(n)]


def _gr_agc(inputs_q15, rate, reference, gain, max_gain):
    return run_gnuradio_ref(
        input_q15=inputs_q15,
        gnuradio_script="""
from gnuradio import gr, blocks, analog
tb = gr.top_block()
src = blocks.vector_source_f(input_float, False)
agc = analog.agc_ff(rate, reference, gain, max_gain)
sink = blocks.vector_sink_f()
tb.connect(src, agc); tb.connect(agc, sink)
tb.run()
output_float = list(sink.data())
""",
        extra_args={"rate": rate, "reference": reference,
                    "gain": gain, "max_gain": max_gain},
    )


def _run_and_compare(rate, reference, *, amp=0.85, gain=0.999, max_gain=_MAXG):
    sig = _signal(amp=amp)
    inq = [_fq(v) for v in sig]
    params = dict(rate=rate, reference=reference, gain=gain, max_gain=max_gain)
    dut = run_block_dut("AGCBlock", inq, params=params, chip_yaml=CHIP_YAML,
                        in_port="sample", out_port="out")
    assert dut.ok, dut.reason
    ref = _gr_agc(inq, rate, reference, gain, max_gain)
    # Recursive block: explicit Q15 loop tolerance + trim the start-up transient
    # (head_shift drops the first _TRIM samples of BOTH before the amplitude check).
    res = compare_against_grc(
        dut.outputs_q15, ref.floats, metric=Metric.AMPLITUDE,
        delay=0, tolerance=_TOL_LSB, head_shift=_TRIM)
    return dut, res


def test_agc_tracks_reference():
    """The on-chip AGC matches GNU Radio agc_ff (attenuating regime) within the
    Q15 loop floor — its output envelope converges to GR's, sample for sample."""
    dut, res = _run_and_compare(rate=0.02, reference=0.3)
    print("\nagc track:", res.summary(), "| words", dut.n_words)
    assert res.passed, res.summary()


@pytest.mark.parametrize("rate", [0.01, 0.02, 0.05])
def test_agc_rate_sweep(rate):
    dut, res = _run_and_compare(rate=rate, reference=0.3)
    print(f"\nrate={rate}:", res.summary())
    assert res.passed, res.summary()


@pytest.mark.parametrize("reference", [0.2, 0.3, 0.5])
def test_agc_reference_sweep(reference):
    dut, res = _run_and_compare(rate=0.02, reference=reference)
    print(f"\nreference={reference}:", res.summary())
    assert res.passed, res.summary()


# --- rising-gain regression at the GR DEFAULT rate=1e-4 (fixed 2026-08-16) ----

def test_rising_gain_default_rate_bit_exact_and_alive():
    """THE BARE-MULQ IIR STALL (regression, fixed 2026-08-16; proven to FAIL on
    the pre-fix block — INV-4).

    ``gain += MULQ(rate, err)`` truncates every increment with
    ``|err| < 2^15/rate_q`` to ZERO; at GR's DEFAULT rate=1e-4 (rate_q=3) that
    is 10923 LSB. The stall is DIRECTION-ASYMMETRIC — floor truncation zeroes
    only POSITIVE sub-LSB increments (a negative err still steps -1) — so only
    a RISING gain froze; the falling regime the existing gates drive
    (gain=0.999 down) self-repaired and hid it (the AGCCC/RMS lesson: pin the
    stall with a start gain BELOW the settled point).

    Setup: constant 0.9 input, reference=0.5 (settled gain ~0.556), start
    gain=0.4. There err ~4588 < 10923, so the PRE-FIX loop is completely
    frozen (constant output ~0.36); the error-feedback fix accumulates the
    sub-LSB increments and the gain provably RISES (~+150 LSB over 600
    samples). Asserts BOTH: the output rises, AND the chip is BIT-EXACT
    against the error-feedback reference model."""
    n = 600
    params = dict(rate=1e-4, reference=0.5, gain=0.4, max_gain=_MAXG)
    inq = [_fq(0.9)] * n
    dut = run_block_dut("AGCBlock", inq, params=params, chip_yaml=CHIP_YAML,
                        in_port="sample", out_port="out")
    assert dut.ok, dut.reason

    def s16(w):
        w = int(w) & 0xFFFF
        return w - 0x10000 if w & 0x8000 else w

    rise = s16(dut.outputs_q15[-1]) - s16(dut.outputs_q15[0])
    assert rise >= 100, (
        f"rising gain STALLED at rate=1e-4: output moved {rise} LSB over {n} "
        f"samples (bare-MULQ truncation freezes the loop; the error-feedback "
        f"accumulator must keep it climbing)")

    # Bit-exact vs the error-feedback reference model (floor-MULQ + 16-bit
    # frac accumulator + ADC carry — the exact on-chip datapath). Feed the
    # model the EXACT chip input words (s16/32768 round-trips losslessly
    # through float_to_q15; the _fq helper's 32767 scale would be 1 LSB off).
    from gr_kyttar.placement.blocks.agc_block import AGCBlock  # noqa: PLC0415
    blk = AGCBlock("ref", **params)
    ref = blk.process_reference([s16(w) / 32768.0 for w in inq])
    res = compare_against_grc(dut.outputs_q15, list(ref), metric=Metric.EXACT,
                              delay=0)
    assert res.passed, f"chip != error-feedback model: {res.summary()}"


def test_rising_gain_default_rate_model_converges():
    """The error-feedback model CONVERGES at rate=1e-4: from gain=0.4 the
    settled |out| reaches the reference (0.5) within a few LSB — the bare-MULQ
    form stalls 10923-err LSB short (|out| frozen at ~0.36). Pure-model long
    run (the chip is linked to this model by the bit-exact gate above — the
    AGCCC regime-mirroring recipe)."""
    from gr_kyttar.placement.blocks.agc_block import AGCBlock  # noqa: PLC0415
    blk = AGCBlock("ref", rate=1e-4, reference=0.5, gain=0.4, max_gain=_MAXG)
    out = blk.process_reference([0.9] * 130000)
    tail = out[-2000:]
    settled = sum(abs(float(v)) for v in tail) / len(tail) * 32768.0
    assert abs(settled - 16384) <= 4.0, (
        f"model failed to converge to the reference at rate=1e-4: settled "
        f"|out| = {settled:.1f} LSB, want 16384 +- 4")


# --- MANDATORY negative tests: the gate must DETECT real corruptions ----------

def test_mutation_inverted_output_fails():
    """A sign-inverted AGC output must FAIL the gate."""
    sig = _signal()
    inq = [_fq(v) for v in sig]
    dut = run_block_dut("AGCBlock", inq, params=dict(
        rate=0.02, reference=0.3, gain=0.999, max_gain=_MAXG),
        chip_yaml=CHIP_YAML, in_port="sample", out_port="out")
    assert dut.ok, dut.reason
    ref = _gr_agc(inq, 0.02, 0.3, 0.999, _MAXG)
    mutated = [(0x10000 - (w or 0)) & 0xFFFF for w in dut.outputs_q15]
    res = compare_against_grc(mutated, ref.floats, metric=Metric.AMPLITUDE,
                              delay=0, tolerance=_TOL_LSB, head_shift=_TRIM)
    assert not res.passed, "gate failed to detect an inverted AGC output!"


def test_mutation_wrong_reference_fails():
    """A DUT run at reference=0.3 must FAIL against a reference=0.6 golden — the
    loop converges to a different level, which the gate must catch."""
    sig = _signal()
    inq = [_fq(v) for v in sig]
    dut = run_block_dut("AGCBlock", inq, params=dict(
        rate=0.02, reference=0.3, gain=0.999, max_gain=_MAXG),
        chip_yaml=CHIP_YAML, in_port="sample", out_port="out")
    assert dut.ok, dut.reason
    ref_wrong = _gr_agc(inq, 0.02, 0.6, 0.999, _MAXG)  # different target level
    res = compare_against_grc(dut.outputs_q15, ref_wrong.floats,
                              metric=Metric.AMPLITUDE, delay=0,
                              tolerance=_TOL_LSB, head_shift=_TRIM)
    assert not res.passed, "gate failed to detect a wrong-reference AGC!"


def test_empty_output_fails():
    ref = _gr_agc([_fq(v) for v in _signal()], 0.02, 0.3, 0.999, _MAXG)
    res = compare_against_grc([], ref.floats, metric=Metric.AMPLITUDE,
                              tolerance=_TOL_LSB)
    assert not res.passed


def test_emit_report():
    dut, res = _run_and_compare(rate=0.02, reference=0.3)
    write_report("AGCBlock", res, coverage={
        "rate_sweep": 3, "reference_sweep": 3, "mutation": True,
        "regime": "attenuating (gain<=1); amplification needs gain headroom"})
    assert res.passed
