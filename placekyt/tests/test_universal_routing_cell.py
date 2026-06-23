"""§1.4 UNIVERSAL routing-cell program (Reading B): every routing cell — including
a PLAIN TRANSIT spine cell — carries the uniform transmit(+relay) program, so the
fabric is made of generic, dynamically-repurposable control cells (§4.2).

The load-bearing correctness property (the builds≠computes hazard): programming a
transit cell must NOT change pass-through — a HOP_CNT<31 word transiting a now-
programmed cell must behave IDENTICALLY to a face-only cell (forwarded on fwd_face,
never firing an entry). This is guaranteed by the hardware (``routing.rs::
route_packet`` decides execute-vs-forward purely on HOP_CNT, never reading memory),
and proven here against simkyt in a minimal 2-cell forward case.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from engine.build import (  # noqa: E402
    _UNIV_BUS_FACE_REG, _UNIV_RELAY_BURST_REG, _universal_routing_program)

from tests.conftest import CHIP_YAML as CT_PATH  # noqa: E402
_WRITE, _JUMP = 0x6000, 0x7000

pytestmark = pytest.mark.skipif(not CT_PATH.exists(), reason="chip yaml absent")


def test_universal_program_assembles_two_distinct_entries():
    """The universal program carries a `transmit` and a `relay` entry at DISTINCT
    addresses, plus bus_face data (R1) and the relay's own burst reg (R2)."""
    for bf in range(4):
        entries, mem = _universal_routing_program(bf)
        assert "transmit" in entries and "relay" in entries
        assert entries["transmit"] != entries["relay"], \
            "transmit and relay must be distinct entry addresses"
        # bus_face data word at R1 carries the face code.
        assert mem.get(_UNIV_BUS_FACE_REG, 0) == bf
        # The relay re-launch reg (R2) exists as state (initialised 0).
        assert _UNIV_RELAY_BURST_REG in mem
        # The program forwards: it has WRITE + JUMP relay instructions.
        assert any((w & 0xF000) == _WRITE for w in mem.values())
        assert any((w & 0xF000) == _JUMP for w in mem.values())
        # Every JUMP re-targets the transmit entry (re-launch the next cell's
        # transmit-through), not some stray address.
        for w in mem.values():
            if (w & 0xF000) == _JUMP:
                assert (w & 0x1F) == (entries["transmit"] & 0x1F)


def test_programmed_transit_passes_hop_lt_31_word_unconsumed():
    """A PROGRAMMED transit cell must pass a HOP_CNT<31 word through UNCONSUMED —
    identical to a face-only cell. Build a 2-cell forward case in simkyt: cell
    (0,0) is the universal transit program (facing EAST), cell (1,0) is a capture
    cell. A word addressed @2 (HOP_CNT=29 at inject → 30 at (0,0): transit → 31 at
    (1,0): land) must transit (0,0) untouched and land in (1,0).R5. Compared to the
    face-only baseline, the result is byte-identical AND (0,0) never executes (its
    R5 stays 0 — the transiting word never fired its program)."""
    import simkyt
    from gr_kyttar.placement.cell_map import CellMap, CellConfig, Face
    from gr_kyttar.bitstream.generator import BitstreamGenerator

    VAL = 0x1234

    def run(programmed_transit: bool):
        chip = simkyt.Chip.from_yaml(str(CT_PATH))
        cm = CellMap(width=12, height=12)
        c00 = CellConfig(fwd_face=Face.EAST, block_name="_routing")
        if programmed_transit:
            entries, mem = _universal_routing_program(1)  # EAST
            for a, w in mem.items():
                c00.memory[a] = w
            c00.entry_addr = entries["transmit"]
        cm.set_cell(0, 0, c00)
        cap = simkyt.Program.from_source(
            "cap", "land:\n    MOVE R5, R0\n    HALT\n", 30)
        c10 = CellConfig(fwd_face=Face.EAST, block_name="cap")
        for i, w in enumerate(cap.get_words()):
            if w:
                c10.memory[i] = w
        c10.entry_addr = 30
        cm.set_cell(1, 0, c10)
        gen = BitstreamGenerator(str(CT_PATH))
        gen.load_cell_map(cm)
        bs = gen.generate()
        chip.load_bitstream_physical(list(bs.words))
        chip.set_port_entry_address("x16_in", 30)
        chip.inject_data_physical([VAL], target_hop_cnt=29, target_addr=5)
        chip.run(max_events=2000)
        chip.inject_jump_physical(target_hop_cnt=29, entry_addr=30)
        chip.run(max_events=4000)
        return (chip.read_cell_memory(1, 5),   # (1,0).R5 — landed value
                chip.read_cell_memory(0, 5))   # (0,0).R5 — must be untouched

    face_only = run(False)
    programmed = run(True)
    assert face_only[0] == VAL, "face-only: word must land at (1,0)"
    assert programmed[0] == VAL, \
        "programmed transit: word must STILL transit (0,0) and land at (1,0)"
    assert programmed == face_only, \
        "programming a transit cell must not change pass-through behaviour"
    assert programmed[1] == 0, \
        "programmed transit cell must NOT consume/execute on the transiting word"
