# SPDX-License-Identifier: GPL-3.0-or-later
"""Verify UpsamplerBlock 1:1 against GNU Radio (zero-stuffing rate expander).

UpsamplerBlock emits, per input sample, the sample followed by ``sps-1`` zeros —
the front half of an interpolating pulse-shaper. The EXACT GNU Radio equivalent is
a unit-tap interpolating FIR::

    filter.interp_fir_filter_fff(sps, [1.0])

which produces precisely  x[0], 0, ..., 0, x[1], 0, ...  (one input -> sps outputs,
the kept sample passed through verbatim). Because the kept sample is a pure
pass-through (no Q15 arithmetic) and the stuffed samples are exact zeros, the
comparison is bit-exact.

This is a RATE-EXPANDING block (1 in -> sps out), so it uses ``run_block_dut_rate``,
which drains the whole per-trigger burst (the plain ``run_block_dut`` keeps only the
last word and would collapse the burst).

Run (GNU Radio lives in the system Python)::

    cd verification
    KYTTAR_GR_PYTHON=/usr/bin/python3 QT_QPA_PLATFORM=offscreen \
        .venv/bin/python -m pytest tests/test_upsampler.py -v
"""
from __future__ import annotations

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
    run_block_dut_rate, run_gnuradio_ref, compare_against_grc, write_report,
    Metric)

CHIP_YAML = str(_PLACEKYT / "resources" / "chips" / "kyttar_10x12.yaml")

_GR_AVAILABLE = os.path.exists(os.environ.get("KYTTAR_GR_PYTHON", "/usr/bin/python3"))
pytestmark = pytest.mark.skipif(
    not _GR_AVAILABLE, reason="GNU Radio interpreter not available")

_TOL_LSB = 1  # pass-through sample + exact-zero stuffing -> only the Q15 floor


def _fq(v: float) -> int:
    q = int(round(v * 32768.0))
    return max(-32768, min(32767, q)) & 0xFFFF


def _gr_upsample(inq: list[int], sps: int):
    """GNU Radio golden: interp_fir_filter_fff(sps, [1.0]) zero-stuffs the stream."""
    return run_gnuradio_ref(
        inq,
        """
from gnuradio import gr, blocks, filter as gfilter

tb = gr.top_block()
src = blocks.vector_source_f(input_float, False, 1, [])
up = gfilter.interp_fir_filter_fff(sps, [1.0])
snk = blocks.vector_sink_f()
tb.connect(src, up, snk)
tb.run()
output_float = list(snk.data())
""",
        extra_args={"sps": sps},
    )


def _run(samples, sps):
    inq = [_fq(v) for v in samples]
    dut = run_block_dut_rate("UpsamplerBlock", inq, params={"sps": sps},
                             chip_yaml=CHIP_YAML, in_port="x", out_port="out")
    assert dut.ok, dut.reason
    ref = _gr_upsample(inq, sps)
    res = compare_against_grc(dut.outputs_q15, ref.floats, metric=Metric.EXACT,
                              delay=0, tolerance=_TOL_LSB)
    return dut, res


# --- correctness ---------------------------------------------------------------

@pytest.mark.parametrize("sps", [2, 4, 8])
def test_upsample_rate(sps):
    """Each input becomes (sample, 0 x (sps-1)) — bit-exact vs interp_fir [1.0]."""
    samples = [0.5, -0.5, 0.25, -0.75, 0.9, -0.1]
    dut, res = _run(samples, sps)
    # rate check: every trigger produced exactly sps words.
    assert all(len(t) == sps for t in dut.per_trigger), \
        [len(t) for t in dut.per_trigger]
    assert len(dut.outputs_q15) == sps * len(samples)
    print(f"\nupsample sps={sps}:", res.summary(), "| words", dut.n_words)
    assert res.passed, res.summary()


def test_upsample_default_sps4():
    """Default sps=4 matches the RRC pulse shaper's SAMPLES_PER_SYMBOL."""
    dut, res = _run([0.5, -0.5, 1.0, -1.0], 4)
    print("\nupsample default:", res.summary())
    assert res.passed, res.summary()


def test_upsample_full_scale_edges():
    """Edge stimulus: +/- full scale passes through unaltered, zeros are exact."""
    dut, res = _run([0.999, -0.999, 0.0], 4)
    print("\nupsample edges:", res.summary())
    assert res.passed, res.summary()


# --- MANDATORY negative tests --------------------------------------------------

def test_mutation_no_zero_stuff_fails():
    """If the DUT failed to insert zeros (repeated the sample instead), the gate
    MUST fail against the zero-stuffed golden."""
    samples = [0.5, -0.5, 0.25, -0.75]
    sps = 4
    inq = [_fq(v) for v in samples]
    # Build a 'repeat' stream (sample duplicated sps times) as the corruption.
    corrupt = []
    for w in inq:
        corrupt.extend([w] * sps)
    ref = _gr_upsample(inq, sps)
    res = compare_against_grc(corrupt, ref.floats, metric=Metric.EXACT,
                              delay=0, tolerance=_TOL_LSB)
    assert not res.passed, "gate failed to detect a repeat-instead-of-zero-stuff!"


def test_mutation_wrong_rate_fails():
    """An sps=2 DUT stream must FAIL against an sps=4 golden (length + content)."""
    samples = [0.5, -0.5, 0.25, -0.75]
    dut2, _ = _run(samples, 2)
    ref4 = _gr_upsample([_fq(v) for v in samples], 4)
    res = compare_against_grc(dut2.outputs_q15, ref4.floats, metric=Metric.EXACT,
                              delay=0, tolerance=_TOL_LSB)
    assert not res.passed, "gate failed to detect a wrong upsample rate!"


def test_empty_output_fails():
    ref = _gr_upsample([_fq(v) for v in [0.5, -0.5]], 4)
    res = compare_against_grc([], ref.floats, metric=Metric.EXACT,
                              tolerance=_TOL_LSB)
    assert not res.passed


# --- report --------------------------------------------------------------------

def test_emit_report():
    dut, res = _run([0.5, -0.5, 0.25, -0.75, 0.9, -0.1], 4)
    write_report("UpsamplerBlock", res, coverage={
        "sps_sweep": [2, 4, 8],
        "patterns": "ramp, full-scale edges, zero",
        "mutation": True,
        "gr_equiv": "filter.interp_fir_filter_fff(sps, [1.0])",
        "note": "rate-EXPANDING (run_block_dut_rate); zero-stuff exact in Q15",
    })
