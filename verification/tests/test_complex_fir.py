# SPDX-License-Identifier: GPL-3.0-or-later
"""ComplexFIRFilterBlock / ComplexLowPassFilter vs GNU Radio ``fir_filter_ccf``.

The complex FIR is the fabric-native complex filter stage: complex I/Q in, complex
I/Q out, ONE shared real tap set filtering each rail independently — exactly GNU
Radio's ``filter.fir_filter_ccf``. It collapses the ``complex_to_float → 2×
fir_filter_fff → float_to_complex`` idiom into a single block so a
``ComplexMixer → ComplexLowPass → IQUpconvert`` chain is pure same-source complex
packets (no fan-out, no reconvergent two-source fan-in).

This suite gates BOTH the generic ComplexFIRFilterBlock (explicit taps) and the
ComplexLowPassFilter firdes wrapper against ``fir_filter_ccf``, across the tap
counts the SSB Weaver needs (single cell up to the 16-cell 31-tap fold). Per
INV-4, every gate is paired with a MANDATORY mutation proving it FAILS on a
corrupted DUT (swapped I/Q, negated Q, wrong taps, 1-sample offset).

Run:
    KYTTAR_GR_PYTHON=/usr/bin/python3 QT_QPA_PLATFORM=offscreen \
      <venv>/python -m pytest verification/tests/test_complex_fir.py -x -q
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
    run_block_dut_complex, run_gnuradio_ref_complex,
    compare_complex_against_grc, Metric)
from gr_kyttar.placement.blocks.complex_fir_filter_block import (  # noqa: E402
    ComplexFIRFilterBlock)
from gr_kyttar.placement.blocks.complex_low_pass_filter_block import (  # noqa: E402
    ComplexLowPassFilter)
from gr_kyttar.placement.blocks.complex_high_pass_filter_block import (  # noqa: E402
    ComplexHighPassFilter)
from gr_kyttar.placement.blocks.complex_band_pass_filter_block import (  # noqa: E402
    ComplexBandPassFilter)
from gr_kyttar.placement.blocks.complex_band_reject_filter_block import (  # noqa: E402
    ComplexBandRejectFilter)
from gr_kyttar.placement.blocks import _firdes  # noqa: E402

CHIP_YAML = str(_PLACEKYT / "resources" / "chips" / "kyttar_10x12.yaml")
_GR_AVAILABLE = os.path.exists(os.environ.get("KYTTAR_GR_PYTHON", "/usr/bin/python3"))
pytestmark = pytest.mark.skipif(
    not (os.path.exists(CHIP_YAML) and _GR_AVAILABLE),
    reason="chip yaml or GNU Radio interpreter absent")


def _complex_stim(seed, n, amp=0.5):
    rng = random.Random(seed)
    return [complex(rng.uniform(-amp, amp), rng.uniform(-amp, amp))
            for _ in range(n)]


def _gr_complex_fir(stim, taps):
    # fir_filter_ccf convolves latest-sample-first (the reverse of the on-chip
    # coefficient order). firdes low-pass taps are linear-phase SYMMETRIC so the
    # reversal is a no-op, but we reverse for generality/explicit-tap cases.
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


def _run_dut(class_name, stim, params):
    dut = run_block_dut_complex(
        class_name, stim, params=params, chip_yaml=CHIP_YAML,
        in_ports=("xi", "xq"), words_per_sample=2)
    assert dut.ok, dut.reason
    assert dut.words_per_sample == 2, (
        f"complex output should be 2 words/sample, got {dut.words_per_sample}")
    return dut


# =============================================================================
# Generic ComplexFIRFilterBlock (explicit taps) vs fir_filter_ccf
# =============================================================================

@pytest.mark.parametrize("transition_width,expect_cells", [
    (20000.0, 1),   # 3 taps, single cell
    (8000.0, 5),    # 9 taps
    (2500.0, 16),   # 31 taps (the SSB Weaver LPF)
])
def test_complex_fir_matches_grc(transition_width, expect_cells):
    # gain 0.9 keeps Sigma|h|<=1 (S=0) so the multi-cell fold fits the budget.
    taps = _firdes.low_pass(0.9, 32000.0, 1200.0, transition_width, "hamming", 6.76)
    blk = ComplexFIRFilterBlock("cfir", taps)
    assert blk.cell_count == expect_cells
    stim = _complex_stim(seed=7, n=48, amp=0.5)
    dut = _run_dut("ComplexFIRFilterBlock", stim, {"coefficients": taps})
    gr = _gr_complex_fir(stim, taps)
    assert gr.is_complex
    res = compare_complex_against_grc(
        dut.i_q15, dut.q_q15, gr.i, gr.q,
        metric=Metric.AMPLITUDE, delay=0, op_count=len(taps))
    print(f"\ncfir tw={transition_width} ({len(taps)} taps, {expect_cells} "
          f"cells) vs GR: {res.summary()}")
    assert res.passed, res.summary()


# =============================================================================
# ComplexLowPassFilter firdes wrapper vs fir_filter_ccf(firdes.low_pass)
# =============================================================================

_LPF_PARAMS = dict(gain=0.9, samp_rate=32000.0, cutoff_freq=1200.0,
                   transition_width=2500.0)


def _lpf_taps():
    return list(ComplexLowPassFilter("ref", **_LPF_PARAMS).design_taps)


def test_complex_lowpass_matches_grc():
    """The Weaver baseband LPF: 31-tap firdes low-pass on both rails, matched to
    GNU Radio fir_filter_ccf fed the same firdes taps."""
    taps = _lpf_taps()
    stim = _complex_stim(seed=11, n=48, amp=0.5)
    dut = _run_dut("ComplexLowPassFilter", stim, _LPF_PARAMS)
    gr = _gr_complex_fir(stim, taps)
    res = compare_complex_against_grc(
        dut.i_q15, dut.q_q15, gr.i, gr.q,
        metric=Metric.AMPLITUDE, delay=0, op_count=len(taps))
    print("\ncomplex LPF vs GR:", res.summary())
    assert res.passed, res.summary()


# =============================================================================
# The band-shape wrappers (High/Band-pass, Band-reject) vs fir_filter_ccf
# =============================================================================
# GNU Radio wraps ONE generic FIR core (fir_filter_ccf) with firdes-tap wrappers
# for each band shape; we mirror that. Gains keep Σ|h|<=1 so the multi-cell FIR
# fits (a band filter at gain=1.0 can exceed the budget — see the guard test).

_BAND_CASES = [
    ("ComplexHighPassFilter", ComplexHighPassFilter,
     dict(gain=0.5, samp_rate=32000.0, cutoff_freq=4000.0,
          transition_width=3000.0)),
    ("ComplexBandPassFilter", ComplexBandPassFilter,
     dict(gain=0.6, samp_rate=32000.0, low_cutoff_freq=3000.0,
          high_cutoff_freq=8000.0, transition_width=3000.0)),
    ("ComplexBandRejectFilter", ComplexBandRejectFilter,
     dict(gain=0.4, samp_rate=32000.0, low_cutoff_freq=3000.0,
          high_cutoff_freq=8000.0, transition_width=3000.0)),
]


@pytest.mark.parametrize("name,cls,params", _BAND_CASES,
                         ids=[c[0] for c in _BAND_CASES])
def test_complex_band_filter_matches_grc(name, cls, params):
    """Each complex band-shape wrapper's firdes taps run on both I/Q rails match
    GNU Radio fir_filter_ccf fed the same taps."""
    taps = list(cls("ref", **params).design_taps)
    stim = _complex_stim(seed=13, n=48, amp=0.4)
    dut = _run_dut(name, stim, params)
    gr = _gr_complex_fir(stim, taps)
    res = compare_complex_against_grc(
        dut.i_q15, dut.q_q15, gr.i, gr.q,
        metric=Metric.AMPLITUDE, delay=0, op_count=len(taps))
    print(f"\n{name} vs GR:", res.summary())
    assert res.passed, res.summary()


# --- MANDATORY mutation tests (the gate MUST detect these) --------------------

def test_mutation_swapped_iq_fails():
    taps = _lpf_taps()
    stim = _complex_stim(seed=11, n=48, amp=0.5)
    dut = _run_dut("ComplexLowPassFilter", stim, _LPF_PARAMS)
    gr = _gr_complex_fir(stim, taps)
    res = compare_complex_against_grc(
        dut.q_q15, dut.i_q15, gr.i, gr.q,   # I/Q swapped
        metric=Metric.AMPLITUDE, delay=0, op_count=len(taps))
    assert not res.passed, "gate failed to detect swapped I/Q!"


def test_mutation_negated_q_fails():
    taps = _lpf_taps()
    stim = _complex_stim(seed=11, n=48, amp=0.5)
    dut = _run_dut("ComplexLowPassFilter", stim, _LPF_PARAMS)
    gr = _gr_complex_fir(stim, taps)
    neg_q = [(0x10000 - (w or 0)) & 0xFFFF for w in dut.q_q15]
    res = compare_complex_against_grc(
        dut.i_q15, neg_q, gr.i, gr.q,
        metric=Metric.AMPLITUDE, delay=0, op_count=len(taps))
    assert not res.passed, "gate failed to detect a negated Q channel!"


def test_mutation_wrong_taps_fails():
    stim = _complex_stim(seed=11, n=48, amp=0.5)
    dut = _run_dut("ComplexLowPassFilter", stim, _LPF_PARAMS)
    wrong = [0.2, 0.2, 0.2, 0.2, 0.2]   # a short box, not the firdes LPF
    gr = _gr_complex_fir(stim, wrong)
    res = compare_complex_against_grc(
        dut.i_q15, dut.q_q15, gr.i, gr.q,
        metric=Metric.AMPLITUDE, delay=0, op_count=len(wrong))
    assert not res.passed, "gate failed to detect a wrong-filter mismatch!"


def test_mutation_one_sample_offset_fails():
    taps = _lpf_taps()
    stim = _complex_stim(seed=11, n=48, amp=0.5)
    dut = _run_dut("ComplexLowPassFilter", stim, _LPF_PARAMS)
    gr = _gr_complex_fir(stim, taps)
    sh_i = [0x0000] + list(dut.i_q15[:-1])
    sh_q = [0x0000] + list(dut.q_q15[:-1])
    res = compare_complex_against_grc(
        sh_i, sh_q, gr.i, gr.q,
        metric=Metric.AMPLITUDE, delay=0, op_count=len(taps))
    assert not res.passed, "gate failed to detect a 1-sample complex latency error!"


# --- The multi-cell Sigma|h|>1 guard ------------------------------------------

def test_multicell_sum_gt_one_rejected():
    """A gain=1.0 (Sigma|h|>1) multi-cell low-pass must be REFUSED at block-verify
    time — the last cell's dual saturating restore would overflow 32 words."""
    hot = ComplexLowPassFilter(
        "hot", gain=1.0, samp_rate=32000.0, cutoff_freq=1200.0,
        transition_width=2500.0)
    assert hot.cell_count > 1
    assert hot._head_shift > 0
    with pytest.raises(ValueError, match=r"Σ|head_shift|overflow"):
        hot.build_cell_programs()
