"""Direct abutment is a valid connection (no filler routing cell required).

User report: a packed layout where blocks are placed edge-to-edge and connected
WITHOUT drawing any route (the source's output cell physically touches the
target's input cell) produced NO output — you had to insert at least one routing
cell between every pair of blocks. That's wrong: zero cells between two abutting
blocks is a legitimate @1 handoff.

Root cause: ``Connection.is_routed`` is False for an empty route, so the build
(`_apply_routes`) skipped the connection entirely — the source block's exit
face/hop was never pointed at the target, so nothing was delivered.

Fix: ``bus_router.abutment_pts`` synthesises the 2-cell path
``[source_output_cell, target_input_cell]`` for an unrouted connection whose
endpoints are orthogonally adjacent, and ``_apply_routes`` now processes it. This
test asserts the resulting WIRING (harness-independent): the source's exit cell
faces the target and its WRITE/JUMP hand off @1 into the target's entry/input.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import sys

import simkyt  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from engine.build import BuildEngine  # noqa: E402
from engine.catalog import BlockCatalog  # noqa: E402
from engine.io.chip_type_io import load_chip_type  # noqa: E402
from model.connection import BlockEndpoint  # noqa: E402
from ui.controller import AppController  # noqa: E402
from engine.bus_router import abutment_pts  # noqa: E402

from tests.conftest import CHIP_YAML as CT_PATH  # noqa: E402
pytestmark = pytest.mark.skipif(not CT_PATH.exists(), reason="chip yaml absent")


@pytest.fixture(scope="module")
def qapp():
    return QApplication.instance() or QApplication([])


@pytest.fixture(scope="module")
def catalog(qapp):
    return BlockCatalog.from_gr_kyttar()


@pytest.fixture(scope="module")
def chip_type():
    return load_chip_type(str(CT_PATH))


def _decode(word):
    return simkyt.Program.from_words("d", [word]).disassemble()


def test_abutment_pts_synthesises_adjacent_path(qapp, catalog):
    """An unrouted block→block connection whose output/input cells abut yields the
    2-cell physical path; a non-adjacent one yields None (stays unrouted)."""
    ct = load_chip_type(str(CT_PATH))
    ports = {p.name: (p.cell_x, p.cell_y, None) for p in ct.ports}
    ctrl = AppController(catalog=catalog)
    ctrl.new_project("ab", "kyttar_10x12")
    lib = "lattrex.official"
    g1 = ctrl.place_block("GainBlock", 0, 0, 0, library=lib,
                          params={"gain": 1.0, "gain_range": 15})
    g2 = ctrl.place_block("GainBlock", 0, 1, 0, library=lib,    # abuts g1 to the east
                          params={"gain": 1.0, "gain_range": 15})
    far = ctrl.place_block("GainBlock", 0, 5, 0, library=lib,   # NOT adjacent
                           params={"gain": 1.0, "gain_range": 15})
    c_ab = ctrl.add_route(BlockEndpoint(block=g1, port="out"),
                          BlockEndpoint(block=g2, port="sample"), [])
    c_far = ctrl.add_route(BlockEndpoint(block=g2, port="out"),
                           BlockEndpoint(block=far, port="sample"), [])
    prj = ctrl.project
    ab = abutment_pts(prj, prj.connection(c_ab), catalog, ports)
    assert ab == [(0, 0), (1, 0)], ab           # adjacent → 2-cell handoff
    assert abutment_pts(prj, prj.connection(c_far), catalog, ports) is None


def test_abutment_wires_source_exit_into_target(qapp, catalog, chip_type):
    """Building an abutment-only g1→g2 connection points g1's exit @1 EAST into
    g2's entry — the handoff the empty route used to drop on the floor."""
    ctrl = AppController(catalog=catalog)
    ctrl.new_project("ab2", "kyttar_10x12")
    lib = "lattrex.official"
    g1 = ctrl.place_block("GainBlock", 0, 0, 0, library=lib,
                          params={"gain": 1.0, "gain_range": 15})
    ctrl.place_block("GainBlock", 0, 1, 0, library=lib,
                     params={"gain": 1.0, "gain_range": 15})
    ctrl.add_route(BlockEndpoint(block=g1, port="out"),
                   BlockEndpoint(block="gain_1", port="sample"), [])
    res = BuildEngine(catalog, str(CT_PATH)).build(
        ctrl.project, {"kyttar_10x12": chip_type})
    assert res.ok, [str(e) for e in res.errors]
    chip = simkyt.Chip.from_yaml(str(CT_PATH))
    chip.load_bitstream_physical(res.words(0))
    g2_entry, _ = catalog.resolved_io("GainBlock")
    # g1 is cell (0,0) = cid 0. Find its WRITE + JUMP exit words.
    writes, jumps = [], []
    for a in range(32):
        w = chip.read_cell_memory(0, a)
        if not w:
            continue
        try:
            d = _decode(w)
        except Exception:  # noqa: BLE001
            continue
        if "Write" in d:
            writes.append(d)
        if "Jump" in d:
            jumps.append(d)
    assert any("hop_cnt: 30" in d for d in writes), f"no @1 WRITE: {writes}"
    # JUMP hands off @1 (hop_cnt 30) to g2's entry.
    assert any(f"hop_cnt: 30" in d and f"dest: {g2_entry}" in d for d in jumps), \
        f"no @1 JUMP to g2 entry {g2_entry}: {jumps}"


if __name__ == "__main__":
    app = QApplication.instance() or QApplication([])
    cat = BlockCatalog.from_gr_kyttar()
    ct = load_chip_type(str(CT_PATH))
    test_abutment_pts_synthesises_adjacent_path(app, cat)
    print("abutment_pts synth: PASS")
    test_abutment_wires_source_exit_into_target(app, cat, ct)
    print("abutment wires source exit into target: PASS")
