# SPDX-License-Identifier: GPL-3.0-or-later
"""Verify ComplexToRealBlock / ComplexToImagBlock vs GNU Radio
blocks.complex_to_real / blocks.complex_to_imag.

Channel selectors: forward ONE rail of a complex stream as a real output
(complex_to_real → re, complex_to_imag → im). A complex sample is carried as a
two-operand (re@R0, im@R1) pair; the block emits the selected operand. Pure data
movement → EXACT (zero Q15 error), so the gate is bit-exact.

Per INV-4 the key mutation is selecting the WRONG channel (compare the real block
to the GR imag reference), which proves the block picks the correct rail. delay=0.

Run:
    KYTTAR_GR_PYTHON=/usr/bin/python3 QT_QPA_PLATFORM=offscreen \
      <venv>/python -m pytest verification/tests/test_complex_real_imag.py -x -q
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
    run_block_dut_complex, run_gnuradio_ref_complex, compare_against_grc,
    write_report, Metric)

CHIP_YAML = str(_PLACEKYT / "resources" / "chips" / "kyttar_10x12.yaml")
_GR_AVAILABLE = os.path.exists(os.environ.get("KYTTAR_GR_PYTHON", "/usr/bin/python3"))
pytestmark = pytest.mark.skipif(
    not (os.path.exists(CHIP_YAML) and _GR_AVAILABLE),
    reason="chip yaml or GNU Radio interpreter absent")

_VARIANTS = {
    "real": ("ComplexToRealBlock", "complex_to_real"),
    "imag": ("ComplexToImagBlock", "complex_to_imag"),
}


def _gr_chan(stim, gr_block):
    return run_gnuradio_ref_complex(
        stim,
        gnuradio_script=f"""
from gnuradio import gr, blocks
tb = gr.top_block()
src = blocks.vector_source_c(input_complex, False)
c = blocks.{gr_block}()
snk = blocks.vector_sink_f()
tb.connect(src, c); tb.connect(c, snk)
tb.run()
output_float = list(snk.data())
""")


EDGE = [complex(0.0, 0.0), complex(0.5, -0.5), complex(-0.999, 0.999),
        complex(0.25, 0.75), complex(-1.0, 0.5), complex(0.6, -0.999),
        complex(-0.3, -0.7), complex(0.9, 0.1)]


def _random(seed, n=24):
    rng = random.Random(seed)
    return [complex(rng.uniform(-0.99, 0.99), rng.uniform(-0.99, 0.99))
            for _ in range(n)]


def _run_dut(block_type, stim):
    dut = run_block_dut_complex(
        block_type, stim, chip_yaml=CHIP_YAML,
        in_ports=("re", "im"), words_per_sample=1)
    assert dut.ok, dut.reason
    return dut


def _compare(dut, gr):
    return compare_against_grc(dut.i_q15, gr.i, metric=Metric.EXACT, delay=0)


# --- structure ----------------------------------------------------------------

@pytest.mark.parametrize("variant", ["real", "imag"])
def test_drives_and_captures(variant):
    block_type, _ = _VARIANTS[variant]
    dut = _run_dut(block_type, _random(1, 12))
    assert dut.words_per_sample == 1
    assert dut.in_regs == (0, 1)
    assert all(v is not None for v in dut.i_q15)


# --- exact equivalence vs GNU Radio -------------------------------------------

@pytest.mark.parametrize("variant", ["real", "imag"])
def test_edge_vectors(variant):
    block_type, gr_block = _VARIANTS[variant]
    dut = _run_dut(block_type, EDGE)
    gr = _gr_chan(EDGE, gr_block)
    res = _compare(dut, gr)
    print(f"\n{variant} edge:", res.summary())
    assert res.passed, res.summary()


@pytest.mark.parametrize("variant", ["real", "imag"])
@pytest.mark.parametrize("seed", [1, 7, 42])
def test_random_vectors(variant, seed):
    block_type, gr_block = _VARIANTS[variant]
    stim = _random(seed)
    dut = _run_dut(block_type, stim)
    gr = _gr_chan(stim, gr_block)
    res = _compare(dut, gr)
    print(f"\n{variant} random seed={seed}:", res.summary())
    assert res.passed, res.summary()


# --- MANDATORY mutation tests -------------------------------------------------

@pytest.mark.parametrize("variant,other", [("real", "imag"), ("imag", "real")])
def test_mutation_wrong_channel_fails(variant, other):
    """Selecting the WRONG rail must FAIL — proves the block picks the right one."""
    block_type, _ = _VARIANTS[variant]
    _, other_block = _VARIANTS[other]
    stim = _random(7, 32)
    dut = _run_dut(block_type, stim)
    gr_other = _gr_chan(stim, other_block)   # reference for the OTHER channel
    res = compare_against_grc(dut.i_q15, gr_other.i, metric=Metric.EXACT, delay=0)
    assert not res.passed, f"gate failed to detect {variant} vs {other} channel!"


@pytest.mark.parametrize("variant", ["real", "imag"])
def test_mutation_one_sample_offset_fails(variant):
    block_type, gr_block = _VARIANTS[variant]
    stim = _random(7, 32)
    dut = _run_dut(block_type, stim)
    gr = _gr_chan(stim, gr_block)
    shifted = [0x0000] + list(dut.i_q15[:-1])
    res = compare_against_grc(shifted, gr.i, metric=Metric.EXACT, delay=0)
    assert not res.passed, "gate failed to detect a 1-sample latency error!"


@pytest.mark.parametrize("variant", ["real", "imag"])
def test_empty_output_fails(variant):
    _, gr_block = _VARIANTS[variant]
    gr = _gr_chan(EDGE, gr_block)
    res = compare_against_grc([], gr.i, metric=Metric.EXACT, delay=0)
    assert not res.passed


# --- dashboard reports --------------------------------------------------------

@pytest.mark.parametrize("variant", ["real", "imag"])
def test_emit_report(variant):
    block_type, gr_block = _VARIANTS[variant]
    dut = _run_dut(block_type, EDGE)
    gr = _gr_chan(EDGE, gr_block)
    res = _compare(dut, gr)
    assert res.passed, res.summary()
    write_report(block_type, res, coverage={
        "edge": True, "random": 3, "bit_exact": True, "mutation": True})
