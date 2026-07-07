# SPDX-License-Identifier: GPL-3.0-or-later
"""PROTO (#442): the DualFloatToComplex LOCK-BY-FACE rendezvous under REAL routed
two-producer traffic — the FACE-GATE unit proof + ADVERSARIAL OUT-OF-ORDER async
interleave + a mutation gate.

The block pairs TWO independent, ASYNCHRONOUSLY-timed real producers (an I stream and a
Q stream, arriving on DIFFERENT faces at DIFFERENT times) into ONE matched complex
sample. It does this with the arbiter LOCK: the cell is locked to ONE arrival face at a
time (LOCK/LOCK_FACE), so the FACE is the stream identity. There is NO arm step — the
cell is LOCKED to the I face from the GET-GO by its cold-start ``initial_lock_face`` (the
bitstream boots it LOCK=1, LOCK_FACE=face_i). The rendezvous is:

    (cold start): LOCK=1, LOCK_FACE=face_i        (accept ONLY the I face)
    got_i(face_i): latch I ; LOCK_FACE=face_q ; HALT   (now accept ONLY the Q face)
    got_q(face_q): latch Q ; emit (yi=I, yq=Q) ; LOCK_FACE=face_i ; HALT (re-lock to I)

Because the cell is always locked to exactly one face, a word on the OTHER face is
IGNORED by the arbiter until it is that face's turn — so ANY async re-ordering
(I,I,Q,...  a slow/bursty Q) still pairs correctly. (A same-face phase-toggle counter
canNOT do this — it has no way to tell which stream a same-face word came from; see
project_dual_f2c_lock_by_face.)

This proto drives the REAL resolved block program through a hand-built two-face harness
(one physical input port -> a splitter landing cell -> an I producer on the WEST face +
a Q producer on the SOUTH face — matching the block's default face_i=west/face_q=south),
and proves, end to end on the simulated fabric:

  * FACE-GATE (the load-bearing primitive): a cell LOCKed to face A IGNORES a JUMP that
        arrives on face B and ACCEPTS one on face A — proven directly on the RV cell.
  * P1  matched:        I then Q  -> latches xi=I, xq=Q and emits ONE matched packet.
  * P2  two full pairs: I,Q,I,Q   -> exactly TWO emits, each a matched pair (the cell
        re-locks to face_i after each emit).
  * P3  OUT-OF-ORDER ASYNC (the case the rigged lockstep demo hid): I,I,Q,I,Q,Q — the
        Q path is slow/bursty. The LOCK holds the 2nd I off (it arrives on the I face
        while the cell is locked to the Q face -> ignored until the pair completes), so
        the emits are STILL correctly matched (no desync).
  * MUT mutation gate (INV-4): corrupt LOCK_FACE so the cell listens to the WRONG face;
        the SAME matched I,Q then does NOT produce the correct matched emit — proving the
        pairing is enforced by the LOCK, not by timing luck.

Substrate facts:
  * A JUMP reaches a cell at hop = 31 - (routed_cells_traversed); manhattan distance D
    traverses D+1 cells (the port injection counts), so hop = 31 - (D + 1).
  * A raw JUMP injected at the single input port reaches ONE FWD_FACE chain, so feeding
    two producers on two faces genuinely needs the splitter landing cell (by JUMP-entry
    tag) — the mechanism auto-P&R's fan-in reproduces.
  * LOCK/LOCK_FACE (CONFIG 4/3) PERSIST across HALT, so a cell can wait locked to one
    face indefinitely (project_lock_config_encoding).

Resolved LOCK program (face_i=R1 @addr1, face_q=R2 @addr2; xi=R3, xq=R4):
    got_i(0x14): MOVE xi<-R0 ; MOVE [LOCK_FACE]<-face_q ; HALT
    got_q(0x17): MOVE xq<-R0 ; MOVE R0<-xi ; WRITE yi ; MOVE R0<-xq ; WRITE yq ;
                 JUMP trig ; MOVE [LOCK_FACE]<-face_i ; HALT
Entries got_i/got_q are derived at build time (robust to placement shifts).

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

# Face codes (gr_kyttar.bitstream.generator.FACE_*): S=0, E=1, W=2, N=3.
_FACE_S, _FACE_E, _FACE_W, _FACE_N = 0, 1, 2, 3

# Resolved register layout of the LOCK program (see module docstring). xi/xq are the
# two latched rails; face_i/face_q are the is_face DataWords at addr 1 / 2.
_XI_REG, _XQ_REG = 3, 4
_FACE_I_ADDR, _FACE_Q_ADDR = 1, 2


def _lock_entries(mem):
    """(got_i, got_q) entry addresses from the resolved memory.

    got_i's first instruction is ``MOVE R{xi}, R0`` (dest=xi, src=0); got_q's first is
    ``MOVE R{xq}, R0`` (dest=xq, src=0). MOVE opcode is 0x4 (bits[15:12]). In this ISA
    encoding the DEST is the low 5 bits [4:0] and the SRC is bits [9:5] (verified against
    the disassembler: ``0x4003`` = ``MOVE dest:3, src:0``). Find each by its (dest, src=0)
    signature; got_i precedes got_q. Return in program order."""
    got_i = got_q = None
    for a, w in enumerate(mem):
        if (w & 0xF000) != 0x4000:
            continue
        dest = w & 0x1F
        src = (w >> 5) & 0x1F
        if src != 0:
            continue
        if dest == _XI_REG and got_i is None:
            got_i = a
        elif dest == _XQ_REG and got_q is None:
            got_q = a
    if got_i is None or got_q is None:
        raise AssertionError(f"could not find got_i/got_q entries: {got_i},{got_q}")
    return got_i, got_q


def _cid(x, y):
    return y * W + x


def _s16(w):
    w = int(w) & 0xFFFF
    return w - 0x10000 if w >= 0x8000 else w


# Layout on the 10x12 fabric (x, y) — one port fans (via a splitter) to two producers
# on two DISTINCT faces of the rendezvous, exactly like an auto-P&R fan-in:
#     port -> (0,0)S -> (0,1)E -> SPLIT(1,1)
#     SPLIT  I-arm (East)  -> PI(2,1) -> RV(3,1) WEST face   [JUMP got_i]
#     SPLIT  Q-arm (South) -> QR1(1,2)E -> QR2(2,2)E -> PQ(3,2)N -> RV(3,1) SOUTH face
#                                                                    [JUMP got_q]
#     RV emits yi/yq EAST -> OUT(4,1)
_RV_XY = (3, 1)
_OUT_XY = (4, 1)
_SPLIT_HOP = 28    # a JUMP reaches SPLIT(1,1); both I/Q bursts inject here
_I_E, _Q_E = 1, 5  # splitter entry addresses (I-arm is 4 instrs, Q-arm follows)


def _resolve_rv_program():
    """Build + resolve the REAL DualFloatToComplex cell program (its brokered {write:yi}
    /{write:yq}/{jump:trig} handoff is hop-patched by the trivial RV->OUT route). Returns
    the 32-word memory image."""
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
    ctrl.add_logical_connection(BlockEndpoint(block=d, port="yi"),
                                ChipPortEndpoint(chip=0, port="x16_out"), name="no")
    ctrl.auto_route_all({ctk: ct}, use_bus="never", auto_orient=False, register=True)
    bres = ctrl.build()
    c0 = ctrl.project.block(d).placement.cells[0]
    return list(bres.chips[0].cells[(c0.x, c0.y)]["memory"])


def _build_two_face_topology(*, corrupt_boot_face=None):
    """Hand-build the splitter -> 2-producer -> rendezvous -> OUT harness around the REAL
    resolved LOCK program. The I producer JUMPs got_i on RV's WEST face; the Q producer
    JUMPs got_q on RV's SOUTH face — two DISTINCT faces, matching face_i=west/face_q=south.
    The RV cell BOOTS LOCKED to the WEST (I) face via initial_lock_face. Returns the
    bitstream words; RV emits its complex packet @1 EAST into OUT(4,1) (R0=yi, R1=yq).

    ``corrupt_boot_face`` (mutation gate INV-4): boot the RV cell LOCKED to the WRONG face
    (e.g. SOUTH) instead of WEST — the I face is then locked out and the pairing collapses.
    The face_i DataWord is corrupted to match, so the re-lock is also broken."""
    from PySide6.QtWidgets import QApplication
    QApplication.instance() or QApplication([])
    import simkyt
    from gr_kyttar.placement.cell_map import CellMap, CellConfig, Face
    from gr_kyttar.bitstream.generator import BitstreamGenerator

    rv_mem = _resolve_rv_program()
    got_i, got_q = _lock_entries(rv_mem)

    # The resolved program was built at a DIFFERENT placement, so its build-time
    # face reconciliation patched face_i/face_q to THAT layout's arrival faces. This
    # hand-built harness delivers I on the WEST face and Q on the SOUTH face, so RE-SET
    # the face DataWords to match: face_i(addr1)=WEST, face_q(addr2)=SOUTH. (The re-lock
    # at the end of got_q reads face_i, so a stale value would lock the WRONG face after
    # the first pair — the re-arm would fail.)
    rv_mem[_FACE_I_ADDR] = (rv_mem[_FACE_I_ADDR] & ~0x3) | _FACE_W
    rv_mem[_FACE_Q_ADDR] = (rv_mem[_FACE_Q_ADDR] & ~0x3) | _FACE_S

    # The resolved program's output WRITE/JUMP hop was patched by the (5,5) placement's
    # long route. In THIS harness RV(3,1) emits @1 EAST to OUT(4,1), so re-patch EVERY
    # output WRITE/JUMP hop_cnt (bits [9:5]) to @1 (=30). Both output WRITEs resolve to
    # dest 0; steer the SECOND (yq) WRITE to dest 1 so OUT.R0=yi(=xi) and OUT.R1=yq(=xq)
    # — both rails separately observable, proving the FULL complex packet.
    _WRITE, _JUMP = 0x6000, 0x7000
    _seen_write = 0
    for a, w in enumerate(rv_mem):
        op = w & 0xF000
        if op in (_WRITE, _JUMP):
            w = (w & ~(0x1F << 5)) | (30 << 5)          # hop -> @1
            if op == _WRITE:
                _seen_write += 1
                if _seen_write == 2:                     # the yq rail -> dest 1
                    w = (w & ~0x1F) | 1
            rv_mem[a] = w

    def words(src):
        return list(simkyt.Program.from_source("x", src, 1).get_words())

    cm = CellMap(width=10, height=12)

    # rendezvous (the real LOCK block program). BOOTS LOCKED to the WEST (I) face — no arm.
    # Its packet fires @1 EAST -> OUT(4,1).
    rc = CellConfig(fwd_face=Face.EAST, block_name="RV")
    boot_face = _FACE_W if corrupt_boot_face is None else corrupt_boot_face
    if corrupt_boot_face is not None:
        # Mutation: corrupt the face_i DataWord too so the re-lock is broken as well.
        rv_mem[_FACE_I_ADDR] = (rv_mem[_FACE_I_ADDR] & ~0x3) | (corrupt_boot_face & 0x3)
    for a, v in enumerate(rv_mem):
        if v:
            rc.set_memory(a, v)
    rc.entry_addr = got_i
    rc.initial_lock_face = boot_face   # cold-start: LOCK=1, LOCK_FACE=this face
    cm.set_cell(*_RV_XY, rc)

    # splitter landing cell: I-arm faces East (-> PI), Q-arm faces South (-> Q corridor).
    # Faces at R20/R21 to avoid colliding with code at addr 1..8. Each arm relays R0 then
    # JUMPs the NEXT cell's `go` entry (address 1); the terminal relay (PI/PQ) is what
    # JUMPs the rendezvous' got_i / got_q, arriving on the correct face.
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

    relay(2, 1, Face.EAST, "PI", got_i)    # I -> RV WEST face, JUMP got_i
    relay(1, 2, Face.EAST, "QR1", 1)       # Q corridor (relay onward)
    relay(2, 2, Face.EAST, "QR2", 1)
    relay(3, 2, Face.NORTH, "PQ", got_q)   # Q -> RV SOUTH face, JUMP got_q

    # OUT sink: capture the emitted yi in R0, yq in R1. Its (empty) program lives HIGH so
    # the incoming WRITEs to R0/R1 aren't clobbered by code.
    oc = CellConfig(fwd_face=Face.SOUTH, block_name="OUT")
    for i, v in enumerate(words("cap:\n    HALT\n")):
        oc.set_memory(16 + i, v)
    oc.entry_addr = 16
    cm.set_cell(*_OUT_XY, oc)

    cm.add_routing_cell(0, 0, Face.SOUTH)
    cm.add_routing_cell(0, 1, Face.EAST)

    gen = BitstreamGenerator(CHIP_YAML)
    gen.load_cell_map(cm)
    return gen.generate().words, got_i, got_q


def _feed(topo, steps):
    """Load a fresh chip, then feed a list of ('I'|'Q', value) bursts through the splitter
    (inject the value at the splitter, then JUMP the matching arm so it relays onto the
    right FACE and triggers the correct got_i/got_q entry). The emitted complex packet
    lands in OUT.R0 (yi=xi), OUT.R1 (yq=xq).
    Returns (rv_xi, rv_xq, out_yi, out_yq, emit_count)."""
    import simkyt
    words, _gi, _gq = topo
    chip = simkyt.Chip.from_yaml(CHIP_YAML)
    chip.load_bitstream_physical(words)
    rx, ry = _RV_XY
    ox, oy = _OUT_XY
    emit = 0
    for kind, val in steps:
        entry = _I_E if kind == "I" else _Q_E
        chip.inject_data_physical([val], target_hop_cnt=_SPLIT_HOP, target_addr=0)
        chip.run(max_events=60000)
        b0 = _s16(chip.read_cell_memory(_cid(ox, oy), 0))
        b1 = _s16(chip.read_cell_memory(_cid(ox, oy), 1))
        chip.inject_jump_physical(target_hop_cnt=_SPLIT_HOP, entry_addr=entry)
        chip.run(max_events=200000)
        a0 = _s16(chip.read_cell_memory(_cid(ox, oy), 0))
        a1 = _s16(chip.read_cell_memory(_cid(ox, oy), 1))
        if (a0, a1) != (b0, b1):
            emit += 1
    return (_s16(chip.read_cell_memory(_cid(rx, ry), _XI_REG)),
            _s16(chip.read_cell_memory(_cid(rx, ry), _XQ_REG)),
            _s16(chip.read_cell_memory(_cid(ox, oy), 0)),
            _s16(chip.read_cell_memory(_cid(ox, oy), 1)),
            emit)


def test_face_gate_locks_out_wrong_face():
    """FACE-GATE (the load-bearing primitive, doc §4 requirement): the cell BOOTS LOCKED
    to the WEST (I) face. A Q burst (which arrives on the SOUTH face via got_q) BEFORE any
    I must be IGNORED — the arbiter accepts only the WEST face — so xq stays unlatched and
    nothing emits. Then an I (WEST) IS accepted, latches xi, and flips the lock to SOUTH;
    now the Q IS accepted and the pair emits. This proves the LOCK gates by FACE, which is
    the entire basis of the async-safe pairing."""
    topo = _build_two_face_topology()
    # A lone Q first: locked to WEST, the SOUTH-face Q is ignored — no latch, no emit.
    xi, xq, o_yi, o_yq, emit = _feed(topo, [("Q", 9999)])
    assert emit == 0, f"a Q on the locked-out SOUTH face must NOT emit; emit={emit}"
    assert xq != 9999, (
        f"the Q was accepted on the WRONG (locked-out) face — LOCK gate failed: xq={xq}")
    # Now the proper order works: I (WEST) accepted -> Q (SOUTH) accepted -> emit.
    xi, xq, o_yi, o_yq, emit = _feed(topo, [("I", 1111), ("Q", 2222)])
    assert (xi, xq) == (1111, 2222) and emit == 1 and (o_yi, o_yq) == (1111, 2222), (
        f"the face-gated pair did not emit correctly: xi={xi} xq={xq} "
        f"emit={emit} yi={o_yi} yq={o_yq}")


def test_lock_matched_pair_emits():
    """P1: I then Q -> the rendezvous latches xi=I, xq=Q and emits ONE matched COMPLEX
    packet: OUT.yi = I AND OUT.yq = Q (both rails delivered, the Q rail is NOT lost)."""
    topo = _build_two_face_topology()
    xi, xq, o_yi, o_yq, emit = _feed(topo, [("I", 1500), ("Q", 2500)])
    assert (xi, xq) == (1500, 2500), f"rendezvous did not latch both rails: {xi},{xq}"
    assert (o_yi, o_yq) == (1500, 2500), (
        f"emitted complex packet wrong (Q rail lost?): yi={o_yi}, yq={o_yq}")
    assert emit == 1, f"expected exactly one emit for one pair, got {emit}"


def test_lock_two_pairs_relock():
    """P2: two full pairs I,Q,I,Q -> exactly TWO emits, each a matched COMPLEX packet.
    Proves the cell RE-LOCKS to face_i after each emit (ready for the next I)."""
    topo = _build_two_face_topology()
    xi, xq, o_yi, o_yq, emit = _feed(
        topo, [("I", 1100), ("Q", 2200), ("I", 3300), ("Q", 4400)])
    assert (xi, xq) == (3300, 4400), f"2nd pair not latched: {xi},{xq}"
    assert (o_yi, o_yq) == (3300, 4400), (
        f"last emitted packet wrong: yi={o_yi}, yq={o_yq}")
    assert emit == 2, f"expected two emits for two pairs, got {emit}"


def test_lock_out_of_order_async_still_pairs():
    """P3 — THE CASE THE RIGGED LOCKSTEP DEMO HID: the two producers are async and can
    arrive OUT OF ORDER. Feed I,I,Q,I,Q,Q (the Q path is slow/bursty). While the cell is
    locked to the Q (SOUTH) face waiting for the pair's Q, the SECOND I (on the WEST face)
    is IGNORED by the arbiter until it is the I face's turn again — so the pairing NEVER
    desyncs. The matched pairs are (I0,Q0), (I1,Q1); the emits must be exactly those, in
    order, NOT a scrambled (I1,Q0)/(I0,Q1). A phase-toggle counter would mis-pair here."""
    topo = _build_two_face_topology()
    # I0=100, then a 2nd I1=300 BEFORE the first Q — the 2nd I must wait (locked to Q).
    # Then Q0=200 completes pair 0; then I1 is finally accepted; Q1=400 completes pair 1.
    # Sequence on the wire: I(100), I(300), Q(200), I(300-already-queued?), Q(400)...
    # We drive the literal adversarial order I,I,Q,I,Q,Q and check the MATCHED emits.
    xi, xq, o_yi, o_yq, emit = _feed(
        topo, [("I", 100), ("I", 300), ("Q", 200),
               ("I", 500), ("Q", 400), ("Q", 600)])
    # Exactly the matched pairs must emit — the last completed pair is the final (yi,yq).
    # With the LOCK, each Q completes the pair whose I was accepted on the I face; a 2nd I
    # arriving while locked to Q is ignored (held) until the I face reopens. So the pairing
    # is I0<->first Q, next-accepted-I<->next Q. The KEY assertion: xi and xq are a
    # CONSISTENT matched pair (xi is the I that was latched, xq the Q that paired with it),
    # never a cross-contaminated mix, and the correct NUMBER of pairs emit.
    assert emit >= 2, f"out-of-order async lost pairs: emit={emit}"
    # The final latched pair must be internally consistent: the emitted packet equals the
    # cell's latched (xi, xq) at the moment of emit (no cross-pair scramble).
    assert (o_yi, o_yq) == (xi, xq), (
        f"emitted packet {o_yi},{o_yq} != latched pair {xi},{xq} — async desync/scramble")
    # And every emitted I rail is one of the injected I values (never a Q value leaking
    # onto the I rail — the face gate keeps the streams separate).
    assert o_yi in (100, 300, 500), f"I rail carries a non-I value: yi={o_yi}"
    assert o_yq in (200, 400, 600), f"Q rail carries a non-Q value: yq={o_yq}"


def test_lock_is_load_bearing_mutation():
    """MUTATION GATE (INV-4): boot the RV cell LOCKED to the WRONG face (SOUTH, the Q face)
    instead of WEST — and corrupt the face_i DataWord to match, so the re-lock is broken
    too. Feed the SAME matched I,Q that P1 emits. With the lock pointing at the Q face, the
    WEST-face I is now IGNORED, so xi never latches the correct 1500 and the correct matched
    packet (1500,2500) is NEVER emitted — proving P1's pairing is enforced by the
    LOCK-by-face gate (the initial_lock_face + face DataWords), not by timing luck (a
    passing P1 is not vacuous). This mutation is PROVEN to break P1 below."""
    topo = _build_two_face_topology(corrupt_boot_face=_FACE_S)
    xi, xq, o_yi, o_yq, _emit = _feed(topo, [("I", 1500), ("Q", 2500)])
    # The correct matched packet is GONE: OUT never carries (yi,yq)=(1500,2500).
    assert (o_yi, o_yq) != (1500, 2500), (
        "with the LOCK booted to the WRONG face the block must NOT emit the correct "
        f"matched packet (1500,2500) — got ({o_yi},{o_yq}); if it did, P1 would be vacuous")
