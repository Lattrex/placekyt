# SPDX-License-Identifier: GPL-3.0-or-later
"""Verify PSKSymbolMapperBlock (BPSK) 1:1 against GNU Radio.

The TX-chain symbol mapper turns a stream of bits (0/1) into BPSK constellation
symbols. The exact GNU Radio equivalent is::

    digital.chunks_to_symbols_bf([1.0, -1.0], 1)

i.e. a 1-bit-per-symbol map  bit 0 -> +1.0,  bit 1 -> -1.0  (no Gray ambiguity for
BPSK). The on-chip block emits one I symbol per input bit; Q is identically zero,
so a real-valued comparison against ``chunks_to_symbols_bf`` is exact.

Run (GNU Radio lives in the system Python)::

    cd verification
    KYTTAR_GR_PYTHON=/usr/bin/python3 QT_QPA_PLATFORM=offscreen \
        .venv/bin/python -m pytest tests/test_psk_symbol_mapper.py -v
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

_PLACEKYT = Path(__file__).resolve().parents[2] / "placekyt"
_VERIFY = Path(__file__).resolve().parents[1]
for p in (str(_PLACEKYT), str(_VERIFY)):
    if p not in sys.path:
        sys.path.insert(0, p)

from kyttar_verify import (  # noqa: E402
    run_block_dut, run_gnuradio_ref, compare_against_grc, write_report, Metric)

CHIP_YAML = str(_PLACEKYT / "resources" / "chips" / "kyttar_10x12.yaml")

_GR_AVAILABLE = os.path.exists(os.environ.get("KYTTAR_GR_PYTHON", "/usr/bin/python3"))
pytestmark = pytest.mark.skipif(
    not _GR_AVAILABLE, reason="GNU Radio interpreter not available")

# BPSK symbols are full-scale +/-1, exact in Q15 -> tolerance is the rounding floor.
_TOL_LSB = 1


def _gr_bpsk(bits: list[int]):
    """GNU Radio golden: chunks_to_symbols_bf([1,-1]) over the bit stream.

    ``input_q15`` carries the bits verbatim (0/1) — we read them as ints, not as
    Q15 floats, since they index the symbol table.
    """
    return run_gnuradio_ref(
        bits,
        """
from gnuradio import gr, blocks, digital

bits = [int(v) & 0xFFFF for v in input_q15]

tb = gr.top_block()
src = blocks.vector_source_b(bits, False, 1, [])
mapper = digital.chunks_to_symbols_bf([1.0, -1.0], 1)
# chunks_to_symbols_bf emits float symbols; capture them directly.
snk = blocks.vector_sink_f()
tb.connect(src, mapper, snk)
tb.run()
output_float = list(snk.data())
""",
    )


def _run(bits):
    dut = run_block_dut("PSKSymbolMapperBlock", bits,
                        params={"modulation": "bpsk"}, chip_yaml=CHIP_YAML,
                        in_port="sample", out_port="out")
    assert dut.ok, dut.reason
    ref = _gr_bpsk(bits)
    res = compare_against_grc(dut.outputs_q15, ref.floats, metric=Metric.EXACT,
                              delay=0, tolerance=_TOL_LSB)
    return dut, res


# --- correctness ---------------------------------------------------------------

def test_bpsk_alternating():
    """Alternating bits map to alternating +1/-1 symbols, bit-exact vs GR."""
    bits = [0, 1] * 16
    dut, res = _run(bits)
    print("\nbpsk alt:", res.summary(), "| words", dut.n_words)
    assert res.passed, res.summary()


def test_bpsk_random_pattern():
    """A pseudo-random bit pattern maps exactly (full-scale +/-1, no Q15 loss)."""
    # deterministic LFSR-ish pattern, no Date.now/random needed
    bits = [(i * 13 + 7) & 1 for i in range(64)]
    dut, res = _run(bits)
    print("\nbpsk rand:", res.summary())
    assert res.passed, res.summary()


def test_bpsk_all_zeros_all_ones():
    """Edge stimulus: all 0 -> all +1, all 1 -> all -1."""
    for bits in ([0] * 24, [1] * 24):
        dut, res = _run(bits)
        print(f"\nbpsk const {bits[0]}:", res.summary())
        assert res.passed, res.summary()


# --- MANDATORY negative tests --------------------------------------------------

def test_mutation_swapped_mapping_fails():
    """If the DUT output were sign-flipped (0->-1, 1->+1) the gate MUST fail."""
    bits = [0, 1, 1, 0, 1, 0, 0, 1, 1, 1, 0, 0]
    dut = run_block_dut("PSKSymbolMapperBlock", bits,
                        params={"modulation": "bpsk"}, chip_yaml=CHIP_YAML,
                        in_port="sample", out_port="out")
    assert dut.ok, dut.reason
    ref = _gr_bpsk(bits)
    flipped = [(0x10000 - (w or 0)) & 0xFFFF for w in dut.outputs_q15]
    res = compare_against_grc(flipped, ref.floats, metric=Metric.EXACT,
                              delay=0, tolerance=_TOL_LSB)
    assert not res.passed, "gate failed to detect a sign-flipped BPSK mapping!"


def test_mutation_shifted_stream_fails():
    """A one-symbol delay must be caught (EXACT metric, no realignment)."""
    bits = [0, 1, 1, 0, 1, 0, 0, 1, 0, 1, 1, 0]
    dut, _ = _run(bits)
    shifted = [0x0000] + list(dut.outputs_q15[:-1])
    ref = _gr_bpsk(bits)
    res = compare_against_grc(shifted, ref.floats, metric=Metric.EXACT,
                              delay=0, tolerance=_TOL_LSB)
    assert not res.passed, "gate failed to detect a one-symbol stream shift!"


def test_empty_output_fails():
    ref = _gr_bpsk([0, 1, 0, 1])
    res = compare_against_grc([], ref.floats, metric=Metric.EXACT,
                              tolerance=_TOL_LSB)
    assert not res.passed


# --- report --------------------------------------------------------------------

def test_emit_report():
    dut, res = _run([0, 1] * 16)
    write_report("PSKSymbolMapperBlock", res, coverage={
        "modulation": "bpsk",
        "patterns": "alternating, random, all-0, all-1",
        "mutation": True,
        "gr_equiv": "digital.chunks_to_symbols_bf([1.0,-1.0], 1)",
        "note": "BPSK I-only, full-scale +/-1 exact in Q15",
    })
