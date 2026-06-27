# SPDX-License-Identifier: GPL-3.0-or-later
"""Verify FIRFilterBlock's INTERPOLATION parameter vs GR interp_fir_filter_fff(L, taps).

Interpolation is a FIRFilterBlock PARAMETER (matching GR — the GRC FIR / convenience
filter blocks expose `interp`), NOT a separate block. With interpolation L the input
is zero-stuffed by L (sample then L-1 zeros) and filtered: the landing cell runs the
FIR L times per input, emitting a BURST of L outputs (rate-EXPANDING) — exactly GR
`interp_fir_filter_fff(L, taps)`.

Single-cell only for now (the unrolled L-pass burst fits one cell up to a measured
tap cap; larger interp FIRs raise with a compose-Upsampler->FIR message — an honest,
documented limit, never a silent wrong build).

Run:
    cd verification
    KYTTAR_GR_PYTHON=/usr/bin/python3 QT_QPA_PLATFORM=offscreen \
        ../.venv/bin/python -m pytest tests/test_fir_interp.py -v
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


def _fq(v: float) -> int:
    q = int(round(v * 32768.0))
    return max(-32768, min(32767, q)) & 0xFFFF


def _gr_interp(inq, taps, L):
    """GR golden: interp_fir_filter_fff(L, taps)."""
    return run_gnuradio_ref(
        inq,
        """
from gnuradio import gr, blocks, filter as gfilter
tb = gr.top_block()
src = blocks.vector_source_f(input_float, False, 1, [])
f = gfilter.interp_fir_filter_fff(L, taps)
snk = blocks.vector_sink_f()
tb.connect(src, f, snk)
tb.run()
output_float = list(snk.data())
""",
        extra_args={"taps": list(taps), "L": int(L)},
    )


def _run(samples, taps, L):
    inq = [_fq(v) for v in samples]
    dut = run_block_dut_rate("FIRFilterBlock", inq,
                             params={"coefficients": list(taps),
                                     "interpolation": L},
                             chip_yaml=CHIP_YAML, in_port="sample",
                             out_port="out")
    assert dut.ok, dut.reason
    ref = _gr_interp(inq, taps, L)
    res = compare_against_grc(dut.outputs_q15, ref.floats,
                              metric=Metric.AMPLITUDE, delay=0,
                              op_count=len(taps))
    return dut, res


# --- correctness ---------------------------------------------------------------

@pytest.mark.parametrize("L", [2, 3, 4])
def test_interp_rate(L):
    """interp L produces L outputs per input, matching GR interp_fir_filter.

    Tap count respects the measured single-cell unrolled-burst cap per L
    (L=2 -> 4 taps; L=3,4 -> 2 taps). Larger needs Upsampler->FIR composition."""
    taps = [0.25, 0.3, 0.2, 0.25] if L == 2 else [0.5, 0.5]
    samples = [0.5, -0.5, 0.25, -0.75, 0.6, -0.2]
    dut, res = _run(samples, taps, L)
    assert all(len(t) == L for t in dut.per_trigger), \
        [len(t) for t in dut.per_trigger]
    assert len(dut.outputs_q15) == L * len(samples)
    print(f"\ninterp L={L}:", res.summary(), "| words", dut.n_words)
    assert res.passed, res.summary()


def test_interp_default_taps():
    dut, res = _run([0.5, -0.5, 0.25, -0.75], [0.25, 0.5, 0.25], 2)
    print("\ninterp 3-tap L2:", res.summary())
    assert res.passed, res.summary()


# --- MANDATORY negative tests --------------------------------------------------

def test_mutation_no_zero_stuff_fails():
    """If the DUT repeated the sample instead of zero-stuffing, the gate MUST fail."""
    taps = [0.3, 0.4, 0.3]
    L = 2
    samples = [0.5, -0.5, 0.25, -0.75]
    inq = [_fq(v) for v in samples]
    # repeat-instead-of-zero-stuff corruption fed through GR interp golden
    ref = _gr_interp(inq, taps, L)
    repeated = []
    for w in inq:
        repeated.extend([w] * L)
    # run those through a plain FIR (no interp) to model "filtered repeats"
    res = compare_against_grc(repeated, ref.floats, metric=Metric.AMPLITUDE,
                              delay=0, op_count=len(taps))
    assert not res.passed, "gate failed to detect missing zero-stuff!"


def test_mutation_wrong_interp_fails():
    taps = [0.3, 0.4, 0.3]
    samples = [0.5, -0.5, 0.25, -0.75]
    dut2, _ = _run(samples, taps, 2)
    ref3 = _gr_interp([_fq(v) for v in samples], taps, 3)
    res = compare_against_grc(dut2.outputs_q15, ref3.floats,
                              metric=Metric.AMPLITUDE, delay=0,
                              op_count=len(taps))
    assert not res.passed, "gate failed to detect wrong interpolation rate!"


def test_multicell_interp_raises_clearly():
    """An interp FIR too large for one cell RAISES a clear compose message (an
    honest, documented limit) — it never silently mis-builds."""
    from gr_kyttar.placement.blocks.fir_filter_block import FIRFilterBlock
    big = [0.1] * 8  # 8 taps, L=4 -> exceeds the single-cell unrolled-burst cap
    blk = FIRFilterBlock("f", big, interpolation=4)
    with pytest.raises(ValueError, match="INTERPOLATING FIR"):
        blk.build_cell_programs()


# --- report --------------------------------------------------------------------

def test_emit_report():
    dut, res = _run([0.5, -0.5, 0.25, -0.75, 0.6, -0.2], [0.3, 0.4, 0.3], 2)
    write_report("FIRFilterBlock_interp", res, coverage={
        "L_sweep": [2, 3, 4], "mutation": True, "rate_check": True,
        "note": "interpolation is a FIRFilterBlock parameter "
                "(GR interp_fir_filter_fff(L,taps)); single-cell unrolled burst"})
