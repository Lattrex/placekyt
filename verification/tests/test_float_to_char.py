# SPDX-License-Identifier: GPL-3.0-or-later
"""Verify FloatToCharBlock — the drop-in for GNU Radio ``blocks.float_to_char``.

GNU Radio ``float_to_char(scale)`` computes ``out = saturate_int8(lrintf(in*scale))``
with round-to-nearest-**ties-to-even** and int8 saturation to [-128, 127]. The Kyttar
block computes the SAME function bit-for-bit on the Q15 fabric representation of the
input (``in = k/32768``, ``k`` int in [-32768, 32767]):

    P = k*scale ; q = round_half_even(P/2^15) ; out = clamp(q, -128, 127)

The gate compares the on-chip DUT (built + simulated on simKYT) against LIVE GNU Radio
``float_to_char``, driving GR with the SAME Q15-quantised value the chip sees, so the
comparison is exact on the block's supported domain:

  * DOMAIN — input: the whole Q15 range [-1, 1); scale: a non-negative integer
    (HW-DEVIATION; the block RAISES otherwise).
  * BIT-EXACT — the int8 output word must equal GR's int8 exactly (this is a
    quantiser, not an amplitude block: no tolerance, the decision must match).
  * SATURATION EDGES — ±full-scale inputs at scale that push past ±128 pin to
    +127 / -128 exactly.
  * ROUND-HALF-TO-EVEN — inputs constructed to land exactly on a half-way tie
    (k*scale = 2^14*(2m+1)) round to the EVEN neighbour, matching GR.
  * MUTATION (INV-4) — a truncating (floor) DUT, a wrap-instead-of-saturate DUT,
    a wrong-scale reference, and a 1-sample shift must all FAIL the gate.

Run::

    KYTTAR_GR_PYTHON=/usr/bin/python3 QT_QPA_PLATFORM=offscreen \
        PYTHONPATH=<worktree>/runtime/python \
        .venv/bin/python -m pytest verification/tests/test_float_to_char.py -q
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
for p in (str(_RUNTIME), str(_PLACEKYT), str(_VERIFY)):
    if p not in sys.path:
        sys.path.insert(0, p)

from kyttar_verify import (  # noqa: E402
    run_block_dut, run_gnuradio_ref, write_report, CompareResult, Metric)
from gr_kyttar.placement.blocks.float_to_char_block import FloatToCharBlock  # noqa: E402

CHIP_YAML = str(_PLACEKYT / "resources" / "chips" / "kyttar_10x12.yaml")
_GR_AVAILABLE = os.path.exists(os.environ.get("KYTTAR_GR_PYTHON", "/usr/bin/python3"))
pytestmark = pytest.mark.skipif(
    not (os.path.exists(CHIP_YAML) and _GR_AVAILABLE),
    reason="chip yaml or GNU Radio interpreter absent")


def _q15(v: float) -> int:
    q = int(round(v * 32768.0))
    return max(-32768, min(32767, q)) & 0xFFFF


def _s8(w: int) -> int:
    """Interpret a raw output word's low byte as a signed int8 (the block's output
    convention: an int8 sign-extended into a 16-bit word)."""
    w &= 0xFFFF
    b = w & 0xFF
    return b - 256 if b >= 128 else b


def _gr_float_to_char(inputs_q15, scale):
    """LIVE GNU Radio float_to_char, driven with the SAME Q15 values the chip sees.

    Returns the int8 outputs. We read the raw ``floats`` field (GR's int8 cast to
    float), NOT the harness's Q15 re-quantisation — an int8 like 127 is not a Q15
    fraction, so the default q15 field would corrupt it."""
    res = run_gnuradio_ref(
        input_q15=inputs_q15,
        gnuradio_script="""
from gnuradio import gr, blocks
tb = gr.top_block()
src = blocks.vector_source_f(input_float, False)
f2c = blocks.float_to_char(1, float(scale))
sink = blocks.vector_sink_b()
tb.connect(src, f2c); tb.connect(f2c, sink)
tb.run()
# vector_sink_b yields the raw byte value (0..255); reinterpret as signed int8.
output_float = [float(int(v) - 256 if int(v) >= 128 else int(v)) for v in sink.data()]
""",
        extra_args={"scale": int(scale)},
    )
    return [int(round(f)) for f in res.floats]


def _run_dut(inputs_q15, scale):
    dut = run_block_dut("FloatToCharBlock", inputs_q15,
                        params={"scale": float(scale)}, chip_yaml=CHIP_YAML)
    assert dut.ok, dut.reason
    return dut


def _compare(scale, inputs_q15, *, label=""):
    dut = _run_dut(inputs_q15, scale)
    gr = _gr_float_to_char(inputs_q15, scale)
    dut_i8 = [_s8(w) if w is not None else None for w in dut.outputs_q15]
    n = min(len(dut_i8), len(gr))
    errs = [(k, inputs_q15[k], dut_i8[k], gr[k])
            for k in range(n) if dut_i8[k] != gr[k]]
    return dut, dut_i8, gr, errs


# --- stimulus families ---------------------------------------------------------

# Edge Q15 words: 0, +/-0.5, +/-0.25, +/-full-scale, near-rail.
EDGE = [0x0000, 0x4000, 0x2000, 0xC000, 0xE000, 0x7FFF, 0x8000, 0x8001, 0x6000, 0xA000]


def _random_words(seed, n=48):
    rng = random.Random(seed)
    return [rng.randint(0, 0xFFFF) for _ in range(n)]


def _tie_words(scale, span=6):
    """Q15 words k where k*scale = 2^14*(2m+1) exactly — the round-half-even ties."""
    ws = []
    for m in range(-span, span + 1):
        num = (1 << 14) * (2 * m + 1)
        if num % scale == 0:
            k = num // scale
            if -32768 <= k <= 32767:
                ws.append(k & 0xFFFF)
    return ws


# --- correctness: bit-exact vs live GR ----------------------------------------

@pytest.mark.parametrize("scale", [1, 2, 127, 128])
def test_edge_bit_exact(scale):
    dut, dut_i8, gr, errs = _compare(scale, EDGE)
    print(f"\nscale={scale} edge: dut={dut_i8} gr={gr} | hop {dut.hop_count}")
    assert not errs, f"scale={scale} mismatches (k,dut,gr): {errs}"


@pytest.mark.parametrize("scale", [1, 2, 64, 127, 128, 200])
@pytest.mark.parametrize("seed", [1, 7, 42])
def test_random_bit_exact(scale, seed):
    dut, dut_i8, gr, errs = _compare(scale, _random_words(seed))
    print(f"\nscale={scale} seed={seed}: {len(gr)} samples, {len(errs)} errs")
    assert not errs, f"scale={scale} seed={seed} mismatches: {errs[:8]}"


@pytest.mark.parametrize("scale", [2, 3, 7, 64, 127, 128])
def test_round_half_to_even_ties(scale):
    """Exact half-way ties must round to the EVEN integer, matching GR's lrintf."""
    ties = _tie_words(scale)
    if not ties:
        pytest.skip(f"no representable Q15 tie for scale={scale}")
    dut, dut_i8, gr, errs = _compare(scale, ties)
    print(f"\nscale={scale} ties: k={ties} dut={dut_i8} gr={gr}")
    assert not errs, f"round-half-even mismatch scale={scale}: {errs}"


def test_saturation_edges():
    """+/-full-scale at a large scale pins to +127 / -128 exactly (int8 rails)."""
    # scale=200: 0.999969*200 -> 199 -> +127; -1.0*200 -> -200 -> -128.
    inputs = [0x7FFF, 0x8000, _q15(0.9), _q15(-0.9), _q15(0.64), _q15(-0.64)]
    dut, dut_i8, gr, errs = _compare(200, inputs)
    print(f"\nsaturation scale=200: dut={dut_i8} gr={gr}")
    assert not errs, f"saturation mismatch: {errs}"
    assert dut_i8[0] == 127 and gr[0] == 127
    assert dut_i8[1] == -128 and gr[1] == -128


def test_reference_matches_chip_bit_exact():
    """The block's own process_reference equals the on-chip stream, word for word."""
    blk = FloatToCharBlock("f", scale=127)
    words = _random_words(99, 40)
    dut = _run_dut(words, 127)
    dut_i8 = [_s8(w) for w in dut.outputs_q15]
    ref = [int(x) for x in blk.process_reference(words)]
    assert dut_i8 == ref, f"chip != reference:\n chip {dut_i8}\n ref  {ref}"


# --- MANDATORY negative tests (INV-4) -----------------------------------------

def test_mutation_truncate_instead_of_round_fails():
    """A DUT that TRUNCATES (floor) instead of round-half-even must FAIL."""
    scale = 127
    dut = _run_dut(EDGE, scale)
    gr = _gr_float_to_char(EDGE, scale)
    # floor(k*scale/2^15) — the truncating mutation.
    mutated = []
    for w in EDGE:
        k = w - 0x10000 if w >= 0x8000 else w
        q = (k * scale) >> 15
        mutated.append(max(-128, min(127, q)))
    assert mutated != gr[:len(mutated)], \
        "gate missed a truncate-instead-of-round mutation!"


def test_mutation_wrap_instead_of_saturate_fails():
    """A DUT that WRAPS (mod 256) instead of saturating to int8 must FAIL."""
    scale = 200  # forces past +/-128 so wrap != saturate
    gr = _gr_float_to_char(EDGE, scale)
    wrapped = []
    for w in EDGE:
        k = w - 0x10000 if w >= 0x8000 else w
        P = k * scale
        q = P >> 15
        r = P - (q << 15)
        if r > (1 << 14) or (r == (1 << 14) and (q & 1)):
            q += 1
        b = q & 0xFF                       # WRAP to 8 bits (no saturation)
        wrapped.append(b - 256 if b >= 128 else b)
    assert wrapped != gr[:len(wrapped)], \
        "gate missed a wrap-instead-of-saturate mutation!"


def test_mutation_round_half_up_fails():
    """A DUT that rounds half-UP (ties toward +inf) instead of half-to-EVEN must
    disagree with GR on the exact-tie inputs — proves the tie handling is real."""
    scale = 128
    ties = _tie_words(scale)
    assert ties, "expected representable ties for scale=128"
    gr = _gr_float_to_char(ties, scale)
    up = []
    for w in ties:
        k = w - 0x10000 if w >= 0x8000 else w
        P = k * scale
        q = P >> 15
        r = P - (q << 15)
        if r >= (1 << 14):        # round half UP (the mutation)
            q += 1
        up.append(max(-128, min(127, q)))
    assert up != gr, "gate missed a round-half-up (not half-even) mutation!"


def test_mutation_wrong_scale_fails():
    """A DUT built at the wrong scale must disagree with the right reference."""
    dut = _run_dut(EDGE, 127)
    dut_i8 = [_s8(w) for w in dut.outputs_q15]
    gr_wrong = _gr_float_to_char(EDGE, 64)   # reference for a DIFFERENT scale
    assert dut_i8 != gr_wrong[:len(dut_i8)], "gate missed a wrong-scale mismatch!"


def test_mutation_one_sample_offset_fails():
    dut = _run_dut(EDGE, 127)
    dut_i8 = [_s8(w) for w in dut.outputs_q15]
    gr = _gr_float_to_char(EDGE, 127)
    shifted = [0] + dut_i8[:-1]
    assert shifted != gr[:len(shifted)], "gate missed a 1-sample latency error!"


def test_empty_output_fails():
    gr = _gr_float_to_char(EDGE, 127)
    assert gr and [] != gr


def test_non_integer_scale_raises():
    """HW-DEVIATION: a non-integer scale is not representable and must RAISE."""
    with pytest.raises(ValueError):
        FloatToCharBlock("f", scale=1.5)
    with pytest.raises(ValueError):
        FloatToCharBlock("f", scale=-1)


# --- report --------------------------------------------------------------------

def test_emit_report():
    scale = 127
    dut, dut_i8, gr, errs = _compare(scale, _random_words(5, 32))
    n = min(len(dut_i8), len(gr))
    bit_errs = sum(1 for k in range(n) if dut_i8[k] != gr[k])
    res = CompareResult(passed=(bit_errs == 0), metric=Metric.DECISION,
                        n_compared=n, bit_errors=bit_errs, delay_used=0)
    assert res.passed, res.summary()
    write_report("FloatToCharBlock", res, coverage={
        "decision": "out = clamp(round_half_even(k*scale/2^15), -128, 127), int8",
        "domain": "input Q15 [-1,1); scale = non-negative INTEGER (HW-DEVIATION)",
        "bit_exact": "vs LIVE GNU Radio float_to_char on the whole Q15 range",
        "edge": True, "random": 3, "param_sweep": "scale in {1,2,64,127,128,200}",
        "round_half_even": "exact k*scale=2^14*(2m+1) ties -> even, matches lrintf",
        "saturation": "+/-full-scale -> +127/-128 rails, verified vs GR",
        "mutation": "truncate, wrap-not-saturate, wrong-scale, 1-sample-shift all FAIL",
        "gr_equiv": "blocks.float_to_char(scale)",
    })
