# SPDX-License-Identifier: GPL-3.0-or-later
"""Verify Nlog10Block is a Q15 drop-in for GNU Radio ``blocks.nlog10_ff``.

GR ``nlog10_ff(n, vlen, k)`` computes ``out = n*log10(in) + k`` (power/level -> dB).
On the Q15 fabric a "float" sample lives in [-1, 1); a dB value is ~45x outside that
span, so the block emits a SCALED dB word: ``out_word/32768 == (n*log10(in)+k)/db_scale``
with ``db_scale`` an auto-derived power of two (default 64 for n=10, k=0). See the
class docstring / INV-0 HW-DEVIATION. This suite:

  * feeds the SAME positive Q15 inputs to the DUT (built + simulated on simKYT) and
    to a LIVE GNU Radio ``nlog10_ff`` (the golden reference), comparing the DUT's
    RECOVERED dB (word * db_scale) against GR's dB within the DERIVED Q15 tolerance
    (10 LSB, from the fixed-point error budget — see the class docstring);
  * sweeps the positive-input domain incl. powers of ten, near 1.0, small fractions,
    and the exact powers of two the normalizer keys on;
  * covers the ``in <= 0`` edge (GR clamps to a huge-negative dB floor; the scaled
    Q15 floor is ``-db_scale`` dB = word 0x8000);
  * sweeps ``n`` and ``k``;
  * FIRST proves GR itself produces the expected dB on the stimulus (INV-26), and
  * includes the mandatory mutation tests (INV-4): the gate MUST fail on a
    natural-log DUT (ln instead of log10), a dropped ``k``, a wrong ``n``, an
    inverted output, a +1-sample delay, and empty output.

Run:
    KYTTAR_GR_PYTHON=/usr/bin/python3 QT_QPA_PLATFORM=offscreen \
      <venv>/python -m pytest verification/tests/test_nlog10.py -q
"""

from __future__ import annotations

import math
import os
import random
import sys
from pathlib import Path

import numpy as np
import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

_PLACEKYT = Path(__file__).resolve().parents[2] / "placekyt"
_VERIFY = Path(__file__).resolve().parents[1]
for p in (str(_PLACEKYT), str(_VERIFY)):
    if p not in sys.path:
        sys.path.insert(0, p)

from kyttar_verify import (  # noqa: E402
    run_block_dut, run_gnuradio_ref, compare_against_grc, write_report, Metric)
from gr_kyttar.placement.blocks import all_block_classes  # noqa: E402

CHIP_YAML = str(_PLACEKYT / "resources" / "chips" / "kyttar_10x12.yaml")

_GR_AVAILABLE = os.path.exists(os.environ.get("KYTTAR_GR_PYTHON", "/usr/bin/python3"))
pytestmark = pytest.mark.skipif(
    not (os.path.exists(CHIP_YAML) and _GR_AVAILABLE),
    reason="chip yaml or GNU Radio interpreter absent")

# The DERIVED Q15 tolerance (see the class docstring / INV-4): the cubic-log2 approx
# error is sub-LSB; the dominant term is A_q15 rounding (+-0.5 LSB) multiplied by the
# exponent |e-15| <= 15 -> up to ~7.5 LSB, + C_q15/final rounding -> ~9.2 LSB. Bound
# at 10 LSB. This is a scaled-dB Q15 LSB (db_scale/32768 dB each). NOT tuned to pass.
TOL_LSB = 10


def _cls():
    return all_block_classes()["Nlog10Block"]


def _db_scale(n, k):
    return _cls()("probe", n=n, k=k).db_scale


def _q15w(x_float: float) -> int:
    """A positive linear input as a Q15 word."""
    q = int(round(x_float * 32768.0))
    return max(-32768, min(32767, q)) & 0xFFFF


def _gr_nlog10(words, n, k):
    """LIVE GNU Radio nlog10_ff over the Q15 words (interpreted as linear in)."""
    return run_gnuradio_ref(
        input_q15=words,
        gnuradio_script="""
from gnuradio import gr, blocks
# each fabric word is a signed Q15 numerator -> linear input in/32768
xs = [((w - 0x10000) if w >= 0x8000 else w) / 32768.0 for w in input_q15]
tb = gr.top_block()
src = blocks.vector_source_f(xs)
op = blocks.nlog10_ff(n, 1, k)
snk = blocks.vector_sink_f()
tb.connect(src, op); tb.connect(op, snk)
tb.run()
output_float = list(snk.data())
""",
        extra_args={"n": float(n), "k": float(k)},
    )


def _run_and_compare(n, k, words, tol=TOL_LSB):
    """Compare the DUT's scaled-dB Q15 words against LIVE GR (scaled by 1/db_scale so
    the harness quantizes GR's dB into the SAME scaled Q15 the DUT emits)."""
    dut = run_block_dut("Nlog10Block", words, params={"n": n, "k": k},
                        chip_yaml=CHIP_YAML)
    assert dut.ok, dut.reason
    gr = _gr_nlog10(words, n, k)
    ds = _db_scale(n, k)
    # GR dB -> scaled Q15 float (clip to the representable [-db_scale, db_scale) span
    # so the floor case maps to -1.0 exactly like the DUT).
    scaled = [max(-1.0, min(32767.0 / 32768.0, v / ds)) for v in gr.floats]
    res = compare_against_grc(dut.outputs_q15, scaled, metric=Metric.AMPLITUDE,
                              delay=0, tolerance=tol)
    return dut, res, ds


# --- stimulus families --------------------------------------------------------
# powers of ten (as fractions of full scale), near 1.0, small fractions, powers of
# two the normalizer keys on, and mid values.
_POWERS_OF_TEN = [1.0, 0.1, 0.01, 0.001]
_NEAR_ONE = [32767 / 32768, 0.9, 0.75, 0.5]
_SMALL = [2 ** -10, 2 ** -14, 2 ** -15]
_POW2 = [2 ** -e for e in range(0, 15)]
_MID = [0.3, 0.123, 0.6667, 0.05]

EDGE_FLOATS = _POWERS_OF_TEN + _NEAR_ONE + _SMALL + _MID
EDGE = [_q15w(x) for x in EDGE_FLOATS]
POW2_WORDS = [_q15w(x) for x in _POW2]


def _random_words(seed, n=32):
    rng = random.Random(seed)
    # positive Q15 numerators across the whole domain
    return [rng.randint(1, 32767) for _ in range(n)]


# --- GR competence FIRST (INV-26) ---------------------------------------------

def test_gr_produces_expected_db():
    """The golden reference must itself be correct on the stimulus before we gate
    the DUT against it (INV-26): GR nlog10_ff(10,·,0) at in=0.5 is ~-3.01 dB."""
    gr = _gr_nlog10([_q15w(0.5), _q15w(0.1), _q15w(1.0)], 10.0, 0.0)
    vals = list(gr.floats)
    assert abs(vals[0] - (-3.0103)) < 1e-2, vals
    assert abs(vals[1] - (-10.0)) < 1e-2, vals
    assert abs(vals[2] - 0.0) < 1e-2, vals


# --- equivalence: DUT vs LIVE GR ----------------------------------------------

def test_edge_vectors_default():
    dut, res, ds = _run_and_compare(10.0, 0.0, EDGE)
    print(f"\nedge n=10 k=0 (db_scale={ds}):", res.summary())
    assert res.passed, res.summary()


def test_powers_of_two():
    """Exact powers of two exercise every exponent bucket the normalizer counts."""
    dut, res, ds = _run_and_compare(10.0, 0.0, POW2_WORDS)
    print(f"\npowers-of-two n=10 (db_scale={ds}):", res.summary())
    assert res.passed, res.summary()


def test_full_domain_sweep():
    """A dense sweep across the whole positive Q15 domain."""
    words = list(range(1, 32768, 137))
    dut, res, ds = _run_and_compare(10.0, 0.0, words)
    print(f"\nfull-domain n=10 (db_scale={ds}):", res.summary(),
          f"| max_abs_err={res.max_abs_err} LSB")
    assert res.passed, res.summary()


@pytest.mark.parametrize("seed", [1, 7, 42])
def test_random_vectors(seed):
    words = _random_words(seed)
    dut, res, ds = _run_and_compare(10.0, 0.0, words)
    print(f"\nrandom seed={seed}:", res.summary())
    assert res.passed, res.summary()


@pytest.mark.parametrize("n,k", [(10.0, 0.0), (20.0, 0.0), (1.0, 0.0),
                                 (10.0, 30.0), (5.0, 10.0), (10.0, -10.0)])
def test_param_sweep_n_k(n, k):
    dut, res, ds = _run_and_compare(n, k, EDGE)
    print(f"\nn={n} k={k} (db_scale={ds}):", res.summary())
    assert res.passed, res.summary()


def test_in_le_zero_floor():
    """in <= 0 (word 0 and negatives) -> the scaled Q15 floor 0x8000 (-db_scale dB).
    GR clamps in to a tiny epsilon and returns a huge-negative dB; both are the most
    negative representable value, so the comparison passes at the floor."""
    words = [0x0000, (-16384) & 0xFFFF, (-1) & 0xFFFF, 0x8000]  # 0, -0.5, -eps, -1.0
    dut = run_block_dut("Nlog10Block", words, params={"n": 10.0, "k": 0.0},
                        chip_yaml=CHIP_YAML)
    assert dut.ok, dut.reason
    for o in dut.outputs_q15:
        assert (o is not None) and ((o & 0xFFFF) == 0x8000), \
            f"in<=0 must emit the Q15 floor 0x8000, got {o:#06x}"


def test_delay_is_zero():
    """Memoryless -> group delay 0: y[n] tracks in[n] with no lag (a +1-delay
    comparison FAILS — see the mutation below)."""
    dut, res, ds = _run_and_compare(10.0, 0.0, EDGE)
    assert res.passed and res.delay_used == 0, res.summary()


# --- MANDATORY mutation tests (INV-4): the gate must DETECT corruption ---------

def test_mutation_natural_log_fails():
    """A DUT that computed n*ln(in)+k (natural log, ~2.3026x log10) must FAIL vs the
    log10 golden — the single most important semantic mutation for this block."""
    ds = _db_scale(10.0, 0.0)
    gr = _gr_nlog10(EDGE, 10.0, 0.0)
    ref_scaled = [max(-1.0, min(32767.0 / 32768.0, v / ds)) for v in gr.floats]
    # fabricate a ln-based DUT output (scaled Q15) on the SAME inputs
    mutated = []
    for w in EDGE:
        x = ((w - 0x10000) if w >= 0x8000 else w) / 32768.0
        db = 10.0 * math.log(x) if x > 0 else -ds  # ln, not log10
        q = int(round(max(-1.0, min(32767.0 / 32768.0, db / ds)) * 32768))
        mutated.append(q & 0xFFFF)
    res = compare_against_grc(mutated, ref_scaled, metric=Metric.AMPLITUDE,
                              delay=0, tolerance=TOL_LSB)
    assert not res.passed, "gate failed to detect a natural-log (ln) DUT!"


def test_mutation_dropped_k_fails():
    """A DUT built with k=0 must FAIL against a GR reference built with k=30 (the k
    term is real and must reach the output)."""
    dut = run_block_dut("Nlog10Block", EDGE, params={"n": 10.0, "k": 0.0},
                        chip_yaml=CHIP_YAML)
    assert dut.ok, dut.reason
    ds = _db_scale(10.0, 30.0)   # GR reference uses k=30
    gr = _gr_nlog10(EDGE, 10.0, 30.0)
    ref_scaled = [max(-1.0, min(32767.0 / 32768.0, v / ds)) for v in gr.floats]
    res = compare_against_grc(dut.outputs_q15, ref_scaled,
                              metric=Metric.AMPLITUDE, delay=0, tolerance=TOL_LSB)
    assert not res.passed, "gate failed to detect a dropped k!"


def test_mutation_wrong_n_fails():
    """A DUT built at n=10 must FAIL against a GR reference built at n=12.

    NOTE: n=12 shares the SAME db_scale (64) as n=10, so the scaled-Q15 words
    genuinely differ. (A proportionally-larger n like 20 also scales db_scale to
    128, leaving A = n*log10(2)/db_scale — hence the scaled word — nearly
    unchanged, which would NOT exercise the gate. n=12 is the honest mutation.)"""
    dut = run_block_dut("Nlog10Block", EDGE, params={"n": 10.0, "k": 0.0},
                        chip_yaml=CHIP_YAML)
    assert dut.ok, dut.reason
    assert _db_scale(12.0, 0.0) == _db_scale(10.0, 0.0)  # same scale -> real diff
    ds = _db_scale(12.0, 0.0)
    gr = _gr_nlog10(EDGE, 12.0, 0.0)
    ref_scaled = [max(-1.0, min(32767.0 / 32768.0, v / ds)) for v in gr.floats]
    res = compare_against_grc(dut.outputs_q15, ref_scaled,
                              metric=Metric.AMPLITUDE, delay=0, tolerance=TOL_LSB)
    assert not res.passed, "gate failed to detect a wrong n!"


def test_mutation_inverted_output_fails():
    dut, res, ds = _run_and_compare(10.0, 0.0, EDGE)
    assert res.passed
    gr = _gr_nlog10(EDGE, 10.0, 0.0)
    ref_scaled = [max(-1.0, min(32767.0 / 32768.0, v / ds)) for v in gr.floats]
    mutated = [(0x10000 - (w or 0)) & 0xFFFF for w in dut.outputs_q15]  # negate
    r2 = compare_against_grc(mutated, ref_scaled, metric=Metric.AMPLITUDE,
                             delay=0, tolerance=TOL_LSB)
    assert not r2.passed, "gate failed to detect an inverted output!"


def test_mutation_one_sample_offset_fails():
    dut, res, ds = _run_and_compare(10.0, 0.0, EDGE)
    assert res.passed
    gr = _gr_nlog10(EDGE, 10.0, 0.0)
    ref_scaled = [max(-1.0, min(32767.0 / 32768.0, v / ds)) for v in gr.floats]
    shifted = [0x0000] + list(dut.outputs_q15[:-1])   # +1 sample delay
    r2 = compare_against_grc(shifted, ref_scaled, metric=Metric.AMPLITUDE,
                             delay=0, tolerance=TOL_LSB)
    assert not r2.passed, "gate failed to detect a 1-sample latency error!"


def test_empty_output_fails():
    ds = _db_scale(10.0, 0.0)
    gr = _gr_nlog10(EDGE, 10.0, 0.0)
    ref_scaled = [v / ds for v in gr.floats]
    res = compare_against_grc([], ref_scaled, metric=Metric.AMPLITUDE,
                              tolerance=TOL_LSB)
    assert not res.passed


# --- HW-DEVIATION surfacing ----------------------------------------------------

def test_db_scale_is_power_of_two_and_fits():
    """db_scale is a derived power of two that keeps the dB output inside Q15."""
    for n, k in [(10.0, 0.0), (20.0, 0.0), (1.0, 0.0), (10.0, 30.0), (5.0, 10.0)]:
        b = _cls()("t", n=n, k=k)
        ds = b.db_scale
        # power of two
        assert ds >= 1.0 and (int(ds) & (int(ds) - 1)) == 0, (n, k, ds)
        # the most negative dB (in=2^-15) fits the scaled [-1, 1) span
        lo = n * math.log10(2 ** -15) + k
        assert -1.0 <= lo / ds < 1.0, (n, k, ds, lo)


def test_emit_report():
    """Emit the dashboard report (records verified metrics + coverage). Runs last so
    it reflects a passing verification."""
    dut, res, ds = _run_and_compare(10.0, 0.0, EDGE)
    assert res.passed, res.summary()
    write_report("Nlog10Block", res, coverage={
        "edge": True, "random": 3, "param_sweep": 6, "powers_of_two": True,
        "full_domain": True, "in_le_zero_floor": True, "hw_deviation": True,
        "mutation": True,
    })
