# SPDX-License-Identifier: GPL-3.0-or-later
"""HammingDecoderBlock — see :class:`HammingDecoderBlock`."""
import numpy as np
from typing import Any, Dict, List, Tuple

from ..block import CellProgram, Port, EntryPoint, StateVar, DataWord
from ._base import KyttarBlock, BlockInterface


class HammingDecoderBlock(KyttarBlock):
    """
    Systematic Hamming(7,4) hard-decision FEC DECODER (2 cells).

    Consumes 7 received bits (one 0/1 word per sample, LSB used — the
    Pack/Unpack bit-stream convention), computes the 3-bit syndrome, corrects
    any SINGLE bit error via an 8-entry syndrome→flip-position LUT, and emits
    the 4 corrected data bits MSB-first (rate-REDUCING 7:4). No stock GNU
    Radio counterpart exists (gr-fec has no plain Hamming(7,4) factory); the
    golden reference is the standard textbook syndrome decoder (R. W. Hamming,
    "Error Detecting and Error Correcting Codes", Bell Syst. Tech. J. 29(2),
    1950; Lin & Costello, "Error Control Coding", 2nd ed., §3.3 syndrome
    decoding).

    THE CONVENTION (pinned; the sibling ``HammingEncoderBlock`` uses the SAME)
    ========================================================================
    Systematic codeword layout MSB-first on the wire::

        c = d3 d2 d1 d0 p2 p1 p0        (d3 is the FIRST bit on the wire)

    with the data nibble MSB-first (d3 first) and EVEN parity::

        p2 = d3 ^ d2 ^ d1
        p1 = d3 ^ d2 ^ d0
        p0 = d3 ^ d1 ^ d0

    The parity-check matrix H follows directly (syndrome s = s2 s1 s0, each
    ``sK`` = the received ``pK`` XORed with its recomputed parity)::

        s2 = p2 ^ d3 ^ d2 ^ d1
        s1 = p1 ^ d3 ^ d2 ^ d0
        s0 = p0 ^ d3 ^ d1 ^ d0

    so each codeword bit participates in the syndrome with a fixed 3-bit
    COLUMN (H's columns), all distinct and non-zero — the single-error-
    correcting property::

        bit :  d3  d2  d1  d0  p2  p1  p0     (wire order, MSB first)
        col :   7   6   5   3   4   2   1     (s2 s1 s0 as a 3-bit value)

    and the syndrome→flip-position LUT (which received bit to flip; bit 6 =
    d3 … bit 0 = p0; syndrome 0 = no error)::

        s    : 0  1  2  3  4  5   6   7
        flip : 0  1  2  8  4  16  32  64      (single-bit masks over c)

    Decode = flip the indicated bit, then emit the corrected d3 d2 d1 d0
    (bits 6..3 of the corrected word), MSB-first. All 7 single-bit errors are
    corrected exactly; DOUBLE-bit errors are UNCORRECTABLE (Hamming distance
    3) — the non-zero syndrome mis-corrects a third bit, the standard,
    documented limit of every (7,4) Hamming code.

    On-chip architecture (2 cells, linear pipeline)
    ===============================================
    ``front`` (landing cell) — a FUSED bit-packer + syndrome accumulator. A
    single 16-bit register carries BOTH the packing word and the running
    syndrome, via pre-shifted column constants: processing bit j (j = 0..6),

        reg' = (reg << 1) ^ bit * T[j],   T[j] = (col[j] << (2 + j)) | 1

    The ``| 1`` lands the data bit itself at bit 0 (packing), and the
    pre-shifted column lands at bits [2+j, 4+j]; after the remaining ``6-j``
    left-shifts every column contribution aligns at bits [8, 10] where the
    XORs accumulate the syndrome. In-flight, a column contribution at step k
    occupies bits [2+k, 4+k] while the packed word occupies bits [0, k-1] —
    provably disjoint, so the fused register never cross-contaminates. After
    7 bits: ``reg & 0x7F`` = the received word, ``reg >> 8`` = the syndrome.
    The 7-entry T table is LOAD-indirect indexed by the down-counting bit
    counter (count 7..1 doubles as the table address — INV-33-safe: the
    input lands at R0, the table sits at addresses 1..7, state above it).

    ``fix`` (output cell) — receives the fused register as ONE operand,
    looks up ``flip[s]`` in the 8-entry LUT (addresses 1..8; the input is
    pinned at R0, OUTSIDE the table range — the QAM16-mapper table-aliasing
    trap), XORs the correction, masks the data-nibble window (``& 0x78``,
    bits 6..3) and emits the 4 data bits MSB-first as a counted-loop burst
    (the UnpackKBits emit idiom).

    ``front`` deliberately does NOT clear the packed-word bits between
    groups (register budget); stale group bits climb into bits 7+ of ``reg``
    but every read of them is masked: the syndrome window read (``>> 8``)
    only ever sees XOR-accumulated columns because the syndrome bits are
    cleared each group, and the data window is masked ``& 0x78``. The
    syndrome/count state IS reset every group.

    Streaming: like GNU Radio's pack_k_bits, a trailing partial group of
    fewer than 7 bits is never emitted.

    Interface:
        - Entry: R1
        - Input: R0 (one received bit per trigger, LSB used)
        - Output: R0 (4 corrected data bits MSB-first per 7 input bits)
    """
    CATEGORY = "fec"
    TAGS = ["hamming", "fec", "decoder", "syndrome", "block code", "coding"]

    _interface = BlockInterface(entry_address=1, input_registers=[0],
                                output_registers=[0])

    # H columns in wire order (d3 d2 d1 d0 p2 p1 p0) — derived VERBATIM from
    # p2=d3^d2^d1, p1=d3^d2^d0, p0=d3^d1^d0 (the pinned convention).
    COLS = (7, 6, 5, 3, 4, 2, 1)
    # syndrome -> single-bit flip mask over the 7-bit codeword (bit6=d3..bit0=p0)
    FLIP_LUT = (0, 1, 2, 8, 4, 16, 32, 64)

    def __init__(self, name: str):
        """Hamming(7,4) hard-decision syndrome decoder (no parameters — the
        code, the systematic layout and the bit order are the pinned
        convention above; there is no GNU Radio counterpart whose parameter
        set could be mirrored)."""
        super().__init__(name)

    @property
    def cell_count(self) -> int:
        return 2

    @property
    def interface(self) -> BlockInterface:
        return self._interface

    def build_cell_programs(self) -> Dict[Any, CellProgram]:
        # Fused pack+syndrome constants: T[j] = (col[j] << (2+j)) | 1, stored at
        # address 7-j so the DOWN-counting bit counter (7..1) IS the LOAD address.
        tvals = [(self.COLS[j] << (2 + j)) | 1 for j in range(7)]  # j = 0..6
        # tvals = [29, 49, 81, 97, 257, 257, 257]
        front = CellProgram(
            inputs=[Port("sample", register=0)],
            outputs=[Port("comb"), Port("go")],
            entries=[EntryPoint("default")],
            data=[
                # T table at addresses 1..7 (addr = count = 7-j). Two entries
                # double as the scalars the program needs (the INV-19 merge-
                # identical-DataWords trick): t0 = 29 is only a table entry, but
                # the constants 1 and 7 are NOT in the table, so they get their
                # own words at 8/9.
                DataWord("t6", tvals[6], address=1),   # j=6 (p0): 257
                DataWord("t5", tvals[5], address=2),   # j=5 (p1): 257
                DataWord("t4", tvals[4], address=3),   # j=4 (p2): 257
                DataWord("t3", tvals[3], address=4),   # j=3 (d0): 97
                DataWord("t2", tvals[2], address=5),   # j=2 (d1): 81
                DataWord("t1", tvals[1], address=6),   # j=1 (d2): 49
                DataWord("t0", tvals[0], address=7),   # j=0 (d3): 29
                DataWord("one", 1, address=8),
                DataWord("seven", 7, address=9),
            ],
            state=[
                StateVar("bit"),                      # the masked input bit
                StateVar("reg"),                      # fused word+syndrome accumulator
                StateVar("count", initial_value=7),   # down-counter = T address
            ],
            assembly_template="""\
start:
    AND R{in:sample}, R{data:one}      ; R0 = bit (input LSB; input read ONCE)
    MOVE R{state:bit}, R0
    SHL R{state:reg}, #1               ; R0 = reg << 1
    MOVE R{state:reg}, R0
    LOAD R{state:count}                ; R0 = T[7-count] (count = 7..1)
    MUL R0, R{state:bit}               ; R0 = bit ? T : 0
    XOR R0, R{state:reg}               ; reg' = (reg<<1) ^ bit*T
    MOVE R{state:reg}, R0
    SUB R{state:count}, R{data:one}    ; R0 = count-1; Z flag on the 7th bit
    MOVE R{state:count}, R0            ; (MOVE preserves flags)
    BR.NZ done
    ; --- group complete: word in reg[6:0], syndrome in reg[10:8] ---
    MOVE R0, R{state:reg}
    ; fused register -> fix cell (one operand), then the trigger:
    {write:comb}
    {jump:go}
    XOR R{state:reg}, R{state:reg}     ; R0 = 0 (clears the syndrome window;
    MOVE R{state:reg}, R0              ;  the word bits are masked downstream)
    MOVE R{state:count}, R{data:seven}
done:
""",
        )

        # fix: fused reg @R0 -> LUT-correct -> 4-bit MSB-first burst. The 8-entry
        # flip LUT sits at addresses 1..8; the input is pinned at R0, OUTSIDE the
        # table range (the table-aliasing trap: an input/state register inside a
        # LOAD table silently corrupts exactly the colliding index). Three LUT
        # entries double as the program's scalars: flip[1]=1 ("one", also the
        # table base offset), flip[4]=4 ("four", the emit counter seed).
        flip = self.FLIP_LUT
        fix = CellProgram(
            inputs=[Port("comb", register=0)],
            outputs=[Port("out")],
            entries=[EntryPoint("default")],
            data=[
                DataWord("f0", flip[0], address=1),    # s=0: no error
                DataWord("one", flip[1], address=2),   # s=1: flip p0 (=1; dual-use scalar)
                DataWord("f2", flip[2], address=3),    # s=2: flip p1
                DataWord("f3", flip[3], address=4),    # s=3: flip d0
                DataWord("four", flip[4], address=5),  # s=4: flip p2 (=4; dual-use scalar)
                DataWord("f5", flip[5], address=6),    # s=5: flip d1
                DataWord("f6", flip[6], address=7),    # s=6: flip d2
                DataWord("f7", flip[7], address=8),    # s=7: flip d3
                DataWord("mask78", 0x78, address=9),   # data-nibble window (bits 6..3)
            ],
            state=[
                StateVar("w"),    # fused input, then the sliding emit window
                StateVar("cnt"),  # LUT address scratch, then the emit counter
            ],
            assembly_template="""\
start:
    MOVE R{state:w}, R{in:comb}        ; save the fused register (input read ONCE)
    SHR R{state:w}, #8                 ; R0 = syndrome s (reg <= 0x7FF: exact)
    ADD R0, R{data:one}                ; LUT address = 1 + s (table at 1..8)
    MOVE R{state:cnt}, R0
    LOAD R{state:cnt}                  ; R0 = flip[s]
    XOR R0, R{state:w}                 ; corrected word (low 7; high bits stale)
    AND R0, R{data:mask78}             ; d3 d2 d1 d0 window at bits 6..3
    MOVE R{state:w}, R0
    MOVE R{state:cnt}, R{data:four}    ; 4 data bits to emit
loop:
    SHR R{state:w}, #6                 ; R0 = window MSB (the next data bit)
    {write:out}
    {jump:out}
    SHL R{state:w}, #1                 ; slide the window up ...
    AND R0, R{data:mask78}             ; ... keeping bits 6..3 only
    MOVE R{state:w}, R0
    SUB R{state:cnt}, R{data:one}      ; count down; Z after the 4th bit
    MOVE R{state:cnt}, R0
    BR.NZ loop
""",
        )
        return {"front": front, "fix": fix}

    def internal_connections(self) -> List[Tuple[str, str, str, str]]:
        return [("front", "comb", "fix", "comb")]

    def internal_jumps(self) -> List[Tuple[str, str, str, str]]:
        return [("front", "go", "fix", "default")]

    def output_cell_id(self) -> Any:
        return "fix"

    def default_layout(self) -> Dict[Any, Tuple[int, int, str]]:
        # 2x1 fold: input (front) and output (fix) side by side on one
        # bus-facing edge (INV-8/14) — the QAM16Slicer row layout.
        return {"front": (0, 0, "east"), "fix": (1, 0, "east")}

    # ------------------------------------------------------------- reference
    @classmethod
    def syndrome_of(cls, word7: int) -> int:
        """The 3-bit syndrome of a received 7-bit word (bit6=d3 .. bit0=p0),
        from the pinned H columns."""
        s = 0
        for j in range(7):                      # j=0 -> d3 (bit 6) ... j=6 -> p0
            if (word7 >> (6 - j)) & 1:
                s ^= cls.COLS[j]
        return s

    @classmethod
    def decode_word(cls, word7: int) -> int:
        """Standard syndrome decode of one 7-bit word -> the 4-bit data nibble
        (d3 d2 d1 d0, MSB-first). Corrects any single-bit error; a double-bit
        error yields a wrong nibble (uncorrectable, distance-3 limit)."""
        w = int(word7) & 0x7F
        c = w ^ cls.FLIP_LUT[cls.syndrome_of(w)]
        return (c >> 3) & 0xF

    def process_reference(self, input_bits) -> np.ndarray:
        """Golden: group the bit stream into 7-bit words (MSB-first, LSB of
        each input item — the GR pack_k_bits convention), syndrome-decode each,
        and emit the 4 corrected data bits MSB-first. A trailing partial group
        is dropped. Raw words, not Q15."""
        bits = [int(b) & 1 for b in np.asarray(input_bits).reshape(-1)]
        out: list[int] = []
        for g in range(len(bits) // 7):
            w = 0
            for b in bits[g * 7:(g + 1) * 7]:
                w = ((w << 1) | b) & 0x7F
            nib = self.decode_word(w)
            out.extend((nib >> k) & 1 for k in (3, 2, 1, 0))
        return np.asarray(out, dtype=np.int16)

    def reset(self):
        """No cross-call Python-side state (each reference call is a fresh stream)."""
        pass
