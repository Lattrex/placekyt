"""Build the GardnerTimingRecovery block through the real placeKYT pipeline.

The 3-cell timing-recovery loop (resampler -> ted -> loop_filter) with the period
FEEDBACK (loop_filter -> resampler, via the row-below transit return path). Proves
the block is catalog-discovered, places, and routes to a valid bitstream that
loads into simkyt. The on-chip lock/recovery is verified in the verification
harness (proto_gardner_chip.py: bit-exact + BER=0).
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
    # 4 cells: resampler, ted, loop_filter + the period_relay (the PI loop filter /
    # feedback relay that breaks the feedback closed through a data path — see the block).
    assert spec.default_cell_count == 4
    # The old broken GardnerTimingRecoveryBlock must be gone.
    assert catalog.get("GardnerTimingRecoveryBlock", "lattrex.official") is None


def test_places_with_programmed_relay_feedback(qapp, catalog):
    ctrl = _place(catalog, 1, 1)
    blk = ctrl.project.blocks[-1]
    assert blk.placement is not None
    # 4 PROGRAMMED cells (resampler, ted, loop_filter, period_relay) and NO face-only
    # transit. The period feedback returns through the PROGRAMMED ``period_relay``
    # (loop_filter WEST -> relay -> resampler NORTH), NOT a face-only transit: a
    # transit cell would chain-ack through to the resampler and re-create the
    # feedback ring closed through a data path. The relay is a real consumer, so it
    # gives the ring slack (and also runs the PI loop filter).
    assert len(blk.placement.cells) == 4
    assert any(c.cell_id == "period_relay" for c in blk.placement.cells)
    transit = getattr(blk.placement, "transit", None) or \
        getattr(blk.placement, "transit_cells", [])
    assert len(transit) == 0, f"expected 0 face-only transit cells, got {len(transit)}"


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


def test_output_egress_preserves_period_feedback(qapp, catalog, chip_type):
    """Gardner's loop_filter cell emits BOTH its `out` (forward, to the slicer/bus)
    AND its `e_fb` feedback (to the period_relay PI filter, which writes the corrected
    period back into the resampler) — they share the cell's single output face via an
    in-program FACE flip. When the `out` net is BROKER-routed to a downstream block,
    the build must patch ONLY the output WRITE/JUMP (the LAST in the cell), leaving the
    `e_fb` feedback WRITE intact. Patching EVERY WRITE in the cell would clobber the
    feedback (@N → @0) and break the timing loop. ``output_cell_id() == "loop_filter"``
    makes ``_output_cell_carries_handoffs`` return True (patch the last write alone).
    This pins that BOTH feedback legs survive a brokered output route: (a) the
    loop_filter's `e_fb` WRITE to the relay, and (b) the relay's `period` WRITE back to
    the resampler — each with a non-trivial (multi-hop) feedback hop."""
    import simkyt
    from engine.build import _output_cell_carries_handoffs
    from commands import SetConnectionRouteCommand
    from model.connection import BlockEndpoint

    # output_cell_id is the loop_filter (it carries the `e_fb` feedback handoff AND
    # the `out` egress), so _output_cell_carries_handoffs must return True.
    gb = catalog.instantiate("GardnerTimingRecovery", "g",
                             None, library="lattrex.official")
    assert gb.output_cell_id() == "loop_filter"
    assert _output_cell_carries_handoffs(gb), \
        "Gardner's loop_filter carries the e_fb handoff — must be detected"
    # Resolve the relay's e_in register (the loop_filter's e_fb dest) and the
    # resampler's period register (the relay's pout dest) from the block programs.
    cps = gb.build_cell_programs()
    relay_e_in = next(p.register for p in cps["period_relay"].inputs
                      if p.name == "e_in")
    from gr_kyttar.placement.resolver import CellProgramResolver
    rs_period = CellProgramResolver()._allocate_state(
        cps["resampler"].state, list(range(3, 31)))["period"]

    # Place Gardner + a downstream sink; broker-route gardner.out → sink and confirm
    # both feedback WRITEs survive in the built programs.
    ctrl = AppController(catalog=catalog)
    ctrl.new_project("Gardner", "kyttar_10x12")
    ctrl.place_block("GardnerTimingRecovery", 0, 3, 0, library="lattrex.official")
    gname = ctrl.project.blocks[-1].name
    ctrl.place_block("BPSKSlicerBlock", 0, 5, 2, library="lattrex.official")
    sname = ctrl.project.blocks[-1].name
    ctrl.add_logical_connection(BlockEndpoint(block=gname, port="out"),
                                BlockEndpoint(block=sname, port="llr"), name="net4")
    # Route includes the source exit cell (5,0) then the broker (5,1), a free cell
    # abutting the slicer at (5,2) — the bus/broker convention.
    SetConnectionRouteCommand(ctrl.project, "net4", [(5, 0), (5, 1)]).execute()
    res = BuildEngine(catalog, str(CT_PATH)).build(
        ctrl.project, {"kyttar_10x12": chip_type})
    assert res.ok, [str(e) for e in res.errors]

    g = ctrl.project.block(gname)
    lf = g.placement.cell("loop_filter")
    mem = res.chips[0].cells[(lf.x, lf.y)]["memory"]
    # (a) The loop_filter's `e_fb` WRITE targets the relay's e_in register and must
    # keep a NON-trivial (multi-hop) feedback hop, NOT be collapsed to @0 by a
    # patch-every-WRITE of the brokered output.
    fb = [(a, w) for a, w in enumerate(mem)
          if (w & 0xF000) == 0x6000 and (w & 0x1F) == relay_e_in]
    assert fb, "e_fb WRITE (dest=relay e_in reg) must still be present"
    _addr, word = fb[0]
    hop_cnt = (word >> 5) & 0x1F        # @N = 31 - hop_cnt
    assert hop_cnt < 31, \
        f"e_fb feedback hop was clobbered to @0 (hop_cnt={hop_cnt})"
    # (b) The relay's `period` WRITE back to the resampler must also survive @>0.
    rel = g.placement.cell("period_relay")
    rmem = res.chips[0].cells[(rel.x, rel.y)]["memory"]
    pfb = [(a, w) for a, w in enumerate(rmem)
           if (w & 0xF000) == 0x6000 and (w & 0x1F) == rs_period]
    assert pfb, "relay period WRITE (dest=resampler period reg) must be present"
    assert ((pfb[0][1] >> 5) & 0x1F) < 31, "relay period feedback hop clobbered to @0"
    # And it must still build into a loadable bitstream.
    chip = simkyt.Chip.from_yaml(str(CT_PATH))
    chip.load_bitstream_physical(res.words(0))
