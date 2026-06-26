# SPDX-License-Identifier: GPL-3.0-or-later
"""Verify ComplexToFloatBlock / FloatToComplexBlock vs GNU Radio
blocks.complex_to_float / blocks.float_to_complex.

The single most common type-conversion pair in any I/Q graph. On the Kyttar
substrate a complex sample IS a two-operand (re@R0, im@R1) pair, so BOTH GR
conversions are the same identity datapath (read the pair, emit it as two words);
they differ only in GRC port typing. Pure data movement (MOVE/WRITE, no
arithmetic) → the conversion is EXACT (zero Q15 error), so the gate is bit-exact.

The verify harness carries the I/Q pair as one complex array; the block emits two
words/trigger (re then im) on one bus corridor, de-interleaved into I (re) and Q
(im) — the NCO/mixer two-word-egress pattern. Per INV-4 every gate is paired with
a mutation (swapped re/im, negated channel, +1 delay, empty) that must FAIL.
Memoryless → delay=0.

Run:
    KYTTAR_GR_PYTHON=/usr/bin/python3 QT_QPA_PLATFORM=offscreen \
      <venv>/python -m pytest verification/tests/test_complex_float.py -x -q
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
    run_block_dut_complex, run_gnuradio_ref_complex,
    compare_complex_against_grc, Metric)

CHIP_YAML = str(_PLACEKYT / "resources" / "chips" / "kyttar_10x12.yaml")
_GR_AVAILABLE = os.path.exists(os.environ.get("KYTTAR_GR_PYTHON", "/usr/bin/python3"))
pytestmark = pytest.mark.skipif(
    not (os.path.exists(CHIP_YAML) and _GR_AVAILABLE),
    reason="chip yaml or GNU Radio interpreter absent")

# GR fragments: each sets output_complex with i=re, q=im (the I/Q pair the DUT
# carries), so the comparator checks both channels uniformly.
_C2F_SCRIPT = """
from gnuradio import gr, blocks
tb = gr.top_block()
src = blocks.vector_source_c(input_complex, False)
c2f = blocks.complex_to_float()
sre = blocks.vector_sink_f(); sim = blocks.vector_sink_f()
tb.connect(src, c2f)
tb.connect((c2f, 0), sre); tb.connect((c2f, 1), sim)
tb.run()
_re = list(sre.data()); _im = list(sim.data())
output_complex = [complex(r, i) for r, i in zip(_re, _im)]
"""

_F2C_SCRIPT = """
from gnuradio import gr, blocks
tb = gr.top_block()
sre = blocks.vector_source_f(input_i, False)
sim = blocks.vector_source_f(input_q, False)
f2c = blocks.float_to_complex()
snk = blocks.vector_sink_c()
tb.connect(sre, (f2c, 0)); tb.connect(sim, (f2c, 1)); tb.connect(f2c, snk)
tb.run()
output_complex = list(snk.data())
"""

_VARIANTS = {
    "c2f": ("ComplexToFloatBlock", _C2F_SCRIPT),
    "f2c": ("FloatToComplexBlock", _F2C_SCRIPT),
}


def _s16(v):
    if v is None:
        return None
    v = int(v) & 0xFFFF
    return v - 0x10000 if v >= 0x8000 else v


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
        in_ports=("re", "im"), words_per_sample=2)
    assert dut.ok, dut.reason
    return dut


def _gr(script, stim):
    return run_gnuradio_ref_complex(stim, gnuradio_script=script)


def _compare(dut, gr):
    # identity (no arithmetic) → bit-exact on BOTH channels.
    return compare_complex_against_grc(dut.i_q15, dut.q_q15, gr.i, gr.q,
                                       metric=Metric.EXACT, delay=0)


# --- structure ----------------------------------------------------------------

@pytest.mark.parametrize("variant", ["c2f", "f2c"])
def test_drives_and_captures(variant):
    block_type, _ = _VARIANTS[variant]
    dut = _run_dut(block_type, _random(1, 12))
    assert dut.words_per_sample == 2
    assert dut.in_regs == (0, 1)
    assert all(v is not None for v in dut.i_q15) and all(v is not None for v in dut.q_q15)


# --- exact equivalence vs GNU Radio -------------------------------------------

@pytest.mark.parametrize("variant", ["c2f", "f2c"])
def test_edge_vectors(variant):
    block_type, script = _VARIANTS[variant]
    dut = _run_dut(block_type, EDGE)
    gr = _gr(script, EDGE)
    res = _compare(dut, gr)
    print(f"\n{variant} edge:", res.summary())
    assert res.passed, res.summary()


@pytest.mark.parametrize("variant", ["c2f", "f2c"])
@pytest.mark.parametrize("seed", [1, 7, 42])
def test_random_vectors(variant, seed):
    block_type, script = _VARIANTS[variant]
    stim = _random(seed)
    dut = _run_dut(block_type, stim)
    gr = _gr(script, stim)
    res = _compare(dut, gr)
    print(f"\n{variant} random seed={seed}:", res.summary())
    assert res.passed, res.summary()


# --- MANDATORY mutation tests -------------------------------------------------

def _setup(variant):
    block_type, script = _VARIANTS[variant]
    stim = _random(7, 32)
    dut = _run_dut(block_type, stim)
    gr = _gr(script, stim)
    return dut, gr


@pytest.mark.parametrize("variant", ["c2f", "f2c"])
def test_mutation_swapped_channels_fails(variant):
    """Swapping re<->im must FAIL (catches a transposed conversion)."""
    dut, gr = _setup(variant)
    res = compare_complex_against_grc(dut.q_q15, dut.i_q15, gr.i, gr.q,
                                      metric=Metric.EXACT, delay=0)
    assert not res.passed, "gate failed to detect swapped re/im!"


@pytest.mark.parametrize("variant", ["c2f", "f2c"])
def test_mutation_negated_imag_fails(variant):
    """Negating the imaginary channel (a conjugate) must FAIL."""
    dut, gr = _setup(variant)
    neg_q = [(0x10000 - (w or 0)) & 0xFFFF for w in dut.q_q15]
    res = compare_complex_against_grc(dut.i_q15, neg_q, gr.i, gr.q,
                                      metric=Metric.EXACT, delay=0)
    assert not res.passed, "gate failed to detect a negated imag channel!"


@pytest.mark.parametrize("variant", ["c2f", "f2c"])
def test_mutation_one_sample_offset_fails(variant):
    dut, gr = _setup(variant)
    sh_i = [0x0000] + list(dut.i_q15[:-1])
    sh_q = [0x0000] + list(dut.q_q15[:-1])
    res = compare_complex_against_grc(sh_i, sh_q, gr.i, gr.q,
                                      metric=Metric.EXACT, delay=0)
    assert not res.passed, "gate failed to detect a 1-sample latency error!"


@pytest.mark.parametrize("variant", ["c2f", "f2c"])
def test_empty_output_fails(variant):
    _, gr = _setup(variant)
    res = compare_complex_against_grc([], [], gr.i, gr.q,
                                      metric=Metric.EXACT, delay=0)
    assert not res.passed


# --- dashboard reports --------------------------------------------------------

@pytest.mark.parametrize("variant", ["c2f", "f2c"])
def test_emit_report(variant):
    import json
    block_type, script = _VARIANTS[variant]
    dut = _run_dut(block_type, EDGE)
    gr = _gr(script, EDGE)
    res = _compare(dut, gr)
    assert res.passed, res.summary()
    report = {
        "kyttar_block": block_type, "passed": True, "metric": "exact",
        "n_compared": res.i.n_compared, "max_abs_err": res.i.max_abs_err,
        "tolerance": res.i.tolerance, "nmse_db": res.i.nmse_db,
        "correlation": res.i.correlation, "bit_errors": 0, "delay_used": 0,
        "coverage": {"edge": True, "random": 3, "bit_exact": True, "mutation": True},
    }
    (_VERIFY / "reports").mkdir(exist_ok=True)
    (_VERIFY / "reports" / f"{block_type}.json").write_text(json.dumps(report))
