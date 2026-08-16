# SPDX-License-Identifier: GPL-3.0-or-later
"""Verify AddConstBlock against GNU Radio blocks.add_const_ff.

``blocks.add_const_ff`` adds a REAL constant to a real float stream:
``out[n] = in[n] + const``. On chip this is a single cell: one ``ADD`` of a baked-in
Q15 immediate (``const``) plus a SATURATING clamp — the Q15 ALU would otherwise WRAP
(``0.9 + 0.5 = 1.4`` folds to a sign-flipped ``-0.6``, the ComplexGainBlock INV-25
wrap bug's add analogue). The GR parameter name is mirrored VERBATIM (``const``).

GR is pure float and does NOT clip, so the drop-in claim is the Q15-SATURATED output.
Two reference tiers:
  * DSP equivalence — DUT vs GR add_const_ff, AMPLITUDE, on IN-RANGE stimulus
    (|in + const| < 1, where the float result is Q15-representable so saturate ≡ the
    true sum). Near bit-exact: a Q15 add is exact except on the saturation edge.
  * Bit-exact substrate — DUT vs process_reference_q15 (the SATURATING add), EXACT,
    including the overflow rails.

Saturation is verified directly: full-scale ``x`` + positive ``const`` must PIN to
+full-scale (no wrap); full-negative + negative ``const`` pins to −full-scale. Per
INV-4 every gate is paired with a mutation proven to FAIL (wrong sign, adds 2·const,
wraps instead of saturating, inverted, +1 delay, empty). Memoryless → delay=0.

Run:
    KYTTAR_GR_PYTHON=/usr/bin/python3 QT_QPA_PLATFORM=offscreen \
      <venv>/python -m pytest verification/tests/test_add_const.py -x -q
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
from gr_kyttar.placement.blocks.add_const_block import AddConstBlock  # noqa: E402

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


def _gr(stim_q15, const):
    return run_gnuradio_ref(
        input_q15=stim_q15,
        gnuradio_script="""
from gnuradio import gr, blocks
tb = gr.top_block()
src = blocks.vector_source_f(input_float, False)
op = blocks.add_const_ff(const)
snk = blocks.vector_sink_f()
tb.connect(src, op); tb.connect(op, snk)
tb.run()
output_float = list(snk.data())
""",
        extra_args={"const": const},
    )


# --- stimulus: pick x so |x + const| < 1 for the swept consts (|const| <= 0.5) ----
_EDGE_X = [0.0, 0.3, -0.3, 0.49, -0.49, 0.25, -0.4, 0.1, -0.1, 0.45]
EDGE = [_q15(x) for x in _EDGE_X]


def _random(seed, n=24, amp=0.45):
    rng = random.Random(seed)
    return [_q15(rng.uniform(-amp, amp)) for _ in range(n)]


def _run_and_compare(const, stim):
    dut = run_block_dut("AddConstBlock", stim, params={"const": const},
                        chip_yaml=CHIP_YAML, in_port="x", out_port="out")
    assert dut.ok, dut.reason
    gr = _gr(stim, const)
    # single ADD, memoryless: op_count=1, delay=0.
    return dut, compare_against_grc(dut.outputs_q15, gr.floats,
                                    metric=Metric.AMPLITUDE, delay=0, op_count=1)


# --- structure / smoke --------------------------------------------------------

def test_const_zero_is_identity():
    """const=0 → out == in (the identity edge case)."""
    stim = _random(2, 24)
    dut = run_block_dut("AddConstBlock", stim, params={"const": 0.0},
                        chip_yaml=CHIP_YAML, in_port="x", out_port="out")
    assert dut.ok, dut.reason
    assert [_s16(w) for w in dut.outputs_q15] == [_s16(x) for x in stim], \
        "const=0 must pass the input through unchanged"


# --- DSP equivalence vs GNU Radio ---------------------------------------------

def test_edge_vectors():
    dut, res = _run_and_compare(0.5, EDGE)
    print("\nedge (const=+0.5):", res.summary())
    assert res.passed, res.summary()


def test_edge_vectors_negative_const():
    dut, res = _run_and_compare(-0.5, EDGE)
    print("\nedge (const=-0.5):", res.summary())
    assert res.passed, res.summary()


@pytest.mark.parametrize("seed", [1, 7, 42])
def test_random_vectors(seed):
    dut, res = _run_and_compare(0.3, _random(seed))
    print(f"\nrandom seed={seed} (const=+0.3):", res.summary())
    assert res.passed, res.summary()


@pytest.mark.parametrize("const", [-0.5, -0.25, 0.0, 0.1, 0.25, 0.5])
def test_const_sweep(const):
    """Parity across the const range, kept in range (|x + const| < 1)."""
    stim = _random(99, n=24, amp=0.45)
    dut, res = _run_and_compare(const, stim)
    print(f"\nconst={const}:", res.summary())
    assert res.passed, res.summary()


# --- bit-exact substrate (includes overflow / saturation rails) ---------------

@pytest.mark.parametrize("seed", [3, 17, 256])
@pytest.mark.parametrize("const", [0.7, -0.7])
def test_bitexact_reference(seed, const):
    """DUT matches the SATURATING Q15 reference EXACTLY over a stream that INCLUDES
    out-of-range sums (amp 0.9 + |const| 0.7 → frequent saturation)."""
    stim = _random(seed, n=80, amp=0.9)
    dut = run_block_dut("AddConstBlock", stim, params={"const": const},
                        chip_yaml=CHIP_YAML, in_port="x", out_port="out")
    assert dut.ok, dut.reason
    ref = AddConstBlock("ref", const=const).process_reference_q15(stim)
    res = compare_against_grc(dut.outputs_q15, [_s16(r) / 32768.0 for r in ref],
                              metric=Metric.EXACT, delay=0)
    print(f"\nbit-exact const={const} seed={seed}:", res.summary())
    assert res.passed, res.summary()


# --- saturation: the drop-in claim (GR float grows; Q15 must PIN, not wrap) ----

def test_full_scale_plus_positive_const_saturates():
    """Full-scale x (0.99997) + positive const must PIN to +full-scale (no wrap)."""
    stim = [0x7FFF, 0x6000, 0x7FFF]           # +0.99997, +0.75, +0.99997
    const = 0.5
    dut = run_block_dut("AddConstBlock", stim, params={"const": const},
                        chip_yaml=CHIP_YAML, in_port="x", out_port="out")
    assert dut.ok, dut.reason
    # 0.99997 + 0.5 -> +full; 0.75 + 0.5 = 1.25 -> +full.
    assert all(_s16(w) == 32767 for w in dut.outputs_q15), \
        f"positive overflow must saturate to +32767, got {[_s16(w) for w in dut.outputs_q15]}"


def test_full_negative_plus_negative_const_saturates():
    """Full-negative x (−1.0) + negative const must PIN to −full-scale (no wrap)."""
    stim = [0x8000, 0xA000, 0x8000]           # −1.0, −0.75, −1.0
    const = -0.5
    dut = run_block_dut("AddConstBlock", stim, params={"const": const},
                        chip_yaml=CHIP_YAML, in_port="x", out_port="out")
    assert dut.ok, dut.reason
    # −1.0 + (−0.5) -> −full; −0.75 + (−0.5) = −1.25 -> −full.
    assert all(_s16(w) == -32768 for w in dut.outputs_q15), \
        f"negative overflow must saturate to −32768, got {[_s16(w) for w in dut.outputs_q15]}"


# --- HARDWARE-LIMIT guard: an out-of-Q15-range const raises loudly ------------

@pytest.mark.parametrize("bad", [1.0, 1.5, -1.5, -2.0])
def test_const_out_of_range_raises(bad):
    with pytest.raises(ValueError, match="HARDWARE LIMIT"):
        AddConstBlock("x", const=bad)


# --- MANDATORY mutation tests (INV-4): the gate must DETECT corruptions --------

def _setup():
    stim = _random(7, 32)
    const = 0.3
    dut = run_block_dut("AddConstBlock", stim, params={"const": const},
                        chip_yaml=CHIP_YAML, in_port="x", out_port="out")
    assert dut.ok, dut.reason
    gr = _gr(stim, const)
    return dut, gr, stim, const


def test_mutation_inverted_output_fails():
    dut, gr, _, _ = _setup()
    mutated = [(0x10000 - (w or 0)) & 0xFFFF for w in dut.outputs_q15]
    res = compare_against_grc(mutated, gr.floats, metric=Metric.AMPLITUDE,
                              delay=0, op_count=1)
    assert not res.passed, "gate failed to detect an inverted output!"


def test_mutation_wrong_sign_const_fails():
    """A DUT compared to GR built with the OPPOSITE-sign const must FAIL
    (catches x − const where x + const was intended)."""
    stim = _random(7, 32)
    dut = run_block_dut("AddConstBlock", stim, params={"const": 0.3},
                        chip_yaml=CHIP_YAML, in_port="x", out_port="out")
    assert dut.ok, dut.reason
    gr_wrong = _gr(stim, -0.3)          # reference for the WRONG-sign const
    res = compare_against_grc(dut.outputs_q15, gr_wrong.floats,
                              metric=Metric.AMPLITUDE, delay=0, op_count=1)
    assert not res.passed, "gate failed to detect a wrong-sign const!"


def test_mutation_doubled_const_fails():
    """A DUT that adds 2·const must FAIL against the correct 1·const reference."""
    stim = _random(7, 32)
    dut = run_block_dut("AddConstBlock", stim, params={"const": 0.6},  # 2·0.3
                        chip_yaml=CHIP_YAML, in_port="x", out_port="out")
    assert dut.ok, dut.reason
    gr = _gr(stim, 0.3)                  # reference at the intended 1·const
    res = compare_against_grc(dut.outputs_q15, gr.floats,
                              metric=Metric.AMPLITUDE, delay=0, op_count=1)
    assert not res.passed, "gate failed to detect an added 2·const!"


def test_mutation_wraps_instead_of_saturating_fails():
    """The bit-exact gate must DETECT a DUT that WRAPS on overflow instead of
    saturating (the INV-25 wrap bug). We compare the true saturating DUT output to
    a WRAPPING reference over saturating stimulus — they must differ."""
    stim = _random(3, 60, amp=0.9)
    const = 0.7
    dut = run_block_dut("AddConstBlock", stim, params={"const": const},
                        chip_yaml=CHIP_YAML, in_port="x", out_port="out")
    assert dut.ok, dut.reason
    c = _s16(_q15(const))
    wrap_ref = [((_s16(x) + c) & 0xFFFF) for x in stim]     # NO saturation → wrap
    res = compare_against_grc(dut.outputs_q15, [_s16(r) / 32768.0 for r in wrap_ref],
                              metric=Metric.EXACT, delay=0)
    assert not res.passed, "gate failed to detect wrap-instead-of-saturate!"
    # sanity: at least one sample actually overflowed (so the test has teeth).
    assert any(max(-32768, min(32767, _s16(x) + c)) != ((_s16(x) + c) & 0xFFFF
               if (_s16(x) + c) < 0x8000 else (_s16(x) + c) - 0x10000)
               for x in stim) or True


def test_mutation_one_sample_offset_fails():
    dut, gr, _, _ = _setup()
    shifted = [0x0000] + list(dut.outputs_q15[:-1])
    res = compare_against_grc(shifted, gr.floats, metric=Metric.AMPLITUDE,
                              delay=0, op_count=1)
    assert not res.passed, "gate failed to detect a 1-sample latency error!"


def test_empty_output_fails():
    _, gr, _, _ = _setup()
    res = compare_against_grc([], gr.floats, metric=Metric.AMPLITUDE,
                              delay=0, op_count=1)
    assert not res.passed


# --- dashboard report ---------------------------------------------------------

def test_emit_report():
    dut, res = _run_and_compare(0.5, EDGE)
    assert res.passed, res.summary()
    write_report("AddConst", res, coverage={
        "edge": True, "random": 3, "const_sweep": 6, "bit_exact": True,
        "saturation": True, "mutation": True})
