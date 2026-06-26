# SPDX-License-Identifier: GPL-3.0-or-later
"""Verify KeepOneInNBlock against GNU Radio blocks.keep_one_in_n.

Decimate-by-drop: keep one sample of every n (no filter). Output rate = input/n.
A pure pass-through gated by a modulo-n counter (the decimator's emit gate without
the FIR). GR keeps the LAST sample of each group of n (verified: keep_one_in_n(3)
of 0..11 → 2,5,8,11 = phase n−1), so the block emits on input indices n−1, 2n−1, …

Exact pass-through (no Q15 arithmetic) → bit-exact gate. ``run_block_dut`` records
None on the dropped triggers, so the emitted stream is ``outputs[n-1::n]`` and the
emit-phase contract (emit iff i%n == n−1) is asserted directly. Per INV-4 every
gate is paired with a mutation (wrong n, +1 delay, empty) that must FAIL. delay=0.

Run:
    KYTTAR_GR_PYTHON=/usr/bin/python3 QT_QPA_PLATFORM=offscreen \
      <venv>/python -m pytest verification/tests/test_keep_one_in_n.py -x -q
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
from gr_kyttar.placement.blocks.keep_one_in_n_block import KeepOneInNBlock  # noqa: E402

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


def _random(seed, n=40):
    rng = random.Random(seed)
    return [rng.randint(0, 0xFFFF) for _ in range(n)]


def _gr(stim, n):
    return run_gnuradio_ref(
        stim,
        gnuradio_script=f"""
from gnuradio import gr, blocks
tb = gr.top_block()
src = blocks.vector_source_f(input_float, False)
k = blocks.keep_one_in_n(gr.sizeof_float, {int(n)})
snk = blocks.vector_sink_f()
tb.connect(src, k); tb.connect(k, snk)
tb.run()
output_float = list(snk.data())
""")


def _emitted(dut, n):
    """The kept stream: phase n−1 (indices n−1, 2n−1, …)."""
    return dut.outputs_q15[n - 1::n]


def _run_dut(stim, n):
    dut = run_block_dut("KeepOneInNBlock", stim, params={"n": n},
                        chip_yaml=CHIP_YAML, in_port="x", out_port="out")
    assert dut.ok, dut.reason
    return dut


# --- emit-phase contract ------------------------------------------------------

@pytest.mark.parametrize("n", [1, 2, 3, 4, 5])
def test_emit_phase(n):
    """The block emits EXACTLY on input indices n−1, 2n−1, … (GR's phase). A
    non-None output must appear iff (i % n == n−1)."""
    stim = _random(7, 40)
    dut = _run_dut(stim, n)
    for i, w in enumerate(dut.outputs_q15):
        assert (w is not None) == (i % n == n - 1), \
            f"n={n} sample {i}: emitted={w is not None}, expected {i % n == n - 1}"


# --- exact equivalence vs GNU Radio -------------------------------------------

@pytest.mark.parametrize("n", [1, 2, 3, 4, 5])
@pytest.mark.parametrize("seed", [1, 7, 42])
def test_keeps_match_gnuradio(n, seed):
    stim = _random(seed, 40)
    dut = _run_dut(stim, n)
    gr = _gr(stim, n)
    res = compare_against_grc(_emitted(dut, n), gr.floats,
                              metric=Metric.EXACT, delay=0)
    print(f"\nn={n} seed={seed}:", res.summary())
    assert res.passed, res.summary()


def test_bitexact_reference():
    stim = _random(3, 60)
    for n in (2, 3, 7):
        dut = _run_dut(stim, n)
        blk = KeepOneInNBlock("ref", n=n)
        ref = [_s16(w) for w in blk.process_reference_q15(stim)]
        got = [_s16(w) for w in _emitted(dut, n)]
        assert got == ref, f"n={n}: DUT keep stream != reference"


# --- MANDATORY mutation tests -------------------------------------------------

def test_mutation_wrong_n_fails():
    """A DUT decimating by 2 compared to a GR keep-1-in-3 reference must FAIL."""
    stim = _random(7, 40)
    dut = _run_dut(stim, 2)
    gr_wrong = _gr(stim, 3)
    res = compare_against_grc(_emitted(dut, 2), gr_wrong.floats,
                              metric=Metric.EXACT, delay=0)
    assert not res.passed, "gate failed to detect a wrong keep factor!"


def test_mutation_wrong_phase_fails():
    """Reading the WRONG phase (n−2 instead of n−1) must FAIL vs GR."""
    stim = _random(7, 40)
    n = 3
    dut = _run_dut(stim, n)
    gr = _gr(stim, n)
    wrong_phase = dut.outputs_q15[n - 2::n]  # GR keeps phase n−1, not n−2
    res = compare_against_grc(wrong_phase, gr.floats, metric=Metric.EXACT, delay=0)
    assert not res.passed, "gate failed to detect a wrong decimation phase!"


def test_mutation_one_sample_offset_fails():
    stim = _random(7, 40)
    dut = _run_dut(stim, 2)
    gr = _gr(stim, 2)
    shifted = [0x0000] + list(_emitted(dut, 2)[:-1])
    res = compare_against_grc(shifted, gr.floats, metric=Metric.EXACT, delay=0)
    assert not res.passed, "gate failed to detect a 1-sample latency error!"


def test_empty_output_fails():
    gr = _gr(_random(7, 40), 2)
    res = compare_against_grc([], gr.floats, metric=Metric.EXACT, delay=0)
    assert not res.passed


# --- dashboard report ---------------------------------------------------------

def test_emit_report():
    stim = _random(1, 40)
    dut = _run_dut(stim, 3)
    gr = _gr(stim, 3)
    res = compare_against_grc(_emitted(dut, 3), gr.floats,
                              metric=Metric.EXACT, delay=0)
    assert res.passed, res.summary()
    write_report("KeepOneInNBlock", res, coverage={
        "param_sweep": 5, "phase_contract": True, "bit_exact": True,
        "mutation": True})
