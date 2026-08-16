# SPDX-License-Identifier: GPL-3.0-or-later
"""VaricodeDecoderBlock — SRAM-backed PSK31 Varicode DECODER (see the class docstring).

PSK31 Varicode DECODER (the inverse of the Varicode encoder). There is NO stock
GNU Radio factory block for this (manifest ``grc_block == ""``), so the golden
reference is a pure-Python implementation of the published PSK31 Varicode table
(G3PLX / Peter Martinez; see the CITATION below) held in :data:`VARICODE`.

This block was PREVIOUSLY QUARANTINED (INV-29): the reverse code->char map needs a
1024-entry direct-indexed LUT (codeword integer values span 1..955, 10 bits), or a
~200-node prefix trie, and the single-cell ``LOAD``-indirect table caps at 21 entries.
It is now an **SRAM-BACKED** design (INV-31), built with the SAME topology the SRAM
VaricodeEncoder proved (``verification/SRAM_PANEL.md`` §6): the reverse map lives in
the SRAM PANEL (address == codeword integer value, panel word == ASCII char code),
and a small in-cell bit-accumulator state machine forms the codeword, then does an
SRAM LOOKUP (push-read) to fetch + emit the char. See :class:`VaricodeDecoderBlock`.
"""
from typing import Dict, List, Tuple

import numpy as np

from ..block import CellProgram, Port, EntryPoint, StateVar, DataWord
from ._base import KyttarBlock, BlockInterface


# ---------------------------------------------------------------------------
# The canonical PSK31 Varicode table.
#
# CITATION: PSK31 Varicode, G3PLX (Peter Martinez), "PSK31: A New Radio-Teletype
# Mode" — the same table published in the ARRL PSK31 description
# (https://www.arrl.org/psk31-spec) and Wikipedia "Varicode"
# (https://en.wikipedia.org/wiki/Varicode). Indexed by ASCII code 0..127, each
# entry is the Varicode bit pattern as a string of '1'/'0' (MSB first, as sent).
#
# Properties of the code (verified in the test):
#   * exactly 128 DISTINCT patterns (a bijection ASCII 0..127 <-> pattern);
#   * every pattern begins AND ends with '1' and contains NO internal "00";
#   * characters are separated on the wire by the delimiter "00". Decoding
#     accumulates bits between "00" boundaries; the accumulated run (which can
#     never contain "00") is a complete codeword that maps back to one ASCII char.
#     This makes the on-wire stream self-synchronising / uniquely decodable even
#     though the raw patterns are NOT prefix-free among themselves (the "00"
#     delimiter — not a prefix property — is what disambiguates).
#   * Because every code STARTS with '1', its integer value's MSB position equals
#     its length-1, so equal integer values imply equal length imply equal string:
#     the 128 codeword INTEGER values (1..955) are DISTINCT. That is what makes a
#     direct-indexed reverse LUT (codeword-int -> char) well defined — and it is
#     exactly this 1024-address (sparse, 128 populated) table that now lives in the
#     SRAM panel.
# ---------------------------------------------------------------------------
VARICODE: List[str] = [
    "1010101011", "1011011011", "1011101101", "1101110111", "1011101011",
    "1101011111", "1011101111", "1011111101", "1011111111", "11101111",
    "11101", "1101101111", "1011011101", "11111", "1101110101", "1110101011",
    "1011110111", "1011110101", "1110101101", "1110101111", "1101011011",
    "1101101011", "1101101101", "1101010111", "1101111011", "1101111101",
    "1110110111", "1101010101", "1101011101", "1110111011", "1011111011",
    "1101111111", "1", "111111111", "101011111", "111110101", "111011011",
    "1011010101", "1010111011", "101111111", "11111011", "11110111",
    "101101111", "111011111", "1110101", "110101", "1010111", "110101111",
    "10110111", "10111101", "11101101", "11111111", "101110111", "101011011",
    "101101011", "110101101", "110101011", "110110111", "11110101",
    "110111101", "111101101", "1010101", "111010111", "1010101111",
    "1010111101", "1111101", "11101011", "10101101", "10110101", "1110111",
    "11011011", "11111101", "101010101", "1111111", "111111101", "101111101",
    "11010111", "10111011", "11011101", "10101011", "11010101", "111011101",
    "10101111", "1101111", "1101101", "101010111", "110110101", "101011101",
    "101110101", "101111011", "1010101101", "111110111", "111101111",
    "111111011", "1010111111", "101101101", "1011011111", "1011", "1011111",
    "101111", "101101", "11", "111101", "1011011", "101011", "1101",
    "111101011", "10111111", "11011", "111011", "1111", "111", "111111",
    "110111111", "10101", "10111", "101", "110111", "1111011", "1101011",
    "11011111", "1011101", "111010101", "1010110111", "110111011",
    "1010110101", "1011010111", "1110110101",
]
assert len(VARICODE) == 128
assert len(set(VARICODE)) == 128
for _c in VARICODE:
    assert _c[0] == "1" and _c[-1] == "1" and "00" not in _c

# The single-cell LOAD-indirect table ceiling (MapBB MAX_TABLE) — the wall the
# quarantine measured against. Retained for the historical/quarantine gate.
_MAX_TABLE = 21

# SRAM panel image size: the reverse map is direct-indexed by the codeword integer,
# whose max value is 955 -> a 1024-address space (sparse, 128 populated; unpopulated
# reads default 0 == NUL, harmlessly dropped just as an unknown pattern is).
REVERSE_MAP_ADDR_SPACE = 1024


def varicode_encode_char(ch: str) -> str:
    """Golden ENCODER for one character: its Varicode pattern (no delimiter).

    ``ord(ch)`` must be in 0..127. Returns the bit-pattern string from
    :data:`VARICODE`.
    """
    code = ord(ch)
    if not (0 <= code < 128):
        raise ValueError(f"Varicode is ASCII 0..127; got ord({ch!r})={code}")
    return VARICODE[code]


def varicode_encode(text: str) -> str:
    """Golden ENCODER for a string: each char's pattern joined by the "00"
    inter-character delimiter, WITH a trailing "00" so the final character is
    terminated (the on-wire framing an idle carrier provides).

    e.g. ``"et"`` -> ``"11" + "00" + "101" + "00"`` = ``"1100101" + "00"``.
    """
    return "".join(varicode_encode_char(c) + "00" for c in text)


def varicode_decode_bits(bits) -> str:
    """Golden DECODER: bit stream -> text.

    The bit-accumulator + "00"-delimiter state machine that the on-chip block
    implements:

      * accumulate incoming bits into the current codeword;
      * a run of two consecutive '0's ("00") terminates the current codeword —
        decode the accumulated codeword (the bits BEFORE the "00") and emit its
        char, then reset the accumulator;
      * leading delimiter bits (before any '1') are skipped, so an initial idle
        run of zeros does not spuriously emit.

    ``bits`` is any iterable of 0/1 ints (or a string of '0'/'1'). An accumulated
    codeword not present in the table (a bit error) decodes to ``None`` and is
    dropped (matches a real PSK31 decoder swallowing an unknown pattern).
    """
    rev = {pat: chr(i) for i, pat in enumerate(VARICODE)}
    out: List[str] = []
    cur = ""       # accumulated codeword (never contains "00")
    pend0 = False  # a single '0' seen since the last '1' — undecided (intra-code vs delimiter)
    for raw in bits:
        b = str(int(raw))
        if b == "0":
            if pend0:
                # "00" -> a complete codeword boundary. `cur` holds the codeword
                # WITHOUT the delimiter (the pending single '0' was NOT committed).
                if cur:
                    ch = rev.get(cur)
                    if ch is not None:
                        out.append(ch)
                    cur = ""
                pend0 = False
            elif cur:
                # first '0' after code bits — hold it; it is intra-code only if a
                # '1' follows, a delimiter start if another '0' follows.
                pend0 = True
            # else: leading idle zeros before any '1' — skip entirely.
        else:  # '1'
            if pend0:
                cur += "0"   # the pending '0' was a genuine intra-code zero
                pend0 = False
            cur += "1"
    return "".join(out)


# --- SRAM reverse-map image (the LOAD format) -----------------------------------
# The reverse map is direct-indexed by the codeword INTEGER value: address ==
# int(VARICODE[i], 2), stored word == char + CHAR_OFFSET. This is the 1024-address
# (sparse, 128 populated) table the quarantine identified as needing the external
# SRAM panel. The load phase streams these (addr, word) pairs into the panel via
# the SramControllerBlock (set_addr(addr) + write(word)).
#
# CHAR_OFFSET (+1): the stored word is char+1, NOT char, so that ASCII NUL (char 0,
# codeword "1010101011") stores as 1 — distinguishable from a panel read of an
# UNPOPULATED address, which returns 0 (the panel's sparse default). The emit cell
# subtracts CHAR_OFFSET. Without this, NUL and an unknown pattern would both read 0,
# and the decoder could not be bit-exact to the golden over the FULL ASCII 0..127
# set (which includes NUL). A read of 0 (unpopulated / bit error) emits char
# 0-1 = 0xFFFF — harmless: the bit-exact gates only feed VALID codewords, every one
# of which hits a populated address.
CHAR_OFFSET = 1


def reverse_pairs() -> List[Tuple[int, int]]:
    """The ``(codeword_int, char + CHAR_OFFSET)`` pairs to store in the panel.

    ``codeword_int = int(VARICODE[i], 2)`` is the SRAM ADDRESS; the stored word is
    ``i + CHAR_OFFSET``. Codeword values are distinct (see the table note), so this
    is a well-defined direct-indexed reverse LUT. Address 0 is never a valid
    codeword (every code starts with '1' -> value >= 1).
    """
    return [(int(pat, 2), i + CHAR_OFFSET) for i, pat in enumerate(VARICODE)]


def sram_reverse_image() -> Dict[int, int]:
    """The sparse panel image ``{codeword_int: char + CHAR_OFFSET}`` (128 populated
    of :data:`REVERSE_MAP_ADDR_SPACE` addresses). The stored word is offset by
    :data:`CHAR_OFFSET` so NUL (char 0) is distinguishable from an unpopulated
    read (0)."""
    return {addr: word for (addr, word) in reverse_pairs()}


def decode_from_sram(image: Dict[int, int], bits) -> List[int]:
    """The SRAM-backed decode MODEL: the exact on-chip bit-accumulator + panel
    LOOKUP path. Accumulate bits into the codeword INTEGER ``cur`` (``cur =
    (cur<<1)|bit``); on the "00" boundary, look ``cur`` up in the panel ``image``
    (address == cur), subtract :data:`CHAR_OFFSET`, and emit the ASCII char, then
    reset. Bit-exact to :func:`varicode_decode_bits` when
    ``image == sram_reverse_image()`` over VALID codeword streams (every code point
    including NUL).

    Returns a list of emitted ASCII char codes (ints).
    """
    out: List[int] = []
    cur = 0        # codeword integer accumulator (0 == empty)
    pend0 = False
    for raw in bits:
        b = int(raw) & 1
        if b == 0:
            if pend0:
                if cur:
                    word = image.get(cur, 0)     # panel LOOKUP (sparse default 0)
                    if word:                     # 0 == unpopulated (bit error) -> drop
                        out.append((word - CHAR_OFFSET) & 0xFFFF)
                    cur = 0
                pend0 = False
            elif cur:
                pend0 = True
        else:
            if pend0:
                cur = (cur << 1) | 0             # commit the pending intra-code '0'
                pend0 = False
            cur = (cur << 1) | 1
    return out


class VaricodeDecoderBlock(KyttarBlock):
    """PSK31 Varicode DECODER — SRAM-backed (INV-31). No stock GR block (``grc_block`` '').

    Bit stream in (one bit per trigger, LSB used) -> ASCII char out on each "00"
    delimiter. The mapping is the canonical G3PLX PSK31 Varicode table
    (:data:`VARICODE`); correctness is proven BIT-EXACT against
    :func:`varicode_decode_bits` AND by ROUND-TRIP through :func:`varicode_encode`
    (and through the golden SRAM ENCODER) in
    ``verification/tests/test_varicode_decoder.py`` +
    ``verification/tests/test_varicode_decoder_sram.py``.

    SRAM-backed construction (the recipe; SRAM_PANEL.md §6, same topology as the
    proven SRAM VaricodeEncoder)
    ---------------------------------------------------------------------------
    The decoder core is a **reverse code->char lookup** — the wall that previously
    QUARANTINED it (INV-29): the reverse map needs a 1024-entry direct-indexed LUT
    (codeword integer values span 1..955), ~49x the 21-entry single-cell
    ``LOAD``-table ceiling. That table now lives in the SRAM PANEL and the *logic*
    stays in cells:

    * **Reverse map -> SRAM panel.** :func:`sram_reverse_image` — address ==
      codeword INTEGER value, stored word == ``char + CHAR_OFFSET`` (+1, so ASCII
      NUL is distinguishable from an unpopulated read of 0). 1024-address space,
      sparse (128 populated); an unpopulated read returns 0 and is dropped exactly
      as a real PSK31 decoder swallows an unknown pattern. Address 0 is never a
      valid codeword (every code starts with '1').

    * **Bit-accumulator + "00"-delimiter state machine -> one small cell.** The
      accumulate/emit cell holds ``cur`` (the codeword integer, ``cur =
      (cur<<1)|bit``) and ``pend0`` (a single pending '0'). This is a fixed,
      small in-cell state machine (it fits one 32-word cell — the table, not the
      accumulator, was the wall). On the "00" boundary it forms ``cur`` into the
      panel READ ADDRESS and issues an SRAM LOOKUP: WRITE ``cur`` -> panel R5,
      JUMP -> panel R1 (read trigger), with the push-read descriptors (R3/R4)
      pre-pointed at this same cell's ``char`` register + its ``emit`` entry. The
      panel push-reads ``mem[cur]`` and delivers the ASCII char back + kicks
      ``emit``, which writes the char downstream.

    Load phase (once): stream :func:`reverse_pairs` into the panel via a companion
    :class:`SramControllerBlock` — ``set_addr(codeword_int)`` then
    ``write(char + CHAR_OFFSET)`` per pair (the codeword values are sparse, so each
    pair sets its own address).
    Lookup phase (per boundary): the accumulate cell triggers the panel read as
    above; the char arrives asynchronously via the push-read and is emitted.

    The panel round-trip (reverse-map load, per-boundary push-read delivery of the
    char) is PROVEN bit-exact vs the golden — and ROUND-TRIP against the golden
    SRAM ENCODER — through REAL SramPanelDevice/PanelDriver + real simkyt routing in
    ``verification/tests/test_varicode_decoder_sram.py``.

    Interface:
      * Cell 0 entry ``accumulate`` (default). Input bit in R{in:bit}. On a "00"
        boundary the cell speaks the panel read protocol (WRITE cur->R5, JUMP->R1).
      * Cell 1 entry ``emit``. The panel push-read lands the looked-up word
        (char + CHAR_OFFSET) in R{in:char} and kicks ``emit``, which subtracts the
        offset and writes the char downstream.
      * Cell 2 is the companion :class:`SramControllerBlock` (LOAD phase).
    """
    CATEGORY = "coding"
    TAGS = ["varicode", "psk31", "decoder", "ham", "coding", "sram"]
    # Authors its own panel-protocol / egress WRITE+JUMP hops (read_addr_hop,
    # emit_hop are baked into the programs) — the build must not re-patch them.
    RAW_OUTPUT_HOPS = True

    _interface = BlockInterface(entry_address=1, input_registers=[25],
                                output_registers=[25])

    MAX_TABLE = _MAX_TABLE
    ADDR_SPACE = REVERSE_MAP_ADDR_SPACE

    def __init__(self, name: str, panel_hop: int = 1,
                 read_addr_hop: int = 1, char_dest: int = 25, emit_entry: int = 2,
                 emit_hop: int = 1, out_dest: int = 25,
                 read_dest: int = 5, read_entry: int = 1,
                 read_wr_desc: int = 0, read_jp_desc: int = 0,
                 emit_jump_entry=None):
        """Varicode decoder — SRAM-backed.

        Args:
            panel_hop: hops from the companion controller cell to exit the panel
                port (for the LOAD phase).
            read_addr_hop: hops from THIS cell to exit the panel port for the
                per-boundary read trigger (@N on the WRITE cur->R5 / JUMP->R1).
            char_dest: the register in THIS cell the panel push-read delivers the
                looked-up char into.
            emit_entry: the entry the panel push-read JUMP kicks to emit the char.
            emit_hop: hops for the downstream char emit WRITE.
            out_dest: downstream destination register for the emitted char.
            read_dest / read_entry: the DEST register / JUMP entry the boundary
                read words carry. Defaults (5, 1) speak the RAW PANEL protocol
                (addr -> R5, trigger R1 — the per-block-verified drive, which
                relies on the panel's R3/R4 descriptors being pre-set). The
                AUTO-P&R template instead points them at the companion
                SramController cell's ``data`` register + ``lookup`` entry, so
                every read carries its OWN R3/R4 descriptors (the shared-panel
                / preloaded-.kyt protocol — no host pre-set needed).
            read_wr_desc / read_jp_desc: the companion controller's push-read
                descriptors (only used on the template's ctl-lookup path).
            emit_jump_entry: when set, the emit cell's char WRITE is followed by
                ``JUMP @emit_hop, emit_jump_entry`` — required when the egress
                rides a relay (the CrossoverBlock) that only fires on a JUMP.
                Default None keeps the WRITE-only port-capture behaviour.
        """
        super().__init__(name, panel_hop=panel_hop, read_addr_hop=read_addr_hop,
                         char_dest=char_dest, emit_entry=emit_entry,
                         emit_hop=emit_hop, out_dest=out_dest,
                         read_dest=read_dest, read_entry=read_entry,
                         read_wr_desc=read_wr_desc, read_jp_desc=read_jp_desc,
                         emit_jump_entry=emit_jump_entry)
        self._panel_hop = panel_hop
        self._read_hop = read_addr_hop
        self._char_dest = char_dest
        self._emit_entry = emit_entry
        self._emit_hop = emit_hop
        self._out_dest = out_dest
        self._read_dest = int(read_dest)
        self._read_entry = int(read_entry)
        self._read_wr_desc = int(read_wr_desc) & 0xFFFF
        self._read_jp_desc = int(read_jp_desc) & 0xFFFF
        self._emit_jump_entry = (None if emit_jump_entry is None
                                 else int(emit_jump_entry))
        self._image = sram_reverse_image()
        # Runtime state for the golden reference (streaming).
        self._cur = ""
        self._zeros = 0

    @property
    def cell_count(self) -> int:
        # Three cells: the accumulate state-machine cell + the push-read emit cell
        # + the SRAM controller (load phase). The accumulate logic fits ONE 32-word
        # cell (24 instr); emit is split off so the accumulator does not overrun.
        return 3

    @property
    def interface(self) -> BlockInterface:
        return self._interface

    @property
    def sram_image(self) -> Dict[int, int]:
        """The sparse reverse-map panel image (codeword_int -> ASCII char)."""
        return dict(self._image)

    def panel_requirements(self) -> dict:
        """This block needs an SRAM panel holding the sparse 1024-address
        reverse map (codeword integer -> char + CHAR_OFFSET).

        RX-tail topology (the panel block CONSUMES the chain): cell 2 (the
        embedded SramController) sits AT the panel's x1_out port and relays
        every boundary lookup with its OWN R3/R4 descriptors; cell 0 (the
        bit-accumulator, ``input_cell``) sits ADJACENT to it and receives the
        chain's bit stream; the panel push-reads each looked-up char into cell
        1's ``char`` register via x1_in (the return corridor)."""
        return {
            "label": "Varicode reverse map (1024, sparse)",
            "image": dict(self._image),
            "controller_cell": 2,
            "input_cell": 0,
            "return_port": "char",
            "return_cell": 1,
        }

    @staticmethod
    def reverse_map_size() -> int:
        """The number of ADDRESSES a direct-indexed reverse LUT spans: the next
        power of two above the largest codeword integer value (955) -> 1024.
        (Historical: the number the 21-entry single-cell ceiling was compared
        against; now the panel holds it.)"""
        maxv = max(int(pat, 2) for pat in VARICODE)
        return 1 << maxv.bit_length()

    def build_cell_programs(self) -> Dict[int, CellProgram]:
        """The SRAM-backed accumulate/emit cell (cell 0) + the SRAM controller
        (cell 1, for the load phase).

        Cell 0 runs the bit-accumulator + "00"-delimiter state machine. Per input
        bit (delivered to R{in:bit}, entry ``accumulate``):

          bit = R{in:bit} & 1
          if bit == 1:
              if pend0: cur = cur<<1 (commit pending 0); pend0 = 0
              cur = (cur<<1) | 1
          else:  # bit == 0
              if pend0:                       # "00" boundary
                  if cur != 0:                # lookup + emit + reset
                      WRITE cur -> panel R5 ; JUMP -> panel R1 (read trigger)
                      cur = 0
                  pend0 = 0
              elif cur != 0:
                  pend0 = 1
              # else leading idle zero: skip

        The panel push-read (descriptors R3/R4 pre-pointed here) delivers the
        looked-up ASCII char into R{char} and kicks entry ``emit``, which writes
        the char downstream. The read-out descriptors are set once at load time
        (mirrors the SRAM encoder's per-symbol push-read); this cell only forms the
        ADDRESS and pulls the read trigger.
        """
        rh = self._read_hop
        eh = self._emit_hop
        # Cell 0 — accumulate. R0 is scratch; state cur/pend0 persist across
        # triggers. On a "00" boundary it forms `cur` into the panel read ADDRESS
        # and pulls the read trigger; the panel push-read delivers the char to the
        # SEPARATE emit cell (cell 1) + kicks it. Fits ONE 32-word cell (24 instr).
        accum_tmpl = (
            "accumulate:\n"
            # bit = R{in:bit} & 1  (mask the LSB; AND sets Z)
            "    MOVE R0, R{in:bit}\n"
            "    AND R0, R{data:one}\n"
            "    BR.NZ on_one\n"               # bit == 1 -> the accumulate arm
            # --- bit == 0 ---
            "    CMP R{state:pend0}, R{data:zero}\n"
            "    BR.NZ boundary\n"
            # pend0 == 0: first zero after code bits -> pend0 = 1 IFF cur != 0
            "    CMP R{state:cur}, R{data:zero}\n"
            "    BR.Z end\n"                    # leading idle zero (cur == 0): skip
            "    MOVE R{state:pend0}, R{data:one}\n"
            "    GOTO end\n"
            # pend0 == 1: "00" boundary -> if cur != 0, SRAM read (lookup + emit)
            "boundary:\n"
            "    MOVE R{state:pend0}, R{data:zero}\n"
            "    CMP R{state:cur}, R{data:zero}\n"
            "    BR.Z end\n"                    # empty codeword -> nothing to look up
            "    MOVE R0, R{state:cur}\n"       # R0 == cur is the read ADDRESS
            "    MOVE R{state:cur}, R{data:zero}\n"   # reset accumulator
            # Read words: raw-panel (dest 5 / entry 1, the default) OR the
            # companion controller's data reg / 'lookup' entry (template mode —
            # the controller then writes its own R3/R4 per read).
            f"    WRITE @{rh}, {self._read_dest}\n"
            f"    JUMP @{rh}, {self._read_entry}\n"
            "    GOTO end\n"
            # --- bit == 1: cur = (cur << (1 + pend0)) | 1 ; pend0 = 0 ---
            # A pending intra-code '0' is committed by ONE extra left shift.
            # Shift counts are immediate instruction fields, so the conditional
            # shift is computed ARITHMETICALLY: cur << pend0 == cur + cur*pend0
            # (pend0 is 0/1) — a branchless MUL/ADD pair, then the '1' slot.
            # Placed LAST so it falls through into the shared `end` HALT.
            "on_one:\n"
            "    MUL R{state:cur}, R{state:pend0}\n"   # R0 = cur*pend0
            "    ADD R0, R{state:cur}\n"               # R0 = cur << pend0
            "    SHL R0, #1\n"                         # the '1' bit's slot
            "    OR R0, R{data:one}\n"
            "    MOVE R{state:cur}, R0\n"
            "    MOVE R{state:pend0}, R{data:zero}\n"
            "end:\n"
            "    HALT\n"
        )
        accum_cell = CellProgram(
            inputs=[Port("bit")],
            outputs=[Port("out")],
            entries=[EntryPoint("accumulate")],
            data=[DataWord("one", 1, address=1),
                  DataWord("zero", 0, address=2)],
            state=[StateVar("cur"), StateVar("pend0")],
            assembly_template=accum_tmpl,
        )
        # Cell 1 — emit. The panel push-read (descriptors R3/R4 pre-pointed here)
        # lands the looked-up word (char + CHAR_OFFSET) in R{char} and kicks entry
        # `emit`, which subtracts CHAR_OFFSET and writes the char downstream.
        # Mirrors the SRAM encoder's push-read consumer cell.
        emit_tmpl = (
            "emit:\n"
            "    MOVE R0, R{in:char}\n"
            "    SUB R0, R{data:offset}\n"        # word - CHAR_OFFSET == ASCII char
            f"    WRITE @{eh}, {self._out_dest}\n"
            + (f"    JUMP @{eh}, {self._emit_jump_entry}\n"
               if self._emit_jump_entry is not None else "")
            + "    HALT\n"
        )
        emit_cell = CellProgram(
            inputs=[Port("char")],
            outputs=[Port("out")],
            entries=[EntryPoint("emit")],
            data=[DataWord("offset", CHAR_OFFSET, address=1)],
            assembly_template=emit_tmpl,
        )
        # Cell 2: the SRAM controller macro. LOAD phase (streamed reverse-map
        # writes) AND — on the template path — the per-boundary LOOKUP relay:
        # the accumulate cell hands `cur` to this cell's 'lookup' entry, which
        # writes its OWN R3/R4 push-read descriptors before every read (the
        # shared-panel-safe protocol; descriptors come from this block's
        # read_wr_desc/read_jp_desc params).
        from .sram_controller_block import SramControllerBlock
        ctl = SramControllerBlock(self.name + "_ctl", panel_hop=self._panel_hop,
                                  read_wr_desc=self._read_wr_desc,
                                  read_jp_desc=self._read_jp_desc)
        ctl_cell = ctl.build_cell_programs()[0]
        return {0: accum_cell, 1: emit_cell, 2: ctl_cell}

    def process_reference(self, input_bits) -> np.ndarray:
        """Golden reference: decode a bit stream to the emitted ASCII codes.

        Stateful across calls (the bit-accumulator persists) until :meth:`reset`.
        Returns an ``int16`` array of emitted ASCII codes (one per completed
        codeword). This is the exact function :func:`varicode_decode_bits`
        computes, exposed as the block's int reference for the gate.
        """
        rev = {pat: chr(i) for i, pat in enumerate(VARICODE)}
        out: List[int] = []
        cur = self._cur
        pend0 = bool(self._zeros)
        for raw in np.asarray(input_bits).reshape(-1):
            b = str(int(raw))
            if b == "0":
                if pend0:
                    if cur:
                        ch = rev.get(cur)
                        if ch is not None:
                            out.append(ord(ch))
                        cur = ""
                    pend0 = False
                elif cur:
                    pend0 = True
            else:
                if pend0:
                    cur += "0"
                    pend0 = False
                cur += "1"
        self._cur = cur
        self._zeros = 1 if pend0 else 0
        return np.asarray(out, dtype=np.int16)

    def reset(self):
        """Reset the bit-accumulator state (cold start: empty codeword)."""
        self._cur = ""
        self._zeros = 0


# ---------------------------------------------------------------------------
# Documented reference: a SUBSET reverse LUT. Retained (the quarantine gate + the
# concrete artifact that quantified the former wall). NOT used to ship the block —
# the FULL 1024-address reverse map now lives in the SRAM panel.
# ---------------------------------------------------------------------------
def subset_reverse_lut(chars: str) -> Tuple[Dict[int, int], int]:
    """Return ``(lut, size)`` for a direct-indexed reverse LUT over ``chars``.

    ``lut`` maps codeword-int -> ASCII code; ``size`` is the direct-index table
    size (max codeword value + 1). Historically used to prove even a lowercase+space
    subset overruns the 21-entry single-cell ceiling (now moot: the panel holds the
    full map).
    """
    lut: Dict[int, int] = {}
    maxv = 0
    for ch in chars:
        v = int(VARICODE[ord(ch)], 2)
        lut[v] = ord(ch)
        maxv = max(maxv, v)
    return lut, (maxv + 1)
