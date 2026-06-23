# SPDX-License-Identifier: GPL-3.0-or-later
"""Verify FIRFilterBlock against GNU Radio's filter.fir_filter_fff.

FIR is the first block with a real coefficient set and the first that *scales*
with a parameter (the tap list sizes the filter). It exercises parts of the
harness that GainBlock did not: a params-dependent entry address, a derived
tolerance that grows with tap count (each Q15 MAC contributes up to ~1 LSB), and
GNU Radio's reversed-tap convention.

VERIFIED RANGE (this suite): 2–7 taps, single-cell. Within that range the block
filters correctly to within the derived per-tap Q15 tolerance.

KNOWN LIMITS (tracked, NOT verified here — see KNOWLEDGE_BASE/lessons_log.md):
  * 8+ taps: the single-cell build fails with "no register space" — the block's
    `<=12 taps => 1 cell` threshold is too aggressive for the 31-register budget.
  * 13+ taps: builds as a multi-cell wavefront but produces no output through the
    single-block harness (egress exits the last cell, which run_block_dut's
    single-landing-cell drive does not yet handle).
These are real block/harness limitations, deliberately not papered over; the FIR
is reported "in progress", not "done", until they are fixed.

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

# The verified single-cell range.
MAX_VERIFIED_TAPS = 7

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


def _random(seed, n=20):
    rng = random.Random(seed)
    return [rng.randint(0, 0xFFFF) for _ in range(n)]


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


# --- the verified range -------------------------------------------------------
TAP_SETS = [
    [0.5, 0.5],                        # 2-tap
    [0.2, 0.2, 0.2],                   # 3-tap averager
    [0.1, 0.2, 0.3, 0.2, 0.1],         # 5-tap symmetric
    [round(1.0 / 7, 4)] * 7,           # 7-tap (top of the verified range)
]


@pytest.mark.parametrize("taps", TAP_SETS, ids=lambda t: f"{len(t)}tap")
def test_fir_edge_vectors(taps):
    dut, res = _verify(taps, EDGE)
    print(f"\n{len(taps)}-tap edge:", res.summary(), "| hop", dut.hop_count)
    assert res.passed, res.summary()


@pytest.mark.parametrize("seed", [1, 7, 42])
def test_fir_random(seed):
    dut, res = _verify([0.2, 0.2, 0.2], _random(seed))
    print(f"\nrandom seed={seed}:", res.summary())
    assert res.passed, res.summary()


def test_fir_tap_count_sweep_in_range():
    """Scaling: the filter is correct across its verified single-cell tap range,
    and the derived tolerance grows with tap count (more MACs, more rounding)."""
    for n in range(2, MAX_VERIFIED_TAPS + 1):
        taps = [round(1.0 / n, 4)] * n
        dut, res = _verify(taps, EDGE)
        assert res.passed, f"{n}-tap: {res.summary()}"
        assert res.tolerance == n + 1, \
            f"{n}-tap tolerance should scale to {n + 1}, got {res.tolerance}"


# --- mandatory negative tests -------------------------------------------------

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


# --- known-limit guards: document the ceiling as executable expectations ------

def test_fir_8tap_known_build_limit():
    """8+ taps currently exceed the single-cell register budget. This guards the
    KNOWN limit: when the block is fixed to fit (or fold), flip this expectation."""
    dut = run_block_dut("FIRFilterBlock", EDGE[:4],
                        params={"coefficients": [0.125] * 8}, chip_yaml=CHIP_YAML)
    assert not dut.ok and "register" in dut.reason.lower(), (
        "8-tap FIR now builds — the single-cell register limit may be fixed; "
        "extend MAX_VERIFIED_TAPS and remove this guard.")


def test_fir_16tap_known_egress_limit():
    """13+ taps build as a multi-cell wavefront but produce no egress through the
    single-block harness. Guards the KNOWN limit until multi-cell egress works."""
    dut = run_block_dut("FIRFilterBlock", EDGE[:4],
                        params={"coefficients": [0.0625] * 16}, chip_yaml=CHIP_YAML)
    produced = dut.ok and any(o is not None for o in dut.outputs_q15)
    assert not produced, (
        "16-tap FIR now produces output — multi-cell egress may be fixed; "
        "extend the verified range and remove this guard.")


def test_emit_report():
    """Record the verified result for the dashboard (the 5-tap mid-range case)."""
    dut, res = _verify([0.1, 0.2, 0.3, 0.2, 0.1], EDGE)
    assert res.passed, res.summary()
    write_report("FIRFilterBlock", res, coverage={
        "edge": True, "random": 3, "param_sweep": MAX_VERIFIED_TAPS - 1,
        "mutation": True})
