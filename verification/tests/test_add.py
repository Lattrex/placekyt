# SPDX-License-Identifier: GPL-3.0-or-later
"""Verify AddBlock / SubtractBlock against GNU Radio blocks.add_ff / sub_ff.

These are the generic two-stream combiners — GR's ``blocks.add_ff``
(``out = a + b``) and ``blocks.sub_ff`` (``out = a - b``) of two real input
streams. On chip each is a single cell: one ADD/SUB plus a SATURATING clamp
(production fixed-point — the Q15 ALU would otherwise WRAP, turning 0.6+0.6 into
a sign-flipped -0.8). add_ff/sub_ff take no params (full GRC parity).

The two streams use the proven complex-burst fan-in (the Costas xi/xq tap): each
sample is ``WRITE a -> R0`` + ``WRITE b -> R1`` + one ``JUMP``. The harness carries
them as one complex array (real = a, imag = b); the real output lands in I.

Two reference tiers:
  * DSP equivalence — DUT vs GR add_ff/sub_ff, AMPLITUDE, on IN-RANGE stimulus
    (|a±b| < 1, where the float result is Q15-representable so saturate ≡ true sum).
  * Bit-exact substrate — DUT vs process_reference_q15 (the SATURATING add/sub),
    EXACT, including the overflow corners.

Saturation is verified directly: an out-of-range sum must PIN to ±full-scale (no
wrap / sign flip). Per INV-4 every gate is paired with a mutation that must FAIL;
subtract additionally checks swapped-stream (a-b ≠ b-a), while add does not (a+b
is commutative — documented). Memoryless → delay=0.

Run:
    KYTTAR_GR_PYTHON=/usr/bin/python3 QT_QPA_PLATFORM=offscreen \
      <venv>/python -m pytest verification/tests/test_add.py -x -q
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
from gr_kyttar.placement.blocks.add_block import AddBlock, SubtractBlock  # noqa: E402

CHIP_YAML = str(_PLACEKYT / "resources" / "chips" / "kyttar_10x12.yaml")
_GR_AVAILABLE = os.path.exists(os.environ.get("KYTTAR_GR_PYTHON", "/usr/bin/python3"))
pytestmark = pytest.mark.skipif(
    not (os.path.exists(CHIP_YAML) and _GR_AVAILABLE),
    reason="chip yaml or GNU Radio interpreter absent")

# (block_type, GR factory, class) — parameterizes the whole suite over add & sub.
_VARIANTS = {
    "add": ("AddBlock", "blocks.add_ff()", AddBlock),
    "sub": ("SubtractBlock", "blocks.sub_ff()", SubtractBlock),
}


def _s16(v):
    if v is None:
        return None
    v = int(v) & 0xFFFF
    return v - 0x10000 if v >= 0x8000 else v


def _q15(v: float) -> int:
    q = int(round(v * 32768.0))
    return max(-32768, min(32767, q)) & 0xFFFF


# In-range edge pairs (|a±b| < 1 for BOTH add and sub).
_EDGE_A = [0.0, 0.3, -0.3, 0.49, -0.49, 0.25, -0.4, 0.1, -0.1, 0.45]
_EDGE_B = [0.49, 0.3, 0.3, 0.49, -0.49, -0.25, 0.4, -0.45, 0.45, -0.45]
EDGE = [complex(a, b) for a, b in zip(_EDGE_A, _EDGE_B)]


def _random(seed, n=24, amp=0.45):
    rng = random.Random(seed)
    return [complex(rng.uniform(-amp, amp), rng.uniform(-amp, amp))
            for _ in range(n)]


def _run_dut(block_type, stim):
    dut = run_block_dut_complex(
        block_type, stim, chip_yaml=CHIP_YAML,
        in_ports=("a", "b"), words_per_sample=1)
    assert dut.ok, dut.reason
    return dut


def _gr(gr_factory, stim):
    return run_gnuradio_ref_complex(
        stim,
        gnuradio_script=f"""
from gnuradio import gr, blocks
tb = gr.top_block()
sa = blocks.vector_source_f(input_i, False)
sb = blocks.vector_source_f(input_q, False)
op = {gr_factory}
snk = blocks.vector_sink_f()
tb.connect(sa, (op, 0)); tb.connect(sb, (op, 1)); tb.connect(op, snk)
tb.run()
output_float = list(snk.data())
""")


def _compare(dut, gr):
    # one ADD/SUB, memoryless: op_count=1, delay=0.
    return compare_against_grc(dut.i_q15, gr.i, metric=Metric.AMPLITUDE,
                               delay=0, op_count=1)


# --- structure / smoke --------------------------------------------------------

@pytest.mark.parametrize("variant", ["add", "sub"])
def test_drives_and_captures(variant):
    block_type, _, _ = _VARIANTS[variant]
    dut = _run_dut(block_type, _random(1, 12))
    assert dut.words_per_sample == 1
    assert dut.in_regs == (0, 1), "the two streams should land a@R0, b@R1"
    assert all(v is not None for v in dut.i_q15)


# --- DSP equivalence vs GNU Radio ---------------------------------------------

@pytest.mark.parametrize("variant", ["add", "sub"])
def test_edge_vectors(variant):
    block_type, gr_factory, _ = _VARIANTS[variant]
    dut = _run_dut(block_type, EDGE)
    gr = _gr(gr_factory, EDGE)
    res = _compare(dut, gr)
    print(f"\n{variant} edge:", res.summary())
    assert res.passed, res.summary()


@pytest.mark.parametrize("variant", ["add", "sub"])
@pytest.mark.parametrize("seed", [1, 7, 42])
def test_random_vectors(variant, seed):
    block_type, gr_factory, _ = _VARIANTS[variant]
    stim = _random(seed)
    dut = _run_dut(block_type, stim)
    gr = _gr(gr_factory, stim)
    res = _compare(dut, gr)
    print(f"\n{variant} random seed={seed}:", res.summary())
    assert res.passed, res.summary()


@pytest.mark.parametrize("variant", ["add", "sub"])
@pytest.mark.parametrize("amp", [0.1, 0.25, 0.4, 0.49])
def test_amplitude_sweep(variant, amp):
    """Parity across amplitude regimes (the param-sweep analogue), kept in range."""
    block_type, gr_factory, _ = _VARIANTS[variant]
    stim = _random(99, n=24, amp=amp)
    dut = _run_dut(block_type, stim)
    gr = _gr(gr_factory, stim)
    res = _compare(dut, gr)
    print(f"\n{variant} amp={amp}:", res.summary())
    assert res.passed, res.summary()


# --- bit-exact substrate ------------------------------------------------------

@pytest.mark.parametrize("variant", ["add", "sub"])
@pytest.mark.parametrize("seed", [3, 17, 256])
def test_bitexact_reference(variant, seed):
    """DUT matches the SATURATING Q15 reference EXACTLY over a long stream that
    INCLUDES out-of-range sums (amp 0.9 → frequent saturation)."""
    block_type, _, cls = _VARIANTS[variant]
    stim = _random(seed, n=80, amp=0.9)
    dut = _run_dut(block_type, stim)
    blk = cls("ref")
    a = [_q15(c.real) for c in stim]
    b = [_q15(c.imag) for c in stim]
    ref = blk.process_reference_q15(a, b)
    res = compare_against_grc(dut.i_q15, [_s16(r) / 32768.0 for r in ref],
                              metric=Metric.EXACT, delay=0)
    print(f"\n{variant} bit-exact seed={seed}:", res.summary())
    assert res.passed, res.summary()


@pytest.mark.parametrize("variant,corner,rail", [
    ("add", complex(0.9, 0.9), 32767),     # 1.8  -> +full
    ("add", complex(-0.9, -0.9), -32768),  # -1.8 -> -full
    ("sub", complex(0.9, -0.9), 32767),    # 0.9-(-0.9)=1.8 -> +full
    ("sub", complex(-0.9, 0.9), -32768),   # -0.9-0.9=-1.8  -> -full
])
def test_saturates_not_wraps(variant, corner, rail):
    """An out-of-range result must PIN to ±full-scale (no wrap / sign flip)."""
    block_type, _, cls = _VARIANTS[variant]
    stim = [complex(0.1, 0.1), corner, complex(-0.2, 0.2), corner]
    dut = _run_dut(block_type, stim)
    assert _s16(dut.i_q15[1]) == rail and _s16(dut.i_q15[3]) == rail, \
        f"{variant} must saturate to {rail}, got {_s16(dut.i_q15[1])}"


# --- MANDATORY mutation tests -------------------------------------------------

def _setup(variant):
    block_type, gr_factory, _ = _VARIANTS[variant]
    stim = _random(7, 32)
    dut = _run_dut(block_type, stim)
    gr = _gr(gr_factory, stim)
    return dut, gr, stim, gr_factory


@pytest.mark.parametrize("variant", ["add", "sub"])
def test_mutation_inverted_output_fails(variant):
    dut, gr, _, _ = _setup(variant)
    mutated = [(0x10000 - (w or 0)) & 0xFFFF for w in dut.i_q15]
    res = compare_against_grc(mutated, gr.i, metric=Metric.AMPLITUDE,
                              delay=0, op_count=1)
    assert not res.passed, "gate failed to detect an inverted output!"


@pytest.mark.parametrize("variant", ["add", "sub"])
def test_mutation_wrong_second_stream_fails(variant):
    block_type, gr_factory, _ = _VARIANTS[variant]
    stim = _random(7, 32)
    dut = _run_dut(block_type, stim)
    other = _random(8, 32)
    wrong = [complex(s.real, o.imag) for s, o in zip(stim, other)]
    gr_wrong = _gr(gr_factory, wrong)
    res = compare_against_grc(dut.i_q15, gr_wrong.i, metric=Metric.AMPLITUDE,
                              delay=0, op_count=1)
    assert not res.passed, "gate failed to detect a wrong second stream!"


def test_subtract_mutation_swapped_streams_fails():
    """Subtract is NOT commutative: a-b ≠ b-a, so a swapped-stream DUT must FAIL.
    (Add is commutative — no such mutation; documented in the module.)"""
    block_type, gr_factory, _ = _VARIANTS["sub"]
    # build the DUT on (a,b) but compare to GR on (b,a) — a real corruption.
    stim = _random(11, 32)
    swapped = [complex(c.imag, c.real) for c in stim]
    dut = _run_dut(block_type, stim)
    gr_swapped = _gr(gr_factory, swapped)
    res = compare_against_grc(dut.i_q15, gr_swapped.i, metric=Metric.AMPLITUDE,
                              delay=0, op_count=1)
    assert not res.passed, "gate failed to detect swapped subtract operands!"


@pytest.mark.parametrize("variant", ["add", "sub"])
def test_mutation_one_sample_offset_fails(variant):
    dut, gr, _, _ = _setup(variant)
    shifted = [0x0000] + list(dut.i_q15[:-1])
    res = compare_against_grc(shifted, gr.i, metric=Metric.AMPLITUDE,
                              delay=0, op_count=1)
    assert not res.passed, "gate failed to detect a 1-sample latency error!"


@pytest.mark.parametrize("variant", ["add", "sub"])
def test_empty_output_fails(variant):
    _, gr, _, _ = _setup(variant)
    res = compare_against_grc([], gr.i, metric=Metric.AMPLITUDE,
                              delay=0, op_count=1)
    assert not res.passed


# --- dashboard reports --------------------------------------------------------

@pytest.mark.parametrize("variant", ["add", "sub"])
def test_emit_report(variant):
    block_type, gr_factory, _ = _VARIANTS[variant]
    dut = _run_dut(block_type, EDGE)
    gr = _gr(gr_factory, EDGE)
    res = _compare(dut, gr)
    assert res.passed, res.summary()
    write_report(block_type, res, coverage={
        "edge": True, "random": 3, "amplitude_sweep": 4, "bit_exact": True,
        "saturation": True, "mutation": True})
