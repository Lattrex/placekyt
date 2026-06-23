"""Tests for the bitstream disassembler (engine.disasm) — #202.

The strongest check is a round-trip against the real simkyt assembler: every
instruction we can assemble must disassemble back to a string naming the same
opcode + operands. We also pin the spec §4.4 example encodings directly.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "simkyt" / "python"))

from engine.disasm import disassemble_bitstream, disassemble_word  # noqa: E402

try:
    from gr_kyttar.placement.kyttar_block import assemble_to_words
    _HAVE_ASM = True
except Exception:  # noqa: BLE001 — simkyt not built in this env
    _HAVE_ASM = False


# Spec §4.4 / §4.5 example encodings (canonical reference values).
SPEC_ENCODINGS = [
    (0x4800, "MOVE R0, [FLAGS]"),
    (0x4401, "MOVE [FACE], R0"),
    (0x4840, "MOVE R0, [IN_FACE]"),
    (0x4000, "MOVE R0, R0"),
    (0x0000, "HALT"),
    (0xE001, "CMP R0, R1"),
    (0x5205, "BR.Z +5"),
    (0x533D, "BR.NZ -3"),
]


@pytest.mark.parametrize("word,expected", SPEC_ENCODINGS)
def test_spec_example_encodings(word, expected):
    assert disassemble_word(word) == expected


def test_reserved_opcodes_are_halt():
    # 0x0/0x1/0x2/0x3 all execute as HALT (spec §4.2).
    for op in (0x0, 0x1, 0x2, 0x3):
        assert disassemble_word(op << 12) == "HALT"


def test_write_cfg_bit_is_bit10():
    # WRITE.CFG @1, FACE — CFG at bit[10], HOP_CNT=30 (@1), DEST=1.
    word = (0x6 << 12) | (1 << 10) | (30 << 5) | 1
    assert disassemble_word(word) == "WRITE.CFG @1, FACE"
    # Plain WRITE @1, 5.
    word = (0x6 << 12) | (30 << 5) | 5
    assert disassemble_word(word) == "WRITE @1, 5"


def test_data_word_rendering():
    assert disassemble_word(0xCAFE, is_data=True) == "DW   0xCAFE"


def test_stateful_write_data_jump_burst():
    # WRITE @1,0 / DW value / JUMP @1,3 — the payload must read as DW.
    words = [(0x6 << 12) | (30 << 5), 0x1234, (0x7 << 12) | (30 << 5) | 3]
    out = disassemble_bitstream(words, stateful=True).splitlines()
    assert "WRITE @1, 0" in out[0]
    assert "DW   0x1234" in out[1]
    assert "JUMP @1, 3" in out[2]


# Round-trip: assemble each mnemonic, disassemble, check the opcode mnemonic +
# register operands survive. This catches any opcode/field-layout drift.
ROUNDTRIP_ASM = """\
start:
    ADD R3, R4
    ADC R1, R2
    SUB R5, R6
    SBC R7, R8
    AND R9, R10
    OR R11, R12
    XOR R13, R14
    NOT R15
    MUL R16, R17
    MULQ R18, R19
    MULHI R20, R21
    MAC R22, R23
    MACQ R24, R25
    MSU R26, R27
    MSUQ R28, R29
    CMP R30, R31
    SHL R5, #3
    SHR R6, #5
    ROL R7, #1
    ROR R8, #2
    CMP R0, R1
    BR.N is_one
    MOVE R0, R2
    JUMP @1, 3
is_one:
    MOVE R0, R3
"""

EXPECTED_ROUNDTRIP = [
    "ADD R3, R4", "ADC R1, R2", "SUB R5, R6", "SBC R7, R8",
    "AND R9, R10", "OR R11, R12", "XOR R13, R14", "NOT R15",
    "MUL R16, R17", "MULQ R18, R19", "MULHI R20, R21",
    "MAC R22, R23", "MACQ R24, R25", "MSU R26, R27", "MSUQ R28, R29",
    "CMP R30, R31",
    "SHL R5, #3", "SHR R6, #5", "ROL R7, #1", "ROR R8, #2",
    "CMP R0, R1", "BR.N +2", "MOVE R0, R2", "JUMP @1, 3", "MOVE R0, R3",
]


@pytest.mark.skipif(not _HAVE_ASM, reason="simkyt assembler not available")
def test_roundtrip_all_mnemonics():
    words = assemble_to_words(ROUNDTRIP_ASM, base_addr=0)
    got = [disassemble_word(w) for w in words]
    assert got == EXPECTED_ROUNDTRIP
