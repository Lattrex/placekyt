# SPDX-License-Identifier: GPL-3.0-or-later
"""Shared on-chip DUT runners for the 16-QAM mapper + slicer.

The mapper emits a COMPLEX (I, Q) pair per symbol (two egress words down the shared
complex-egress corridor); the slicer emits ONE 4-bit symbol word per (I, Q) input.
Both need the drain-all-words-per-trigger loop (not the single-word ``run_block_dut``),
so we reuse the FSK4 stream runner. Modeled on ``fsk4_dut``.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

_PLACEKYT = Path(__file__).resolve().parents[2] / "placekyt"
_RUNTIME = Path(__file__).resolve().parents[2] / "runtime" / "python"
_VERIFY = Path(__file__).resolve().parents[1]
for p in (str(_PLACEKYT), str(_RUNTIME), str(_VERIFY)):
    if p not in sys.path:
        sys.path.insert(0, p)

from fsk4_dut import _run_single_block_stream, _s16  # noqa: E402


def run_qam16_mapper_dut(bits, chip_yaml):
    """Feed bits (0/1, MSB-first, 4 per symbol) to the 16-QAM mapper; return the
    recovered constellation points as a list of complex FLOATS (one per symbol).

    The mapper emits I then Q down the shared complex-egress corridor, so the flat
    egress stream is I0,Q0,I1,Q1,... — de-interleave into complex points."""
    words = _run_single_block_stream(
        "QAM16SymbolMapperBlock", {}, [int(b) & 1 for b in bits], chip_yaml,
        in_port="sample", out_port="out_i")
    pts = []
    for k in range(0, len(words) - 1, 2):
        pts.append(complex(_s16(words[k]) / 32768.0, _s16(words[k + 1]) / 32768.0))
    return pts


def run_qam16_slicer_dut(iq_pairs_q15, chip_yaml):
    """Feed (I, Q) Q15 pairs to the 16-QAM slicer; return the emitted 4-bit symbol
    indices (0..15), one per input point."""
    from fsk4_dut import _run_single_block_stream as _r  # noqa: PLC0415
    # The slicer is a 2-input complex block; drive via the complex runner.
    return _run_qam16_slicer_complex(iq_pairs_q15, chip_yaml)


def _run_qam16_slicer_complex(iq_pairs_q15, chip_yaml):
    """Build the slicer (x16_in complex -> slicer -> x16_out), drive (I,Q) pairs,
    drain the 4-bit symbol word per pair."""
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
    blk = ctrl.place_block("QAM16SlicerBlock", 0, 1, 1, library="lattrex.official",
                           params={})
    ctrl.add_logical_connection(ChipPortEndpoint(chip=0, port="x16_in"),
                                BlockEndpoint(block=blk, port="in_i"), name="ini")
    ctrl.add_logical_connection(BlockEndpoint(block=blk, port="out"),
                                ChipPortEndpoint(chip=0, port="x16_out"), name="o")
    rep = ctrl.auto_route_all({ctk: ct})
    assert rep.ok, rep.failed
    res = BuildEngine(cat, chip_yaml).build(ctrl.project, {ctk: ct})
    assert res.ok, res.errors

    entry, ins = cat.resolved_io("QAM16SlicerBlock", {}, library="lattrex.official")
    port = ct.port("x16_in")
    bo = ctrl.project.block(blk)
    lc = bo.placement.cells[0]
    dist = abs(lc.x - port.cell_x) + abs(lc.y - port.cell_y) + 1
    hop = max(0, 31 - dist)

    chip = simkyt.Chip.from_yaml(chip_yaml)
    chip.load_bitstream_physical(res.words(0))
    chip.set_port_entry_address("x16_in", entry)

    out = []
    for (i, q) in iq_pairs_q15:
        # deliver I then Q to the slicer's two input registers, then trigger
        chip.inject_data_physical([int(i) & 0xFFFF], target_hop_cnt=hop,
                                  target_addr=int(ins[0]))
        chip.run(max_events=6000)
        chip.inject_data_physical([int(q) & 0xFFFF], target_hop_cnt=hop,
                                  target_addr=int(ins[1]))
        chip.run(max_events=6000)
        chip.inject_jump_physical(target_hop_cnt=hop, entry_addr=entry)
        chip.run(max_events=200000)
        while chip.output_available("x16_out"):
            out += [int(x) & 0xFFFF for x in
                    chip.read_port_i16("x16_out").view("uint16").tolist()]
            chip.release_output_ack("x16_out")
            chip.run(max_events=8000)
    return [w & 0xF for w in out]
