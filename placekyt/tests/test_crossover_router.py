"""The §1.2 TIME-MULTIPLEXED BUS crossover (the FINAL auto-P&R piece).

Two parts:
  * the crossover ROUTER logic + build demux + DRC are unit-tested over the
    flagship routes (``engine.bus_router.crossover_plan`` /
    ``engine.bus_drc.check_bus``);
  * the emitted crossover demux PROGRAM (``build._crossover_program``) is driven
    on-chip through simkyt — two crossing streams sharing ONE cell, demuxed by
    JUMP entry, each delivered to a DIFFERENT neighbour (the COMPUTE proof).
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication  # noqa: E402

from engine.bus_drc import check_bus  # noqa: E402

from tests.conftest import CHIP_YAML as CT_PATH  # noqa: E402
from tests.conftest import EXAMPLES_DIR  # noqa: E402
GRC = EXAMPLES_DIR / "coherent_bpsk_rx.grc"
pytestmark = pytest.mark.skipif(
    not (CT_PATH.exists() and GRC.exists()), reason="chip yaml / .grc absent")


@pytest.fixture(scope="module")
def qapp():
    return QApplication.instance() or QApplication([])


# --------------------------------------------------------------------------- #
# (1) The crossover COMPUTES: two crossing streams demuxed through ONE cell.
# --------------------------------------------------------------------------- #

def test_crossover_program_demuxes_two_streams_on_chip(qapp):
    """``build._crossover_program`` is the proven CrossoverBlock demux: two streams
    enter ONE cell via DIFFERENT JUMP entries, set DIFFERENT exit faces, and each
    relays its landed burst to a DIFFERENT neighbour. Drive it on-chip and assert
    BOTH neighbours receive their OWN stream's value (the demux, not a collision)."""
    import simkyt
    from gr_kyttar.placement.cell_map import CellMap, CellConfig, Face
    from gr_kyttar.bitstream.generator import BitstreamGenerator
    from engine.build import _crossover_program

    # Crossover at (5,5): track0 -> WEST into (4,5) R7, track1 -> EAST into (6,5) R8.
    # face codes: S=0,E=1,W=2,N=3.
    tracks = [
        ("netW", 2, 1, 7, 0),   # (conn, exit_face=WEST, out_hop=1, dest=R7, entry=0)
        ("netE", 1, 1, 8, 0),   # (conn, exit_face=EAST, out_hop=1, dest=R8, entry=0)
    ]
    by_conn, memory = _crossover_program(tracks)
    assert set(by_conn) == {"netW", "netE"}
    assert by_conn["netW"] != by_conn["netE"], "the two streams need DISTINCT entries"

    cm = CellMap(width=12, height=12)
    cross = CellConfig(block_name="_crossover")
    cross.memory.update(memory)
    cross.entry_addr = min(by_conn.values())
    cm.set_cell(5, 5, cross)
    # Two sink cells that just receive (no program) — read their R7 / R8 after.
    cm.set_cell(4, 5, CellConfig(fwd_face=Face.WEST, block_name="_sinkW"))
    cm.set_cell(6, 5, CellConfig(fwd_face=Face.EAST, block_name="_sinkE"))

    gen = BitstreamGenerator(str(CT_PATH))
    gen.load_cell_map(cm)
    words = gen.generate().words

    chip = simkyt.Chip.from_yaml(str(CT_PATH))
    chip.load_bitstream_physical(list(words))
    cross_id = chip.cell_id_at(5, 5)
    sinkW = chip.cell_id_at(4, 5)
    sinkE = chip.cell_id_at(6, 5)

    # Stream W: land 0x1111 at the crossover via track0's entry -> relayed WEST to R7.
    chip.write_cell_memory(cross_id, 0, 0x1111)
    chip.inject_jump(cross_id, by_conn["netW"])
    chip.run(max_events=4000)
    # Stream E: land 0x2222 via track1's entry -> relayed EAST to R8.
    chip.write_cell_memory(cross_id, 0, 0x2222)
    chip.inject_jump(cross_id, by_conn["netE"])
    chip.run(max_events=4000)

    assert chip.read_cell_memory(sinkW, 7) == 0x1111, "WEST stream mis-delivered"
    assert chip.read_cell_memory(sinkE, 8) == 0x2222, "EAST stream mis-delivered"


# --------------------------------------------------------------------------- #
# (2) The router NAMES the crossover cells; the DRC PASSES them but FAILS an
#     un-crossover'd conflict (P3.4 — a silent corruption becomes a named one).
# --------------------------------------------------------------------------- #

def _flagship_routes(qapp):
    """Flagship routes with the slicer FORCED to the old corner (9,3).

    The tool's auto-place now applies the §5.3 ABUT-FIRST pass (it re-seats a
    single-cell bus-fed terminal NEXT to its driver, NOT at the egress corner), so the
    live flagship no longer produces the (9,0) E/W corner conflict these crossover/DRC
    unit tests pin — the placer correctly AVOIDS it. To keep exercising the crossover
    machinery against the conflict it exists to resolve, this fixture re-pins the
    slicer at the OLD stranded corner (9,3) after auto-place (overriding the abut
    pass for this one block), reproducing the corner crossover deterministically.
    """
    from engine.catalog import BlockCatalog
    from engine.io.chip_type_io import load_chip_type
    from ui.controller import AppController
    from commands import MoveBlockCommand
    ct = load_chip_type(str(CT_PATH))
    cat = BlockCatalog.from_gr_kyttar()
    ctrl = AppController(catalog=cat)
    ctrl.import_grc(str(GRC), chip_type="kyttar_10x12")
    ctrl.auto_place(0)
    # Re-pin the single-cell slicer to the old far corner (9,3) so the (9,0) corner
    # crossover the abut pass now prevents is reproduced for these machinery tests.
    sli = next(b for b in ctrl.project.blocks
               if b.placement is not None and len(b.placement.cells) == 1)
    c = sli.placement.cells[0]
    MoveBlockCommand(ctrl.project, sli.name, 9 - c.x, 3 - c.y).execute()
    ctrl.auto_route_all({"kyttar_10x12": ct}, auto_orient=False, use_bus="always")
    return ctrl, ct, cat


def test_crossover_plan_names_the_corner_conflict(qapp):
    """On the flagship, ``crossover_plan`` identifies the (9,0) corner as a crossover
    carrying BOTH net3 (Costas->Gardner, exits WEST) and net5 (slicer->x16_out,
    exits EAST) — the demux that resolves the single-fwd_face conflict."""
    from engine.bus_router import crossover_plan
    ctrl, ct, cat = _flagship_routes(qapp)
    taps = crossover_plan(ctrl.project, 0, ct, cat)
    assert (9, 0) in taps, f"expected a crossover at (9,0), got {sorted(taps)}"
    tracks = {t.conn: t.exit_face for t in taps[(9, 0)].tracks}
    assert tracks.get("net3") == 2, "net3 must exit WEST (face 2)"
    assert tracks.get("net5") == 1, "net5 must exit EAST (face 1)"


def test_drc_names_uncrossovered_conflict_but_passes_the_crossover(qapp):
    """The face-conflict DRC: with NO exempt set, the (9,0) transit/egress overlap is
    NAMED (the gap the old DRC silently tolerated, because net5's egress was an
    endpoint). Exempting the crossover cell (the build promotes it) clears it — a
    DELIBERATE un-crossover'd conflict is still named (gate #4)."""
    from engine.bus_router import crossover_plan, broker_plan
    ctrl, ct, cat = _flagship_routes(qapp)
    routes = {c.name: [(p.x, p.y) for p in c.route]
              for c in ctrl.project.connections if c.is_routed}
    # The slicer->x16_out egress face at its final cell (EAST) — supplied so the
    # egress counts as a forward (closes the silent-endpoint gap).
    egress = {"net5": ((9, 0), 1)}

    # WITHOUT exempting the crossover: the un-crossover'd (9,0) conflict is NAMED.
    bare = check_bus(None, routes, {}, egress=egress)
    named = [v for v in bare if v.kind == "face_conflict" and v.cell == (9, 0)]
    assert named, "the un-crossover'd (9,0) E/W conflict must be NAMED"
    assert set(named[0].nets) == {"net3", "net5"}

    # WITH the crossover (+ brokers) exempt: the conflict is RESOLVED, no violation.
    exempt = set(crossover_plan(ctrl.project, 0, ct, cat).keys())
    exempt |= set(broker_plan(ctrl.project, 0, ct, cat).keys())
    clean = check_bus(None, routes, {}, exempt_cells=exempt, egress=egress)
    assert not any(v.kind == "face_conflict" and v.cell == (9, 0) for v in clean), \
        "a programmed crossover legitimately serves both faces — no violation"
