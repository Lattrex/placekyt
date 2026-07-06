# SPDX-License-Identifier: GPL-3.0-or-later
"""PROTO: the DualFloatToComplex LOCK rendezvous, driven by REAL routed traffic.

This drives the built DualFloatToComplexBlock through a hand-built CellMap topology
(a producer cell PI that relays an injected I EASTward into the rendezvous cell's
locked West face) and proves the arbiter-LOCK rendezvous works on REAL on-chip
routing — not just raw injection (which cannot reach an interior entry, and which
the LOCK correctly rejects when it arrives on the wrong face).

PROVEN here (the hard part of the mechanism):
  * ARM executes: the rendezvous cell's config shows LOCK enabled + LOCK_FACE=West.
  * PI relays I through the LOCKED West face → the cell latches it (xi/R4 == I).
  * got_i flips the LOCK to face_q (config LOCK_FACE bits → South) — the cell now
    only accepts the matching Q.

STILL TODO (tracked, task #428): the SECOND producer PQ, feeding Q on the other
locked face, to complete the emit — the two producers require a port SPLITTER (the
single input port injects on ONE FWD_FACE, so steering I-traffic and Q-traffic to
producers on two different faces of the rendezvous needs a two-entry FACE-steering
landing cell). The adversarial interleave + mutation gates ride on top of that.

Substrate facts this proto pinned down:
  * A JUMP reaches a cell at hop = 31 - (routed_cells_traversed); a routed path of
    manhattan distance D traverses D+1 cells (the port injection counts), so
    hop = 31 - (D + 1).  [empirically: a cell 2 away executes at hop 28, not 29.]
  * A raw JUMP cannot transit THROUGH a program cell to reach a cell beyond it —
    the PQ route must avoid the rendezvous/output program cells (hence the splitter).

Run::

    cd /home/system/placekyt
    QT_QPA_PLATFORM=offscreen .venv/bin/python -m pytest \
        verification/tests/proto_dual_f2c_rendezvous.py -q
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

_ROOT = Path(__file__).resolve().parents[2]
for p in (str(_ROOT / "placekyt"), str(_ROOT / "runtime" / "python")):
    if p not in sys.path:
        sys.path.insert(0, p)

CHIP_YAML = str(_ROOT / "placekyt" / "resources" / "chips" / "kyttar_10x12.yaml")
LIB = "lattrex.official"
W = 10


def _cid(x, y):
    return y * W + x


def _s16(w):
    w = int(w) & 0xFFFF
    return w - 0x10000 if w >= 0x8000 else w


def _build_rv_topology():
    """Build the CellMap: a rendezvous cell (the resolved DualFloatToComplex
    program) at (3,0) locked to West/South, a producer PI at (2,0) that relays an
    injected I EAST into the rendezvous, plus the port→PI routing. Returns
    (bitstream_words, ARM, GOT_I)."""
    from PySide6.QtWidgets import QApplication
    QApplication.instance() or QApplication([])
    import simkyt
    from gr_kyttar.placement.cell_map import CellMap, CellConfig, Face
    from gr_kyttar.bitstream.generator import BitstreamGenerator
    from engine.catalog import BlockCatalog
    from engine.io.chip_type_io import load_chip_type
    from ui.controller import AppController
    from model.connection import ChipPortEndpoint, BlockEndpoint

    # 1) resolve the DualFloatToComplex program (face_i=West, face_q=South, out @1 East)
    cat = BlockCatalog.from_gr_kyttar()
    ct = load_chip_type(CHIP_YAML)
    ctk = getattr(ct, "name", None) or "kyttar_10x12"
    ctrl = AppController(catalog=cat)
    ctrl.new_project("rv", ctk)
    d = ctrl.place_block("DualFloatToComplexBlock", 0, 5, 5, library=LIB,
                         params={"face_i": "west", "face_q": "south",
                                 "hop": 1, "dest_i": 0, "dest_q": 1, "entry": 1})
    ctrl.add_logical_connection(ChipPortEndpoint(chip=0, port="x16_in"),
                                BlockEndpoint(block=d, port="i"), name="ni")
    ctrl.add_logical_connection(BlockEndpoint(block=d, port="out"),
                                ChipPortEndpoint(chip=0, port="x16_out"), name="no")
    ctrl.auto_route_all({ctk: ct}, use_bus="never", auto_orient=False, register=True)
    bres = ctrl.build()
    c0 = ctrl.project.block(d).placement.cells[0]
    rv_mem = list(bres.chips[0].cells[(c0.x, c0.y)]["memory"])
    ARM, GOT_I = 17, 20  # entry addresses in the resolved program

    # 2) hand-build the topology
    cm = CellMap(width=10, height=12)
    rc = CellConfig(fwd_face=Face.EAST, block_name="rv")
    for a, v in enumerate(rv_mem):
        if v:
            rc.set_memory(a, v)
    rc.entry_addr = ARM
    cm.set_cell(3, 0, rc)
    # producer PI at (2,0): relay entry writes R0 EAST into the rendezvous' got_i.
    prog = simkyt.Program.from_source(
        "pi", f"go:\n    MOVE [FACE], R10\n    WRITE @1, 0\n    JUMP @1, {GOT_I}\n"
        "    HALT\n", 1)
    pc = CellConfig(fwd_face=Face.EAST, block_name="PI")
    for i, w in enumerate(prog.get_words()):
        pc.set_memory(1 + i, w)
    pc.set_memory(10, int(Face.EAST))
    pc.entry_addr = 1
    cm.set_cell(2, 0, pc)
    cm.add_routing_cell(0, 0, Face.EAST)
    cm.add_routing_cell(1, 0, Face.EAST)

    gen = BitstreamGenerator(CHIP_YAML)
    gen.load_cell_map(cm)
    return gen.generate().words, ARM, GOT_I


def test_arm_and_i_latch_through_real_routing():
    """ARM locks the cell to West; PI relays I through the LOCKED West face and the
    cell latches it; got_i flips the LOCK to face_q (South)."""
    import simkyt
    words, ARM, GOT_I = _build_rv_topology()
    chip = simkyt.Chip.from_yaml(CHIP_YAML)
    chip.load_bitstream_physical(words)

    # arm the rendezvous @ (3,0): routed distance 3 → hop 31-(3+1)=27
    chip.inject_jump_physical(target_hop_cnt=27, entry_addr=ARM)
    chip.run(max_events=30000)
    cfg = chip.read_config(_cid(3, 0))
    # LOCK_FACE = West (2): config bits [13:12] == 10.
    assert (cfg >> 12) & 0x3 == 2, f"arm did not LOCK to West: cfg={cfg:#06x}"

    # PI @ (2,0): inject I=1500 (routed dist 2 → hop 28), then JUMP its relay entry.
    chip.inject_data_physical([1500], target_hop_cnt=28, target_addr=0)
    chip.run(max_events=30000)
    chip.inject_jump_physical(target_hop_cnt=28, entry_addr=1)
    chip.run(max_events=60000)

    # the rendezvous latched I into its xi state register (R4) — through the LOCK.
    assert _s16(chip.read_cell_memory(_cid(3, 0), 4)) == 1500, "I not latched via lock"
    # and got_i flipped the LOCK to face_q = South (bits [13:12] == 00).
    cfg2 = chip.read_config(_cid(3, 0))
    assert (cfg2 >> 12) & 0x3 == 0, f"got_i did not flip LOCK to South: cfg={cfg2:#06x}"
