# SPDX-License-Identifier: GPL-3.0-or-later
"""PROTO: the DualFloatToComplex LOCK rendezvous, driven by REAL routed traffic.

This drives the built DualFloatToComplexBlock through a hand-built CellMap topology
and proves — end to end, on the simulated fabric — that the arbiter-LOCK rendezvous
pairs TWO independent real producers (I on one face, Q on another) into ONE complex
packet, MATCHED-PAIRS-ONLY, regardless of interleaving.

Two topologies are exercised:

1. `_build_rv_topology` (one producer): PI relays an injected I EASTward into the
   rendezvous cell's locked West face — proves the arm + first-face latch + lock
   retarget in isolation (`test_arm_and_i_latch_through_real_routing`).

2. `_build_two_face_topology` (task #428, the full mechanism): a SplitterBlock-style
   landing cell steers the shared input port's bursts by JUMP-entry tag —
     * rx-arm (I): FACE=East → PI → the rendezvous' locked West face,
     * tx-arm (Q): FACE=South → a Q corridor → PQ → the rendezvous' locked South face,
   so ONE physical input port feeds BOTH producers on BOTH faces (a single port
   injects on ONE FWD_FACE, so two-face delivery genuinely needs this splitter).

PROVEN end-to-end (`test_two_face_matched_pair_emit`):
  * P1  matched:      I then Q  → emits the pair (xi=I, xq=Q) downstream.
  * P2  wrong-face:   Q arriving FIRST (while locked to West) is REJECTED (xq stays 0,
        nothing emitted) — the lock refuses the unmatched face.
  * P3  interleave:   Q-early THEN I → the queued Q drains after the lock retargets to
        South → still a matched (I, Q). The async input FIFO + lock retarget reorder
        an out-of-order arrival into a correct pair.
  * MUT mutation gate (INV-4): with the ARM removed (no lock), the SAME early Q is
        consumed UNPAIRED (xq=Q with no I) — the matched-pairs guarantee collapses.
        This proves the guarantee is enforced by the LOCK, not by luck of timing.

Substrate facts this proto pinned down:
  * A JUMP reaches a cell at hop = 31 - (routed_cells_traversed); a routed path of
    manhattan distance D traverses D+1 cells (the port injection counts), so
    hop = 31 - (D + 1).  [empirically: a cell 2 away executes at hop 28, not 29.]
  * A raw JUMP cannot be injected at the single input port and reach two producers on
    two different corridors — the port feeds ONE FWD_FACE chain. The splitter landing
    cell is what fans one port to two faces (by JUMP-entry tag).

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


# --------------------------------------------------------------------------- #
#  Task #428: the FULL two-face emit — one port -> splitter -> two producers.  #
# --------------------------------------------------------------------------- #

# Layout on the 10x12 fabric (x, y):
#     port -> (0,0)S -> (0,1)E -> SPLIT(1,1)
#     SPLIT rx-arm (East) -> PI(2,1) -> RV(3,1) West face   [got_i]
#     SPLIT tx-arm (South) -> QR1(1,2)E -> QR2(2,2)E -> PQ(3,2) -> RV(3,1) South face [got_q]
#     RV emits (xi,xq) EAST -> OUT(4,1)   [dest_i=0, dest_q=1]
_RV_XY = (3, 1)
_OUT_XY = (4, 1)
_ARM_HOP = 26      # proven: JUMP@ARM reaches RV(3,1) through the splitter/PI chain
_SPLIT_HOP = 28    # proven: JUMP reaches SPLIT(1,1); both rx/tx bursts use it
_RX_E, _TX_E = 1, 5  # splitter entry addresses (rx block is 4 instrs, tx follows)


def _build_two_face_topology():
    """Build the full splitter->2-producer->rendezvous->OUT topology.

    Returns the bitstream words. The rendezvous program is the REAL resolved
    DualFloatToComplexBlock (face_i=West, face_q=South); everything else is a
    minimal hand-built relay/steer harness that mimics the auto-P&R fan-out.
    """
    from PySide6.QtWidgets import QApplication
    QApplication.instance() or QApplication([])
    import simkyt
    from gr_kyttar.placement.cell_map import CellMap, CellConfig, Face
    from gr_kyttar.bitstream.generator import BitstreamGenerator
    from engine.catalog import BlockCatalog
    from engine.io.chip_type_io import load_chip_type
    from ui.controller import AppController
    from model.connection import ChipPortEndpoint, BlockEndpoint

    # resolve the REAL rendezvous program (face_i=West, face_q=South, out @1 East)
    cat = BlockCatalog.from_gr_kyttar()
    ct = load_chip_type(CHIP_YAML)
    ctk = getattr(ct, "name", None) or "kyttar_10x12"
    ctrl = AppController(catalog=cat)
    ctrl.new_project("rv2", ctk)
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
    ARM, GOT_I, GOT_Q = 17, 20, 23  # resolved entry addresses

    def words(src):
        return list(simkyt.Program.from_source("x", src, 1).get_words())

    cm = CellMap(width=10, height=12)

    # rendezvous (the real block program)
    rc = CellConfig(fwd_face=Face.EAST, block_name="RV")
    for a, v in enumerate(rv_mem):
        if v:
            rc.set_memory(a, v)
    rc.entry_addr = ARM
    cm.set_cell(*_RV_XY, rc)

    # splitter landing cell: rx-arm faces East (I), tx-arm faces South (Q).
    # Faces live at R20 (East) / R21 (South) to avoid colliding with code at addr 1..8.
    sp_src = (
        "rx:\n    MOVE [FACE], R20\n    WRITE @1, 0\n    JUMP @1, 1\n    HALT\n"
        "tx:\n    MOVE [FACE], R21\n    WRITE @1, 0\n    JUMP @1, 1\n    HALT\n"
    )
    sp = CellConfig(fwd_face=Face.EAST, block_name="SPLIT")
    for i, v in enumerate(words(sp_src)):
        sp.set_memory(1 + i, v)
    sp.set_memory(20, int(Face.EAST))
    sp.set_memory(21, int(Face.SOUTH))
    sp.entry_addr = _RX_E
    cm.set_cell(1, 1, sp)

    def relay(x, y, face, jump_entry, name):
        src = ("go:\n    MOVE [FACE], R20\n    WRITE @1, 0\n"
               f"    JUMP @1, {jump_entry}\n    HALT\n")
        c = CellConfig(fwd_face=face, block_name=name)
        for i, v in enumerate(words(src)):
            c.set_memory(1 + i, v)
        c.set_memory(20, int(face))
        c.entry_addr = 1
        cm.set_cell(x, y, c)

    relay(2, 1, Face.EAST, GOT_I, "PI")    # I -> RV.West got_i
    relay(1, 2, Face.EAST, 1, "QR1")       # Q corridor
    relay(2, 2, Face.EAST, 1, "QR2")
    relay(3, 2, Face.NORTH, GOT_Q, "PQ")   # Q -> RV.South got_q

    # OUT sink: capture the emitted packet in R0/R1 (dest_i/dest_q). Its own read
    # program lives at HIGH addresses so the incoming WRITEs to R0/R1 don't clobber
    # code; the packet is read directly from R0/R1 after the run.
    out_src = "cap:\n    HALT\n"
    oc = CellConfig(fwd_face=Face.SOUTH, block_name="OUT")
    for i, v in enumerate(words(out_src)):
        oc.set_memory(16 + i, v)   # code at addr 16..; R0/R1 stay free for the packet
    oc.entry_addr = 16
    cm.set_cell(*_OUT_XY, oc)

    cm.add_routing_cell(0, 0, Face.SOUTH)
    cm.add_routing_cell(0, 1, Face.EAST)

    gen = BitstreamGenerator(CHIP_YAML)
    gen.load_cell_map(cm)
    return gen.generate().words


def _arm_and_feed(words, steps, arm=True):
    """Load a fresh chip, optionally ARM, then feed a list of ('I'|'Q', value) bursts
    through the splitter. Returns (rv_xi, rv_xq, out_xi, out_xq)."""
    import simkyt
    chip = simkyt.Chip.from_yaml(CHIP_YAML)
    chip.load_bitstream_physical(words)
    if arm:
        chip.inject_jump_physical(target_hop_cnt=_ARM_HOP, entry_addr=17)  # ARM
        chip.run(max_events=60000)
    for kind, val in steps:
        entry = _RX_E if kind == "I" else _TX_E
        chip.inject_data_physical([val], target_hop_cnt=_SPLIT_HOP, target_addr=0)
        chip.run(max_events=60000)
        chip.inject_jump_physical(target_hop_cnt=_SPLIT_HOP, entry_addr=entry)
        chip.run(max_events=200000)
    rx, ry = _RV_XY
    ox, oy = _OUT_XY
    return (_s16(chip.read_cell_memory(_cid(rx, ry), 4)),   # RV.xi (R4)
            _s16(chip.read_cell_memory(_cid(rx, ry), 5)),   # RV.xq (R5)
            _s16(chip.read_cell_memory(_cid(ox, oy), 0)),   # OUT.R0 (emitted xi)
            _s16(chip.read_cell_memory(_cid(ox, oy), 1)))   # OUT.R1 (emitted xq)


def test_two_face_matched_pair_emit():
    """P1: I then Q -> the rendezvous emits the MATCHED complex pair (xi, xq)
    downstream through the two-face LOCK."""
    words = _build_two_face_topology()
    xi, xq, o_xi, o_xq = _arm_and_feed(words, [("I", 1500), ("Q", 2500)])
    assert (xi, xq) == (1500, 2500), f"rendezvous did not latch both faces: {xi},{xq}"
    assert (o_xi, o_xq) == (1500, 2500), f"emitted packet wrong: {o_xi},{o_xq}"


def test_two_face_early_q_rejected():
    """P2: a Q arriving FIRST (while the cell is locked to West) is REJECTED by the
    lock — nothing is latched, nothing is emitted."""
    words = _build_two_face_topology()
    xi, xq, o_xi, o_xq = _arm_and_feed(words, [("Q", 2500)])
    assert xq == 0, f"early Q was latched despite West lock: xq={xq}"
    assert (o_xi, o_xq) == (0, 0), f"early Q caused a spurious emit: {o_xi},{o_xq}"


def test_two_face_out_of_order_still_pairs():
    """P3: Q-early THEN I -> the queued Q drains after the lock retargets to South,
    producing the correct matched pair (I, Q)."""
    words = _build_two_face_topology()
    xi, xq, o_xi, o_xq = _arm_and_feed(words, [("Q", 2500), ("I", 1500)])
    assert (xi, xq) == (1500, 2500), f"out-of-order did not pair: {xi},{xq}"
    assert (o_xi, o_xq) == (1500, 2500), f"out-of-order emit wrong: {o_xi},{o_xq}"


def test_two_face_lock_is_load_bearing_mutation():
    """MUTATION GATE (INV-4): remove the ARM (no lock). The SAME early Q that P2
    proved is rejected is now consumed UNPAIRED (xq=Q with no I ever sent). The
    matched-pairs guarantee collapses without the lock — proving the guarantee is
    enforced by the LOCK mechanism, not by timing luck.

    This test asserts the CORRUPTED behavior, so it is a live proof that P2's gate
    would FAIL on a DUT with the lock disabled (a passing P2 is not vacuous)."""
    words = _build_two_face_topology()
    # skip ARM: fire Q first, unlocked.
    xi, xq, o_xi, o_xq = _arm_and_feed(words, [("Q", 2500)], arm=False)
    assert xq == 2500, (
        "without the lock the early Q should be latched UNPAIRED (mutation gate); "
        f"got xq={xq} — if this is 0 the lock is being applied even without ARM, "
        "which would make the P2 rejection test vacuous")
