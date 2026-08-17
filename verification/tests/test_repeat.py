# SPDX-License-Identifier: GPL-3.0-or-later
"""Verify RepeatBlock 1:1 against GNU Radio (hold-upsampling rate expander).

RepeatBlock emits, per input sample, ``interp`` COPIES of that sample — the
symbol-hold a shaped-envelope TX needs between its mapper and an envelope stage
(the PSK31 RaisedCosineEnvelope consumes a HELD ±A stream, where the zero-stuffing
UpsamplerBlock would feed it zeros). The EXACT GNU Radio equivalent is::

    blocks.repeat(gr.sizeof_float, interp)

Pure pass-through (no Q15 arithmetic), so the comparison is bit-exact. This is a
RATE-EXPANDING block (1 in -> interp out) and uses ``run_block_dut_rate``.

Run (GNU Radio lives in the system Python)::

    cd verification
    KYTTAR_GR_PYTHON=/usr/bin/python3 QT_QPA_PLATFORM=offscreen \
        .venv/bin/python -m pytest tests/test_repeat.py -v
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

_TOL_LSB = 1  # pure pass-through -> only the Q15 floor


def _fq(v: float) -> int:
    q = int(round(v * 32768.0))
    return max(-32768, min(32767, q)) & 0xFFFF


def _gr_repeat(inq: list[int], interp: int):
    """GNU Radio golden: blocks.repeat(sizeof_float, interp) holds each sample."""
    return run_gnuradio_ref(
        inq,
        """
from gnuradio import gr, blocks

tb = gr.top_block()
src = blocks.vector_source_f(input_float, False, 1, [])
rep = blocks.repeat(gr.sizeof_float, interp)
snk = blocks.vector_sink_f()
tb.connect(src, rep, snk)
tb.run()
output_float = list(snk.data())
""",
        extra_args={"interp": interp},
    )


def _run(samples, interp):
    inq = [_fq(v) for v in samples]
    dut = run_block_dut_rate("RepeatBlock", inq, params={"interp": interp},
                             chip_yaml=CHIP_YAML, in_port="x", out_port="out")
    assert dut.ok, dut.reason
    ref = _gr_repeat(inq, interp)
    res = compare_against_grc(dut.outputs_q15, ref.floats, metric=Metric.EXACT,
                              delay=0, tolerance=_TOL_LSB)
    return dut, res


# --- correctness ---------------------------------------------------------------

@pytest.mark.parametrize("interp", [2, 4, 8])
def test_repeat_rate(interp):
    """Each input becomes interp copies — bit-exact vs blocks.repeat."""
    samples = [0.5, -0.5, 0.25, -0.75, 0.9, -0.1]
    dut, res = _run(samples, interp)
    # rate check: every trigger produced exactly interp words.
    assert all(len(t) == interp for t in dut.per_trigger), \
        [len(t) for t in dut.per_trigger]
    assert len(dut.outputs_q15) == interp * len(samples)
    print(f"\nrepeat interp={interp}:", res.summary(), "| words", dut.n_words)
    assert res.passed, res.summary()


def test_repeat_full_scale_edges():
    """Edge stimulus: ± full scale + zero pass through unaltered in every copy."""
    dut, res = _run([0.999, -0.999, 0.0], 4)
    print("\nrepeat edges:", res.summary())
    assert res.passed, res.summary()


def test_repeat_interp1_identity():
    """interp=1 is the identity — one copy per input."""
    dut, res = _run([0.5, -0.25, 0.75, -0.9, 0.1], 1)
    assert len(dut.outputs_q15) == 5
    assert res.passed, res.summary()


def test_interp_above_cell_budget_raises():
    """interp > MAX_INTERP RAISES (unrolled emit cell budget) — never silently
    truncates. The explicit known-limit guard (INV-0/INV-29 discipline)."""
    from gr_kyttar.placement.blocks.repeat_block import RepeatBlock
    with pytest.raises(ValueError, match="interp"):
        RepeatBlock("r", interp=RepeatBlock.MAX_INTERP + 1)


# --- MANDATORY negative tests --------------------------------------------------

def test_mutation_zero_stuff_fails():
    """If the DUT zero-stuffed (Upsampler semantics) instead of holding, the gate
    MUST fail against the repeat golden."""
    samples = [0.5, -0.5, 0.25, -0.75]
    interp = 4
    inq = [_fq(v) for v in samples]
    corrupt = []
    for w in inq:
        corrupt.append(w)
        corrupt.extend([0] * (interp - 1))
    ref = _gr_repeat(inq, interp)
    res = compare_against_grc(corrupt, ref.floats, metric=Metric.EXACT,
                              delay=0, tolerance=_TOL_LSB)
    assert not res.passed, "gate failed to detect zero-stuff-instead-of-hold!"


def test_mutation_wrong_rate_fails():
    """An interp=2 DUT stream must FAIL against an interp=4 golden."""
    samples = [0.5, -0.5, 0.25, -0.75]
    dut2, _ = _run(samples, 2)
    ref4 = _gr_repeat([_fq(v) for v in samples], 4)
    res = compare_against_grc(dut2.outputs_q15, ref4.floats, metric=Metric.EXACT,
                              delay=0, tolerance=_TOL_LSB)
    assert not res.passed, "gate failed to detect a wrong repeat rate!"


def test_mutation_inverted_fails():
    """A sign-inverted hold stream must FAIL (catches an inverted datapath)."""
    samples = [0.5, -0.5, 0.25]
    interp = 4
    inq = [_fq(v) for v in samples]
    inv = [_fq(-v) for v in samples]
    corrupt = []
    for w in inv:
        corrupt.extend([w] * interp)
    ref = _gr_repeat(inq, interp)
    res = compare_against_grc(corrupt, ref.floats, metric=Metric.EXACT,
                              delay=0, tolerance=_TOL_LSB)
    assert not res.passed, "gate failed to detect an inverted output!"


def test_empty_output_fails():
    ref = _gr_repeat([_fq(v) for v in [0.5, -0.5]], 4)
    res = compare_against_grc([], ref.floats, metric=Metric.EXACT,
                              tolerance=_TOL_LSB)
    assert not res.passed


# --- report --------------------------------------------------------------------

def test_emit_report():
    dut, res = _run([0.5, -0.5, 0.25, -0.75, 0.9, -0.1], 4)
    write_report("RepeatBlock", res, coverage={
        "interp_sweep": [1, 2, 4, 8],
        "patterns": "ramp, full-scale edges, zero",
        "mutation": True,
        "gr_equiv": "blocks.repeat(gr.sizeof_float, interp)",
        "note": "rate-EXPANDING (run_block_dut_rate); hold exact in Q15; "
                "interp<=8 single cell (guard test raises above)",
    })
