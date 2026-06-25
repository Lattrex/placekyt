# SPDX-License-Identifier: GPL-3.0-or-later
"""Prove the COMPLEX (I/Q) + LLR (soft-decision) verification harness end to end.

The base harness (test_gain.py / test_fir_filter.py) is REAL-only: one Q15 stream
in, one Q15 stream out. This suite proves the additive complex/LLR capability on
EXISTING, already-working blocks (the blocks are NOT modified — this is a harness
proof, not a block change):

  * COMPLEX path — ComplexRRCMatchedFilterBlock: a true I/Q-in / I/Q-out block.
    The complex DUT driver (run_block_dut_complex) delivers each sample as a
    two-operand (xi, xq) transaction and de-interleaves the (yi, yq) it emits;
    the complex comparator gates BOTH channels against GNU Radio's fir_filter_ccf
    within the derived Q15 floor, and against the block's bit-exact reference.

  * LLR path — SoftDemodulatorBlock: BPSK soft (LLR) output. The LLR comparator
    gates on hard-decision SIGN agreement (the bit the FEC decoder acts on) plus a
    derived magnitude tolerance, against GNU Radio's BPSK constellation_soft_decoder.

Per INV-4, every gate is paired with a MANDATORY mutation that proves it FAILS on
a corrupted DUT (swapped I/Q, negated Q, +1 sample delay, wrong taps, flipped LLR
sign). A harness that cannot fail certifies nothing.

Run:
    KYTTAR_GR_PYTHON=/usr/bin/python3 QT_QPA_PLATFORM=offscreen \
      <venv>/python -m pytest verification/tests/test_complex_harness.py -x -q
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
    run_block_dut, run_block_dut_complex, run_gnuradio_ref,
    run_gnuradio_ref_complex, compare_complex_against_grc,
    compare_llr_against_grc, compare_against_grc, Metric)
from gr_kyttar.placement.blocks.complex_rrc_matched_filter_block import (  # noqa: E402
    ComplexRRCMatchedFilterBlock)

CHIP_YAML = str(_PLACEKYT / "resources" / "chips" / "kyttar_10x12.yaml")
_GR_AVAILABLE = os.path.exists(os.environ.get("KYTTAR_GR_PYTHON", "/usr/bin/python3"))
pytestmark = pytest.mark.skipif(
    not (os.path.exists(CHIP_YAML) and _GR_AVAILABLE),
    reason="chip yaml or GNU Radio interpreter absent")


def _s16(v):
    v = int(v) & 0xFFFF
    return v - 0x10000 if v >= 0x8000 else v


# =============================================================================
# COMPLEX path — ComplexRRCMatchedFilterBlock (I/Q in, I/Q out)
# =============================================================================

def _scaled_taps_float():
    """The block's EXACT on-chip taps (unit-energy sqrt-RRC, pre-scaled /2^S) as
    floats — the coefficients GNU Radio's fir_filter_ccf must use to be the same
    filter. The block stores these Q15-quantized; we read them straight off it."""
    b = ComplexRRCMatchedFilterBlock("ref")
    return [_s16(t) / 32768.0 for t in b.coeff_q15]


def _gr_complex_fir(stim, taps):
    # GNU Radio's fir_filter_ccf convolves with taps in latest-sample-first order
    # (the reverse of the on-chip coefficient order), so reverse them.
    return run_gnuradio_ref_complex(
        stim,
        gnuradio_script="""
from gnuradio import gr, blocks, filter as gr_filter
tb = gr.top_block()
src = blocks.vector_source_c(input_complex, False)
fir = gr_filter.fir_filter_ccf(1, taps)
sink = blocks.vector_sink_c()
tb.connect(src, fir); tb.connect(fir, sink)
tb.run()
output_complex = list(sink.data())
""",
        extra_args={"taps": list(reversed(taps))})


def _block_ref_iq(stim):
    """The block's bit-exact COEFFICIENT-HEADROOM Q15 reference, as float I/Q
    channels (so they feed straight into the complex comparator, which re-quantizes
    to Q15). Models the exact on-chip wrapping MACQ datapath."""
    b = ComplexRRCMatchedFilterBlock("ref")

    def fq(f):
        return _s16(int(round(max(-1.0, min(0.999, f)) * 32768.0)) & 0xFFFF)

    qstim = np.array([complex(fq(s.real), fq(s.imag)) for s in stim])
    ref = b.process_reference(qstim)
    ri = [_s16(int(yi)) / 32768.0 for yi, yq in ref]
    rq = [_s16(int(yq)) / 32768.0 for yi, yq in ref]
    return ri, rq


def _complex_stim(seed, n, amp=0.6):
    rng = random.Random(seed)
    return [complex(rng.uniform(-amp, amp), rng.uniform(-amp, amp))
            for _ in range(n)]


def _run_complex_dut(stim):
    dut = run_block_dut_complex(
        "ComplexRRCMatchedFilterBlock", stim, params={}, chip_yaml=CHIP_YAML,
        in_ports=("xi", "xq"), words_per_sample=2)
    assert dut.ok, dut.reason
    assert dut.words_per_sample == 2, (
        f"complex output should be 2 words/sample, got {dut.words_per_sample}")
    return dut


def test_complex_dut_drives_and_captures():
    """Smoke + structure: the complex driver builds the block, drives an I/Q
    burst as two-operand transactions, derives a placement-dependent hop (INV-1),
    resolves the two input registers with params (INV-6), and de-interleaves a
    (yi, yq) output. No swallowed failures."""
    stim = _complex_stim(seed=1, n=24)
    dut = _run_complex_dut(stim)
    print(f"\ncomplex DUT: hop={dut.hop_count} entry={dut.entry_addr} "
          f"in_regs={dut.in_regs} words={dut.n_words} "
          f"wps={dut.words_per_sample}")
    assert dut.in_regs == (0, 1), "complex landing cell should take xi@R0, xq@R1"
    assert len(dut.i_q15) == len(stim) and len(dut.q_q15) == len(stim)
    assert all(v is not None for v in dut.i_q15), "I channel has a missing egress"
    assert all(v is not None for v in dut.q_q15), "Q channel has a missing egress"


def test_complex_matches_gnuradio_fir_ccf():
    """The DSP-equivalence claim: the complex DUT is a drop-in for GNU Radio's
    fir_filter_ccf. Drive a modest-amplitude I/Q burst (no accumulator wrap, so
    the on-chip saturating datapath == GR float clipped), compare BOTH I and Q
    channels to GR within the derived 17-tap Q15 floor."""
    taps = _scaled_taps_float()
    stim = _complex_stim(seed=7, n=48, amp=0.5)
    dut = _run_complex_dut(stim)
    gr = _gr_complex_fir(stim, taps)
    assert gr.is_complex
    # Both rails are length-17 linear-phase FIRs; on-chip the filter runs
    # continuously and is NOT trimmed, matching fir_filter_ccf at delay 0.
    res = compare_complex_against_grc(
        dut.i_q15, dut.q_q15, gr.i, gr.q,
        metric=Metric.AMPLITUDE, delay=0, op_count=len(taps))
    print("\ncomplex vs GR:", res.summary())
    assert res.passed, res.summary()


def test_complex_bitexact_reference():
    """The substrate claim: the DUT matches the block's bit-exact Q15 reference
    EXACTLY on both channels (the on-chip wrapping MACQ datapath), driven with a
    long random burst so every cell of both 5-cell rails is exercised (INV-12)."""
    stim = _complex_stim(seed=42, n=60, amp=0.6)
    dut = _run_complex_dut(stim)
    ri, rq = _block_ref_iq(stim)
    res = compare_complex_against_grc(
        dut.i_q15, dut.q_q15, ri, rq, metric=Metric.EXACT, delay=0)
    print("\ncomplex bit-exact:", res.summary())
    assert res.passed, res.summary()


# --- MANDATORY complex mutation tests (the gate must DETECT these) ------------

def test_complex_mutation_swapped_iq_fails():
    """Swapping the I and Q channels must FAIL — the canonical complex bug an
    I-only check would miss."""
    taps = _scaled_taps_float()
    stim = _complex_stim(seed=7, n=48, amp=0.5)
    dut = _run_complex_dut(stim)
    gr = _gr_complex_fir(stim, taps)
    res = compare_complex_against_grc(
        dut.q_q15, dut.i_q15, gr.i, gr.q,   # I and Q swapped
        metric=Metric.AMPLITUDE, delay=0, op_count=len(taps))
    assert not res.passed, "gate failed to detect swapped I/Q!"


def test_complex_mutation_negated_q_fails():
    """Negating only the Q channel must FAIL (a conjugation / Q-sign bug)."""
    taps = _scaled_taps_float()
    stim = _complex_stim(seed=7, n=48, amp=0.5)
    dut = _run_complex_dut(stim)
    gr = _gr_complex_fir(stim, taps)
    neg_q = [(0x10000 - (w or 0)) & 0xFFFF for w in dut.q_q15]
    res = compare_complex_against_grc(
        dut.i_q15, neg_q, gr.i, gr.q,
        metric=Metric.AMPLITUDE, delay=0, op_count=len(taps))
    assert not res.passed, "gate failed to detect a negated Q channel!"


def test_complex_mutation_one_sample_offset_fails():
    """A +1-sample delay on both channels must FAIL when delay=0 is asserted."""
    taps = _scaled_taps_float()
    stim = _complex_stim(seed=7, n=48, amp=0.5)
    dut = _run_complex_dut(stim)
    gr = _gr_complex_fir(stim, taps)
    sh_i = [0x0000] + list(dut.i_q15[:-1])
    sh_q = [0x0000] + list(dut.q_q15[:-1])
    res = compare_complex_against_grc(
        sh_i, sh_q, gr.i, gr.q,
        metric=Metric.AMPLITUDE, delay=0, op_count=len(taps))
    assert not res.passed, "gate failed to detect a 1-sample complex latency error!"


def test_complex_mutation_wrong_taps_fails():
    """The DUT compared against GR built with DIFFERENT taps must FAIL."""
    stim = _complex_stim(seed=7, n=48, amp=0.5)
    dut = _run_complex_dut(stim)
    # A clearly different filter (a short box) — not the RRC.
    wrong = [0.2, 0.2, 0.2, 0.2, 0.2]
    gr = _gr_complex_fir(stim, wrong)
    res = compare_complex_against_grc(
        dut.i_q15, dut.q_q15, gr.i, gr.q,
        metric=Metric.AMPLITUDE, delay=0, op_count=len(wrong))
    assert not res.passed, "gate failed to detect a wrong-filter mismatch!"


def test_complex_empty_output_fails():
    """An empty DUT output is a hard fail on a complex comparison too."""
    taps = _scaled_taps_float()
    stim = _complex_stim(seed=7, n=8, amp=0.5)
    gr = _gr_complex_fir(stim, taps)
    res = compare_complex_against_grc(
        [], [], gr.i, gr.q, metric=Metric.AMPLITUDE, delay=0, op_count=len(taps))
    assert not res.passed


# =============================================================================
# LLR path — SoftDemodulatorBlock (BPSK soft-decision output)
# =============================================================================
# GNU Radio's BPSK constellation_soft_decoder emits LLR = 4*I; the Kyttar block
# emits LLR = 0.5*I (its Q15 coefficient saturates to 0.5). The two share IDENTICAL
# signs (the hard decision); the scale maps via llr_scale = 0.5/4 = 0.125.
_LLR_SCALE = 0.5 / 4.0


def _gr_bpsk_llr(inputs_q15):
    """GNU Radio BPSK soft-decision LLR for an I stimulus (real, on the I axis)."""
    return run_gnuradio_ref(
        input_q15=inputs_q15,
        gnuradio_script="""
from gnuradio import gr, blocks, digital
con = digital.constellation_bpsk().base()
tb = gr.top_block()
src = blocks.vector_source_c([complex(v, 0.0) for v in input_float], False)
dec = digital.constellation_soft_decoder_cf(con)
sink = blocks.vector_sink_f()
tb.connect(src, dec); tb.connect(dec, sink)
tb.run()
output_float = list(sink.data())
""")


def _llr_stim(seed, n=24):
    """BPSK-ish I samples spread across both signs (avoid the |I|~0 boundary so the
    hard decision is unambiguous, per the comparator's dead-zone)."""
    rng = random.Random(seed)
    return [int(round(rng.choice([1, -1]) * rng.uniform(0.2, 0.9) * 32768))
            & 0xFFFF for _ in range(n)]


def _run_llr_dut(inputs_q15, noise_variance=0.1):
    # SoftDemodulatorBlock is REAL-I in (already-derotated I) -> real LLR out, so
    # it drives through the existing real DUT path; the LLR-aware COMPARATOR is the
    # new capability under test here.
    dut = run_block_dut("SoftDemodulatorBlock", inputs_q15,
                        params={"noise_variance": noise_variance},
                        chip_yaml=CHIP_YAML)
    assert dut.ok, dut.reason
    return dut


def test_llr_matches_gnuradio_soft_decoder():
    """The LLR DUT (BPSK soft demod) agrees with GNU Radio's soft decoder on the
    HARD DECISION (sign) for every confident sample, and its soft magnitude is
    within the derived Q15 tolerance after the LLR scale is applied."""
    stim = _llr_stim(seed=3)
    dut = _run_llr_dut(stim)
    gr = _gr_bpsk_llr(stim)
    res = compare_llr_against_grc(
        dut.outputs_q15, gr.floats, delay=0, llr_scale=_LLR_SCALE, op_count=1)
    print("\nllr vs GR:", res.summary())
    assert res.passed, res.summary()
    assert res.sign_mismatches == 0, "hard decisions must match GR exactly"


@pytest.mark.parametrize("seed", [11, 23, 31])
def test_llr_random_sign_agreement(seed):
    stim = _llr_stim(seed=seed)
    dut = _run_llr_dut(stim)
    gr = _gr_bpsk_llr(stim)
    res = compare_llr_against_grc(
        dut.outputs_q15, gr.floats, delay=0, llr_scale=_LLR_SCALE, op_count=1)
    print(f"\nllr seed={seed}:", res.summary())
    assert res.passed, res.summary()


# --- MANDATORY LLR mutation tests (the gate must DETECT these) ----------------

def test_llr_mutation_flipped_sign_fails():
    """Flipping the sign of every DUT LLR is a wrong-bit corruption — the
    sign-agreement gate MUST fail (a magnitude-only gate would pass it, since |LLR|
    is unchanged). This is the heart of the LLR metric."""
    stim = _llr_stim(seed=3)
    dut = _run_llr_dut(stim)
    gr = _gr_bpsk_llr(stim)
    flipped = [(0x10000 - (w or 0)) & 0xFFFF for w in dut.outputs_q15]  # negate LLR
    res = compare_llr_against_grc(
        flipped, gr.floats, delay=0, llr_scale=_LLR_SCALE, op_count=1)
    assert not res.passed, "gate failed to detect flipped LLR signs (wrong bits)!"
    assert res.sign_mismatches > 0


def test_llr_mutation_one_sample_offset_fails():
    """A +1-sample LLR latency must FAIL when delay=0 is asserted (the shifted bit
    decisions no longer line up with GR's)."""
    stim = _llr_stim(seed=3)
    dut = _run_llr_dut(stim)
    gr = _gr_bpsk_llr(stim)
    shifted = [0x0000] + list(dut.outputs_q15[:-1])
    res = compare_llr_against_grc(
        shifted, gr.floats, delay=0, llr_scale=_LLR_SCALE, op_count=1)
    assert not res.passed, "gate failed to detect a 1-sample LLR latency error!"


def test_llr_empty_output_fails():
    """An empty LLR output is a hard fail."""
    stim = _llr_stim(seed=3)
    gr = _gr_bpsk_llr(stim)
    res = compare_llr_against_grc([], gr.floats, delay=0, llr_scale=_LLR_SCALE)
    assert not res.passed


def test_llr_sign_dead_zone_excludes_boundary():
    """A magnitude-tolerance check the SIGN gate must NOT punish: a sample sitting
    on the decision boundary (ref LLR ~ 0) whose DUT sign happens to flip is
    quantization-benign and is excluded by the dead zone — proving the gate is not
    spuriously strict at |LLR|~0. Constructed directly (not via the chip): one
    boundary sample with a flipped DUT sign must still pass."""
    # ref LLRs: one near zero, the rest strongly signed and sign-agreeing.
    ref = [0.001, 0.5, -0.5, 0.4, -0.4, 0.6]
    # DUT (already scaled to the 0.5 range): flip ONLY the boundary sample's sign.
    dut = [(-0.001 * _LLR_SCALE), 0.5 * _LLR_SCALE, -0.5 * _LLR_SCALE,
           0.4 * _LLR_SCALE, -0.4 * _LLR_SCALE, 0.6 * _LLR_SCALE]
    dut_q15 = [int(round(v * 32768.0)) & 0xFFFF for v in dut]
    # An explicit small tolerance: the boundary sample's sign-flipped magnitude
    # differs by a few LSB (|±4| -> 8 LSB), which is the quantization noise the
    # dead zone is there to forgive. This test isolates the SIGN exclusion — the
    # magnitude gate is given a realistic near-zero tolerance.
    res = compare_llr_against_grc(
        dut_q15, ref, delay=0, llr_scale=_LLR_SCALE, tolerance=16,
        sign_dead_zone=0.02)
    assert res.passed, (
        f"boundary-only sign flip should be excluded by the dead zone: "
        f"{res.summary()}")
    assert res.sign_mismatches == 0
