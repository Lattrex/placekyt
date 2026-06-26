# SPDX-License-Identifier: GPL-3.0-or-later
"""Verify AbsBlock against GNU Radio blocks.abs_ff.

Absolute value out = |in| of a real stream. On chip: one conditional negate (CMP
vs 0, and 0 − in if negative). No params. The result is exact (no Q15 error)
except the lone in = −1.0 corner (whose negate wraps), so the gate is amplitude
with the single-op floor and a bit-exact substrate tier that includes the corner.

Per INV-4 the key mutation is a NON-rectified DUT (a negative passed through),
which proves the block actually takes the magnitude. Memoryless → delay=0.

Run:
    KYTTAR_GR_PYTHON=/usr/bin/python3 QT_QPA_PLATFORM=offscreen \
      <venv>/python -m pytest verification/tests/test_abs.py -x -q
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
    run_block_dut, run_gnuradio_ref, compare_against_grc, write_report, Metric)
from gr_kyttar.placement.blocks.abs_block import AbsBlock  # noqa: E402

CHIP_YAML = str(_PLACEKYT / "resources" / "chips" / "kyttar_10x12.yaml")
_GR_AVAILABLE = os.path.exists(os.environ.get("KYTTAR_GR_PYTHON", "/usr/bin/python3"))
pytestmark = pytest.mark.skipif(
    not (os.path.exists(CHIP_YAML) and _GR_AVAILABLE),
    reason="chip yaml or GNU Radio interpreter absent")


def _s16(v):
    if v is None:
        return None
    v = int(v) & 0xFFFF
    return v - 0x10000 if v >= 0x8000 else v


# Edge vectors off the −1.0 (0x8000) abs-wrap corner.
EDGE = [0x0000, 0x4000, 0xC000, 0x7FFF, 0x8001, 0x2000, 0xE000, 0x6000]


def _random(seed, n=24):
    rng = random.Random(seed)
    # avoid exactly 0x8000 (−1.0); span the rest of the range.
    return [rng.randint(0x8001, 0xFFFF) if rng.random() < 0.5
            else rng.randint(0x0000, 0x7FFF) for _ in range(n)]


def _gr(stim):
    return run_gnuradio_ref(
        stim,
        gnuradio_script="""
from gnuradio import gr, blocks
tb = gr.top_block()
src = blocks.vector_source_f(input_float, False)
a = blocks.abs_ff()
snk = blocks.vector_sink_f()
tb.connect(src, a); tb.connect(a, snk)
tb.run()
output_float = list(snk.data())
""")


def _run_and_compare(stim):
    dut = run_block_dut("AbsBlock", stim, chip_yaml=CHIP_YAML,
                        in_port="x", out_port="out")
    assert dut.ok, dut.reason
    gr = _gr(stim)
    return dut, compare_against_grc(dut.outputs_q15, gr.floats,
                                    metric=Metric.AMPLITUDE, delay=0, op_count=1)


# --- DSP equivalence vs GNU Radio ---------------------------------------------

def test_edge_vectors():
    dut, res = _run_and_compare(EDGE)
    print("\nedge:", res.summary())
    assert res.passed, res.summary()


@pytest.mark.parametrize("seed", [1, 7, 42])
def test_random_vectors(seed):
    dut, res = _run_and_compare(_random(seed))
    print(f"\nrandom seed={seed}:", res.summary())
    assert res.passed, res.summary()


# --- bit-exact substrate (includes the −1.0 negate-wrap corner) ---------------

def test_bitexact_reference_with_corner():
    stim = list(_random(3, 40)) + [0x8000, 0x8000]  # include −1.0
    dut = run_block_dut("AbsBlock", stim, chip_yaml=CHIP_YAML,
                        in_port="x", out_port="out")
    assert dut.ok, dut.reason
    blk = AbsBlock("ref")
    ref = blk.process_reference_q15(stim)
    res = compare_against_grc(dut.outputs_q15, [_s16(r) / 32768.0 for r in ref],
                              metric=Metric.EXACT, delay=0)
    print("\nbit-exact (incl corner):", res.summary())
    assert res.passed, res.summary()
    assert _s16(ref[-1]) == -32768, "|−1.0| wraps to −1.0 on the Q15 datapath"


# --- MANDATORY mutation tests -------------------------------------------------

def test_mutation_not_rectified_fails():
    """A DUT that passed a negative through (no rectify) must FAIL — proves the
    block takes the magnitude."""
    stim = [0xC000, 0xA000, 0xE000, 0x9000, 0xB000, 0xD000]  # all negative
    dut = run_block_dut("AbsBlock", stim, chip_yaml=CHIP_YAML,
                        in_port="x", out_port="out")
    assert dut.ok, dut.reason
    gr = _gr(stim)
    # mutate: negate the DUT back to the (negative) input
    un = [(0x10000 - (w or 0)) & 0xFFFF for w in dut.outputs_q15]
    res = compare_against_grc(un, gr.floats, metric=Metric.AMPLITUDE,
                              delay=0, op_count=1)
    assert not res.passed, "gate failed to detect a non-rectified output!"


def test_mutation_one_sample_offset_fails():
    stim = _random(7)
    dut = run_block_dut("AbsBlock", stim, chip_yaml=CHIP_YAML,
                        in_port="x", out_port="out")
    assert dut.ok, dut.reason
    gr = _gr(stim)
    shifted = [0x0000] + list(dut.outputs_q15[:-1])
    res = compare_against_grc(shifted, gr.floats, metric=Metric.AMPLITUDE,
                              delay=0, op_count=1)
    assert not res.passed, "gate failed to detect a 1-sample latency error!"


def test_empty_output_fails():
    gr = _gr(EDGE)
    res = compare_against_grc([], gr.floats, metric=Metric.AMPLITUDE,
                              delay=0, op_count=1)
    assert not res.passed


# --- dashboard report ---------------------------------------------------------

def test_emit_report():
    dut, res = _run_and_compare(EDGE)
    assert res.passed, res.summary()
    write_report("AbsBlock", res, coverage={
        "edge": True, "random": 3, "bit_exact": True, "mutation": True})
