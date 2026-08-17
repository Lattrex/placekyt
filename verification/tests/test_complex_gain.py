# SPDX-License-Identifier: GPL-3.0-or-later
"""Verify ComplexGainBlock against GNU Radio blocks.multiply_const_cc(gain).

ComplexGainBlock is the 2-rail twin of GainBlock: it scales BOTH the I and Q rails
of a complex stream by the SAME real constant ``gain`` (out = gain * in), with NO
rotation — a drop-in for GR ``blocks.multiply_const_cc(gain)``. It supports the full
valid range ``gain in (0, 4)``: the datapath is Q15 ``[-1, 1)`` but ``gain`` may
exceed 1 (a receiver amplifies), so the block stores ``gain/4`` in Q15 (always
representable), multiplies each rail by it (the product is ALWAYS in range — no
accumulator wrap), then restores the gain with a SATURATING left shift by 2 (two
``ADD R0,R0`` doublings, INV-13's doubling variant). So the block SATURATES on
overload exactly like multiply_const_cc (GR clips to the Q15 rails).

Two reference tiers:
  * DSP equivalence — DUT vs GNU Radio multiply_const_cc, AMPLITUDE, BOTH channels,
    within the DERIVED Q15 floor for the ``gain/4``-then-``<<2`` datapath.
  * Bit-exact substrate — DUT vs process_reference (the exact per-rail
    truncating-MULQ + saturating doubling), EXACT, on both channels.

DERIVED TOLERANCE (not tuned). Representing ``gain>1`` forces the ``gain/4`` (S=2)
coefficient + a ``<<2`` restore, which AMPLIFIES both the coefficient quantization
and the MULQ truncation by 2^S=4. Worst case per output:
    |out - x*gain| <= 2^S * (coeff_err + trunc) + gr_quant
                    = 4     * (0.5       + 1.0  ) + 1        = 7 LSB
(coeff_err <= 0.5 LSB from rounding gain/4 to Q15; trunc <= 1.0 LSB because hardware
MULQ TRUNCATES toward -inf (verified via sim trace) rather than rounds; +1 for GR's
own float->Q15 output quantization). Measured worst across a dense gain sweep = 6
LSB, so the derived 7 bounds it with margin. This is the honest fixed-point floor of
a Q15 gain>1 scaler, NOT a loosened tolerance.

Per INV-4 every gate is paired with a mutation (swap I/Q, negate Q, +1 delay, wrong
gain, empty) that must FAIL. Gain is memoryless -> group delay 0.

Run:
    KYTTAR_GR_PYTHON=/usr/bin/python3 QT_QPA_PLATFORM=offscreen \
      <venv>/python -m pytest verification/tests/test_complex_gain.py -x -q
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
    run_block_dut_complex, run_gnuradio_ref_complex, compare_complex_against_grc,
    write_report, Metric)
from gr_kyttar.placement.blocks.complex_gain_block import ComplexGainBlock  # noqa: E402

CHIP_YAML = str(_PLACEKYT / "resources" / "chips" / "kyttar_10x12.yaml")
_GR_AVAILABLE = os.path.exists(os.environ.get("KYTTAR_GR_PYTHON", "/usr/bin/python3"))
pytestmark = pytest.mark.skipif(
    not (os.path.exists(CHIP_YAML) and _GR_AVAILABLE),
    reason="chip yaml or GNU Radio interpreter absent")

# Derived Q15 floor for the gain/4-then-<<2 (S=2) datapath — see the module docstring.
#   TOL = 2^S * (coeff_err + trunc) + gr_quant = 4*(0.5+1.0)+1 = 7 LSB.
TOL = 7


def _s16(v):
    if v is None:
        return None
    v = int(v) & 0xFFFF
    return v - 0x10000 if v >= 0x8000 else v


def _gr_gain(stim, gain):
    """GNU Radio multiply_const_cc(gain) over the complex stimulus."""
    return run_gnuradio_ref_complex(
        stim,
        gnuradio_script="""
from gnuradio import gr, blocks
tb = gr.top_block()
src = blocks.vector_source_c(input_complex, False)
mult = blocks.multiply_const_cc(gain)
snk = blocks.vector_sink_c()
tb.connect(src, mult); tb.connect(mult, snk)
tb.run()
output_complex = list(snk.data())
""",
        extra_args={"gain": gain})


def _run_dut(stim, gain):
    dut = run_block_dut_complex(
        "ComplexGainBlock", stim, params={"gain": gain}, chip_yaml=CHIP_YAML,
        in_ports=("xi", "xq"), words_per_sample=2)
    assert dut.ok, dut.reason
    return dut


def _signal(seed, n, amp=0.9):
    rng = random.Random(seed)
    return [complex(rng.uniform(-amp, amp), rng.uniform(-amp, amp)) for _ in range(n)]


# EDGE: full-scale rails on each axis (drive the saturating restore), zero, quadrant
# corners. At gain>1 the outer points overload and MUST pin to the Q15 rails.
EDGE = [complex(0.9, 0.1), complex(0.1, 0.9), complex(-0.9, -0.1),
        complex(-0.1, -0.9), complex(0.5, -0.5), complex(-0.5, 0.5),
        complex(0.0, 0.0), complex(0.999, -0.999), complex(-0.999, 0.999)]


def _cmp_gr(dut, gr):
    return compare_complex_against_grc(
        dut.i_q15, dut.q_q15, gr.i, gr.q, metric=Metric.AMPLITUDE,
        delay=0, tolerance=TOL)


def _cmp_ref(dut, stim, gain):
    ref = ComplexGainBlock("ref", gain=gain).process_reference(stim)
    ri = [_s16(a) / 32768.0 for a, b in ref]
    rq = [_s16(b) / 32768.0 for a, b in ref]
    return compare_complex_against_grc(
        dut.i_q15, dut.q_q15, ri, rq, metric=Metric.EXACT, delay=0)


# --- structure / smoke --------------------------------------------------------

def test_gain_drives_and_captures():
    dut = _run_dut(_signal(1, 24), 2.4)
    assert dut.words_per_sample == 2, f"expected 2 words/sample, got {dut.words_per_sample}"
    assert dut.in_regs == (0, 1), "complex signal should land xi@R0, xq@R1"
    assert all(v is not None for v in dut.i_q15) and all(v is not None for v in dut.q_q15)


# --- DSP equivalence vs GNU Radio multiply_const_cc ---------------------------

def test_gain_edge_vectors():
    """Real ComplexGainBlock matches GR multiply_const_cc on edge/full-scale vectors
    (both rails, including the saturating outer points) at the receiver gain 2.4."""
    dut = _run_dut(EDGE, 2.4)
    res = _cmp_gr(dut, _gr_gain(EDGE, 2.4))
    print("\nedge:", res.summary(), "| hop", dut.hop_count)
    assert res.passed, res.summary()


@pytest.mark.parametrize("seed", [1, 7, 42])
def test_gain_random_vectors(seed):
    dut = _run_dut(_signal(seed, 40), 2.4)
    res = _cmp_gr(dut, _gr_gain(_signal(seed, 40), 2.4))
    print(f"\nrandom seed={seed}:", res.summary())
    assert res.passed, res.summary()


@pytest.mark.parametrize("gain", [0.1, 0.3, 0.5, 0.9, 1.0, 1.5, 2.0, 2.4, 3.0, 3.9,
                                  3.999])
def test_gain_param_sweep(gain):
    """Sweep the WHOLE declared range gain in (0, 4): near-zero, sub-unity, unity,
    and >1 (integer-doubling + saturating restore). Both rails match GR within the
    derived floor at every point."""
    stim = _signal(11, 40)
    dut = _run_dut(stim, gain)
    res = _cmp_gr(dut, _gr_gain(stim, gain))
    print(f"\ngain={gain}:", res.summary())
    assert res.passed, res.summary()


# --- bit-exact substrate ------------------------------------------------------

@pytest.mark.parametrize("gain", [0.3, 0.5, 1.0, 2.4, 3.9])
def test_gain_bitexact_reference(gain):
    """DUT matches the on-chip Q15 reference EXACTLY on BOTH channels (per-rail
    truncating MULQ by gain/4 + the saturating <<2 doubling) over a long random
    burst that exercises the in-range AND the saturating paths."""
    stim = _signal(42, 60)
    dut = _run_dut(stim, gain)
    res = _cmp_ref(dut, stim, gain)
    print(f"\nbit-exact gain={gain}:", res.summary())
    assert res.passed, res.summary()


def test_gain_out_of_range_raises():
    """HARDWARE RANGE (documented): gain outside (0, 4) is not representable in the
    gain/4-then-<<2 Q15 datapath and RAISES (never silently clamps) — the guard on
    the declared parameter space."""
    for bad in (0.0, -0.5, 4.0, 4.5):
        with pytest.raises(ValueError, match="gain must be in"):
            ComplexGainBlock("c", gain=bad)


# --- MANDATORY mutation tests (the gate must DETECT these) ---------------------

def _setup():
    dut = _run_dut(_signal(7, 48), 2.4)
    gr = _gr_gain(_signal(7, 48), 2.4)
    return dut, gr


def test_gain_mutation_swapped_iq_fails():
    dut, gr = _setup()
    res = compare_complex_against_grc(dut.q_q15, dut.i_q15, gr.i, gr.q,
                                      metric=Metric.AMPLITUDE, delay=0, tolerance=TOL)
    assert not res.passed, "gate failed to detect swapped I/Q!"


def test_gain_mutation_negated_q_fails():
    dut, gr = _setup()
    neg_q = [(0x10000 - (w or 0)) & 0xFFFF for w in dut.q_q15]
    res = compare_complex_against_grc(dut.i_q15, neg_q, gr.i, gr.q,
                                      metric=Metric.AMPLITUDE, delay=0, tolerance=TOL)
    assert not res.passed, "gate failed to detect a negated Q channel!"


def test_gain_mutation_one_sample_offset_fails():
    dut, gr = _setup()
    sh_i = [0x0000] + list(dut.i_q15[:-1])
    sh_q = [0x0000] + list(dut.q_q15[:-1])
    res = compare_complex_against_grc(sh_i, sh_q, gr.i, gr.q,
                                      metric=Metric.AMPLITUDE, delay=0, tolerance=TOL)
    assert not res.passed, "gate failed to detect a 1-sample latency error!"


def test_gain_mutation_wrong_gain_fails():
    """A DUT built at the wrong gain must FAIL against the right reference — the
    core equivalence claim (and proof the tolerance is not so loose it accepts any
    scaling)."""
    stim = _signal(7, 48)
    dut = _run_dut(stim, 2.4)
    gr_wrong = _gr_gain(stim, 1.2)   # reference for a DIFFERENT gain
    res = compare_complex_against_grc(dut.i_q15, dut.q_q15, gr_wrong.i, gr_wrong.q,
                                      metric=Metric.AMPLITUDE, delay=0, tolerance=TOL)
    assert not res.passed, "gate failed to detect a wrong-gain mismatch!"


def test_gain_mutation_no_saturation_fails():
    """The pre-fix DUT WRAPPED instead of saturating on overload (the real bug this
    block was carrying). A wrapping reference (mod-2^16, no rail clamp) must FAIL the
    bit-exact gate against the DUT — proving the DUT actually saturates."""
    gain = 2.4
    stim = EDGE  # full-scale points overload at gain 2.4
    dut = _run_dut(stim, gain)
    blk = ComplexGainBlock("ref", gain=gain)

    def wrap_rail(x):
        p = (_s16(x) * _s16(blk._gain_q)) >> 15   # MULQ (truncating)
        return _s16((p << 2) & 0xFFFF)            # <<2 WITHOUT saturation (wraps)

    def fq(v):
        return max(-32768, min(32767, int(round(v * 32768.0)))) & 0xFFFF

    wr_i, wr_q = [], []
    for c in stim:
        wr_i.append(wrap_rail(fq(c.real)) / 32768.0)
        wr_q.append(wrap_rail(fq(c.imag)) / 32768.0)
    res = compare_complex_against_grc(dut.i_q15, dut.q_q15, wr_i, wr_q,
                                      metric=Metric.EXACT, delay=0)
    assert not res.passed, (
        "gate failed to detect a wrapping (non-saturating) datapath — the DUT must "
        "SATURATE, so it must DIFFER from the wrapping model on overload!")


def test_gain_empty_output_fails():
    _, gr = _setup()
    res = compare_complex_against_grc([], [], gr.i, gr.q,
                                      metric=Metric.AMPLITUDE, delay=0, tolerance=TOL)
    assert not res.passed


# --- dashboard report ---------------------------------------------------------

def test_emit_report():
    """Emit the dashboard report at the receiver gain 2.4 (the load-bearing
    qam16_modem operating point). Runs a passing equivalence check first."""
    gain = 2.4
    stim = _signal(7, 64)
    dut = _run_dut(stim, gain)
    res = _cmp_gr(dut, _gr_gain(stim, gain))
    assert res.passed, res.summary()
    write_report("ComplexGainBlock", res.i, coverage={
        "edge": True, "random": 3, "param_sweep": 11, "bit_exact": True,
        "mutation": True, "orientation": True})
