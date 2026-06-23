# SPDX-License-Identifier: GPL-3.0-or-later
"""Prove the verification framework on the GainBlock.

This is the gold-standard template: it shows the full block-level flow
(stimulus -> GRC predictor + simKYT DUT -> compare) AND the mandatory negative
test (the gate must FAIL on a deliberately corrupted DUT). If both hold, the
harness is trustworthy for this block class.

Run:
    KYTTAR_GR_PYTHON=/usr/bin/python3 \
      <venv>/python -m pytest verification/tests/test_gain.py -x -q
"""

from __future__ import annotations

import os
import random
import sys
from pathlib import Path

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

# placekyt package root (has engine/, ui/, model/) + the verification package.
_PLACEKYT = Path(__file__).resolve().parents[2] / "placekyt"
_VERIFY = Path(__file__).resolve().parents[1]
for p in (str(_PLACEKYT), str(_VERIFY)):
    if p not in sys.path:
        sys.path.insert(0, p)

from kyttar_verify import (  # noqa: E402
    run_block_dut, run_gnuradio_ref, compare_against_grc, Metric)

CHIP_YAML = str(_PLACEKYT / "resources" / "chips" / "kyttar_10x12.yaml")

_GR_AVAILABLE = os.path.exists(os.environ.get("KYTTAR_GR_PYTHON", "/usr/bin/python3"))
pytestmark = pytest.mark.skipif(
    not (os.path.exists(CHIP_YAML) and _GR_AVAILABLE),
    reason="chip yaml or GNU Radio interpreter absent")


def _gr_gain(inputs_q15, gain):
    return run_gnuradio_ref(
        input_q15=inputs_q15,
        gnuradio_script="""
from gnuradio import gr, blocks
tb = gr.top_block()
src = blocks.vector_source_f(input_float, False)
mult = blocks.multiply_const_ff(gain)
sink = blocks.vector_sink_f()
tb.connect(src, mult); tb.connect(mult, sink)
tb.run()
output_float = list(sink.data())
""",
        extra_args={"gain": gain},
    )


# --- stimulus families: edge + random -----------------------------------------
EDGE = [0x0000, 0x4000, 0x2000, 0xC000, 0x7FFF, 0x8001, 0x6000, 0xA000]


def _random(seed, n=16):
    rng = random.Random(seed)
    return [rng.randint(0, 0xFFFF) for _ in range(n)]


def _run_and_compare(gain, inputs):
    dut = run_block_dut("GainBlock", inputs, params={"gain": gain},
                        chip_yaml=CHIP_YAML)
    assert dut.ok, dut.reason
    ref = _gr_gain(inputs, gain)
    # GainBlock is single-cell feed-forward: 1 MULQ, no group delay.
    return dut, compare_against_grc(
        dut.outputs_q15, ref.floats, metric=Metric.AMPLITUDE,
        delay=0, op_count=1)


def test_gain_edge_vectors():
    """Real GainBlock matches GRC multiply_const on edge vectors, within floor."""
    dut, res = _run_and_compare(0.5, EDGE)
    print("\nedge:", res.summary(), "| hop", dut.hop_count, "| words", dut.n_words)
    assert res.passed, res.summary()


@pytest.mark.parametrize("seed", [1, 7, 42])
def test_gain_random_vectors(seed):
    dut, res = _run_and_compare(0.5, _random(seed))
    print(f"\nrandom seed={seed}:", res.summary())
    assert res.passed, res.summary()


@pytest.mark.parametrize("gain", [0.25, 0.5, 0.75, 0.9])
def test_gain_param_sweep(gain):
    """Parameter sweep: parity must hold across the block's gain range."""
    dut, res = _run_and_compare(gain, EDGE)
    print(f"\ngain={gain}:", res.summary())
    assert res.passed, res.summary()


# --- MANDATORY negative tests: the gate must DETECT real corruptions ----------

def test_mutation_inverted_output_fails():
    """A sign-inverted DUT must FAIL (catches an inverted/negated block)."""
    dut = run_block_dut("GainBlock", EDGE, params={"gain": 0.5},
                        chip_yaml=CHIP_YAML)
    assert dut.ok, dut.reason
    ref = _gr_gain(EDGE, 0.5)
    mutated = [(0x10000 - (w or 0)) & 0xFFFF for w in dut.outputs_q15]  # negate
    res = compare_against_grc(mutated, ref.floats, metric=Metric.AMPLITUDE,
                              delay=0, op_count=1)
    assert not res.passed, "gate failed to detect an inverted output!"


def test_mutation_wrong_gain_fails():
    """A DUT built at the wrong gain must FAIL against the right reference."""
    dut = run_block_dut("GainBlock", EDGE, params={"gain": 0.5},
                        chip_yaml=CHIP_YAML)
    assert dut.ok, dut.reason
    ref_wrong = _gr_gain(EDGE, 0.9)   # reference for a DIFFERENT gain
    res = compare_against_grc(dut.outputs_q15, ref_wrong.floats,
                              metric=Metric.AMPLITUDE, delay=0, op_count=1)
    assert not res.passed, "gate failed to detect a wrong-gain mismatch!"


def test_mutation_one_sample_offset_fails():
    """A +1-sample delay must FAIL when delay=0 is asserted (catches latency bugs)."""
    dut = run_block_dut("GainBlock", EDGE, params={"gain": 0.5},
                        chip_yaml=CHIP_YAML)
    assert dut.ok, dut.reason
    ref = _gr_gain(EDGE, 0.5)
    shifted = [0x0000] + list(dut.outputs_q15[:-1])  # delay the DUT by one
    res = compare_against_grc(shifted, ref.floats, metric=Metric.AMPLITUDE,
                              delay=0, op_count=1)
    assert not res.passed, "gate failed to detect a 1-sample latency error!"


def test_empty_output_fails():
    """An empty DUT output is a hard fail (green must not be reachable empty)."""
    ref = _gr_gain(EDGE, 0.5)
    res = compare_against_grc([], ref.floats, metric=Metric.AMPLITUDE)
    assert not res.passed
