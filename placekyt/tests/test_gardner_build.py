# SPDX-License-Identifier: GPL-3.0-or-later
"""Build the GardnerTimingRecovery block through the real placeKYT pipeline.

The 7-cell timing-recovery loop (counter -> dline -> interp -> ted -> loop_filter,
plus a DEDICATED ``qout`` egress cell and a ``period_relay`` that closes the PI
feedback back into the counter by DIRECT ABUTMENT — the ring is a six-cycle, which
is even, so it needs no transit lane at all). These tests pin
the STRUCTURE the block depends on; the DSP itself is verified in
``verification/tests/test_gardner_timing_recovery.py`` (GR-equivalence, BER 0
on-chip) and ``test_gardner_convergence.py`` (long-burst bit-exactness).

THE STRUCTURAL PROPERTY THESE GUARD (see the 2026-08-27 lessons_log entry): the
block's EXTERNAL-EGRESS cell and the source of its INTERNAL FEEDBACK are DIFFERENT
cells. Four separate build passes each claim an exit cell's WRITE/JUMP words, and a
cell asked to be both an egress and a feedback source loses one role to the other —
which is exactly what kept the earlier 4-cell fused design from closing its loop on
silicon. ``output_cell_id() == "qout"`` (one WRITE, one JUMP, one face, no state)
and the feedback leaves ``loop_filter`` on a perpendicular face.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication  # noqa: E402

from engine.build import BuildEngine  # noqa: E402
from engine.catalog import BlockCatalog  # noqa: E402
from engine.io.chip_type_io import load_chip_type  # noqa: E402
from ui.controller import AppController  # noqa: E402

from tests.conftest import CHIP_YAML as CT_PATH  # noqa: E402
pytestmark = pytest.mark.skipif(not CT_PATH.exists(), reason="chip yaml absent")

# The 7 PROGRAMMED cells, in program-dict (== layout) order.
_PROGRAM_CELLS = ["counter", "dline", "interp", "ted", "loop_filter", "qout",
                  "period_relay"]


@pytest.fixture(scope="module")
def qapp():
    return QApplication.instance() or QApplication([])


@pytest.fixture(scope="module")
def catalog():
    return BlockCatalog.from_gr_kyttar()


@pytest.fixture(scope="module")
def chip_type():
    return load_chip_type(CT_PATH)


def _place(catalog, x=0, y=0):
    ctrl = AppController(catalog=catalog)
    ctrl.new_project("Gardner", "kyttar_10x12")
    ctrl.place_block("GardnerTimingRecovery", 0, x, y,
                     library="lattrex.official")
    return ctrl


def test_in_catalog(catalog):
    spec = catalog.get("GardnerTimingRecovery", "lattrex.official")
    assert spec is not None
    assert spec.default_cell_count == len(_PROGRAM_CELLS)
    # The old broken GardnerTimingRecoveryBlock must be gone.
    assert catalog.get("GardnerTimingRecoveryBlock", "lattrex.official") is None


def test_places_with_split_egress_and_feedback_cells(qapp, catalog):
    """THE STRUCTURAL GATE. The block must place its 7 programmed cells plus the
    face-only feedback return lane, and — the point of the whole design — the
    external-egress cell (``qout``) must NOT be the cell that sources the internal
    feedback (``loop_filter`` -> ``period_relay``). Fusing those two roles into one
    cell is what prevented the earlier design's loop from closing."""
    ctrl = _place(catalog, 1, 1)
    blk = ctrl.project.blocks[-1]
    assert blk.placement is not None
    ids = [c.cell_id for c in blk.placement.cells
           if not str(c.cell_id).startswith("transit_")]
    assert ids == _PROGRAM_CELLS, f"unexpected cell set/order: {ids}"

    gb = catalog.instantiate("GardnerTimingRecovery", "g", None,
                             library="lattrex.official")
    # The declared egress cell is the DEDICATED one...
    assert gb.output_cell_id() == "qout"
    # ...and it is neither the source nor the destination of any internal edge
    # other than receiving the recovered symbol and (complex) its Q partner.
    conns = gb.internal_connections()
    qout_srcs = [c for c in conns if c[0] == "qout"]
    assert not qout_srcs, f"qout must source NO internal edge, got {qout_srcs}"
    # The feedback source is loop_filter (forward, to period_relay) and the single
    # BACKWARD edge is period_relay -> counter. qout is in neither.
    order = list(gb.build_cell_programs().keys())
    idx = {cid: i for i, cid in enumerate(order)}
    backward = [c for c in conns if idx[c[2]] < idx[c[0]]]
    assert backward == [("period_relay", "pout", "counter", "v")], (
        f"expected exactly ONE backward internal edge, got {backward}")

    # The real fold needs NO transit cells: the ring
    # counter -> dline -> interp -> ted -> loop_filter -> period_relay -> counter
    # is a SIX-cycle, which is EVEN, so it closes by abutment on a bipartite grid.
    # Assert that, because transits are not free — a transit's authored face is
    # NOT safe where an external corridor crosses it, and every one added is a
    # cell a dense design does not get to use.
    transit = getattr(blk.placement, "transit_cells", []) or []
    assert not transit, (
        f"the real fold should need no transits, got {len(transit)}: the ring is "
        f"an even cycle and closes by abutment")
    # And the relay really does abut the landing cell (that is what makes it so).
    pos = {c.cell_id: (c.x, c.y) for c in blk.placement.cells}
    (cx, cy), (rx, ry) = pos["counter"], pos["period_relay"]
    assert abs(cx - rx) + abs(cy - ry) == 1, (
        f"period_relay {pos['period_relay']} must ABUT counter {pos['counter']}")


@pytest.mark.parametrize("complex_mode", [False, True])
def test_faces_match_the_layout(catalog, complex_mode):
    """INV-37: a baked ``is_face=True`` constant PINS the fold, so the block's
    three face constants must be DERIVED from ``default_layout``, not chosen
    independently. Re-derive them from the geometry and compare:

      * ``_FACE_OUT``  = direction loop_filter -> qout
      * ``_FACE_FB``   = direction loop_filter -> period_relay
      * ``_FACE_LOCK`` = the face the feedback ENTERS counter on

    ``_FACE_LOCK`` is the one that bites. It is a DIFFERENT face from
    ``_FACE_FB`` — the counter's arbiter LOCK gates every face except the one the
    feedback arrives on, so naming the wrong one leaves the lock permanently
    engaged and the block emits exactly ONE symbol and goes quiescent. The two
    happened to coincide in an earlier fold, which is precisely why this needs a
    test rather than a convention."""
    gb = catalog.instantiate("GardnerTimingRecovery", "g",
                             {"complex": complex_mode},
                             library="lattrex.official")
    lay = gb.default_layout()
    code = {(0, 1): 0, (1, 0): 1, (-1, 0): 2, (0, -1): 3}   # S, E, W, N

    def face(a, b):
        (ax, ay, _), (bx, by, _) = lay[a], lay[b]
        d = (bx - ax, by - ay)
        assert d in code, f"{a} -> {b} is not a 1-hop abutment ({d})"
        return code[d]

    assert gb._FACE_OUT == face("loop_filter", "qout"), "_FACE_OUT != layout"
    assert gb._FACE_FB == face("loop_filter", "period_relay"), "_FACE_FB != layout"
    # The feedback's LAST hop into counter: the relay itself if it abuts, else
    # the final transit of the return lane.
    (cx, cy, _) = lay["counter"]
    src = None
    for cid, (x, y, _f) in lay.items():
        if cid == "counter":
            continue
        if abs(x - cx) + abs(y - cy) == 1 and (
                cid == "period_relay" or cid.startswith("transit_")):
            src = (x, y)
            break
    assert src is not None, "nothing abuts counter to deliver the feedback"
    assert gb._FACE_LOCK == code[(src[0] - cx, src[1] - cy)], (
        "_FACE_LOCK must name the face the feedback ENTERS counter on; a "
        "mismatch leaves the arbiter LOCK engaged forever")


def test_builds_to_bitstream(qapp, catalog, chip_type):
    ctrl = _place(catalog, 1, 1)
    res = BuildEngine(catalog, str(CT_PATH)).build(
        ctrl.project, {"kyttar_10x12": chip_type})
    assert res.ok, [str(e) for e in res.errors]
    assert len(res.words(0)) > 0


def test_bitstream_loads_into_simkyt(qapp, catalog, chip_type):
    import simkyt

    ctrl = _place(catalog, 1, 1)
    res = BuildEngine(catalog, str(CT_PATH)).build(
        ctrl.project, {"kyttar_10x12": chip_type})
    assert res.ok, [str(e) for e in res.errors]
    chip = simkyt.Chip.from_yaml(str(CT_PATH))
    chip.load_bitstream_physical(res.words(0))


def test_output_route_preserves_period_feedback(qapp, catalog, chip_type):
    """A BROKER-ROUTED output must not disturb the PI feedback.

    ``_apply_routes`` rewrites every WRITE in a ROUTED exit cell to the output
    corridor. Because the exit cell here is the dedicated ``qout`` — which owns
    exactly one WRITE and no feedback — that rewrite has nothing of the loop's to
    clobber. This pins BOTH surviving feedback legs with a non-trivial (multi-hop)
    hop after a routed egress: (a) ``loop_filter``'s ``vf`` WRITE into the relay,
    and (b) the relay's ``pout`` WRITE back into ``counter.v``.

    This is the regression the earlier fused design could not pass: with the egress
    and the feedback source on ONE cell, satisfying the output patch broke the
    feedback WRITE and vice versa."""
    import simkyt
    from commands import SetConnectionRouteCommand
    from model.connection import BlockEndpoint
    from gr_kyttar.placement.resolver import CellProgramResolver

    gb = catalog.instantiate("GardnerTimingRecovery", "g", None,
                             library="lattrex.official")
    cps = gb.build_cell_programs()
    relay_v_in = next(p.register for p in cps["period_relay"].inputs
                      if p.name == "v_in")
    # The relay's pout targets the counter's ``v`` STATE register; resolve it with
    # the same authoritative API the build uses (a hand-rolled gap scan predicts
    # the wrong register — the build reserves low registers for I/O).
    counter_v = CellProgramResolver().compute_state_registers(
        cps["counter"])["v"]

    ctrl = AppController(catalog=catalog)
    ctrl.new_project("Gardner", "kyttar_10x12")
    ctrl.place_block("GardnerTimingRecovery", 0, 1, 0,
                     library="lattrex.official")
    gname = ctrl.project.blocks[-1].name
    ctrl.place_block("BPSKSlicerBlock", 0, 7, 2, library="lattrex.official")
    sname = ctrl.project.blocks[-1].name
    ctrl.add_logical_connection(BlockEndpoint(block=gname, port="out"),
                                BlockEndpoint(block=sname, port="llr"),
                                name="net4")
    g = ctrl.project.block(gname)
    qo = g.placement.cell("qout")
    SetConnectionRouteCommand(
        ctrl.project, "net4",
        [(qo.x, qo.y), (qo.x, qo.y + 1), (qo.x + 1, qo.y + 1)]).execute()
    res = BuildEngine(catalog, str(CT_PATH)).build(
        ctrl.project, {"kyttar_10x12": chip_type})
    assert res.ok, [str(e) for e in res.errors]

    # (a) loop_filter's `vf` WRITE into the relay survives with a real hop.
    lf = g.placement.cell("loop_filter")
    mem = res.chips[0].cells[(lf.x, lf.y)]["memory"]
    fb = [w for w in mem if (w & 0xF000) == 0x6000 and (w & 0x1F) == relay_v_in]
    assert fb, "loop_filter's vf WRITE (dest = relay v_in) must still be present"
    assert ((fb[0] >> 5) & 0x1F) < 31, "vf feedback hop was clobbered to @0"

    # (b) the relay's `pout` WRITE back into counter.v survives with a real hop.
    rel = g.placement.cell("period_relay")
    rmem = res.chips[0].cells[(rel.x, rel.y)]["memory"]
    pfb = [w for w in rmem
           if (w & 0xF000) == 0x6000 and (w & 0x1F) == counter_v]
    assert pfb, "relay pout WRITE (dest = counter.v reg) must be present"
    assert ((pfb[0] >> 5) & 0x1F) < 31, "relay pout feedback hop clobbered to @0"

    chip = simkyt.Chip.from_yaml(str(CT_PATH))
    chip.load_bitstream_physical(res.words(0))


def test_feedback_closes_under_rotation(qapp, catalog, chip_type):
    """The feedback must resolve in EVERY D4 orientation (INV-23), not just at
    identity. The build traces the relay -> counter return along the AUTHORED
    transit faces; if a rotation puts a transit where an external corridor crosses
    it, the route pass (which runs FIRST) overwrites the transit's face, the trace
    dead-ends and the loop silently never closes — the block still builds, still
    routes, and emits at the right rate while never adapting. So assert the relay's
    pout WRITE carries a real multi-hop feedback in all 8 orientations."""
    from commands import OrientBlockCommand
    from gr_kyttar.placement.resolver import CellProgramResolver

    gb = catalog.instantiate("GardnerTimingRecovery", "g", None,
                             library="lattrex.official")
    counter_v = CellProgramResolver().compute_state_registers(
        gb.build_cell_programs()["counter"])["v"]

    for keys in ([], ["cw"], ["cw", "cw"], ["cw", "cw", "cw"],
                 ["mirror_h"], ["mirror_h", "cw"], ["mirror_h", "cw", "cw"],
                 ["mirror_h", "cw", "cw", "cw"]):
        ctrl = _place(catalog, 2, 2)
        name = ctrl.project.blocks[-1].name
        for k in keys:
            OrientBlockCommand(ctrl.project, name, k).execute()
        res = BuildEngine(catalog, str(CT_PATH)).build(
            ctrl.project, {"kyttar_10x12": chip_type})
        assert res.ok, f"{keys}: {[str(e) for e in res.errors]}"
        rel = ctrl.project.block(name).placement.cell("period_relay")
        rmem = res.chips[0].cells[(rel.x, rel.y)]["memory"]
        pfb = [w for w in rmem
               if (w & 0xF000) == 0x6000 and (w & 0x1F) == counter_v]
        assert pfb, f"{keys}: relay pout WRITE into counter.v missing"
        assert ((pfb[0] >> 5) & 0x1F) < 31, (
            f"{keys}: relay pout hop is @0 — the feedback trace dead-ended, so "
            f"the loop would never close in this orientation")


def test_build_flags_stray_emission_into_empty_cell(qapp, catalog, chip_type):
    """The stray-emission DRC (P3.4): a WRITE/JUMP that lands on an EMPTY/unowned
    cell is a NAMED build error (it would stray-execute on the universal forwarding
    program). Forge the bug — point the loop_filter's ``face_out`` at an empty
    direction — and assert the check flags the dead cell. (The real build does not
    produce this; the loop_filter's face words follow its two real neighbours.)"""
    from engine.bus_drc import (check_stray_emissions, owned_cells,
                                _FWD_DELTA)
    from gr_kyttar.placement.resolver import CellProgramResolver

    gb = catalog.instantiate("GardnerTimingRecovery", "g", None,
                             library="lattrex.official")
    face_out_addr = next(
        d.address for d in gb.build_cell_programs()["loop_filter"].data
        if d.name == "face_out")
    assert CellProgramResolver()  # resolver import is part of the contract

    ctrl = _place(catalog, 3, 3)
    res = BuildEngine(catalog, str(CT_PATH)).build(
        ctrl.project, {"kyttar_10x12": chip_type})
    assert res.ok, [str(e) for e in res.errors]
    g = ctrl.project.blocks[-1]
    own = owned_cells(ctrl.project, 0)

    # Forge on whichever cell of the block HAS a free neighbour to forge toward.
    # In the compact 3x3 fold the loop_filter is interior (all four neighbours are
    # its own block), so pin the cell by property rather than by name.
    lf = forged = None
    for c in g.placement.cells:
        cand = next(
            (fc for fc, (dx, dy) in _FWD_DELTA.items()
             if 0 <= c.x + dx < chip_type.width
             and 0 <= c.y + dy < chip_type.height
             and (c.x + dx, c.y + dy) not in own),
            None)
        if cand is not None:
            lf, forged = c, cand
            break
    assert forged is not None, "expected an empty neighbour to forge toward"
    cells = {k: {"memory": list(v["memory"]), "face": v.get("face")}
             for k, v in res.chips[0].cells.items()}
    # Any cell's fwd_face word will do for the DRC's purposes; use the
    # loop_filter's face_out slot when we forged on the loop_filter, else the
    # cell's face directly.
    if lf.cell_id == "loop_filter":
        cells[(lf.x, lf.y)]["memory"][face_out_addr] = forged
    else:
        cells[(lf.x, lf.y)]["face"] = forged
    viols = check_stray_emissions(cells, own, chip_type.width, chip_type.height)
    stray_cells = {v.cell for v in viols}
    nb = (lf.x + _FWD_DELTA[forged][0], lf.y + _FWD_DELTA[forged][1])
    assert nb in stray_cells, \
        f"stray emission into empty cell {nb} must be NAMED, got {sorted(stray_cells)}"
    assert all(v.kind == "stray_emission" for v in viols)
