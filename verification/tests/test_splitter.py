# SPDX-License-Identifier: GPL-3.0-or-later
"""Verify StreamSplitterBlock (GRC: kyttar_splitter) vs GNU Radio blocks.copy.

The splitter is an explicit 1-cell fan-out relay: out = in on every arm, no
Q15 arithmetic (EXACT), memoryless → delay=0. GNU Radio needs no such block
(ports fan out natively — the golden is ``blocks.copy``); on the chip every
fan-out arm costs the source cell exit words, and this relay is authored with
a RESERVED tail (up to 8 arms). Its multi-arm behaviour is chain-proven in
test_fanout_chains.py; this file gates the single-arm relay identity.

Per INV-4 the key mutation is a value-corrupting DUT (a relay that scaled or
offset the stream must FAIL the exact gate).

Run:
    KYTTAR_GR_PYTHON=/usr/bin/python3 QT_QPA_PLATFORM=offscreen \
      <venv>/python -m pytest verification/tests/test_splitter.py -x -q
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
from gr_kyttar.placement.blocks.stream_splitter_block import StreamSplitterBlock  # noqa: E402

CHIP_YAML = str(_PLACEKYT / "resources" / "chips" / "kyttar_10x12.yaml")
_GR_AVAILABLE = os.path.exists(os.environ.get("KYTTAR_GR_PYTHON",
                                              "/usr/bin/python3"))
pytestmark = pytest.mark.skipif(
    not (os.path.exists(CHIP_YAML) and _GR_AVAILABLE),
    reason="chip yaml or GNU Radio interpreter absent")


EDGE = [0x0000, 0x7FFF, 0x8000, 0x8001, 0x4000, 0xC000, 0x0001, 0xFFFF]


def _random(seed, n=24):
    rng = random.Random(seed)
    return [rng.randint(0, 0xFFFF) for _ in range(n)]


def _gr(stim):
    return run_gnuradio_ref(
        stim,
        gnuradio_script="""
from gnuradio import gr, blocks
tb = gr.top_block()
src = blocks.vector_source_f(input_float, False)
c = blocks.copy(gr.sizeof_float)
snk = blocks.vector_sink_f()
tb.connect(src, c); tb.connect(c, snk)
tb.run()
output_float = list(snk.data())
""")


def _run(stim):
    dut = run_block_dut("StreamSplitterBlock", stim, chip_yaml=CHIP_YAML,
                        in_port="x", out_port="out")
    assert dut.ok, dut.reason
    return dut


def test_edge_vectors():
    dut = _run(EDGE)
    res = compare_against_grc(dut.outputs_q15, _gr(EDGE).floats,
                              metric=Metric.EXACT, delay=0)
    print("\nedge:", res.summary())
    assert res.passed, res.summary()


@pytest.mark.parametrize("seed", [1, 7, 42])
def test_random_vectors(seed):
    stim = _random(seed)
    dut = _run(stim)
    res = compare_against_grc(dut.outputs_q15, _gr(stim).floats,
                              metric=Metric.EXACT, delay=0)
    assert res.passed, res.summary()


def test_bitexact_reference():
    """Identity on the wire: the DUT's output equals the reference (= the
    stimulus) EXACTLY, via the same float compare the GR gate uses."""
    stim = _random(3, 40)
    dut = _run(stim)
    ref = StreamSplitterBlock("ref").process_reference_q15(stim)
    assert ref == [w & 0xFFFF for w in stim]

    def s16(v):
        v = int(v) & 0xFFFF
        return v - 0x10000 if v >= 0x8000 else v
    res = compare_against_grc(dut.outputs_q15,
                              [s16(w) / 32768.0 for w in ref],
                              metric=Metric.EXACT, delay=0)
    assert res.passed, res.summary()


# --- MANDATORY mutation tests -------------------------------------------------

def test_mutation_scaled_relay_fails():
    stim = _random(9)
    dut = _run(stim)
    halved = [(int(w) // 2) & 0xFFFF for w in dut.outputs_q15]
    res = compare_against_grc(halved, _gr(stim).floats,
                              metric=Metric.EXACT, delay=0)
    assert not res.passed, "gate blind to a value-corrupting relay!"


def test_mutation_one_sample_offset_fails():
    stim = _random(11)
    dut = _run(stim)
    shifted = [0x0000] + list(dut.outputs_q15[:-1])
    res = compare_against_grc(shifted, _gr(stim).floats,
                              metric=Metric.EXACT, delay=0)
    assert not res.passed


def test_empty_output_fails():
    res = compare_against_grc([], _gr(EDGE).floats,
                              metric=Metric.EXACT, delay=0)
    assert not res.passed


def test_emit_report():
    stim = EDGE
    dut = _run(stim)
    res = compare_against_grc(dut.outputs_q15, _gr(stim).floats,
                              metric=Metric.EXACT, delay=0)
    assert res.passed, res.summary()
    write_report("StreamSplitterBlock", res, coverage={
        "edge": True, "random": 3, "bit_exact": True, "mutation": True})
