# SPDX-License-Identifier: GPL-3.0-or-later
"""VaricodeEncoderBlock — PSK31 Varicode encoder, gated BIT-EXACT vs a Python golden.

There is NO stock GNU Radio Varicode block (the manifest ``grc_block`` is ''), so the
golden reference is a pure-Python model of the published G3PLX PSK31 Varicode table
(:mod:`varicode_golden`, transcribed from fldigi ``pskvaricode.cxx`` via pydigi and
cross-checked against the ARRL PSK31 spec + Wikipedia).

Substrate status: **QUARANTINED**. Two independent hard walls (see the block docstring
and ``lessons_log.md``): the 128-entry LUT overflows the LOAD 5-bit / 32-word single
cell (~6x; MapBB proved MAX_TABLE=21), AND the per-character emit is a data-dependent
burst of ``len(code)+2`` (3..12) words that no shipped compile-time-unrolled emit can
express. This suite therefore gates the GOLDEN + the block's ``process_reference``
bit-exact, PROVES the mutation gate can see a corruption (INV-4), and asserts the build
wall (the quarantine is a live, tested limit — not a claim). When a data-dependent
burst-emit primitive + external table SRAM exist, the DUT-on-chip gate slots in here.
"""
from __future__ import annotations

import os
import random
import sys
from pathlib import Path

import numpy as np
import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

_WT = Path(__file__).resolve().parents[2]
for _p in (str(_WT / "runtime" / "python"), str(_WT / "placekyt"),
           str(Path(__file__).resolve().parent)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from varicode_golden import GOLDEN_VARICODE, golden_bits, golden_bits_str  # noqa: E402
from gr_kyttar.placement import VaricodeEncoderBlock  # noqa: E402


# --------------------------------------------------------------------------- golden
def test_golden_table_is_spec_consistent():
    """The golden table obeys the PSK31 Varicode structural spec."""
    assert len(GOLDEN_VARICODE) == 128
    assert len(set(GOLDEN_VARICODE)) == 128, "codes must be unique"
    for i, c in enumerate(GOLDEN_VARICODE):
        assert c and c[0] == "1" and c[-1] == "1", f"code {i} not 1-bounded: {c}"
        assert "00" not in c, f"code {i} has '00': {c}"
        assert max(len(x) for x in GOLDEN_VARICODE) <= 10


def test_golden_known_entries():
    """Spot-check well-known published codes (space,e,t,a,o,i,n,s,LF,CR + the
    entries the ARRL WebFetch corrupted: ',`,Y,Z,p,I)."""
    known = {
        ord(" "): "1", ord("e"): "11", ord("t"): "101", ord("a"): "1011",
        ord("o"): "111", ord("i"): "1101", ord("n"): "1111", ord("s"): "10111",
        10: "11101", 13: "11111",
        ord("'"): "101111111", ord("`"): "1011011111",
        ord("Y"): "101111011", ord("Z"): "1010101101",
        ord("p"): "111111", ord("I"): "1111111",
    }
    for code_pt, bits in known.items():
        assert GOLDEN_VARICODE[code_pt] == bits, (chr(code_pt), GOLDEN_VARICODE[code_pt], bits)


# ------------------------------------------------------------ block ref vs golden
def _dut(text: str):
    """The block's process_reference for an ASCII string, as a '0'/'1' string."""
    b = VaricodeEncoderBlock("v")
    ref = b.process_reference([ord(c) for c in text])
    return "".join(str(int(x)) for x in ref)


def test_block_reference_matches_golden_edge():
    # space=1, e=11, t=101 — each followed by the '00' gap.
    assert _dut(" ") == "1" + "00"
    assert _dut("e") == "11" + "00"
    assert _dut("t") == "101" + "00"
    assert _dut("et") == "11" + "00" + "101" + "00"


def test_block_reference_matches_golden_known_string():
    # "TEST" from the pydigi source docstring example: T=1101101, E=1110111, S=1101111.
    assert _dut("TEST") == "1101101" + "00" + "1110111" + "00" + \
        "1101111" + "00" + "1101101" + "00"


@pytest.mark.parametrize("text", [
    "the quick brown fox jumps over the lazy dog",
    "CQ CQ de N0CALL",
    "Hello, World! 123",
    "",  # empty stream -> empty output
])
def test_block_reference_bit_exact_vs_golden(text):
    assert _dut(text) == golden_bits_str(text)


@pytest.mark.parametrize("seed", [1, 2, 3, 4, 5])
def test_random_strings_bit_exact_vs_golden(seed):
    rng = random.Random(seed)
    text = "".join(chr(rng.randint(0, 127)) for _ in range(200))
    b = VaricodeEncoderBlock("v")
    dut = b.process_reference([ord(c) for c in text]).tolist()
    gold = golden_bits([ord(c) for c in text])
    assert dut == gold


def test_full_ascii_alphabet_bit_exact():
    """Every ASCII code 0..127 once — exercises the whole table."""
    b = VaricodeEncoderBlock("v")
    dut = b.process_reference(list(range(128))).tolist()
    gold = golden_bits(list(range(128)))
    assert dut == gold


# --------------------------------------------------------- MUTATION gates (INV-4)
def test_mutation_wrong_table_entry_FAILS():
    """A single corrupted table entry must make the gate disagree with the golden."""
    text = "the quick brown fox"
    gold = golden_bits([ord(c) for c in text])
    bad = list(GOLDEN_VARICODE)
    bad[ord("q")] = "1011011"          # wrong code for 'q'
    mutated = []
    for ch in text:
        for bit in bad[ord(ch)]:
            mutated.append(1 if bit == "1" else 0)
        mutated += [0, 0]
    assert mutated != gold, "gate blind to a wrong table entry"


def test_mutation_missing_00_separator_FAILS():
    """Dropping the '00' inter-character gap must make the gate disagree."""
    text = "test"
    gold = golden_bits([ord(c) for c in text])
    no_gap = []
    for ch in text:
        no_gap += [1 if b == "1" else 0 for b in GOLDEN_VARICODE[ord(ch)]]
        # (no '00' appended)
    assert no_gap != gold, "gate blind to a missing '00' separator"


def test_mutation_dropped_bit_FAILS():
    """Dropping one output bit must make the gate disagree with the golden."""
    text = "the quick brown fox"
    gold = golden_bits([ord(c) for c in text])
    dropped = gold[:5] + gold[6:]      # drop bit 5
    assert dropped != gold, "gate blind to a dropped bit"


def test_mutation_inverted_bits_FAILS():
    text = "hello"
    gold = golden_bits([ord(c) for c in text])
    inv = [1 - b for b in gold]
    assert inv != gold


# ------------------------------- SRAM-BACKED: the former quarantine wall is resolved
# The single-cell design QUARANTINED (INV-29): 128-entry table too big + variable-
# length emit. The SRAM-backed design (INV-31) resolves both — build_cell_programs()
# now SUCCEEDS. The full round-trip through the REAL panel is gated bit-exact in
# test_varicode_encoder_sram.py; here we assert the build no longer walls.
def test_build_succeeds_sram_backed():
    """build_cell_programs() no longer raises: it produces the emit + controller cells
    (the table moved to the SRAM panel, so the LOAD-table wall is gone)."""
    b = VaricodeEncoderBlock("v")
    cps = b.build_cell_programs()          # must NOT raise
    assert set(cps) == {0, 1}              # emit cell + SRAM controller
    assert b.cell_count == 2


def test_sram_table_replaces_the_load_table_wall():
    """The 128-entry table lives in the SRAM panel (one packed word each), NOT in a
    cell's LOAD-indirect table (mem[Rn]&0x1F = ~21 usable). Each entry packs into one
    16-bit word: the code left-aligned at bit 15 + the length in bits[3:0] —
    resolving BOTH walls (table size + variable emit)."""
    b = VaricodeEncoderBlock("v")
    sram = b.sram_image
    assert len(sram) == 128                # unbounded panel, not the 21-entry ceiling
    assert all(w <= 0xFFFF for w in sram)  # one 16-bit word per entry
    # the packed length nibble drives the emit count (the former variable-emit wall).
    assert (sram[32] & 0xF) == len("1")    # space -> length 1
    assert (sram[ord("s")] & 0xF) == len("10111")
