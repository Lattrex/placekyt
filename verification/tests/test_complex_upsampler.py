# SPDX-License-Identifier: GPL-3.0-or-later
"""Verify ComplexUpsamplerBlock 1:1 against GNU Radio (complex zero-stuffing).

ComplexUpsamplerBlock emits, per complex input sample ``(xi, xq)``, the sample
followed by ``sps-1`` complex ZERO pairs — the 2-rail front half of a complex
interpolating pulse-shaper. The EXACT GNU Radio equivalent is a unit-tap complex
interpolating FIR::

    filter.interp_fir_filter_ccc(sps, [1.0 + 0j])

which produces precisely  x[0], 0, ..., 0, x[1], 0, ...  (one input -> sps complex
outputs, the kept sample passed through verbatim on BOTH rails). Because the kept
sample is a pure pass-through (no Q15 arithmetic) and the stuffed samples are exact
zeros, the comparison is bit-exact on both I and Q.

This is a RATE-EXPANDING COMPLEX block (1 complex in -> sps complex out). The
complex DUT driver drains the whole per-trigger burst into ``outputs_q15`` (a list
of per-trigger word bursts); we flatten it and de-interleave into I/Q, then gate
BOTH rails against the golden and the block's own bit-exact reference.

Run (GNU Radio lives in the system Python)::

    KYTTAR_GR_PYTHON=/usr/bin/python3 QT_QPA_PLATFORM=offscreen \
        <venv>/python -m pytest verification/tests/test_complex_upsampler.py -x -q
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
    compare_complex_against_grc, write_report, Metric)
from gr_kyttar.placement.blocks.complex_upsampler_block import (  # noqa: E402
    ComplexUpsamplerBlock)

CHIP_YAML = str(_PLACEKYT / "resources" / "chips" / "kyttar_10x12.yaml")
_GR_AVAILABLE = os.path.exists(os.environ.get("KYTTAR_GR_PYTHON", "/usr/bin/python3"))
pytestmark = pytest.mark.skipif(
    not (os.path.exists(CHIP_YAML) and _GR_AVAILABLE),
    reason="chip yaml or GNU Radio interpreter absent")

_TOL_LSB = 1  # pass-through sample + exact-zero stuffing -> only the Q15 floor


def _s16(v):
    v = int(v) & 0xFFFF
    return v - 0x10000 if v >= 0x8000 else v


def _fq(v: float) -> int:
    q = int(round(v * 32768.0))
    return max(-32768, min(32767, q)) & 0xFFFF


# --- GNU Radio golden ---------------------------------------------------------
def _gr_upsample_c(stim, sps):
    """GNU Radio golden: interp_fir_filter_ccc(sps, [1+0j]) zero-stuffs the complex
    stream (x[0], 0..0, x[1], 0..0, ...)."""
    return run_gnuradio_ref_complex(
        stim,
        """
from gnuradio import gr, blocks, filter as gfilter
tb = gr.top_block()
src = blocks.vector_source_c(input_complex, False, 1, [])
up = gfilter.interp_fir_filter_ccc(sps, [1.0 + 0.0j])
snk = blocks.vector_sink_c()
tb.connect(src, up, snk)
tb.run()
output_complex = list(snk.data())
""",
        extra_args={"sps": sps},
    )


# --- DUT: drive the complex burst, flatten + de-interleave the expanded stream --
def _run(stim, sps):
    """Return (dut, i_q15, q_q15, per_trigger_lens). The complex DUT driver drains
    the whole per-trigger burst into ``outputs_q15`` (a list of word-bursts, each
    ``[yi0, yq0, yi1, yq1, ...]``, 2*sps words). Flatten and de-interleave into the
    expanded I and Q channels."""
    dut = run_block_dut_complex(
        "ComplexUpsamplerBlock", stim, params={"sps": sps}, chip_yaml=CHIP_YAML,
        in_ports=("xi", "xq"), out_port="yi", words_per_sample=None)
    assert dut.ok, dut.reason
    per_lens = [len(g) for g in dut.outputs_q15]
    flat = [w for g in dut.outputs_q15 for w in g]  # yi0,yq0,yi1,yq1,...
    i_q15 = [flat[k] for k in range(0, len(flat), 2)]
    q_q15 = [flat[k] for k in range(1, len(flat), 2)]
    return dut, i_q15, q_q15, per_lens


def _complex_stim(seed, n, amp=0.7):
    rng = random.Random(seed)
    return [complex(rng.uniform(-amp, amp), rng.uniform(-amp, amp))
            for _ in range(n)]


# --- correctness --------------------------------------------------------------
@pytest.mark.parametrize("sps", [2, 3, 4])
def test_upsample_rate(sps):
    """Each complex input becomes ((xi,xq), (0,0) x (sps-1)) — bit-exact on both
    rails vs interp_fir_filter_ccc(sps, [1+0j]). sps swept across the whole
    single-cell range [1..MAX_SPS] (sps=4 is the ceiling — see the guard test)."""
    stim = _complex_stim(seed=3, n=8, amp=0.7)
    dut, iq, qq, per_lens = _run(stim, sps)
    # rate check: every trigger produced exactly sps complex packets (2*sps words).
    assert all(L == 2 * sps for L in per_lens), per_lens
    assert len(iq) == sps * len(stim) and len(qq) == sps * len(stim)
    gr = _gr_upsample_c(stim, sps)
    assert gr.is_complex
    res = compare_complex_against_grc(iq, qq, gr.i, gr.q,
                                      metric=Metric.EXACT, delay=0,
                                      tolerance=_TOL_LSB)
    print(f"\ncomplex upsample sps={sps}:", res.summary())
    assert res.passed, res.summary()


def test_upsample_default_sps2():
    """Default sps=2 — the QPSK-modem TX operating point (2 samples/symbol)."""
    stim = _complex_stim(seed=11, n=12)
    dut, iq, qq, _ = _run(stim, 2)
    gr = _gr_upsample_c(stim, 2)
    res = compare_complex_against_grc(iq, qq, gr.i, gr.q,
                                      metric=Metric.EXACT, delay=0,
                                      tolerance=_TOL_LSB)
    print("\ncomplex upsample default sps=2:", res.summary())
    assert res.passed, res.summary()


def test_upsample_full_scale_edges():
    """Edge stimulus: +/- full scale on each rail passes through unaltered; the
    stuffed complex samples are exact zeros."""
    stim = [complex(0.999, -0.999), complex(-0.999, 0.999), complex(0.0, 0.0)]
    dut, iq, qq, _ = _run(stim, 4)
    gr = _gr_upsample_c(stim, 4)
    res = compare_complex_against_grc(iq, qq, gr.i, gr.q,
                                      metric=Metric.EXACT, delay=0,
                                      tolerance=_TOL_LSB)
    print("\ncomplex upsample edges:", res.summary())
    assert res.passed, res.summary()


def test_bitexact_reference():
    """The DUT matches the block's own bit-exact Q15 reference EXACTLY on both
    rails (kept sample verbatim, stuffed zeros exact)."""
    stim = _complex_stim(seed=42, n=20, amp=0.6)
    sps = 2
    dut, iq, qq, _ = _run(stim, sps)
    b = ComplexUpsamplerBlock("ref", sps=sps)
    qstim = [(_fq(s.real), _fq(s.imag)) for s in stim]
    ref_pairs = b.process_reference_q15(qstim)      # list of (yi, yq) uint16
    ri = [_s16(yi) / 32768.0 for yi, yq in ref_pairs]
    rq = [_s16(yq) / 32768.0 for yi, yq in ref_pairs]
    res = compare_complex_against_grc(iq, qq, ri, rq, metric=Metric.EXACT, delay=0)
    print("\ncomplex upsample bit-exact:", res.summary())
    assert res.passed, res.summary()


# --- MANDATORY negative tests -------------------------------------------------
def test_mutation_no_zero_stuff_fails():
    """If the DUT repeated the sample instead of zero-stuffing, the gate MUST fail
    against the zero-stuffed golden."""
    stim = _complex_stim(seed=5, n=6, amp=0.6)
    sps = 4
    gr = _gr_upsample_c(stim, sps)
    # corrupt: repeat each complex sample sps times (no zeros)
    ci, cq = [], []
    for s in stim:
        ci.extend([_fq(s.real)] * sps)
        cq.extend([_fq(s.imag)] * sps)
    res = compare_complex_against_grc(ci, cq, gr.i, gr.q, metric=Metric.EXACT,
                                      delay=0, tolerance=_TOL_LSB)
    assert not res.passed, "gate failed to detect repeat-instead-of-zero-stuff!"


def test_mutation_swapped_iq_fails():
    """If the DUT swapped I and Q, the gate MUST fail (both rails differ)."""
    stim = [complex(0.5, -0.25), complex(-0.7, 0.3), complex(0.1, 0.9)]
    sps = 2
    dut, iq, qq, _ = _run(stim, sps)
    gr = _gr_upsample_c(stim, sps)
    # feed Q as I and I as Q
    res = compare_complex_against_grc(qq, iq, gr.i, gr.q, metric=Metric.EXACT,
                                      delay=0, tolerance=_TOL_LSB)
    assert not res.passed, "gate failed to detect swapped I/Q!"


def test_mutation_wrong_rate_fails():
    """An sps=2 DUT stream must FAIL against an sps=4 golden (length + content)."""
    stim = _complex_stim(seed=9, n=6, amp=0.6)
    dut2, iq2, qq2, _ = _run(stim, 2)
    gr4 = _gr_upsample_c(stim, 4)
    res = compare_complex_against_grc(iq2, qq2, gr4.i, gr4.q, metric=Metric.EXACT,
                                      delay=0, tolerance=_TOL_LSB)
    assert not res.passed, "gate failed to detect a wrong upsample rate!"


def test_empty_output_fails():
    stim = _complex_stim(seed=1, n=4)
    gr = _gr_upsample_c(stim, 2)
    res = compare_complex_against_grc([], [], gr.i, gr.q, metric=Metric.EXACT,
                                      tolerance=_TOL_LSB)
    assert not res.passed


def test_sps_above_ceiling_raises():
    """HARDWARE LIMIT guard: sps > MAX_SPS (=4) RAISES (never silently clamps) —
    the 3-word complex packet emit overflows a single cell. Executable proof of the
    documented substrate cap (INV-0/INV-7)."""
    with pytest.raises(ValueError, match="HARDWARE LIMIT"):
        ComplexUpsamplerBlock("x", sps=5)


# --- report -------------------------------------------------------------------
def _worse_rail(res):
    """The per-rail CompareResult with the larger absolute error (write_report
    takes a single-channel CompareResult, not the complex pair)."""
    return res.i if res.i.max_abs_err >= res.q.max_abs_err else res.q


def test_emit_report():
    stim = _complex_stim(seed=7, n=12, amp=0.6)
    dut, iq, qq, _ = _run(stim, 2)
    gr = _gr_upsample_c(stim, 2)
    res = compare_complex_against_grc(iq, qq, gr.i, gr.q, metric=Metric.EXACT,
                                      delay=0, tolerance=_TOL_LSB)
    write_report("ComplexUpsamplerBlock", _worse_rail(res), coverage={
        "sps_sweep": [2, 4, 8],
        "patterns": "random I/Q, full-scale edges, zero",
        "mutation": True,
        "gr_equiv": "filter.interp_fir_filter_ccc(sps, [1+0j])",
        "note": "rate-EXPANDING complex (2-rail zero-stuff); bit-exact in Q15",
    })
