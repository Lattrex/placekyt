# SPDX-License-Identifier: GPL-3.0-or-later
"""Verify FSK4SlicerBlock — the M17 4FSK hard-decision slicer (RX final stage).

The slicer is the exact INVERSE of :class:`FSK4SymbolMapperBlock` in the same
LSB-first convention (RULE #0): a recovered FM-discriminator level maps to the
2-bit dibit it came from, emitting ``b0`` (LSB) then ``b1`` (MSB). There is no
single GNU Radio block for the M17 slicer, so the gate is:

  * the DECISION table (thresholds at 0 and ±2/3 → nearest of {+3,+1,−1,−3} →
    inverse Gray map), on-chip == the block's bit-exact reference;
  * the mapper→slicer LOOPBACK is bit-for-bit the identity on random bits (the
    strongest test — it pins the two blocks' shared LSB-first convention);
  * mandatory mutation gates (INV-4): a shifted/negated/inner-outer-swapped
    decision must FAIL.

Run::

    KYTTAR_GR_PYTHON=/usr/bin/python3 QT_QPA_PLATFORM=offscreen \
        .venv/bin/python -m pytest verification/tests/test_fsk4_slicer.py -q
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

from kyttar_verify import write_report, CompareResult, Metric  # noqa: E402
from fsk4_dut import run_fsk4_slicer_dut, run_fsk4_mapper_dut  # noqa: E402
from gr_kyttar.placement.blocks.fsk4_slicer_block import FSK4SlicerBlock  # noqa: E402
from gr_kyttar.placement.blocks.fsk4_symbol_mapper_block import (  # noqa: E402
    FSK4SymbolMapperBlock)

CHIP_YAML = str(_PLACEKYT / "resources" / "chips" / "kyttar_10x12.yaml")
_GR_AVAILABLE = os.path.exists(os.environ.get("KYTTAR_GR_PYTHON", "/usr/bin/python3"))
pytestmark = pytest.mark.skipif(
    not _GR_AVAILABLE, reason="GNU Radio interpreter not available")


def _q15(v):
    q = int(round(v * 32768.0))
    return max(-32768, min(32767, q)) & 0xFFFF


# The four normalised M17 levels (mapper output) and their expected DIBITS (0..3).
# The slicer now emits ONE dibit word per symbol (like the QPSK slicer), so a waveform
# sink plots clean 0..3 levels instead of a stacked b0/b1 bit stream. (0.99, not 1.0:
# 1.0*32768 overflows Q15 to -32768 and flips the level's sign.)
_LEVEL_TO_DIBIT = [
    (0.99, 1),       # +3 -> d=1  (b0=1, b1=0)
    (1.0 / 3.0, 0),  # +1 -> d=0  (b0=0, b1=0)
    (-1.0 / 3.0, 2),  # -1 -> d=2  (b0=0, b1=1)
    (-0.99, 3),      # -3 -> d=3  (b0=1, b1=1)
]


def _dibit_bits(d):
    """Unpack a dibit to its two bits LSB-first (b0, then b1) — for loopback vs the
    mapper's bit input."""
    return [d & 1, (d >> 1) & 1]


# --- correctness: the four levels slice to the four M17 dibits -----------------

def test_four_levels_slice_to_dibits():
    """Each of the four M17 levels slices to its dibit (0..3), one word per symbol."""
    levels = [_q15(lv) for lv, _ in _LEVEL_TO_DIBIT]
    dibits = run_fsk4_slicer_dut(levels, CHIP_YAML)
    exp = [d for _, d in _LEVEL_TO_DIBIT]
    print(f"\nlevels->dibits: {dibits}  expect {exp}")
    assert dibits == exp, f"got {dibits}, expected {exp}"


def test_decision_boundaries():
    """Levels near the thresholds (0 and ±2/3) resolve to the right dibit."""
    # Just inside each region: outer-hi, inner-hi, inner-lo, outer-lo.
    cases = [
        (0.9, 1),    # >= +2/3 -> +3 -> d=1
        (0.4, 0),    # in [0, +2/3) -> +1 -> d=0
        (-0.4, 2),   # in [-2/3, 0) -> -1 -> d=2
        (-0.9, 3),   # < -2/3 -> -3 -> d=3
        (0.0, 0),    # exactly 0 -> non-negative inner -> +1 -> d=0
    ]
    levels = [_q15(v) for v, _ in cases]
    dibits = run_fsk4_slicer_dut(levels, CHIP_YAML)
    exp = [d for _, d in cases]
    assert dibits == exp, f"boundary decision wrong: got {dibits}, expected {exp}"


def test_reference_matches_chip_bitexact():
    """The block's own reference (dibits) equals the on-chip stream, word for word."""
    blk = FSK4SlicerBlock("s")
    rng = random.Random(11)
    levels = [_q15(rng.uniform(-0.99, 0.99)) for _ in range(40)]
    dibits = run_fsk4_slicer_dut(levels, CHIP_YAML)
    ref = [int(d) for d in blk.process_reference(levels)]
    assert dibits == ref, f"chip != reference:\n chip {dibits}\n ref  {ref}"


# --- the strongest gate: mapper -> slicer LOOPBACK is the identity -------------

def test_mapper_slicer_loopback_identity():
    """Random bits -> mapper -> (levels) -> slicer -> the SAME dibits. Pins the two
    blocks' shared LSB-first M17 Gray convention end to end (clean channel). The
    slicer emits dibits, so unpack them to bits to compare against the mapper input."""
    rng = random.Random(7)
    n_dibits = 64
    bits_in = []
    for _ in range(n_dibits):
        bits_in += [rng.randint(0, 1), rng.randint(0, 1)]
    levels = run_fsk4_mapper_dut(bits_in, CHIP_YAML)          # 64 PAM levels
    levels_q15 = [_q15(v) for v in levels]
    dibits_out = run_fsk4_slicer_dut(levels_q15, CHIP_YAML)   # 64 dibits (0..3)
    bits_out = []
    for d in dibits_out:
        bits_out += _dibit_bits(d)                            # unpack to 128 bits
    print(f"\nloopback: in {len(bits_in)} bits, out {len(dibits_out)} dibits")
    assert bits_out == bits_in, "mapper->slicer loopback is NOT the identity!"


# --- MANDATORY negative tests (INV-4) -----------------------------------------

def test_mutation_inverted_thresholds_fails():
    """A slicer that inverts its sign decision (b1 flipped) MUST disagree with the
    reference — proves the sign test is under test. (Output is now DIBITS 0..3;
    flipping b1 is XOR-ing bit 1 of each dibit, i.e. d ^= 2.)"""
    blk = FSK4SlicerBlock("s")
    levels = [_q15(v) for v in (0.99, 1.0 / 3.0, -1.0 / 3.0, -0.99)]
    dibits = run_fsk4_slicer_dut(levels, CHIP_YAML)
    ref = [int(d) for d in blk.process_reference(levels)]
    mutated = [d ^ 2 for d in dibits]      # flip b1 (sign) of every dibit
    assert mutated != ref, "gate missed a flipped sign decision!"


def test_mutation_shifted_stream_fails():
    """A one-symbol shift of the sliced dibit stream must be caught."""
    blk = FSK4SlicerBlock("s")
    rng = random.Random(3)
    levels = [_q15(rng.uniform(-0.99, 0.99)) for _ in range(20)]
    dibits = run_fsk4_slicer_dut(levels, CHIP_YAML)
    ref = [int(d) for d in blk.process_reference(levels)]
    shifted = [0] + list(dibits[:-1])
    assert shifted != ref, "gate missed a one-symbol stream shift!"


def test_mutation_magnitude_swap_fails():
    """If the magnitude bit b0 were dropped (all inner) the gate MUST fail. (b0 is
    bit 0 of each dibit; force it to 0 via d &= ~1.)"""
    blk = FSK4SlicerBlock("s")
    levels = [_q15(v) for v in (0.99, 1.0 / 3.0, -1.0 / 3.0, -0.99)]
    dibits = run_fsk4_slicer_dut(levels, CHIP_YAML)
    ref = [int(d) for d in blk.process_reference(levels)]
    mutated = [d & ~1 for d in dibits]     # force every b0 to 0 (never outer)
    assert mutated != ref, "gate missed a dropped magnitude bit!"


def test_empty_output_fails():
    blk = FSK4SlicerBlock("s")
    ref = [int(d) for d in blk.process_reference([_q15(0.99)])]
    assert ref and [] != ref


# --- report --------------------------------------------------------------------

def test_emit_report():
    blk = FSK4SlicerBlock("s")
    rng = random.Random(5)
    levels = [_q15(rng.uniform(-1.0, 1.0)) for _ in range(32)]
    bits = run_fsk4_slicer_dut(levels, CHIP_YAML)
    ref = [int(b) for b in blk.process_reference(levels)]
    n = min(len(bits), len(ref))
    # Decision metric: bit errors over the recovered {0,1} stream.
    bit_errs = sum(1 for k in range(n) if bits[k] != ref[k])
    res = CompareResult(passed=(bit_errs == 0), metric=Metric.DECISION,
                        n_compared=n, bit_errors=bit_errs, delay_used=0)
    assert res.passed, res.summary()
    write_report("FSK4SlicerBlock", res, coverage={
        "decision": "thresholds 0, ±2/3 -> nearest {+3,+1,-1,-3} -> inverse Gray",
        "gray": "LSB-first inverse of FSK4SymbolMapperBlock (b0=|y|>=thr, b1=y<0)",
        "patterns": "four levels, boundaries, random, mapper->slicer loopback",
        "mutation": True,
        "gr_equiv": "no single GR block; inverse of the M17 mapper (loopback identity)",
        "note": "M17 4FSK RX slicer; 1 level -> 2 bits (b0 LSB, b1 MSB).",
    })
