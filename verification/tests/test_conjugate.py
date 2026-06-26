# SPDX-License-Identifier: GPL-3.0-or-later
"""Verify ConjugateBlock against GNU Radio blocks.conjugate_cc.

Complex conjugate: out = re − j·im (negate the imaginary part). The staple of
correlators / conjugate-multiply. On chip: pass re through, negate im (0 − im),
emit the two words. Pure data movement + one negate → EXACT (no Q15 error) for
all but the lone im = −1.0 corner (whose negate wraps), so the gate is bit-exact.

Per INV-4 every gate is paired with a mutation that must FAIL — crucially a
NOT-conjugated DUT (im passed through un-negated), which proves the block actually
conjugates rather than echoing the input. Memoryless → delay=0.

Run:
    KYTTAR_GR_PYTHON=/usr/bin/python3 QT_QPA_PLATFORM=offscreen \
      <venv>/python -m pytest verification/tests/test_conjugate.py -x -q
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
from gr_kyttar.placement.blocks.conjugate_block import ConjugateBlock  # noqa: E402

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


def _q15(v: float) -> int:
    q = int(round(v * 32768.0))
    return max(-32768, min(32767, q)) & 0xFFFF


# Off the im = −1.0 negate-wrap corner so the DUT tracks GR float exactly.
EDGE = [complex(0.0, 0.0), complex(0.5, -0.5), complex(-0.999, 0.999),
        complex(0.25, 0.75), complex(-0.6, 0.5), complex(0.6, -0.99),
        complex(-0.3, -0.7), complex(0.9, 0.1)]


def _random(seed, n=24):
    rng = random.Random(seed)
    return [complex(rng.uniform(-0.99, 0.99), rng.uniform(-0.99, 0.99))
            for _ in range(n)]


def _run_dut(stim):
    dut = run_block_dut_complex(
        "ConjugateBlock", stim, chip_yaml=CHIP_YAML,
        in_ports=("re", "im"), words_per_sample=2)
    assert dut.ok, dut.reason
    return dut


def _gr(stim):
    return run_gnuradio_ref_complex(
        stim,
        gnuradio_script="""
from gnuradio import gr, blocks
tb = gr.top_block()
src = blocks.vector_source_c(input_complex, False)
c = blocks.conjugate_cc()
snk = blocks.vector_sink_c()
tb.connect(src, c); tb.connect(c, snk)
tb.run()
output_complex = list(snk.data())
""")


def _compare(dut, gr):
    return compare_complex_against_grc(dut.i_q15, dut.q_q15, gr.i, gr.q,
                                       metric=Metric.EXACT, delay=0)


# --- structure ----------------------------------------------------------------

def test_drives_and_captures():
    dut = _run_dut(_random(1, 12))
    assert dut.words_per_sample == 2
    assert dut.in_regs == (0, 1)
    assert all(v is not None for v in dut.i_q15) and all(v is not None for v in dut.q_q15)


# --- exact equivalence vs GNU Radio -------------------------------------------

def test_edge_vectors():
    dut = _run_dut(EDGE)
    gr = _gr(EDGE)
    res = _compare(dut, gr)
    print("\nedge:", res.summary())
    assert res.passed, res.summary()


@pytest.mark.parametrize("seed", [1, 7, 42])
def test_random_vectors(seed):
    stim = _random(seed)
    dut = _run_dut(stim)
    gr = _gr(stim)
    res = _compare(dut, gr)
    print(f"\nrandom seed={seed}:", res.summary())
    assert res.passed, res.summary()


# --- bit-exact substrate (includes the im = -1.0 negate-wrap corner) ----------

def test_bitexact_reference_with_corner():
    stim = _random(3, 40) + [complex(0.3, -1.0), complex(-1.0, -1.0)]
    dut = _run_dut(stim)
    blk = ConjugateBlock("ref")
    a = [_q15(c.real) for c in stim]
    b = [_q15(c.imag) for c in stim]
    ref = blk.process_reference_q15(a, b)
    ri = [_s16(r[0]) / 32768.0 for r in ref]
    rq = [_s16(r[1]) / 32768.0 for r in ref]
    res = compare_complex_against_grc(dut.i_q15, dut.q_q15, ri, rq,
                                      metric=Metric.EXACT, delay=0)
    print("\nbit-exact (incl corner):", res.summary())
    assert res.passed, res.summary()
    # the im=-1.0 corner negates to -1.0 (wrap), NOT +1.0
    assert _s16(ref[-2][1]) == -32768


# --- MANDATORY mutation tests -------------------------------------------------

def _setup():
    stim = _random(7, 32)
    dut = _run_dut(stim)
    gr = _gr(stim)
    return dut, gr


def test_mutation_not_conjugated_fails():
    """A DUT that did NOT negate im (passed it through) must FAIL — proves the
    block actually conjugates."""
    dut, gr = _setup()
    un_neg = [(0x10000 - (w or 0)) & 0xFFFF for w in dut.q_q15]  # undo the negate
    res = compare_complex_against_grc(dut.i_q15, un_neg, gr.i, gr.q,
                                      metric=Metric.EXACT, delay=0)
    assert not res.passed, "gate failed to detect a non-conjugated output!"


def test_mutation_swapped_channels_fails():
    dut, gr = _setup()
    res = compare_complex_against_grc(dut.q_q15, dut.i_q15, gr.i, gr.q,
                                      metric=Metric.EXACT, delay=0)
    assert not res.passed, "gate failed to detect swapped re/im!"


def test_mutation_one_sample_offset_fails():
    dut, gr = _setup()
    sh_i = [0x0000] + list(dut.i_q15[:-1])
    sh_q = [0x0000] + list(dut.q_q15[:-1])
    res = compare_complex_against_grc(sh_i, sh_q, gr.i, gr.q,
                                      metric=Metric.EXACT, delay=0)
    assert not res.passed, "gate failed to detect a 1-sample latency error!"


def test_empty_output_fails():
    _, gr = _setup()
    res = compare_complex_against_grc([], [], gr.i, gr.q,
                                      metric=Metric.EXACT, delay=0)
    assert not res.passed


# --- dashboard report ---------------------------------------------------------

def test_emit_report():
    import json
    dut = _run_dut(EDGE)
    gr = _gr(EDGE)
    res = _compare(dut, gr)
    assert res.passed, res.summary()
    report = {
        "kyttar_block": "ConjugateBlock", "passed": True, "metric": "exact",
        "n_compared": res.i.n_compared, "max_abs_err": res.i.max_abs_err,
        "tolerance": res.i.tolerance, "nmse_db": res.i.nmse_db,
        "correlation": res.i.correlation, "bit_errors": 0, "delay_used": 0,
        "coverage": {"edge": True, "random": 3, "bit_exact": True, "mutation": True},
    }
    (_VERIFY / "reports").mkdir(exist_ok=True)
    (_VERIFY / "reports" / "ConjugateBlock.json").write_text(json.dumps(report))
