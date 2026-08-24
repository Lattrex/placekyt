# SPDX-License-Identifier: GPL-3.0-or-later
"""Verify ChirpSymbolMapperBlock — pack log2(m) bits into one RAW symbol word.

HONEST ANCESTRY: this block IS PackKBitsBlock (GNU Radio ``pack_k_bits_bb``)
re-parameterized — the identical single-cell MSB-first packer program with the
alphabet expressed as ``m`` (k = log2 m derived) and the GR uint8 OUTPUT-item
cap lifted (a symbol is a raw 16-bit word, so k up to 15). Consequently:

  * For m <= 256 the block is gated BIT-EXACT against the LIVE GNU Radio
    ``blocks.pack_k_bits_bb(log2 m)`` — a real GR golden (the packed byte IS
    the raw symbol word).
  * EXHAUSTIVE bit patterns for m in {4, 16, 64}: every symbol's MSB-first bit
    expansion round-trips to that symbol vs the numpy golden.
  * The lifted-cap path (m = 1024, k = 10 — beyond pack_k_bits_bb) is gated
    against the same-recurrence numpy golden on-chip.
  * PINNED + gated bit order: MSB-FIRST (the first input bit is the symbol's
    most significant bit); an LSB-first mutant golden must FAIL.
  * INV-4 mutations: LSB-first, wrong m, +1 group delay, inverted bits, empty.
  * GR convention gates: only the input LSB is read (stray high bits ignored);
    a trailing partial group (< k bits) is never emitted (floor(nin/k)).

Rate/orientation/saturation: rate-REDUCING k:1 (run_block_dut None-gaps on the
accumulating samples, the PackKBits pattern); enrolled in the shared
orientation gate (test_orientation_invariance) and the shared saturation gate
(test_pipeline_saturation REAL_1IN).

Run:
    KYTTAR_GR_PYTHON=/usr/bin/python3 QT_QPA_PLATFORM=offscreen \\
      <venv>/python -m pytest verification/tests/test_chirp_symbol_mapper.py -q
"""
from __future__ import annotations

import json
import os
import random
import subprocess
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

from kyttar_verify import write_session_report  # noqa: E402

from kyttar_verify import run_block_dut  # noqa: E402
from gr_kyttar.placement.blocks.chirp_symbol_mapper_block import (  # noqa: E402
    ChirpSymbolMapperBlock)

CHIP_YAML = str(_PLACEKYT / "resources" / "chips" / "kyttar_10x12.yaml")
_GR_PY = os.environ.get("KYTTAR_GR_PYTHON", "/usr/bin/python3")
_GR_AVAILABLE = os.path.exists(CHIP_YAML) and os.path.exists(_GR_PY)
pytestmark = pytest.mark.skipif(
    not _GR_AVAILABLE, reason="chip yaml or GNU Radio interpreter absent")


def _k(m):
    return m.bit_length() - 1


def _bits_for(sym, k, msb_first=True):
    b = [(sym >> (k - 1 - i)) & 1 for i in range(k)]
    return b if msb_first else b[::-1]


def _numpy_golden(bits, m):
    ref = ChirpSymbolMapperBlock("g", m=m).process_reference(bits)
    return [int(x) & 0xFFFF for x in ref.tolist()]


def _gr_pack(inbits, k):
    """GR golden: blocks.pack_k_bits_bb(k) — valid for k <= 8 (m <= 256)."""
    payload = {"inbits": [int(b) & 0xFF for b in inbits], "k": int(k)}
    script = (
        "import json,sys\n"
        "from gnuradio import gr, blocks\n"
        "d = json.loads(sys.stdin.read())\n"
        "tb = gr.top_block()\n"
        "src = blocks.vector_source_b(d['inbits'], False, 1, [])\n"
        "pk  = blocks.pack_k_bits_bb(d['k'])\n"
        "snk = blocks.vector_sink_b(1, 4096)\n"
        "tb.connect(src, pk); tb.connect(pk, snk)\n"
        "tb.run()\n"
        "print(json.dumps([int(x) for x in snk.data()]))\n")
    r = subprocess.run([_GR_PY, "-c", script], input=json.dumps(payload),
                       capture_output=True, text=True)
    assert r.returncode == 0, r.stderr[-800:]
    return json.loads(r.stdout.strip().splitlines()[-1])


def _run_dut(bits, m):
    dut = run_block_dut("ChirpSymbolMapperBlock",
                        [int(b) & 0xFFFF for b in bits], params={"m": m},
                        in_port="sample", chip_yaml=CHIP_YAML)
    assert dut.ok, dut.reason
    return dut


def _symbols(dut):
    """The emitted raw symbol stream: the non-None words (one lands on the
    sample completing each k-bit group; accumulating samples read None)."""
    return [int(w) & 0xFFFF for w in dut.outputs_q15 if w is not None]


# --- exhaustive roundtrip (the CSS contract) ----------------------------------

@pytest.mark.parametrize("m", [4, 16, 64])
def test_exhaustive_roundtrip(m):
    """EVERY symbol 0..m-1, expanded MSB-first to log2(m) bits, maps back to
    exactly that raw symbol word — on-chip, vs the numpy golden, exhaustively."""
    k = _k(m)
    bits = []
    for s in range(m):
        bits += _bits_for(s, k)
    dut = _run_dut(bits, m)
    got = _symbols(dut)
    assert got == list(range(m)), f"m={m} roundtrip failed: {got[:12]}..."
    assert got == _numpy_golden(bits, m)


# --- bit-exact vs the LIVE GR block (m <= 256) --------------------------------

@pytest.mark.parametrize("m", [4, 16, 64])
@pytest.mark.parametrize("seed", [1, 2, 3])
def test_random_bits_match_gr_pack_k_bits(m, seed):
    """For m <= 256 this block IS pack_k_bits_bb(log2 m): random bit streams
    (>= 3 seeds) pack word-for-word like the live GR block."""
    k = _k(m)
    rng = random.Random(1000 * seed + m)
    bits = [rng.randint(0, 1) for _ in range(k * 8)]
    got = _symbols(_run_dut(bits, m))
    gr = _gr_pack(bits, k)
    assert got == gr, f"m={m} seed={seed}: DUT {got} != GR {gr}"


@pytest.mark.parametrize("m", [4, 64])
def test_edges_all_zeros_all_ones(m):
    k = _k(m)
    for bit, want in ((0, 0), (1, m - 1)):
        got = _symbols(_run_dut([bit] * (k * 4), m))
        assert got == [want] * 4, f"m={m} all-{bit}s: {got}"


# --- the lifted GR cap: k > 8 -------------------------------------------------

def test_m1024_beyond_the_gr_byte_cap():
    """m = 1024 (k = 10) — beyond pack_k_bits_bb's uint8 output cap, legal here
    because a symbol is a raw 16-bit word. On-chip vs the numpy golden."""
    m, k = 1024, 10
    rng = random.Random(7)
    syms = [rng.randrange(m) for _ in range(6)]
    bits = []
    for s in syms:
        bits += _bits_for(s, k)
    got = _symbols(_run_dut(bits, m))
    assert got == syms == _numpy_golden(bits, m)


# --- GR conventions -----------------------------------------------------------

def test_only_input_lsb_is_read():
    """Stray high bits on an input word are ignored (GR masks d_bits & 1)."""
    m, k = 16, 4
    bits = [0xFF00, 0xABC1, 0x0002, 0x7FF1]      # LSBs: 0, 1, 0, 1 -> 0b0101
    got = _symbols(_run_dut(bits, m))
    assert got == [0b0101], f"high bits leaked into the symbol: {got}"


def test_trailing_partial_group_dropped():
    """floor(nin/k) symbols: a trailing partial group is never emitted."""
    m, k = 16, 4
    bits = _bits_for(9, k) + _bits_for(6, k) + [1, 0, 1]   # 2 full + 3 spare
    got = _symbols(_run_dut(bits, m))
    assert got == [9, 6], f"partial group leaked: {got}"


def test_param_validation_raises():
    for bad in (3, 0, 1, 65536, 100):
        with pytest.raises(ValueError):
            ChirpSymbolMapperBlock("bad", m=bad)


# --- INV-4 mutations (each must FAIL) -----------------------------------------

def _lsb_first_golden(bits, m):
    """The DEFECT golden: LSB-first packing."""
    k = _k(m)
    out = []
    for j in range(len(bits) // k):
        w = 0
        for i in range(k):
            w |= (int(bits[j * k + i]) & 1) << i
        out.append(w)
    return out


def test_mutation_bit_order_lsb_first_fails():
    """The PINNED MSB-first order: an LSB-first mutant golden must DISAGREE
    with the DUT on an asymmetric pattern."""
    m, k = 16, 4
    bits = _bits_for(0b0001, k) + _bits_for(0b0111, k)   # asymmetric words
    got = _symbols(_run_dut(bits, m))
    lsb = _lsb_first_golden(bits, m)
    assert got != lsb, "gate failed to detect LSB-first packing!"
    assert got == [0b0001, 0b0111]


def test_mutation_wrong_m_fails():
    m = 16
    bits = _bits_for(9, 4) + _bits_for(6, 4)
    got = _symbols(_run_dut(bits, m))
    assert got != _numpy_golden(bits, 4), "gate failed to detect a wrong m!"


def test_mutation_one_group_delay_fails():
    m = 16
    bits = _bits_for(9, 4) + _bits_for(6, 4)
    got = _symbols(_run_dut(bits, m))
    assert [0] + got[:-1] != _numpy_golden(bits, m), \
        "gate failed to detect a +1 symbol delay!"


def test_mutation_inverted_bits_fail():
    m = 16
    bits = _bits_for(9, 4)
    got = _symbols(_run_dut([b ^ 1 for b in bits], m))
    assert got != _numpy_golden(bits, m), "gate failed to detect inverted bits!"


def test_mutation_empty_fails():
    assert [] != _numpy_golden(_bits_for(9, 4), 16)


# --- dashboard report ---------------------------------------------------------

def test_emit_report():
    m, k = 64, 6
    bits = []
    for s in range(m):
        bits += _bits_for(s, k)
    got = _symbols(_run_dut(bits, m))
    gr = _gr_pack(bits, k)
    assert got == list(range(m)) == gr
    report = {
        "metric": "exact", "n_compared": m, "max_abs_err": 0, "tolerance": 0,
        "bit_errors": 0, "delay_used": 0,
        "coverage": {"param_sweep": 4, "exhaustive_m": [4, 16, 64],
                     "gr_bit_exact": True, "lifted_cap_m1024": True,
                     "mutation": True, "msb_first_pinned": True},
    }
    write_session_report("ChirpSymbolMapperBlock", report)
