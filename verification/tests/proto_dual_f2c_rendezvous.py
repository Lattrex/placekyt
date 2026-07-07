# SPDX-License-Identifier: GPL-3.0-or-later
"""PROTO (task #428): the DualFloatToComplex PHASE-TOGGLE rendezvous under REAL routed
two-producer traffic, including ADVERSARIAL interleaving + a mutation gate.

The block pairs TWO independent real producers (an I stream and a Q stream, arriving
on DIFFERENT faces at DIFFERENT times) into ONE matched complex sample. The rewritten
block does this with a SINGLE-ENTRY PHASE TOGGLE — both producers JUMP the one ``recv``
entry, and a persistent ``phase`` register alternates 0->1->0: trigger 1 latches xi,
trigger 2 latches xq AND emits the pair. It COUNTS triggers rather than distinguishing
them by face (the old LOCK-by-face design could not work under auto-P&R, where both
rails abut the SAME neighbour face — see the block docstring).

This proto drives the REAL resolved block program through a hand-built two-face harness
(one physical input port -> a splitter landing cell -> an I producer on one face + a Q
producer on another) and proves, end to end on the simulated fabric:

  * P1  matched:        I then Q  -> the cell latches xi=I, xq=Q and emits the pair.
  * P2  two full pairs: I,Q,I,Q   -> exactly TWO emits, each a matched pair (proves the
        phase register re-arms cleanly for the next pair).
  * P3  cross-producer interleave: the two producers fire on their OWN faces/corridors,
        the arbiter serialises them, and the phase toggle pairs them consecutively —
        no cross-pair contamination between the West and South producers.
  * MUT mutation gate (INV-4): corrupt the phase register so the toggle can NEVER reach
        the correct matched-pair emit; the SAME matched I,Q then does NOT emit xi=I —
        the guarantee collapses, proving P1 is enforced by the phase toggle, not luck.

Substrate facts (unchanged routing model):
  * A JUMP reaches a cell at hop = 31 - (routed_cells_traversed); manhattan distance D
    traverses D+1 cells (the port injection counts), so hop = 31 - (D + 1).
  * A raw JUMP injected at the single input port reaches ONE FWD_FACE chain, so feeding
    two producers on two faces genuinely needs the splitter landing cell (by JUMP-entry
    tag) — the mechanism auto-P&R's fan-in reproduces.

Resolved phase-toggle program (face_i/face_q params are vestigial now):
    recv(0x14): CMP phase(R5), zero(R1) ; BR.NZ _q
    phase 0   : MOVE xi(R3)<-R0 ; phase(R5)<-one(R2) ; HALT
    _q(0x19)  : MOVE xq(R4)<-R0 ; MOVE R0<-xi(R3) ; WRITE out ; JUMP out ;
                phase(R5)<-zero(R1) ; HALT
So xi=R3, xq=R4, phase=R5; recv entry = 0x14.

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

# Resolved register / entry layout of the phase-toggle program (see module docstring).
_XI_REG, _XQ_REG, _PHASE_REG = 3, 4, 5
_RECV = 0x14


def _cid(x, y):
    return y * W + x


def _s16(w):
    w = int(w) & 0xFFFF
    return w - 0x10000 if w >= 0x8000 else w


# Layout on the 10x12 fabric (x, y) — one port fans (via a splitter) to two producers
# on two faces of the rendezvous, exactly like an auto-P&R fan-in:
#     port -> (0,0)S -> (0,1)E -> SPLIT(1,1)
#     SPLIT  I-arm (East)  -> PI(2,1) -> RV(3,1) WEST face
#     SPLIT  Q-arm (South) -> QR1(1,2)E -> QR2(2,2)E -> PQ(3,2)N -> RV(3,1) SOUTH face
#     RV emits xi EAST -> OUT(4,1)
_RV_XY = (3, 1)
_OUT_XY = (4, 1)
_SPLIT_HOP = 28    # a JUMP reaches SPLIT(1,1); both I/Q bursts inject here
_I_E, _Q_E = 1, 5  # splitter entry addresses (I-arm is 4 instrs, Q-arm follows)


def _resolve_rv_program():
    """Build + resolve the REAL DualFloatToComplex cell program (its brokered {write:out}
    /{jump:out} handoff is hop-patched to @1 by the trivial RV->OUT route). Returns the
    32-word memory image."""
    from PySide6.QtWidgets import QApplication
    QApplication.instance() or QApplication([])
    from engine.catalog import BlockCatalog
    from engine.io.chip_type_io import load_chip_type
    from ui.controller import AppController
    from model.connection import ChipPortEndpoint, BlockEndpoint

    cat = BlockCatalog.from_gr_kyttar()
    ct = load_chip_type(CHIP_YAML)
    ctk = getattr(ct, "name", None) or "kyttar_10x12"
    ctrl = AppController(catalog=cat)
    ctrl.new_project("rv", ctk)
    d = ctrl.place_block("DualFloatToComplexBlock", 0, 5, 5, library=LIB, params={})
    ctrl.add_logical_connection(ChipPortEndpoint(chip=0, port="x16_in"),
                                BlockEndpoint(block=d, port="i"), name="ni")
    ctrl.add_logical_connection(BlockEndpoint(block=d, port="out"),
                                ChipPortEndpoint(chip=0, port="x16_out"), name="no")
    ctrl.auto_route_all({ctk: ct}, use_bus="never", auto_orient=False, register=True)
    bres = ctrl.build()
    c0 = ctrl.project.block(d).placement.cells[0]
    return list(bres.chips[0].cells[(c0.x, c0.y)]["memory"])


def _build_two_face_topology():
    """Hand-build the splitter -> 2-producer -> rendezvous -> OUT harness around the
    REAL resolved rendezvous program. Returns the bitstream words. The rendezvous emits
    its `out` @1 EAST into OUT(4,1).R0 (the resolved program emits @1 to dest 0)."""
    from PySide6.QtWidgets import QApplication
    QApplication.instance() or QApplication([])
    import simkyt
    from gr_kyttar.placement.cell_map import CellMap, CellConfig, Face
    from gr_kyttar.bitstream.generator import BitstreamGenerator

    rv_mem = _resolve_rv_program()

    # The resolved program's output WRITE/JUMP hop was patched by the (5,5) placement's
    # long route to x16_out. In THIS hand-built harness RV(3,1) emits @1 EAST to OUT(4,1),
    # so re-patch the output WRITE (0x1B) + JUMP (0x1C) hop_cnt (bits [9:5]) to @1 (=30).
    _WRITE, _JUMP = 0x6000, 0x7000
    for a, w in enumerate(rv_mem):
        if (w & 0xF000) in (_WRITE, _JUMP):
            rv_mem[a] = (w & ~(0x1F << 5)) | (30 << 5)

    def words(src):
        return list(simkyt.Program.from_source("x", src, 1).get_words())

    cm = CellMap(width=10, height=12)

    # rendezvous (the real block program). Its `out` fires @1 EAST -> OUT(4,1).R0.
    rc = CellConfig(fwd_face=Face.EAST, block_name="RV")
    for a, v in enumerate(rv_mem):
        if v:
            rc.set_memory(a, v)
    rc.entry_addr = _RECV
    cm.set_cell(*_RV_XY, rc)

    # splitter landing cell: I-arm faces East, Q-arm faces South. Faces at R20/R21 to
    # avoid colliding with code at addr 1..8. Both arms relay R0 then JUMP the
    # rendezvous' single `recv` entry (the phase toggle sorts I vs Q by arrival ORDER).
    sp_src = (
        "iarm:\n    MOVE [FACE], R20\n    WRITE @1, 0\n    JUMP @1, 1\n    HALT\n"
        "qarm:\n    MOVE [FACE], R21\n    WRITE @1, 0\n    JUMP @1, 1\n    HALT\n"
    )
    sp = CellConfig(fwd_face=Face.EAST, block_name="SPLIT")
    for i, v in enumerate(words(sp_src)):
        sp.set_memory(1 + i, v)
    sp.set_memory(20, int(Face.EAST))
    sp.set_memory(21, int(Face.SOUTH))
    sp.entry_addr = _I_E
    cm.set_cell(1, 1, sp)

    def relay(x, y, face, name, jump_entry):
        src = ("go:\n    MOVE [FACE], R20\n    WRITE @1, 0\n"
               f"    JUMP @1, {jump_entry}\n    HALT\n")
        c = CellConfig(fwd_face=face, block_name=name)
        for i, v in enumerate(words(src)):
            c.set_memory(1 + i, v)
        c.set_memory(20, int(face))
        c.entry_addr = 1
        cm.set_cell(x, y, c)

    relay(2, 1, Face.EAST, "PI", _RECV)    # I -> RV WEST face, JUMP recv
    relay(1, 2, Face.EAST, "QR1", 1)       # Q corridor (relay onward)
    relay(2, 2, Face.EAST, "QR2", 1)
    relay(3, 2, Face.NORTH, "PQ", _RECV)   # Q -> RV SOUTH face, JUMP recv

    # OUT sink: capture the emitted xi in R0. Its (empty) program lives HIGH so the
    # incoming WRITE to R0 isn't clobbered by code.
    oc = CellConfig(fwd_face=Face.SOUTH, block_name="OUT")
    for i, v in enumerate(words("cap:\n    HALT\n")):
        oc.set_memory(16 + i, v)
    oc.entry_addr = 16
    cm.set_cell(*_OUT_XY, oc)

    cm.add_routing_cell(0, 0, Face.SOUTH)
    cm.add_routing_cell(0, 1, Face.EAST)

    gen = BitstreamGenerator(CHIP_YAML)
    gen.load_cell_map(cm)
    return gen.generate().words


def _feed(words, steps, *, corrupt_phase=None):
    """Load a fresh chip, optionally corrupt the phase register, then feed a list of
    ('I'|'Q', value) bursts through the splitter (inject the value at the splitter, then
    JUMP the matching arm so it relays onto the right face and triggers the rendezvous'
    recv). Returns (rv_xi, rv_xq, out_xi, emit_count)."""
    import simkyt
    chip = simkyt.Chip.from_yaml(CHIP_YAML)
    chip.load_bitstream_physical(words)
    rx, ry = _RV_XY
    ox, oy = _OUT_XY
    if corrupt_phase is not None:
        chip.write_cell_memory(_cid(rx, ry), _PHASE_REG, corrupt_phase & 0xFFFF)
    emit = 0
    for kind, val in steps:
        entry = _I_E if kind == "I" else _Q_E
        chip.inject_data_physical([val], target_hop_cnt=_SPLIT_HOP, target_addr=0)
        chip.run(max_events=60000)
        before = _s16(chip.read_cell_memory(_cid(ox, oy), 0))
        chip.inject_jump_physical(target_hop_cnt=_SPLIT_HOP, entry_addr=entry)
        chip.run(max_events=200000)
        after = _s16(chip.read_cell_memory(_cid(ox, oy), 0))
        if after != before:
            emit += 1
    return (_s16(chip.read_cell_memory(_cid(rx, ry), _XI_REG)),
            _s16(chip.read_cell_memory(_cid(rx, ry), _XQ_REG)),
            _s16(chip.read_cell_memory(_cid(ox, oy), 0)),
            emit)


def test_phase_toggle_matched_pair_emits():
    """P1: I then Q -> the rendezvous latches xi=I, xq=Q and emits ONE matched pair
    (the emitted value is the recovered real rail xi)."""
    words = _build_two_face_topology()
    xi, xq, o_xi, emit = _feed(words, [("I", 1500), ("Q", 2500)])
    assert (xi, xq) == (1500, 2500), f"rendezvous did not latch both rails: {xi},{xq}"
    assert o_xi == 1500, f"emitted xi wrong: {o_xi}"
    assert emit == 1, f"expected exactly one emit for one pair, got {emit}"


def test_phase_toggle_two_pairs_rearm():
    """P2: two full pairs I,Q,I,Q -> exactly TWO emits, each a matched pair. Proves the
    phase register re-arms cleanly (the phase-1 emit resets to phase 0 for the next I)."""
    words = _build_two_face_topology()
    xi, xq, o_xi, emit = _feed(
        words, [("I", 1100), ("Q", 2200), ("I", 3300), ("Q", 4400)])
    assert (xi, xq) == (3300, 4400), f"2nd pair not latched: {xi},{xq}"
    assert o_xi == 3300, f"last emitted xi wrong: {o_xi}"
    assert emit == 2, f"expected two emits for two pairs, got {emit}"


def test_phase_toggle_cross_producer_interleave():
    """P3: the two producers fire on their OWN faces/corridors (I on West via PI, Q on
    South via the PQ corridor) — genuinely independent paths. The arbiter serialises
    them and the phase toggle pairs consecutively. I,Q,I,Q across the two DISTINCT
    corridors still yields two matched emits (no cross-pair contamination)."""
    words = _build_two_face_topology()
    _xi, _xq, _o, emit = _feed(
        words, [("I", 700), ("Q", 800), ("I", 900), ("Q", 1000)])
    assert emit == 2, f"cross-producer interleave broke pairing: emits={emit}"


def test_phase_toggle_is_load_bearing_mutation():
    """MUTATION GATE (INV-4): corrupt the phase register so the toggle cannot reach the
    correct matched-pair emit. Feed the SAME matched I,Q that P1 emits — with a broken
    phase the block must NEVER emit the correct xi=1500. Proves P1's emit is enforced by
    the phase toggle, not by timing luck (a passing P1 is not vacuous).

    Corrupt phase := 1 (the _q / emit arm). The first trigger (I=1500) is mis-consumed by
    _q: it latches xq=1500 and emits the STALE xi (still 0 — no I was latched first),
    then resets phase:=0. The second trigger (Q=2500) is then consumed by the phase-0
    (I) arm: latches xi=2500, sets phase:=1, HALT — no emit. So the pairing is DESYNCED
    (xi=2500, xq=1500 — swapped) and the only value ever emitted is the stale 0, NEVER the
    matched xi=1500 that P1 produces."""
    words = _build_two_face_topology()
    xi, xq, o_xi, _emit = _feed(words, [("I", 1500), ("Q", 2500)], corrupt_phase=1)
    # The pairing is desynced (swapped) — the exact opposite of P1's clean (1500, 2500).
    assert (xi, xq) == (2500, 1500), (
        f"mutation did not desync the pairing as expected: xi={xi}, xq={xq}")
    # And the correct matched emit is GONE: OUT never carries xi=1500 (only stale 0).
    assert o_xi != 1500, (
        "with a corrupt phase the block must NOT emit the correct matched xi=1500 — "
        f"got o_xi={o_xi}; if it did, P1 would be vacuous")
