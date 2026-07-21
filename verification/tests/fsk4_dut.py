# SPDX-License-Identifier: GPL-3.0-or-later
"""Shared on-chip DUT runners for the M17 4FSK mapper + slicer.

Both blocks emit a VARIABLE number of output words per input trigger (the mapper
emits ONE PAM level per TWO input bits; the slicer emits TWO bits per input level),
so the per-sample :func:`run_block_dut` — which drains exactly one word per input —
cannot capture them. These runners build the single block wired ``x16_in -> block
-> x16_out``, drive each input word, and drain ALL output words the trigger
produces (the ``output_available`` / ``read_port_i16`` loop), returning the full
output stream. Modeled on ``test_psk_symbol_mapper._run_index_dut``.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

_PLACEKYT = Path(__file__).resolve().parents[2] / "placekyt"
_RUNTIME = Path(__file__).resolve().parents[2] / "runtime" / "python"
for p in (str(_PLACEKYT), str(_RUNTIME)):
    if p not in sys.path:
        sys.path.insert(0, p)


def _s16(v):
    v = int(v) & 0xFFFF
    return v - 0x10000 if v >= 0x8000 else v


def _run_single_block_stream(block_type, params, inputs_q15, chip_yaml,
                             in_port="sample", out_port="out"):
    """Build ``block_type`` (x16_in -> block -> x16_out), drive each input word,
    and return the FLAT list of every output word (uint16) across all triggers."""
    import simkyt
    from PySide6.QtWidgets import QApplication
    QApplication.instance() or QApplication([])
    from engine.catalog import BlockCatalog
    from engine.io.chip_type_io import load_chip_type
    from engine.build import BuildEngine
    from ui.controller import AppController
    from model.connection import ChipPortEndpoint, BlockEndpoint

    cat = BlockCatalog.from_gr_kyttar()
    ct = load_chip_type(chip_yaml)
    ctk = getattr(ct, "name", None) or "kyttar_10x12"
    ctrl = AppController(catalog=cat)
    ctrl.new_project("m", ctk)
    blk = ctrl.place_block(block_type, 0, 1, 1, library="lattrex.official",
                           params=params or {})
    ctrl.add_logical_connection(ChipPortEndpoint(chip=0, port="x16_in"),
                                BlockEndpoint(block=blk, port=in_port), name="in")
    ctrl.add_logical_connection(BlockEndpoint(block=blk, port=out_port),
                                ChipPortEndpoint(chip=0, port="x16_out"), name="o")
    rep = ctrl.auto_route_all({ctk: ct})
    assert rep.ok, rep.failed
    res = BuildEngine(cat, chip_yaml).build(ctrl.project, {ctk: ct})
    assert res.ok, res.errors

    entry, ins = cat.resolved_io(block_type, params or {}, library="lattrex.official")
    port = ct.port("x16_in")
    bo = ctrl.project.block(blk)
    lc = bo.placement.cells[0]
    dist = abs(lc.x - port.cell_x) + abs(lc.y - port.cell_y) + 1
    hop = max(0, 31 - dist)

    chip = simkyt.Chip.from_yaml(chip_yaml)
    chip.load_bitstream_physical(res.words(0))
    chip.set_port_entry_address("x16_in", entry)

    out = []
    for v in inputs_q15:
        chip.inject_data_physical([int(v) & 0xFFFF], target_hop_cnt=hop,
                                  target_addr=int(ins[0]))
        chip.run(max_events=6000)
        chip.inject_jump_physical(target_hop_cnt=hop, entry_addr=entry)
        chip.run(max_events=200000)
        while chip.output_available("x16_out"):
            out += [int(x) & 0xFFFF for x in
                    chip.read_port_i16("x16_out").view("uint16").tolist()]
            chip.release_output_ack("x16_out")
            chip.run(max_events=8000)
    return out


def run_fsk4_mapper_dut(bits, chip_yaml):
    """Feed bits (0/1) to the mapper; return the emitted PAM levels as FLOATS
    (one level per two LSB-first bits)."""
    words = _run_single_block_stream(
        "FSK4SymbolMapperBlock", {}, [int(b) & 0xFFFF for b in bits], chip_yaml)
    return [_s16(w) / 32768.0 for w in words]


def run_fsk4_slicer_dut(levels_q15, chip_yaml):
    """Feed signed Q15 discriminator levels to the slicer; return the emitted bits
    (0/1) as a flat list (two bits, b0 then b1, per input level)."""
    words = _run_single_block_stream(
        "FSK4SlicerBlock", {}, [int(v) & 0xFFFF for v in levels_q15], chip_yaml)
    return [w & 1 for w in words]
