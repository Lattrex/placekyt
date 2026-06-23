"""Bitstream disassembler — turn a list of raw 16-bit Kyttar words into a
human-readable mnemonic listing (#183/#184).

A bitstream (a ``.kbs`` stimulus or a chip program) is a sequence of 16-bit
instruction words. All instructions are 16 bits with OP in bits [15:12]. The
field layout is per-opcode (see ``the Kyttar architecture spec`` §4.2–4.14, which
is the canonical reference and matches simkyt ``instruction``).

For WRITE/JUMP the ``@N`` hop notation is ``N = 31 - HOP_CNT`` (N hops away;
HOP_CNT is consumed at 31). A **stimulus** stream is a run of WRITE+DATA+JUMP
bursts: the word immediately after a WRITE is its DATA payload (a literal value),
NOT an instruction — so a stateful disassembly shows it as ``DW`` rather than
mis-decoding the bit pattern (e.g. 0xCAFE as a bogus MUL).

This is intentionally a small, dependency-free decoder of the fixed ISA encoding
(it does not re-enter simkyt per word).
"""

from __future__ import annotations

# Opcode (bits [15:12]) — authoritative table, spec §4.2.
_OP_HALT = 0x0       # 0001/0010/0011 are reserved and execute as HALT
_OP_MOVE = 0x4
_OP_BRANCH = 0x5
_OP_WRITE = 0x6
_OP_JUMP = 0x7
_OP_LOGIC = 0x8
_OP_ARITH = 0x9
_OP_SHL = 0xA
_OP_SHR = 0xB
_OP_MUL = 0xC
_OP_MAC = 0xD
_OP_CMP = 0xE
_OP_LOAD = 0xF

# Submodes (bits [11:10]).
_ARITH = {0: "ADD", 1: "ADC", 2: "SUB", 3: "SBC"}            # §4.9
_LOGIC = {0: "AND", 1: "OR", 2: "XOR", 3: "NOT"}             # §4.8
_MUL = {0: "MUL", 1: "MULQ", 2: "MULHI", 3: "MUL?"}          # §4.11
_MAC = {0: "MAC", 1: "MACQ", 2: "MSU", 3: "MSUQ"}            # §4.12

# Branch FLAG[11:9] → flag name (spec §4.5). INV[8] picks SET (BR.x) vs CLEAR
# (BR.Nx); for the common flags we use the canonical mnemonic names.
_BR_SET = {0: "BR.C", 1: "BR.Z", 2: "BR.N", 3: "BR.V",
           4: "BR.P", 5: "BR.A", 6: "BR.SLT", 7: "BR.?"}
_BR_CLR = {0: "BR.NC", 1: "BR.NZ", 2: "BR.NN", 3: "BR.NV",
           4: "BR.NP", 5: "BR.NA", 6: "BR.SGE", 7: "BR.N?"}

# CONFIG-register access is bit [10] on MOVE (CFG_DEST) and WRITE (CFG).
_CFG_BIT = 1 << 10
# CFG_SRC on MOVE is bit [11].
_MOVE_CFG_SRC = 1 << 11

# CONFIG register names (spec §3.x) for readable MOVE/WRITE.CFG operands.
_CFG_REG = {0: "FLAGS", 1: "FACE", 2: "IN_FACE", 3: "LOCK_FACE", 4: "LOCK"}


def _hop_to_at(hop_cnt: int) -> int:
    """``@N`` hops-away from the 5-bit HOP_CNT field (N = 31 - HOP_CNT)."""
    return 31 - (hop_cnt & 0x1F)


def _cfg_name(addr: int) -> str:
    return _CFG_REG.get(addr & 0x1F, f"CFG{addr & 0x1F}")


def _branch_offset(word: int) -> int:
    """Signed 6-bit OFFSET[5:0]; new PC = PC + 1 + OFFSET (spec §4.5)."""
    off = word & 0x3F
    return off - 64 if off & 0x20 else off


def disassemble_word(word: int, *, is_data: bool = False) -> str:
    """One 16-bit ``word`` → an ISA mnemonic string. ``is_data=True`` forces a
    data-literal rendering (the payload after a WRITE)."""
    word &= 0xFFFF
    if is_data:
        return f"DW   0x{word:04X}"

    # 0x0000 is HALT (reset safety); any opcode-0 word is HALT (spec §4.3).
    op = (word >> 12) & 0xF
    if op == _OP_HALT or op in (0x1, 0x2, 0x3):
        return "HALT"

    hop = (word >> 5) & 0x1F        # WRITE/JUMP HOP_CNT
    dst = word & 0x1F               # WRITE/JUMP DEST, also SRC_B for ALU
    a = (word >> 5) & 0x1F          # SRC / SRC_A
    b = word & 0x1F                 # DEST / SRC_B
    md = (word >> 10) & 0x3         # submode (ARITH/LOGIC/MUL/MAC)

    if op == _OP_WRITE:
        cfg = (word & _CFG_BIT) != 0
        tgt = _cfg_name(dst) if cfg else str(dst)
        return f"WRITE{'.CFG' if cfg else ''} @{_hop_to_at(hop)}, {tgt}"
    if op == _OP_JUMP:
        return f"JUMP @{_hop_to_at(hop)}, {dst}"
    if op == _OP_MOVE:
        # MOVE: CFG_SRC[11] | CFG_DEST[10] | SRC[9:5] | DEST[4:0].
        src = f"[{_cfg_name(a)}]" if (word & _MOVE_CFG_SRC) else f"R{a}"
        dest = f"[{_cfg_name(b)}]" if (word & _CFG_BIT) else f"R{b}"
        return f"MOVE {dest}, {src}"
    if op == _OP_ARITH:
        return f"{_ARITH[md]} R{a}, R{b}"
    if op == _OP_LOGIC:
        if md == 3:                 # NOT is unary
            return f"NOT R{a}"
        return f"{_LOGIC[md]} R{a}, R{b}"
    if op == _OP_MUL:
        return f"{_MUL[md]} R{a}, R{b}"
    if op == _OP_MAC:
        return f"{_MAC[md]} R{a}, R{b}"
    if op == _OP_CMP:
        return f"CMP R{a}, R{b}"
    if op == _OP_SHL or op == _OP_SHR:
        # Shift: ROT[11] | RSVD[10] | CNT[9:6] | SRC[5:0]. SRC bit5 reserved.
        rot = (word >> 11) & 0x1
        cnt = (word >> 6) & 0xF
        src = word & 0x1F
        mnem = ("ROL" if rot else "SHL") if op == _OP_SHL else ("ROR" if rot else "SHR")
        return f"{mnem} R{src}, #{cnt}"
    if op == _OP_BRANCH:
        flag = (word >> 9) & 0x7
        inv = (word >> 8) & 0x1
        mnem = (_BR_CLR if inv else _BR_SET)[flag]
        return f"{mnem} {_branch_offset(word):+d}"
    if op == _OP_LOAD:
        return f"LOAD [R{dst}]"
    return f"?? 0x{word:04X}"


def disassemble_bitstream(words, *, stateful: bool = True,
                          base_addr: int = 0) -> str:
    """Disassemble a bitstream into an ``addr: HEX  mnemonic`` listing.

    With ``stateful=True`` (the default, for stimulus / WRITE+DATA+JUMP streams)
    the word right after a WRITE is rendered as its DATA payload (``DW``) instead
    of being mis-decoded as an instruction. ``stateful=False`` decodes every word
    independently (useful for inspecting a flat program image)."""
    lines = []
    expect_data = False
    for i, w in enumerate(words):
        w &= 0xFFFF
        if stateful and expect_data:
            text = disassemble_word(w, is_data=True)
            expect_data = False
        else:
            text = disassemble_word(w)
            if stateful and ((w >> 12) & 0xF) == _OP_WRITE:
                expect_data = True
        lines.append(f"{base_addr + i:04X}: {w:04X}  {text}")
    return "\n".join(lines)
