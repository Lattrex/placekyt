# SPDX-License-Identifier: GPL-3.0-or-later
"""Verify MultiplyBlock against GNU Radio blocks.multiply_ff.

MultiplyBlock is the generic two-stream product — GR's ``blocks.multiply_ff``:
``out[n] = a[n]·b[n]`` of two real input streams. On chip it is a SINGLE
``MULQ`` (``out = (a·b) >> 15``, Q15). It takes no parameters (multiply_ff has
none — full GRC parity), so the "param sweep" here is over input amplitude
regimes and random seeds rather than a block param.

The two streams are delivered with the proven complex-burst fan-in (the Costas
xi/xq tap): each sample is ``WRITE a -> R0`` + ``WRITE b -> R1`` + one ``JUMP``.
The verification harness carries the two streams as one complex array
(real = a, imag = b); the DUT's single real output lands in the I channel.

Two reference tiers (mirroring the mixer/FIR pattern):
  * DSP equivalence — DUT vs GNU Radio multiply_ff, AMPLITUDE, single-MULQ floor.
  * Bit-exact substrate — DUT vs process_reference_q15 (the wrapping Q15 MULQ),
    EXACT, over long random streams.

Per INV-4 every gate is paired with a mutation (inverted output, wrong second
stream, +1 delay, halved magnitude, empty) that must FAIL. Memoryless → delay=0.

Q15 OVERFLOW: the lone product that overflows is the exact (-1.0)·(-1.0)=+1.0
corner, where MULQ WRAPS to -1.0 (the datapath does not saturate). A dedicated
test drives that corner and asserts the DUT matches the wrapping reference; the
GR-equivalence stimulus stays off the simultaneous full-scale-negative corner so
the Q15 product tracks GR float within the floor.

Run:
    KYTTAR_GR_PYTHON=/usr/bin/python3 QT_QPA_PLATFORM=offscreen \
      <venv>/python -m pytest verification/tests/test_multiply.py -x -q
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
from gr_kyttar.placement.blocks.multiply_block import MultiplyBlock  # noqa: E402

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


# --- stimulus families: edge + random (a as real, b as imag) ------------------
# Edge pairs exercise zero / half / full-scale, mixed signs. The (-1,-1) corner
# is EXCLUDED here (it is the one Q15-overflow wrap, tested separately) so the
# DUT tracks GR float within the single-MULQ floor.
_EDGE_A = [0.0, 0.5, -0.5, 0.999, -0.999, 0.25, -0.75, 0.9, -0.9, 0.5]
_EDGE_B = [0.999, 0.5, 0.5, 0.5, 0.999, -0.5, 0.75, -0.9, 0.9, -0.999]
EDGE = [complex(a, b) for a, b in zip(_EDGE_A, _EDGE_B)]


def _random(seed, n=24, amp=0.9):
    rng = random.Random(seed)
    return [complex(rng.uniform(-amp, amp), rng.uniform(-amp, amp))
            for _ in range(n)]


def _run_dut(stim):
    dut = run_block_dut_complex(
        "MultiplyBlock", stim, chip_yaml=CHIP_YAML,
        in_ports=("a", "b"), words_per_sample=1)
    assert dut.ok, dut.reason
    return dut


def _gr_multiply(stim):
    return run_gnuradio_ref_complex(
        stim,
        gnuradio_script="""
from gnuradio import gr, blocks
tb = gr.top_block()
sa = blocks.vector_source_f(input_i, False)
sb = blocks.vector_source_f(input_q, False)
mul = blocks.multiply_ff()
snk = blocks.vector_sink_f()
tb.connect(sa, (mul, 0)); tb.connect(sb, (mul, 1)); tb.connect(mul, snk)
tb.run()
output_float = list(snk.data())
""")


def _compare(dut, gr):
    # single MULQ, memoryless: op_count=1, delay=0.
    return compare_against_grc(dut.i_q15, gr.i, metric=Metric.AMPLITUDE,
                               delay=0, op_count=1)


# --- structure / smoke --------------------------------------------------------

def test_multiply_drives_and_captures():
    dut = _run_dut(_random(1, 12))
    assert dut.words_per_sample == 1, f"expected 1 word/sample, got {dut.words_per_sample}"
    assert dut.in_regs == (0, 1), "the two streams should land a@R0, b@R1"
    assert all(v is not None for v in dut.i_q15)


# --- DSP equivalence vs GNU Radio multiply_ff ---------------------------------

def test_multiply_edge_vectors():
    """MultiplyBlock matches GR multiply_ff on edge vectors, within the floor."""
    dut = _run_dut(EDGE)
    gr = _gr_multiply(EDGE)
    res = _compare(dut, gr)
    print("\nedge:", res.summary())
    assert res.passed, res.summary()


@pytest.mark.parametrize("seed", [1, 7, 42])
def test_multiply_random_vectors(seed):
    dut = _run_dut(_random(seed))
    gr = _gr_multiply(_random(seed))
    res = _compare(dut, gr)
    print(f"\nrandom seed={seed}:", res.summary())
    assert res.passed, res.summary()


@pytest.mark.parametrize("amp", [0.25, 0.5, 0.75, 0.9])
def test_multiply_amplitude_sweep(amp):
    """Parity must hold across input amplitude regimes (the param-sweep analogue
    for a parameterless block)."""
    stim = _random(99, n=24, amp=amp)
    dut = _run_dut(stim)
    gr = _gr_multiply(stim)
    res = _compare(dut, gr)
    print(f"\namp={amp}:", res.summary())
    assert res.passed, res.summary()


# --- bit-exact substrate ------------------------------------------------------

@pytest.mark.parametrize("seed", [3, 17, 256])
def test_multiply_bitexact_reference(seed):
    """DUT matches the on-chip Q15 reference EXACTLY (the wrapping MULQ) over a
    long random stream."""
    stim = _random(seed, n=80)
    dut = _run_dut(stim)
    blk = MultiplyBlock("ref")
    a = [_q15(c.real) for c in stim]
    b = [_q15(c.imag) for c in stim]
    ref = blk.process_reference_q15(a, b)
    res = compare_against_grc(dut.i_q15, [_s16(r) / 32768.0 for r in ref],
                              metric=Metric.EXACT, delay=0)
    print(f"\nbit-exact seed={seed}:", res.summary())
    assert res.passed, res.summary()


def test_multiply_q15_overflow_corner_wraps():
    """The lone Q15 overflow — (-1.0)·(-1.0) — must WRAP to -1.0 on chip and match
    the wrapping reference (NOT saturate to +full-scale)."""
    stim = [complex(-1.0, -1.0), complex(0.5, 0.5),
            complex(-1.0, -1.0), complex(0.9, -0.3)]
    dut = _run_dut(stim)
    blk = MultiplyBlock("ref")
    a = [_q15(c.real) for c in stim]
    b = [_q15(c.imag) for c in stim]
    ref = blk.process_reference_q15(a, b)
    assert _s16(ref[0]) == -32768, "reference must model the wrap to -1.0"
    assert [_s16(x) for x in dut.i_q15] == [_s16(r) for r in ref], \
        "DUT must match the wrapping Q15 datapath at the overflow corner"


# --- MANDATORY mutation tests: the gate must DETECT real corruptions ----------
# NOTE: multiply is COMMUTATIVE (a·b == b·a), so a swapped-stream mutation is NOT
# a corruption and is intentionally not tested; the "wrong second stream" test
# below proves the gate detects an actually-wrong operand.

def _setup():
    stim = _random(7, 32)
    dut = _run_dut(stim)
    gr = _gr_multiply(stim)
    return dut, gr, stim


def test_mutation_inverted_output_fails():
    """A sign-inverted DUT must FAIL (catches a negated product)."""
    dut, gr, _ = _setup()
    mutated = [(0x10000 - (w or 0)) & 0xFFFF for w in dut.i_q15]
    res = compare_against_grc(mutated, gr.i, metric=Metric.AMPLITUDE,
                              delay=0, op_count=1)
    assert not res.passed, "gate failed to detect an inverted output!"


def test_mutation_wrong_second_stream_fails():
    """A DUT multiplied against a DIFFERENT b must FAIL vs the right reference
    (proves the gate uses the actual second operand, not an echo of a)."""
    stim = _random(7, 32)
    dut = _run_dut(stim)
    # Reference uses a DIFFERENT second stream (b from another seed).
    other = _random(8, 32)
    wrong = [complex(s.real, o.imag) for s, o in zip(stim, other)]
    gr_wrong = _gr_multiply(wrong)
    res = compare_against_grc(dut.i_q15, gr_wrong.i, metric=Metric.AMPLITUDE,
                              delay=0, op_count=1)
    assert not res.passed, "gate failed to detect a wrong second stream!"


def test_mutation_halved_magnitude_fails():
    """Halving the product (a stuck shift) must FAIL."""
    dut, gr, _ = _setup()
    halved = [_s16(w) // 2 & 0xFFFF for w in dut.i_q15]
    res = compare_against_grc(halved, gr.i, metric=Metric.AMPLITUDE,
                              delay=0, op_count=1)
    assert not res.passed, "gate failed to detect a halved-magnitude output!"


def test_mutation_one_sample_offset_fails():
    """A +1-sample delay must FAIL when delay=0 is asserted."""
    dut, gr, _ = _setup()
    shifted = [0x0000] + list(dut.i_q15[:-1])
    res = compare_against_grc(shifted, gr.i, metric=Metric.AMPLITUDE,
                              delay=0, op_count=1)
    assert not res.passed, "gate failed to detect a 1-sample latency error!"


def test_empty_output_fails():
    _, gr, _ = _setup()
    res = compare_against_grc([], gr.i, metric=Metric.AMPLITUDE,
                              delay=0, op_count=1)
    assert not res.passed


# --- dashboard report ---------------------------------------------------------

def test_emit_report():
    dut = _run_dut(EDGE)
    gr = _gr_multiply(EDGE)
    res = _compare(dut, gr)
    assert res.passed, res.summary()
    write_report("MultiplyBlock", res, coverage={
        "edge": True, "random": 3, "amplitude_sweep": 4, "bit_exact": True,
        "overflow_corner": True, "mutation": True})
