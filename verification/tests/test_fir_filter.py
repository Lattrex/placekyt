# SPDX-License-Identifier: GPL-3.0-or-later
"""Verify FIRFilterBlock against GNU Radio's filter.fir_filter_fff.

FIR is the first block with a real coefficient set and the first that *scales*
with a parameter (the tap list sizes the filter). It exercises parts of the
harness that GainBlock did not: a params-dependent entry address (INV-6), a
derived tolerance that grows with tap count (each Q15 MAC contributes up to ~1
LSB), GNU Radio's reversed-tap convention, and — past 7 taps — a MULTI-CELL
wavefront whose output egresses from the block's LAST cell (INV-7).

VERIFIED RANGE (this suite): 2 taps … 64 taps (the headline target), single-cell
for ≤7 taps and a chained partial-sum systolic wavefront for ≥8. Probing shows
the same design stays correct out to ~360 taps (72 cells); the wall above that is
chip ROUTING CAPACITY, not the block (see the known-limit guard below).

TWO substrate fixes this block forced (both now in KNOWLEDGE_BASE):
  * INV-6 (entry resolved WITH params) — without it the FIR echoed its input.
  * Multi-cell EGRESS — the auto-router resolved a block's output PortMap WITHOUT
    its params, so a params-scaling multi-cell block routed its output from the
    DEFAULT (single-cell) cell 0 instead of its real last cell → no output. Fixed
    by threading each placed block's params into the routing PortMap resolution.

STIMULUS NOTE (INV-4): a multi-cell FIR is only exercised if the input is LONGER
than the filter — otherwise the deep cells never see a non-zero sample and a
deep-cell bug hides. The multi-cell sweeps below drive ≥2·ntaps samples, and a
deep-tap mutation test proves the gate actually sees the deepest cell.

Run:
    KYTTAR_GR_PYTHON=/usr/bin/python3 QT_QPA_PLATFORM=offscreen \
      <venv>/python -m pytest verification/tests/test_fir_filter.py -x -q
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
for p in (str(_PLACEKYT), str(_VERIFY)):
    if p not in sys.path:
        sys.path.insert(0, p)

from kyttar_verify import (  # noqa: E402
    run_block_dut, run_gnuradio_ref, compare_against_grc, write_report, Metric)

CHIP_YAML = str(_PLACEKYT / "resources" / "chips" / "kyttar_10x12.yaml")
_GR_AVAILABLE = os.path.exists(os.environ.get("KYTTAR_GR_PYTHON", "/usr/bin/python3"))
pytestmark = pytest.mark.skipif(
    not (os.path.exists(CHIP_YAML) and _GR_AVAILABLE),
    reason="chip yaml or GNU Radio interpreter absent")

# Largest single-cell FIR (the rest fold to the multi-cell wavefront).
MAX_SINGLE_CELL_TAPS = 7
# Headline scaling target — verified end to end against GNU Radio.
MAX_VERIFIED_TAPS = 64
# A FIR this large overflows the 10x12 array's ROUTING capacity (80 cells leaves
# no free corridor for the I/O ports). The genuine substrate wall — guarded below.
ROUTING_WALL_TAPS = 400

EDGE = [0x0000, 0x4000, 0x2000, 0xC000, 0x7FFF, 0x8001, 0x6000, 0xA000,
        0x1000, 0x3000]


def _gr_fir(inputs_q15, taps):
    # GNU Radio's fir_filter_fff convolves with taps in latest-sample-first
    # order, the reverse of the Kyttar coefficient order — so reverse them here.
    return run_gnuradio_ref(
        input_q15=inputs_q15,
        gnuradio_script="""
from gnuradio import gr, filter as gr_filter, blocks
tb = gr.top_block()
src = blocks.vector_source_f(input_float, False)
fir = gr_filter.fir_filter_fff(1, taps)
sink = blocks.vector_sink_f()
tb.connect(src, fir); tb.connect(fir, sink)
tb.run()
output_float = list(sink.data())
""",
        extra_args={"taps": list(reversed(taps))})


def _random_input(seed, n):
    rng = random.Random(seed)
    return [rng.randint(0, 0xFFFF) for _ in range(n)]


def _norm_taps(n, seed):
    """A realistic (DC-gain≈1) random tap set — Σ|h|≈1 keeps the Q15 output and
    the chained partial sums inside range, the normal case for an FIR filter."""
    rng = random.Random(seed)
    t = [rng.uniform(0.0, 1.0) for _ in range(n)]
    s = sum(t)
    return [round(v / s, 5) for v in t]


def _verify(taps, inputs):
    dut = run_block_dut("FIRFilterBlock", inputs,
                        params={"coefficients": taps}, chip_yaml=CHIP_YAML)
    assert dut.ok, f"build/run failed: {dut.reason}"
    ref = _gr_fir(inputs, taps)
    # FIR (decimation 1) emits one output per input aligned with GNU Radio's
    # output[n] — no extra latency in the compared stream; delay=0. The
    # tolerance is derived from the tap count: each MAC truncation is <=1 LSB.
    return dut, compare_against_grc(
        dut.outputs_q15, ref.floats, metric=Metric.AMPLITUDE,
        delay=0, op_count=len(taps))


# --- single-cell range (edge + random + sweep) --------------------------------
TAP_SETS = [
    [0.5, 0.5],                        # 2-tap
    [0.2, 0.2, 0.2],                   # 3-tap averager
    [0.1, 0.2, 0.3, 0.2, 0.1],         # 5-tap symmetric
    [0.05, 0.1, 0.2, 0.3, 0.2, 0.1, 0.05],   # 7-tap (top of single cell)
    [0.3, -0.2, 0.5, -0.1],            # 4-tap ASYMMETRIC (catches tap-order bugs)
]


@pytest.mark.parametrize("taps", TAP_SETS, ids=lambda t: f"{len(t)}tap")
def test_fir_edge_vectors(taps):
    dut, res = _verify(taps, EDGE)
    print(f"\n{len(taps)}-tap edge:", res.summary(), "| hop", dut.hop_count)
    assert res.passed, res.summary()


@pytest.mark.parametrize("seed", [1, 7, 42])
def test_fir_random(seed):
    dut, res = _verify([0.2, 0.2, 0.2], _random_input(seed, 20))
    print(f"\nrandom seed={seed}:", res.summary())
    assert res.passed, res.summary()


def test_fir_single_cell_sweep():
    """The 1-cell range is correct and the derived tolerance grows with the tap
    count (more MACs, more rounding)."""
    for n in range(2, MAX_SINGLE_CELL_TAPS + 1):
        taps = [round(1.0 / n, 4)] * n
        dut, res = _verify(taps, EDGE)
        assert res.passed, f"{n}-tap: {res.summary()}"
        assert res.tolerance == n + 1, \
            f"{n}-tap tolerance should scale to {n + 1}, got {res.tolerance}"


# --- multi-cell scaling (the headline: 8 .. 64 taps) --------------------------
# Representative sizes spanning 2..13 cells (TAPS_PER_CELL=5).
MULTICELL_SIZES = [8, 9, 13, 16, 32, MAX_VERIFIED_TAPS]


@pytest.mark.parametrize("n", MULTICELL_SIZES, ids=lambda n: f"{n}tap")
def test_fir_multicell_scaling(n):
    """A multi-cell wavefront FIR matches GNU Radio within the derived tolerance.

    Driven with > 2*ntaps RANDOM samples and a REALISTIC (asymmetric, DC≈1) tap
    set, so EVERY cell's delay segment is exercised with real data — a bug in any
    cell (not just the first) would show. (A short/uniform/positive stimulus
    hides such bugs; that is exactly how the prior 'passing' suite missed the
    multi-cell coefficient-ordering bug — INV-4.)"""
    taps = _norm_taps(n, seed=100 + n)
    inputs = _random_input(seed=200 + n, n=2 * n + 16)
    dut, res = _verify(taps, inputs)
    print(f"\n{n}-tap multicell:", res.summary(), "| entry", dut.entry_addr)
    assert res.passed, res.summary()
    # Tolerance is DERIVED from the op count (= tap count), never tuned.
    assert res.tolerance == n + 1, \
        f"{n}-tap tolerance should be {n + 1}, got {res.tolerance}"


# --- mandatory negative tests (prove the gate FAILS on a corrupted DUT) --------

def test_fir_mutation_inverted_fails():
    dut, _ = _verify([0.2, 0.2, 0.2], EDGE)
    ref = _gr_fir(EDGE, [0.2, 0.2, 0.2])
    broken = [(0x10000 - (w or 0)) & 0xFFFF for w in dut.outputs_q15]
    res = compare_against_grc(broken, ref.floats, metric=Metric.AMPLITUDE,
                              delay=0, op_count=3)
    assert not res.passed, "gate failed to catch an inverted FIR output!"


def test_fir_mutation_wrong_taps_fails():
    """A FIR built with different taps must fail against the right reference."""
    dut = run_block_dut("FIRFilterBlock", EDGE,
                        params={"coefficients": [0.2, 0.2, 0.2]},
                        chip_yaml=CHIP_YAML)
    assert dut.ok, dut.reason
    ref_wrong = _gr_fir(EDGE, [0.9, 0.05, 0.05])   # different filter
    res = compare_against_grc(dut.outputs_q15, ref_wrong.floats,
                              metric=Metric.AMPLITUDE, delay=0, op_count=3)
    assert not res.passed, "gate failed to catch a wrong-taps mismatch!"


def test_fir_mutation_delay_offset_fails():
    dut, _ = _verify([0.2, 0.2, 0.2], EDGE)
    ref = _gr_fir(EDGE, [0.2, 0.2, 0.2])
    shifted = [0x0000] + list(dut.outputs_q15[:-1])
    res = compare_against_grc(shifted, ref.floats, metric=Metric.AMPLITUDE,
                              delay=0, op_count=3)
    assert not res.passed, "gate failed to catch a 1-sample FIR latency error!"


def test_fir_mutation_deep_cell_fails():
    """Prove the gate actually verifies the DEEPEST cell of a multi-cell FIR — not
    just the first. Build a 32-tap (7-cell) DUT, drive it with long input, then
    compare against a reference whose ONE perturbed tap lives in the LAST cell
    (index 30 of 32). If the gate passed, the deep cell's output would not depend
    on that tap → the multi-cell datapath would be unverified there. It must FAIL.
    """
    n = 32
    taps = _norm_taps(n, seed=132)
    inputs = _random_input(seed=232, n=2 * n + 16)
    dut = run_block_dut("FIRFilterBlock", inputs,
                        params={"coefficients": taps}, chip_yaml=CHIP_YAML)
    assert dut.ok, dut.reason
    perturbed = list(taps)
    perturbed[30] += 0.15   # a DEEP tap (last cell), well above the LSB floor
    ref_wrong = _gr_fir(inputs, perturbed)
    res = compare_against_grc(dut.outputs_q15, ref_wrong.floats,
                              metric=Metric.AMPLITUDE, delay=0, op_count=n)
    assert not res.passed, (
        "gate did not catch a perturbed DEEP-cell tap — the last cell of the "
        "multi-cell FIR is not actually being verified!")


# --- known-limit guard: the genuine substrate wall ----------------------------

def test_fir_routing_capacity_limit():
    """The block scales correctly to ~360 taps (72 cells); above that the
    serpentine footprint leaves NO free routing corridor for the I/O ports on the
    10x12 array. This is a chip ROUTING-CAPACITY wall, not a block bug. Guarded as
    an executable expectation: if the array grows or placement improves so a
    400-tap (80-cell) FIR routes, this flips — extend the verified range then."""
    dut = run_block_dut("FIRFilterBlock", EDGE[:4],
                        params={"coefficients": [round(1.0 / ROUTING_WALL_TAPS, 6)]
                                * ROUTING_WALL_TAPS}, chip_yaml=CHIP_YAML)
    assert not dut.ok and "corridor" in dut.reason.lower(), (
        f"a {ROUTING_WALL_TAPS}-tap FIR now routes (reason={dut.reason!r}); the "
        "routing-capacity wall moved — extend MAX_VERIFIED_TAPS and this guard.")


def test_emit_report():
    """Record the verified result for the dashboard (the 64-tap scaling case)."""
    n = MAX_VERIFIED_TAPS
    taps = _norm_taps(n, seed=164)
    inputs = _random_input(seed=264, n=2 * n + 16)
    dut, res = _verify(taps, inputs)
    assert res.passed, res.summary()
    write_report("FIRFilterBlock", res, coverage={
        "edge": True, "random": 3, "param_sweep": len(MULTICELL_SIZES)
        + (MAX_SINGLE_CELL_TAPS - 1), "mutation": True,
        "max_verified_taps": MAX_VERIFIED_TAPS})
