# SPDX-License-Identifier: GPL-3.0-or-later
"""Verify MovingAverageBlock against GNU Radio blocks.moving_average_ff.

A moving average IS an FIR with ``length`` constant taps each equal to ``scale``
(out[n] = scale·Σ x[n-k]), so MovingAverageBlock SUBCLASSES the verified
FIRFilterBlock with box taps — all Q15 datapath / fold / COEFFICIENT-HEADROOM
machinery inherited (the LowPassFilter pattern). Constant taps are symmetric →
group delay 0, aligned with GR's causal running sum.

Reference tiers (as for the FIR / LowPassFilter):
  * DSP equivalence — DUT vs GR moving_average_ff, AMPLITUDE, within the
    headroom-aware floor q15_quant_floor(length, S).
  * Bit-exact substrate — DUT vs the inherited process_reference_q15 (the Q15
    saturating FIR datapath), EXACT.

scale = 1/length is a true average (Σ|tap| = 1 → S=0); a larger scale engages the
inherited saturating headroom restore (S>0), exercised against the bit-exact
reference. Per INV-4 every gate is paired with a mutation (inverted, wrong length,
+1 delay, empty) that must FAIL. delay=0.

Run:
    KYTTAR_GR_PYTHON=/usr/bin/python3 QT_QPA_PLATFORM=offscreen \
      <venv>/python -m pytest verification/tests/test_moving_average.py -x -q
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
from gr_kyttar.placement.blocks.moving_average_block import MovingAverageBlock  # noqa: E402

CHIP_YAML = str(_PLACEKYT / "resources" / "chips" / "kyttar_10x12.yaml")
_GR_AVAILABLE = os.path.exists(os.environ.get("KYTTAR_GR_PYTHON", "/usr/bin/python3"))
pytestmark = pytest.mark.skipif(
    not (os.path.exists(CHIP_YAML) and _GR_AVAILABLE),
    reason="chip yaml or GNU Radio interpreter absent")


def _s16(v):
    v = int(v) & 0xFFFF
    return v - 0x10000 if v >= 0x8000 else v


def _random(seed, n=40):
    rng = random.Random(seed)
    return [rng.randint(0, 0xFFFF) for _ in range(n)]


def _gr(stim, length, scale):
    return run_gnuradio_ref(
        stim,
        gnuradio_script=f"""
from gnuradio import gr, blocks
tb = gr.top_block()
src = blocks.vector_source_f(input_float, False)
m = blocks.moving_average_ff({int(length)}, {float(scale)!r})
snk = blocks.vector_sink_f()
tb.connect(src, m); tb.connect(m, snk)
tb.run()
output_float = list(snk.data())
""")


def _run_dut(stim, length, scale):
    dut = run_block_dut("MovingAverageBlock", stim,
                        params={"length": length, "scale": scale},
                        chip_yaml=CHIP_YAML, in_port="sample", out_port="out")
    assert dut.ok, dut.reason
    return dut


def _compare(stim, length, scale):
    blk = MovingAverageBlock("m", length=length, scale=scale)
    dut = _run_dut(stim, length, scale)
    gr = _gr(stim, length, scale)
    res = compare_against_grc(dut.outputs_q15, gr.floats, metric=Metric.AMPLITUDE,
                              delay=0, op_count=length, head_shift=blk._head_shift)
    return dut, res, blk


# --- DSP equivalence vs GNU Radio (true average: scale = 1/length) ------------

def test_edge_vectors():
    stim = [0x0000, 0x4000, 0x2000, 0xC000, 0x7FFF, 0x6000, 0xA000, 0x1000,
            0x3000, 0xE000, 0x5000, 0x9000]
    dut, res, _ = _compare(stim, 4, 0.25)
    print("\nedge:", res.summary())
    assert res.passed, res.summary()


@pytest.mark.parametrize("seed", [1, 7, 42])
def test_random_vectors(seed):
    dut, res, _ = _compare(_random(seed), 4, 0.25)
    print(f"\nrandom seed={seed}:", res.summary())
    assert res.passed, res.summary()


@pytest.mark.parametrize("length", [2, 3, 5, 8, 16])
def test_length_sweep(length):
    """A true-average box of each length matches GR moving_average_ff."""
    dut, res, _ = _compare(_random(99), length, 1.0 / length)
    print(f"\nlength={length}:", res.summary())
    assert res.passed, res.summary()


# --- bit-exact substrate (incl. a headroom scale, S>0) ------------------------

@pytest.mark.parametrize("length,scale", [(4, 0.25), (8, 0.125), (3, 0.5), (6, 0.3)])
def test_bitexact_reference(length, scale):
    """DUT matches the inherited Q15 FIR reference EXACTLY (the (3,0.5) and (6,0.3)
    cases have Σ|tap| > 1 → S=1, exercising the saturating headroom restore).

    NOTE (inherited FIR budget): a SINGLE-cell box with S>0 caps at ~3 taps; a
    4-tap box at scale 0.5 (4 taps + S=1 restore on one cell) exceeds the cell's
    register budget and raises at build — drop the scale to 0.25 (S=0) or use a
    longer length (multi-cell). This is the FIRFilterBlock per-cell budget, not a
    moving-average-specific limit."""
    stim = _random(3, 60)
    blk = MovingAverageBlock("ref", length=length, scale=scale)
    dut = _run_dut(stim, length, scale)
    ref = blk.process_reference_q15(stim)
    res = compare_against_grc(dut.outputs_q15, [_s16(r) / 32768.0 for r in ref],
                              metric=Metric.EXACT, delay=0)
    print(f"\nbit-exact length={length} scale={scale} S={blk._head_shift}:",
          res.summary())
    assert res.passed, res.summary()


# --- MANDATORY mutation tests -------------------------------------------------

def test_mutation_inverted_output_fails():
    stim = _random(7)
    dut, _, _ = _compare(stim, 4, 0.25)
    gr = _gr(stim, 4, 0.25)
    mutated = [(0x10000 - (w or 0)) & 0xFFFF for w in dut.outputs_q15]
    res = compare_against_grc(mutated, gr.floats, metric=Metric.AMPLITUDE,
                              delay=0, op_count=4)
    assert not res.passed, "gate failed to detect an inverted output!"


def test_mutation_wrong_length_fails():
    """A length-4 DUT compared to a GR length-8 reference must FAIL."""
    stim = _random(7)
    dut = _run_dut(stim, 4, 0.25)
    gr_wrong = _gr(stim, 8, 0.25)
    res = compare_against_grc(dut.outputs_q15, gr_wrong.floats,
                              metric=Metric.AMPLITUDE, delay=0, op_count=4)
    assert not res.passed, "gate failed to detect a wrong window length!"


def test_mutation_one_sample_offset_fails():
    stim = _random(7)
    dut = _run_dut(stim, 4, 0.25)
    gr = _gr(stim, 4, 0.25)
    shifted = [0x0000] + list(dut.outputs_q15[:-1])
    res = compare_against_grc(shifted, gr.floats, metric=Metric.AMPLITUDE,
                              delay=0, op_count=4)
    assert not res.passed, "gate failed to detect a 1-sample latency error!"


def test_empty_output_fails():
    gr = _gr(_random(7), 4, 0.25)
    res = compare_against_grc([], gr.floats, metric=Metric.AMPLITUDE,
                              delay=0, op_count=4)
    assert not res.passed


# --- dashboard report ---------------------------------------------------------

def test_emit_report():
    dut, res, _ = _compare(_random(1), 4, 0.25)
    assert res.passed, res.summary()
    write_report("MovingAverageBlock", res, coverage={
        "edge": True, "random": 3, "length_sweep": 5, "bit_exact": True,
        "headroom": True, "mutation": True})
