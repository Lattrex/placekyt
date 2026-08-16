# SPDX-License-Identifier: GPL-3.0-or-later
"""VaricodeEncoderBlock — SRAM-backed PSK31 Varicode encoder (see the class docstring).

Spec: PSK31 Varicode (G3PLX / Peter Martinez). Each ASCII character maps to a
variable-length code of '1'/'0' bits that (a) starts and ends with '1' and (b)
contains NO two consecutive '0's; characters are separated by a '00' gap. Canonical
128-entry table transcribed from the fldigi ``pskvaricode.cxx`` table (via the pydigi
reimplementation) and cross-checked against the ARRL PSK31 spec and the Wikipedia
"Varicode" article: space=``1``, e=``11``, t=``101``, a=``1011``, o=``111``,
i=``1101``, n=``1111``, s=``10111``, LF=``11101``, CR=``11111``.

There is NO stock GNU Radio Varicode block (``grc_block`` is ''); the golden reference
is a pure-Python model of this published table (``verification/tests/varicode_golden.py``).

This block was PREVIOUSLY QUARANTINED (INV-29): the 128-entry variable-length table
does not fit a 32-word cell, and per-character emit is a data-dependent 3..12-word
burst. It is now an **SRAM-BACKED** design (INV-31) — the FIRST SRAM-backed DSP block —
built against the ``SramControllerBlock`` + SRAM panel contract (``verification/
SRAM_PANEL.md`` §6). See :class:`VaricodeEncoderBlock` for the recipe.
"""
from typing import Dict, List

import numpy as np

from ..block import CellProgram, Port, EntryPoint, StateVar, DataWord
from ._base import KyttarBlock, BlockInterface


# --- The canonical PSK31 Varicode table (ASCII 0..127) --------------------------
# Transcribed EXACTLY from fldigi src/psk/pskvaricode.cxx (via ckoval7/pydigi
# pydigi/varicode/psk_varicode.py). Validated: 128 unique entries, every code starts
# AND ends with '1', no '00' within any code, max length 10 bits. This is the golden.
VARICODE_TABLE: List[str] = [
    "1010101011", "1011011011", "1011101101", "1101110111", "1011101011",  # 0-4
    "1101011111", "1011101111", "1011111101", "1011111111", "11101111",    # 5-9
    "11101", "1101101111", "1011011101", "11111", "1101110101",            # 10-14
    "1110101011", "1011110111", "1011110101", "1110101101", "1110101111",  # 15-19
    "1101011011", "1101101011", "1101101101", "1101010111", "1101111011",  # 20-24
    "1101111101", "1110110111", "1101010101", "1101011101", "1110111011",  # 25-29
    "1011111011", "1101111111", "1", "111111111", "101011111",             # 30-34
    "111110101", "111011011", "1011010101", "1010111011", "101111111",     # 35-39
    "11111011", "11110111", "101101111", "111011111", "1110101",           # 40-44
    "110101", "1010111", "110101111", "10110111", "10111101",              # 45-49
    "11101101", "11111111", "101110111", "101011011", "101101011",         # 50-54
    "110101101", "110101011", "110110111", "11110101", "110111101",        # 55-59
    "111101101", "1010101", "111010111", "1010101111", "1010111101",       # 60-64
    "1111101", "11101011", "10101101", "10110101", "1110111",              # 65-69
    "11011011", "11111101", "101010101", "1111111", "111111101",           # 70-74
    "101111101", "11010111", "10111011", "11011101", "10101011",           # 75-79
    "11010101", "111011101", "10101111", "1101111", "1101101",             # 80-84
    "101010111", "110110101", "101011101", "101110101", "101111011",       # 85-89
    "1010101101", "111110111", "111101111", "111111011", "1010111111",     # 90-94
    "101101101", "1011011111", "1011", "1011111", "101111",                # 95-99
    "101101", "11", "111101", "1011011", "101011",                         # 100-104
    "1101", "111101011", "10111111", "11011", "111011",                    # 105-109
    "1111", "111", "111111", "110111111", "10101",                         # 110-114
    "10111", "101", "110111", "1111011", "1101011",                        # 115-119
    "11011111", "1011101", "111010101", "1010110111", "110111011",         # 120-124
    "1010110101", "1011010111", "1110110101",                              # 125-127
]
assert len(VARICODE_TABLE) == 128
assert len(set(VARICODE_TABLE)) == 128
for _c in VARICODE_TABLE:
    assert _c[0] == "1" and _c[-1] == "1" and "00" not in _c


# --- SRAM entry packing (the load format) ---------------------------------------
# Each table entry packs into ONE 16-bit SRAM word: the code sits LEFT-ALIGNED at
# bit 15 (MSB-first bit order preserved — the first bit to emit is ALWAYS bit 15),
# and bits[3:0] hold the code LENGTH (1..10). Left alignment is what lets the
# emitter walk the code with fixed single-bit shifts (test bit 15, shift left one)
# — shift counts are immediate instruction fields (CNT[9:6]), so the walk position
# must be fixed, not data-dependent. The panel returns ONE fixed word per symbol,
# from which the emitter derives EXACTLY how many bits to emit (len(code)+2 for
# the '00' gap). Codes are <= 10 bits, so the aligned code region (bits[15:6])
# never overlaps the length field.
SRAM_CODE_BITS = 10           # Varicode codes are <= 10 bits
SRAM_LEN_BITS = 4             # length 1..10 fits 4 bits
SRAM_CODE_MASK = 0xFFC0       # bits[15:6] — the left-aligned code region
SRAM_LEN_MASK = (1 << SRAM_LEN_BITS) - 1     # bits[3:0]


def pack_entry(code: str) -> int:
    """Pack a Varicode code string into its 16-bit SRAM word:
    ``(code_int << (16 - len)) | len``.

    ``code`` is the '1'/'0' bit string (MSB-first); shifting ``int(code, 2)`` up
    to the top of the word puts the FIRST bit at bit 15 (bit order preserved).
    The length occupies bits[3:0]. Inverse of :func:`unpack_bits`.
    """
    length = len(code)
    if length > SRAM_CODE_BITS:
        raise ValueError(f"code {code!r} exceeds {SRAM_CODE_BITS} bits")
    return ((int(code, 2) << (16 - length)) & 0xFFFF) | length


def unpack_bits(word: int) -> List[int]:
    """Unpack a packed SRAM word back to its Varicode bit list (no '00' gap).

    The emitter uses this on the panel-delivered word: read the length from
    bits[3:0], then take the top ``length`` bits MSB-first. Inverse of
    :func:`pack_entry`.
    """
    length = word & SRAM_LEN_MASK
    return [(word >> (15 - i)) & 1 for i in range(length)]


def sram_table() -> List[int]:
    """The 128-word SRAM panel image (address == ASCII code point) for the load phase.

    Stream this into the panel via the ``SramControllerBlock`` write path
    (``set_addr`` to 0, then one ``write`` per word — the controller auto-increments).
    """
    return [pack_entry(c) for c in VARICODE_TABLE]


def varicode_bits(text_bytes) -> List[int]:
    """The GOLDEN PSK31 Varicode bit stream for a sequence of ASCII bytes.

    For each byte ``b`` (0..127) emit ``VARICODE_TABLE[b]`` as individual 0/1 bits,
    then the ``00`` inter-character gap. Returns a flat list of ints (0/1).
    """
    out: List[int] = []
    for b in text_bytes:
        code = VARICODE_TABLE[int(b) & 0x7F]
        out.extend(1 if ch == "1" else 0 for ch in code)
        out.extend((0, 0))          # inter-character '00' gap
    return out


def emit_from_sram(sram: List[int], text_bytes) -> List[int]:
    """The SRAM-backed encode: for each byte, look the packed word up in the panel
    image ``sram`` (address == byte), unpack (code,length), emit ``length`` bits then
    the ``00`` gap. This models EXACTLY the on-chip lookup+emit path (panel push-read
    delivers ``sram[byte]``; the emitter unpacks + emits). Bit-exact to
    :func:`varicode_bits` when ``sram == sram_table()``.
    """
    out: List[int] = []
    for b in text_bytes:
        word = sram[int(b) & 0x7F]
        out.extend(unpack_bits(word))
        out.extend((0, 0))
    return out


class VaricodeEncoderBlock(KyttarBlock):
    """
    PSK31 Varicode encoder — SRAM-backed (INV-31). No stock GR block (``grc_block`` '').

    For each input character (a byte, ASCII 0..127) the block emits the character's
    variable-length Varicode bits (each as one output word, 0 or 1) followed by the
    ``00`` inter-character gap. The mapping is the canonical G3PLX PSK31 Varicode
    table (:data:`VARICODE_TABLE`).

    SRAM-backed construction (the recipe; SRAM_PANEL.md §6)
    ------------------------------------------------------
    This is the FIRST SRAM-backed DSP block. It resolves the two INV-29 walls that
    previously QUARANTINED it by moving the table off-cell and packing each entry so
    the emit count is fixed-word:

    * **Table size wall → SRAM panel.** The 128-entry table lives in the SRAM PANEL
      (:data:`sram_table`, address == ASCII code point), NOT in cell registers. A
      cell's LOAD-indirect table caps at ~21 entries (``mem[Rn] & 0x1F``); the panel
      is unbounded (INV-31).

    * **Variable-length-emit wall → packed (code,length) word.** Each entry packs
      into ONE 16-bit word: the code LEFT-ALIGNED at bit 15 plus the length in
      bits[3:0] (:func:`pack_entry`). Varicode codes are <= 10 bits, so the code
      region (bits[15:6]) and the 4 length bits never overlap. The panel push-read
      returns ONE fixed word per symbol; the emitter reads the length and emits
      exactly that many bits + ``00`` — a fixed-word delivery, not a data-dependent
      burst across the panel port.

    Load phase (once): ``set_addr`` the controller to base 0, then stream the 128
    :func:`sram_table` words through the controller's ``write`` entry (auto-increment).
    Lookup phase (per byte): point the controller's read address at ``byte``, set the
    push-read descriptors so the panel delivers ``sram[byte]`` back to the emitter
    cell's register + kicks its emit entry, then ``read``. The emitter unpacks
    (:func:`unpack_bits`) and emits ``length`` bits + ``00``.

    The panel round-trip (load via controller ``write``, per-symbol push-read
    delivery) is PROVEN bit-exact vs the golden through REAL SramPanelDevice/
    PanelDriver + real simkyt routing in
    ``verification/tests/test_varicode_encoder_sram.py``.

    Interface:
      * Entry ``emit`` (default). Input byte in R{input}. Output bits on R{output}.
    """
    CATEGORY = "coding"
    TAGS = ["varicode", "psk31", "encoder", "coding", "ham", "sram"]

    _interface = BlockInterface(entry_address=1, input_registers=[25],
                                output_registers=[25])

    # SRAM panel base address for the table (address == ASCII code point).
    TABLE_BASE = 0
    TABLE_WORDS = 128

    def __init__(self, name: str, panel_hop: int = 1,
                 emit_hop: int = 1, emit_dest: int = 25, emit_entry: int = 1,
                 read_wr_desc: int = 0, read_jp_desc: int = 0,
                 addr_base: int = 0):
        super().__init__(name, panel_hop=panel_hop, emit_hop=emit_hop,
                         emit_dest=emit_dest, emit_entry=emit_entry,
                         read_wr_desc=read_wr_desc, read_jp_desc=read_jp_desc,
                         addr_base=addr_base)
        # addr_base: shared-panel table offset (see SramControllerBlock) —
        # the embedded controller adds it to every lookup key, and
        # panel_requirements ships the ROM image at the offset addresses.
        self._addr_base = int(addr_base) & 0xFFFF
        self._table = list(VARICODE_TABLE)
        self._sram = sram_table()
        self._panel_hop = panel_hop
        self._emit_hop = emit_hop
        self._emit_dest = emit_dest
        self._emit_entry = emit_entry
        # The embedded controller's push-read descriptors: where the panel delivers
        # each looked-up packed word (the EMIT cell's word register + emit entry,
        # hop-counted along the x1_in return corridor). PLACEMENT-DERIVED — the
        # example/route builder computes them from the routed return corridor and
        # sets them here; the 0 default is the panel's disabled sentinel semantics
        # only until placement wires them (a build with 0s reads to nowhere).
        self._read_wr_desc = read_wr_desc & 0xFFFF
        self._read_jp_desc = read_jp_desc & 0xFFFF

    @property
    def cell_count(self) -> int:
        # Two cells: the SRAM controller (panel sequencing) + the emit/unpack cell.
        return 2

    @property
    def interface(self) -> BlockInterface:
        return self._interface

    @property
    def table(self):
        """The canonical 128-entry Varicode table (list of bit-strings)."""
        return list(self._table)

    @property
    def sram_image(self) -> List[int]:
        """The 128-word packed SRAM panel image (address == ASCII code point)."""
        return list(self._sram)

    def panel_requirements(self) -> dict:
        """This block needs an SRAM panel holding the 128-word Varicode ROM.

        Cell 0 (the embedded SramController, primary entry ``lookup``) sits AT the
        panel's x1_out port; the panel push-reads each looked-up packed word into
        cell 1's ``word`` register via the chip's x1_in port (the return corridor).
        """
        return {
            "label": "Varicode ROM (128 x 16b)",
            # Shipped at addr_base + a: a SHARED panel offsets this table clear
            # of a co-resident client's addresses (the duplex transceiver).
            "image": {self._addr_base + a: w for a, w in enumerate(self._sram)},
            "controller_cell": 0,
            "return_port": "word",
            "return_cell": 1,
        }

    def build_cell_programs(self) -> Dict[int, CellProgram]:
        """The SRAM-backed lookup+emit cell.

        Cell 0 is the emit/unpack cell that receives a panel-delivered packed word
        in R{in:word}, reads the length (bits[3:0]), and walks the left-aligned
        code out of bit 15 (fixed-position extraction, immediate shift counts),
        emitting the code bits + the ``00`` gap. The panel sequencing (load-phase writes + per-symbol push-read)
        is driven by a companion :class:`SramControllerBlock` (see the class
        docstring recipe); this cell is the consumer the push-read delivers to.

        The emit loop is a data-dependent count of bits derived from the unpacked
        length — representable because the panel delivers a FIXED word per symbol
        (the variable length is a small in-cell counter, not a variable-length burst
        across the panel port, which was the quarantined wall).
        """
        h = self._emit_hop
        # Emit cell: length from bits[3:0]; the code is LEFT-ALIGNED at bit 15,
        # so each bit is extracted at the FIXED position 15 (SHR #15) and the
        # working register walks left one bit per iteration (SHL #1) — shift
        # counts are immediate instruction fields, so the walk position must be
        # fixed, not data-dependent. Then the two '0' gap bits.
        #
        # EVERY emitted bit is a WRITE **+ JUMP** pair: a downstream BLOCK runs
        # only when JUMP-triggered, so a WRITE-only bit stream would deposit each
        # bit over the last (the diff encoder would fire once per character on
        # the final gap bit — the whole-chain failure the port-egress-only SRAM
        # test could not see, since a port captures every passing WRITE word).
        tmpl = (
            "emit:\n"
            # length = word & 0xF -> R{state:len}; work = the aligned code region
            "    MOVE R0, R{in:word}\n"
            "    AND R0, R{data:len_mask}\n"
            "    MOVE R{state:len}, R0\n"
            "    MOVE R0, R{in:word}\n"
            "    AND R0, R{data:code_mask}\n"
            "    MOVE R{state:work}, R0\n"
            # per bit: bit = work >> 15 (the aligned MSB); emit; work <<= 1
            "emit_loop:\n"
            "    SHR R{state:work}, #15\n"          # current bit -> R0 (0/1)
            f"    WRITE @{h}, {self._emit_dest}\n"  # emit one bit downstream
            f"    JUMP @{h}, {self._emit_entry}\n"  # ...and trigger its entry
            "    SHL R{state:work}, #1\n"
            "    MOVE R{state:work}, R0\n"
            "    SUB R{state:len}, R{data:one}\n"   # len-- (sets Z; MOVE keeps flags)
            "    MOVE R{state:len}, R0\n"
            "    BR.NZ emit_loop\n"                 # more bits while len>0
            # inter-character '00' gap: emit two zero bits (each triggered)
            "    MOVE R0, R{data:zero}\n"
            f"    WRITE @{h}, {self._emit_dest}\n"
            f"    JUMP @{h}, {self._emit_entry}\n"
            "    MOVE R0, R{data:zero}\n"
            f"    WRITE @{h}, {self._emit_dest}\n"
            f"    JUMP @{h}, {self._emit_entry}\n"
            "    HALT\n"
        )
        emit_cell = CellProgram(
            inputs=[Port("word")],
            outputs=[Port("bits")],
            entries=[EntryPoint("emit")],
            data=[DataWord("one", 1, address=1),
                  DataWord("zero", 0, address=2),
                  DataWord("len_mask", SRAM_LEN_MASK, address=3),
                  DataWord("code_mask", SRAM_CODE_MASK, address=4)],
            state=[StateVar("len"), StateVar("work")],
            assembly_template=tmpl,
        )
        # Cell 0: the SRAM controller macro (panel sequencing) — imported lazily to
        # avoid a hard import cycle. It is the LANDING cell (first cell with inputs):
        # the char stream injects each ASCII byte into its ``lookup`` entry
        # (primary_entry="lookup" puts lookup first, so a plain net / chip input
        # port JUMPs into the random-access read — sram[byte] push-reads to the
        # emit cell per the read_wr_desc/read_jp_desc descriptors). Cell 1 is the
        # emit cell — the LAST cell, so the block's user-facing output is its
        # ``bits`` port (output_cell_id default = last cell).
        from .sram_controller_block import SramControllerBlock
        ctl = SramControllerBlock(self.name + "_ctl", panel_hop=self._panel_hop,
                                  read_wr_desc=self._read_wr_desc,
                                  read_jp_desc=self._read_jp_desc,
                                  primary_entry="lookup",
                                  addr_base=self._addr_base)
        ctl_cell = ctl.build_cell_programs()[0]
        return {0: ctl_cell, 1: emit_cell}

    def process_reference(self, input_bytes) -> np.ndarray:
        """GOLDEN reference: the PSK31 Varicode bit stream for the input bytes.

        Each byte -> its Varicode bits (one per word) + the ``00`` gap. Bit-exact to
        :func:`varicode_bits` (the published table) AND to :func:`emit_from_sram`
        over :func:`sram_table` (the SRAM-backed path).
        """
        bits = varicode_bits(np.asarray(input_bytes).reshape(-1).tolist())
        return np.asarray(bits, dtype=np.int16)

    def reset(self):
        pass
