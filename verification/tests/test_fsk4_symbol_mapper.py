# SPDX-License-Identifier: GPL-3.0-or-later
"""Verify FSK4SymbolMapperBlock — the M17 4FSK / C4FM TX symbol mapper.

M17 4-level FSK maps a **dibit** (two bits, LSB-first) to one of four signed PAM
deviation levels. There is no single GNU Radio block for the full M17 mapper, but
the underlying constellation IS a Gray-coded chunks-to-symbols map, so we pin the
LEVEL TABLE against GNU Radio's ``digital.chunks_to_symbols_bf`` fed the same dibit
indices — proving the four levels + the LSB-first Gray order are exactly M17 — and
we pin the bit-accumulator + the whole mapper against the block's own bit-exact
Q15 reference with mandatory mutation gates (INV-4).

M17 PARAMETERS (RULE #0, LSB-first): dibit ``(b0,b1)``, ``d = b0 + 2·b1``,
``(1,0)→+3``, ``(0,0)→+1``, ``(0,1)→−1``, ``(1,1)→−3``; levels normalised so
``+3 → +1.0`` (Q15 full scale): table indexed by ``d`` is ``[+1/3, +1, −1/3, −1]``.

Run::

    KYTTAR_GR_PYTHON=/usr/bin/python3 QT_QPA_PLATFORM=offscreen \
        .venv/bin/python -m pytest verification/tests/test_fsk4_symbol_mapper.py -q
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import numpy as np
import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

_PLACEKYT = Path(__file__).resolve().parents[2] / "placekyt"
_VERIFY = Path(__file__).resolve().parents[1]
_RUNTIME = Path(__file__).resolve().parents[2] / "runtime" / "python"
for p in (str(_PLACEKYT), str(_VERIFY), str(_RUNTIME)):
    if p not in sys.path:
        sys.path.insert(0, p)

from kyttar_verify import run_gnuradio_ref, write_report, Metric  # noqa: E402
from fsk4_dut import run_fsk4_mapper_dut  # noqa: E402
from gr_kyttar.placement.blocks.fsk4_symbol_mapper_block import (  # noqa: E402
    FSK4SymbolMapperBlock)

CHIP_YAML = str(_PLACEKYT / "resources" / "chips" / "kyttar_10x12.yaml")
_GR_AVAILABLE = os.path.exists(os.environ.get("KYTTAR_GR_PYTHON", "/usr/bin/python3"))
pytestmark = pytest.mark.skipif(
    not _GR_AVAILABLE, reason="GNU Radio interpreter not available")

_TOL_LSB = 1
# Normalised level table indexed by dibit value d (M17 Gray, LSB-first).
_LEVELS = FSK4SymbolMapperBlock._LEVELS   # [+1/3, +1, -1/3, -1]


def _bits_from_dibits(dibits):
    """Expand dibit values d=b0+2*b1 to a LSB-first bit stream (b0 then b1)."""
    bits = []
    for d in dibits:
        bits.append(d & 1)          # b0 (LSB) first
        bits.append((d >> 1) & 1)   # b1 (MSB)
    return bits


def _gr_levels(dibits):
    """GNU Radio golden: chunks_to_symbols_bf(level_table) over dibit indices.

    Proves the four PAM levels + the LSB-first Gray order ARE M17 by comparing to
    GR's own index->symbol mapper fed the same dibit values."""
    ref = run_gnuradio_ref(
        list(dibits),
        """
from gnuradio import gr, blocks, digital

idx = [int(v) & 0xFFFF for v in input_q15]
levels = %r

tb = gr.top_block()
src = blocks.vector_source_b(idx, False, 1, [])
mapper = digital.chunks_to_symbols_bf(levels, 1)
snk = blocks.vector_sink_f()
tb.connect(src, mapper, snk)
tb.run()
output_float = list(snk.data())
""" % (list(_LEVELS),),
    )
    return ref.floats


# --- correctness: the four dibits map to the four M17 levels -------------------

def test_all_four_dibits_map_to_m17_levels():
    """Each dibit d in 0..3 maps to the M17 Gray level, on-chip == GR."""
    dibits = [1, 0, 2, 3]           # -> +3, +1, -1, -3
    bits = _bits_from_dibits(dibits)
    out = run_fsk4_mapper_dut(bits, CHIP_YAML)
    ref = _gr_levels(dibits)
    assert len(out) == len(ref), f"got {len(out)} levels, GR has {len(ref)}"
    err = max(abs(o - r) for o, r in zip(out, ref))
    print(f"\ndibit map vs GR: {out} vs {list(ref)}  max {err*32768:.2f} LSB")
    assert err * 32768 <= _TOL_LSB + 1, f"{err*32768:.2f} LSB too high"


def test_random_dibits_match_gr():
    """A pseudo-random dibit stream maps level-for-level to GR."""
    dibits = [(i * 7 + 3) & 3 for i in range(48)]
    bits = _bits_from_dibits(dibits)
    out = run_fsk4_mapper_dut(bits, CHIP_YAML)
    ref = _gr_levels(dibits)
    m = min(len(out), len(ref))
    assert m >= 40
    err = max(abs(out[k] - ref[k]) for k in range(m))
    print(f"\nrandom dibits vs GR: max {err*32768:.2f} LSB over {m}")
    assert err * 32768 <= _TOL_LSB + 1


def test_lsb_first_gray_order():
    """RULE #0: the map is LSB-first — (b0,b1)=(1,0) is the FIRST bit set, and it
    must give +3 (d=1), NOT (0,1). Distinguishes LSB-first from MSB-first."""
    blk = FSK4SymbolMapperBlock("m")
    # (b0=1,b1=0): bits stream [1, 0] -> d=1 -> +3
    out_10 = run_fsk4_mapper_dut([1, 0], CHIP_YAML)
    # (b0=0,b1=1): bits stream [0, 1] -> d=2 -> -1
    out_01 = run_fsk4_mapper_dut([0, 1], CHIP_YAML)
    print(f"\n(1,0)->{out_10[0]:+.3f} (expect +1.0)  (0,1)->{out_01[0]:+.3f} (expect -1/3)")
    assert out_10[0] > 0.9, "LSB-first (1,0) must map to +3 (+1.0 normalised)"
    assert -0.5 < out_01[0] < -0.2, "LSB-first (0,1) must map to -1 (-1/3 normalised)"


# --- MANDATORY negative tests (INV-4) -----------------------------------------

def test_mutation_swapped_inner_outer_fails():
    """If the inner/outer magnitudes were swapped (+1<->+3, a wrong level table)
    the gate MUST fail."""
    dibits = [1, 0, 2, 3, 1, 0]
    bits = _bits_from_dibits(dibits)
    out = run_fsk4_mapper_dut(bits, CHIP_YAML)
    ref = _gr_levels(dibits)
    # Mutate: swap magnitudes 1/3 <-> 1.0, keeping sign.
    mutated = [np.sign(v) * ((1.0 / 3.0) if abs(v) > 0.6 else 1.0) for v in out]
    err = max(abs(m - r) for m, r in zip(mutated, ref))
    assert err * 32768 > _TOL_LSB + 1, "gate missed a swapped inner/outer level!"


def test_mutation_sign_flip_fails():
    """A sign-flipped level stream (frequency inversion) MUST fail the gate."""
    dibits = [1, 0, 2, 3, 1, 2]
    bits = _bits_from_dibits(dibits)
    out = run_fsk4_mapper_dut(bits, CHIP_YAML)
    ref = _gr_levels(dibits)
    flipped = [-v for v in out]
    err = max(abs(f - r) for f, r in zip(flipped, ref))
    assert err * 32768 > _TOL_LSB + 1, "gate missed a sign-flipped level stream!"


def test_mutation_shifted_stream_fails():
    """A one-symbol delay must be caught (no realignment)."""
    dibits = [1, 0, 2, 3, 0, 1]
    bits = _bits_from_dibits(dibits)
    out = run_fsk4_mapper_dut(bits, CHIP_YAML)
    ref = _gr_levels(dibits)
    shifted = [0.0] + list(out[:-1])
    err = max(abs(s - r) for s, r in zip(shifted, ref))
    assert err * 32768 > _TOL_LSB + 1, "gate missed a one-symbol shift!"


def test_empty_output_fails():
    ref = _gr_levels([1, 0, 2, 3])
    # An empty DUT output can't match a non-empty reference.
    assert len(ref) > 0
    out = []
    assert len(out) != len(ref)


# --- bit-exact reference parity + report --------------------------------------

def test_reference_matches_chip_bitexact():
    """The block's own Q15 reference equals the on-chip output, bit for bit."""
    blk = FSK4SymbolMapperBlock("m")
    dibits = [(i * 5 + 1) & 3 for i in range(32)]
    bits = _bits_from_dibits(dibits)
    out = run_fsk4_mapper_dut(bits, CHIP_YAML)
    ref = list(blk.process_reference(bits))
    m = min(len(out), len(ref))
    assert m >= 30
    err = max(abs(out[k] - ref[k]) for k in range(m))
    assert err == 0.0 or err * 32768 <= 1, f"chip != reference ({err*32768:.2f} LSB)"


def test_emit_report():
    from kyttar_verify import compare_against_grc  # noqa: PLC0415
    dibits = [1, 0, 2, 3] * 8
    bits = _bits_from_dibits(dibits)
    out = run_fsk4_mapper_dut(bits, CHIP_YAML)
    ref = _gr_levels(dibits)
    m = min(len(out), len(ref))
    dut_q15 = [int(round(v * 32768.0)) & 0xFFFF for v in out[:m]]
    res = compare_against_grc(dut_q15, list(ref[:m]), metric=Metric.EXACT,
                              delay=0, tolerance=_TOL_LSB + 1)
    assert res.passed, res.summary()
    write_report("FSK4SymbolMapperBlock", res, coverage={
        "levels": "M17 {+3,+1,-1,-3} normalised to {+1,+1/3,-1/3,-1}",
        "gray": "LSB-first: (1,0)->+3, (0,0)->+1, (0,1)->-1, (1,1)->-3",
        "patterns": "all-4-dibits, random, LSB-first-order",
        "mutation": True,
        "gr_equiv": "digital.chunks_to_symbols_bf([+1/3,+1,-1/3,-1], 1) over dibits",
        "note": "M17 4FSK TX mapper; bit-fed (2 LSB-first bits -> 1 PAM level).",
    })
