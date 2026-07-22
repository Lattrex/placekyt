# SPDX-License-Identifier: GPL-3.0
"""The chip INPUT PORT cell (0,0) forking to TWO blocks that ABUT it (one EAST, one
SOUTH) must deliver each stream to the RIGHT block.

The shared-input-port DIVERT (_apply_port_diverts, INV-24): the port cell has ONE
``fwd_face``. One fan-out stream RIDES that face straight; the OTHER must LAND at the
port and be RELAYED off it. The relay used the PHYSICAL route (``_phys_pts``), which
STRIPS the trailing input-cell waypoint when the target block ABUTS the port (route ==
[port, in_cell]) — collapsing the 2-cell route to 1 cell. The divert then saw ``len(pts)
< 2`` and SKIPPED that branch, so its word rode the OTHER stream's fwd_face into the
WRONG block (the "cell 0,0 broker only gives one path; the input is interleaved with the
wrong stream" bug).

Fix: use the RAW drawn route for the divert's first-step direction, and when a fan-out
branch abuts the port (no free cell for a downstream broker) deliver @1 STRAIGHT into
that block's own input cell + entry.

This drives ONLY the stream whose branch leaves the port in the NON-fwd_face direction
(the diverting one) and asserts ITS block fires while the OTHER block does not.

Run:
    QT_QPA_PLATFORM=offscreen placekyt/.venv/bin/python -m pytest \
        placekyt/tests/test_port_divert_abutting_fanout.py -q
"""

from __future__ import annotations

import os
from collections import Counter
from pathlib import Path

import numpy as np
import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication  # noqa: E402

from engine.catalog import BlockCatalog  # noqa: E402
from engine.io.chip_type_io import load_chip_type  # noqa: E402
from engine.build import BuildEngine  # noqa: E402
from engine.registry import ChipTypeRegistry  # noqa: E402
from engine.port_config import stream_targets  # noqa: E402
from ui.controller import AppController  # noqa: E402
from model.connection import BlockEndpoint, ChipPortEndpoint  # noqa: E402

_CHIP = str(Path(__file__).resolve().parents[1] / "resources" / "chips"
            / "kyttar_10x12.yaml")
_LIB = "lattrex.official"


@pytest.mark.skipif(not os.path.exists(_CHIP), reason="chip yaml absent")
def test_port_00_fork_delivers_each_stream_to_its_block():
    import simkyt

    QApplication.instance() or QApplication([])
    key = "kyttar_10x12"
    cat = BlockCatalog.from_gr_kyttar()
    ct = load_chip_type(_CHIP)
    ctrl = AppController(catalog=cat)
    ctrl.new_project("fork00", key)

    # Two single-cell real blocks abutting the input port (0,0): a Gain EAST at (1,0)
    # and another Gain SOUTH at (0,1). The port fans out to BOTH.
    east = ctrl.place_block("GainBlock", 0, 1, 0, library=_LIB, params={"gain": 1.0})
    south = ctrl.place_block("GainBlock", 0, 0, 1, library=_LIB, params={"gain": 1.0})
    R = ctrl.add_route
    R(ChipPortEndpoint(chip=0, port="x16_in"),
      BlockEndpoint(block=east, port="sample"), [(0, 0), (1, 0)])
    R(ChipPortEndpoint(chip=0, port="x16_in"),
      BlockEndpoint(block=south, port="sample"), [(0, 0), (0, 1)])
    R(BlockEndpoint(block=east, port="out"),
      ChipPortEndpoint(chip=0, port="x16_out"), [])
    R(BlockEndpoint(block=south, port="out"),
      ChipPortEndpoint(chip=0, port="x16_out"), [])

    # Give the two fan-out branches distinct stream ids (the duplex contract).
    for conn in ctrl.project.connections:
        s = getattr(conn, "source", None)
        if s is not None and getattr(s, "port", None) == "x16_in":
            t = conn.target
            conn.stream_id = "east" if getattr(t, "block", "") == east else "south"
    ctrl.auto_route_all({key: ct}, auto_orient=False)

    bres = BuildEngine(cat, _CHIP).build(ctrl.project, {key: ct})
    assert bres.ok, getattr(bres, "errors", None)
    reg = ChipTypeRegistry()
    reg.register_file(_CHIP)
    tg = stream_targets(ctrl.project, reg, cat, 0, build_result=bres)

    east_cell = (1, 0)
    south_cell = (0, 1)

    def _drive_and_trace(stream_id):
        tinfo = tg[stream_id]
        entry, hop, a0 = tinfo["entry_addr"], tinfo["hop_count"], tinfo["data_addrs"][0]

        def _w(a):
            return (0x6 << 12) | ((hop & 0x1F) << 5) | (int(a) & 0x1F)

        def _j():
            return (0x7 << 12) | ((hop & 0x1F) << 5) | (int(entry) & 0x1F)

        chip = simkyt.Chip.from_yaml(_CHIP)
        chip.load_bitstream_physical(bres.words(0))
        chip.enable_trace()
        stream = []
        for v in (0.5, -0.3, 0.7, -0.1, 0.2, 0.9):
            q = int(np.clip(round(v * 32768), -32768, 32767)) & 0xFFFF
            stream += [_w(a0), q, _j()]
        chip.queue_words_physical("x16_in", stream)
        chip.run(max_events=100000)
        tr = chip.get_trace()

        def _exec(cell):
            lid = chip.cell_id_at(*cell)
            return Counter(t.get("kind") for t in tr
                           if t.get("cell_id") == lid).get("exec_tick", 0)
        return _exec(east_cell), _exec(south_cell)

    # Drive the EAST stream: the EAST block must fire; the SOUTH block must NOT.
    e_exec, s_exec = _drive_and_trace("east")
    assert e_exec > 0, "EAST stream did not reach its (EAST) block"
    assert s_exec == 0, (
        "EAST stream LEAKED into the SOUTH block — the port fork mis-delivered")

    # Drive the SOUTH stream: the SOUTH block must fire; the EAST block must NOT.
    e_exec, s_exec = _drive_and_trace("south")
    assert s_exec > 0, "SOUTH stream did not reach its (SOUTH) block"
    assert e_exec == 0, (
        "SOUTH stream LEAKED into the EAST block — the port fork mis-delivered")
