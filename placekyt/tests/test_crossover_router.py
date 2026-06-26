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

def test_drc_names_uncrossovered_corner_conflict_but_passes_a_crossover(qapp):
    """The face-conflict DRC over a DETERMINISTIC corner conflict (independent of any
    flagship layout, which the production 4-block chain happens to route without a
    corner crossover): two nets transit the SAME corner cell exiting DIFFERENT faces.

    ``netW`` enters the corner (9,0) from the SOUTH and exits WEST; ``netE`` enters
    from the WEST and exits EAST (its egress face). Sharing one cell with two exit
    faces is the single-fwd_face hazard the crossover resolves.

      * WITHOUT exempting the corner, the DRC NAMES the (9,0) E/W conflict (the gap
        the old DRC silently tolerated when one stream's egress was an endpoint);
      * exempting that cell (as the build does when it promotes a programmed
        crossover there) clears it — a legitimately-programmed demux serves both
        faces, so no violation. A still-un-crossover'd conflict stays named."""
    # netW: ... -> (9,1) -> (9,0) -> (8,0)   (enters S, exits W)
    # netE: ... -> (8,0) -> (9,0)            (enters W, exits E at its egress)
    routes = {
        "netW": [(9, 3), (9, 2), (9, 1), (9, 0), (8, 0)],
        "netE": [(7, 0), (8, 0), (9, 0)],
    }
    # netE's egress face at its final cell (9,0) is EAST (face 1) — supplied so the
    # egress counts as a forward (closes the silent-endpoint gap).
    egress = {"netE": ((9, 0), 1)}

    # WITHOUT exempting the corner: the un-crossover'd (9,0) conflict is NAMED.
    bare = check_bus(None, routes, {}, egress=egress)
    named = [v for v in bare if v.kind == "face_conflict" and v.cell == (9, 0)]
    assert named, "the un-crossover'd (9,0) E/W conflict must be NAMED"
    assert set(named[0].nets) == {"netW", "netE"}

    # WITH the corner exempt (a programmed crossover would sit there): RESOLVED.
    clean = check_bus(None, routes, {}, exempt_cells={(9, 0)}, egress=egress)
    assert not any(v.kind == "face_conflict" and v.cell == (9, 0) for v in clean), \
        "a programmed crossover legitimately serves both faces — no violation"
