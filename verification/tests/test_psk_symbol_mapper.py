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


# --- symbol_table parity: GR digital.chunks_to_symbols (index -> symbol) -------
# The block now mirrors chunks_to_symbols: an arbitrary complex symbol_table +
# dimension; INPUT INDEX -> symbol_table[index] (D=1). The bit-packing modulation
# presets remain a documented Kyttar extension (tested above). Index path verified
# against digital.chunks_to_symbols_ic.

import json  # noqa: E402
import subprocess  # noqa: E402


def _gr_chunks(table, idx):
    """GR golden: chunks_to_symbols_ic(symbol_table, 1), index in -> complex out."""
    script = """
from gnuradio import gr, digital, blocks
import json, sys
d = json.loads(sys.stdin.read())
tbl = [complex(a, b) for a, b in d["t"]]
tb = gr.top_block(); src = blocks.vector_source_i(d["i"], False)
c = digital.chunks_to_symbols_ic(tbl, 1); snk = blocks.vector_sink_c()
tb.connect(src, c, snk); tb.run()
print(json.dumps([[z.real, z.imag] for z in snk.data()]))
"""
    gr_py = os.environ.get("KYTTAR_GR_PYTHON", "/usr/bin/python3")
    r = subprocess.run([gr_py, "-c", script],
                       input=json.dumps({"t": [[z.real, z.imag] for z in table],
                                         "i": list(idx)}),
                       capture_output=True, text=True)
    assert r.returncode == 0, r.stderr[-500:]
    return [complex(a, b) for a, b in json.loads(r.stdout.strip().splitlines()[-1])]


def _run_index_dut(table, idx):
    """Build the index-driven mapper, feed indices, drain I+Q per trigger."""
    import simkyt
    from PySide6.QtWidgets import QApplication
    QApplication.instance() or QApplication([])
    from engine.catalog import BlockCatalog
    from engine.io.chip_type_io import load_chip_type
    from engine.build import BuildEngine
    from ui.controller import AppController
    from model.connection import ChipPortEndpoint, BlockEndpoint
    cat = BlockCatalog.from_gr_kyttar()
    ct = load_chip_type(CHIP_YAML)
    ctk = getattr(ct, "name", "kyttar_10x12")
    ctrl = AppController(catalog=cat)
    ctrl.new_project("m", ctk)
    params = {"symbol_table": table, "dimension": 1}
    blk = ctrl.place_block("PSKSymbolMapperBlock", 0, 1, 1,
                           library="lattrex.official", params=params)
    ctrl.add_logical_connection(ChipPortEndpoint(chip=0, port="x16_in"),
                                BlockEndpoint(block=blk, port="index"), name="in")
    ctrl.add_logical_connection(BlockEndpoint(block=blk, port="out_i"),
                                ChipPortEndpoint(chip=0, port="x16_out"), name="o")
    rep = ctrl.auto_route_all({ctk: ct})
    assert rep.ok, rep.failed
    res = BuildEngine(cat, CHIP_YAML).build(ctrl.project, {ctk: ct})
    assert res.ok, res.errors
    entry, ins = cat.resolved_io("PSKSymbolMapperBlock", params, library="lattrex.official")
    port = ct.port("x16_in")
    bo = ctrl.project.block(blk)
    lc = bo.placement.cells[0]
    dist = abs(lc.x - port.cell_x) + abs(lc.y - port.cell_y) + 1
    hop = max(0, 31 - dist)
    chip = simkyt.Chip.from_yaml(CHIP_YAML)
    chip.load_bitstream_physical(res.words(0))
    chip.set_port_entry_address("x16_in", entry)

    def s16(v):
        v = int(v) & 0xFFFF
        return v - 0x10000 if v >= 0x8000 else v
    out = []
    for ix in idx:
        chip.inject_data_physical([int(ix) & 0xFFFF], target_hop_cnt=hop,
                                  target_addr=int(ins[0]))
        chip.run(max_events=6000)
        chip.inject_jump_physical(target_hop_cnt=hop, entry_addr=entry)
        chip.run(max_events=200000)
        w = []
        while chip.output_available("x16_out"):
            w += [int(x) & 0xFFFF for x in chip.read_port_i16("x16_out").view("uint16").tolist()]
            chip.release_output_ack("x16_out")
            chip.run(max_events=8000)
        out.append(complex(s16(w[0]) / 32768.0 if len(w) >= 1 else 0.0,
                           s16(w[1]) / 32768.0 if len(w) >= 2 else 0.0))
    return out


_QPSK_TABLE = [0.7071 + 0.7071j, -0.7071 + 0.7071j,
               0.7071 - 0.7071j, -0.7071 - 0.7071j]


def test_symbol_table_index_matches_gr_qpsk():
    """An arbitrary QPSK symbol_table, index-fed, matches GR chunks_to_symbols
    bit-for-bit (within the Q15 rounding floor) on both I and Q."""
    idx = [0, 1, 2, 3, 2, 1, 0, 3, 1, 2]
    dut = _run_index_dut(_QPSK_TABLE, idx)
    gr = _gr_chunks(_QPSK_TABLE, idx)
    max_err = max(abs(d - g) for d, g in zip(dut, gr))
    print(f"\nsymbol_table qpsk vs GR: max err {max_err * 32768:.2f} LSB")
    assert max_err * 32768 <= _TOL_LSB + 1, f"{max_err * 32768:.2f} LSB too high"


def test_symbol_table_arbitrary_constellation():
    """A non-PSK arbitrary table (e.g. an asymmetric 6-point set) maps exactly —
    proving it is a real table, not a hardwired PSK preset."""
    table = [1.0 + 0j, 0.5 + 0.5j, 0 + 1j, -0.5 + 0.5j, -1.0 + 0j, 0 - 0.8j]
    idx = [0, 5, 2, 3, 4, 1, 0, 2]
    dut = _run_index_dut(table, idx)
    gr = _gr_chunks(table, idx)
    max_err = max(abs(d - g) for d, g in zip(dut, gr))
    print(f"\narbitrary table vs GR: max err {max_err * 32768:.2f} LSB")
    assert max_err * 32768 <= _TOL_LSB + 1, f"{max_err * 32768:.2f} LSB too high"


def test_symbol_table_dimension_gt1_raises():
    """dimension>1 (vector symbols) is the documented HW limit and MUST raise."""
    from gr_kyttar.placement.blocks.psk_symbol_mapper_block import PSKSymbolMapperBlock
    with pytest.raises(ValueError, match="HARDWARE LIMIT"):
        PSKSymbolMapperBlock("x", symbol_table=_QPSK_TABLE, dimension=2)


def test_symbol_table_too_large_raises():
    """A symbol_table beyond the per-cell table budget MUST raise (HW limit)."""
    from gr_kyttar.placement.blocks.psk_symbol_mapper_block import PSKSymbolMapperBlock
    big = [complex(k, -k) for k in range(PSKSymbolMapperBlock.MAX_SYMBOL_TABLE + 1)]
    with pytest.raises(ValueError, match="HARDWARE LIMIT"):
        PSKSymbolMapperBlock("x", symbol_table=big)
