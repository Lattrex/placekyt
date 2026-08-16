# SPDX-License-Identifier: GPL-3.0-or-later
"""HammingEncoderBlock — see the class docstring."""
import numpy as np

from ..block import CellProgram, DataWord, EntryPoint, Port, StateVar
from ._base import BlockInterface, KyttarBlock


class HammingEncoderBlock(KyttarBlock):
    """Systematic Hamming(7,4) hard-decision FEC ENCODER (no GNU Radio
    counterpart — gr-fec has no plain Hamming(7,4) factory).

    THE CONVENTION PIN (shared VERBATIM with HammingDecoderBlock — both sides
    derive from this exact statement; state it LOUDLY):

        systematic codeword layout MSB-first on the wire = d3 d2 d1 d0 p2 p1 p0,
        where the data nibble arrives MSB-first (d3 first), and parity bits are
        p2 = d3^d2^d1, p1 = d3^d2^d0, p0 = d3^d1^d0 (even parity).

    Bit stream in, bit stream out (one 0/1 byte per sample, the Pack/Unpack
    convention; only the LSB of each input word is read, ``& 1``, exactly like
    GR ``pack_k_bits_bb``). The block consumes 4 data bits — the FIRST arriving
    bit is d3, the most significant — and emits the 7-bit systematic codeword
    MSB-first: d3, d2, d1, d0, p2, p1, p0. Rate-EXPANDING 4:7. Like GR's
    ``pack_k_bits_bb``, a trailing partial group of fewer than 4 bits at the end
    of a stream is NOT emitted.

    The generator matrix (R. W. Hamming, "Error Detecting and Error Correcting
    Codes", Bell System Technical Journal 29(2), 1950; the standard systematic
    form G = [I4 | P] found in any coding-theory text, e.g. Lin & Costello,
    "Error Control Coding"). With the message row vector m = [d3 d2 d1 d0] the
    codeword is c = m · G (mod 2), MSB-first left to right:

              d3 d2 d1 d0 p2 p1 p0
        G = [  1  0  0  0  1  1  1 ]   (row for d3)
            [  0  1  0  0  1  1  0 ]   (row for d2)
            [  0  0  1  0  1  0  1 ]   (row for d1)
            [  0  0  0  1  0  1  1 ]   (row for d0)

    i.e. exactly p2 = d3^d2^d1, p1 = d3^d2^d0, p0 = d3^d1^d0 (even parity).
    The code has minimum distance 3: any single bit error is correctable by the
    matching syndrome decoder (HammingDecoderBlock).

    Datapath (TWO cells, feed-forward chain — a straight-line 2x1 fold with I/O
    co-located on the bus edge per INV-8/14; output egresses the LAST cell per
    INV-10):

    Cell ``pack`` (input cell — the PackKBitsBlock k=4 idiom + p2):
      1. bit = sample & 1;  word = (word << 1) | bit  (MSB-first accumulate).
      2. Counter counts DOWN from 4 (StateVar initial_value=4); on the 4th bit:
         word <<= 3 (the nibble now sits at bits 6..3), attach p2 via the P
         (parity) flag of ``word & 0x70`` (bits d3 d2 d1 — P is the XOR of all
         result bits, so P == d3^d2^d1 == p2; BR.NP skips the OR when even),
         forward the partial codeword to ``expand``, and reset word/count.

    Cell ``expand`` (output cell — attach p1, p0, then the UnpackKBitsBlock
    counted-loop burst emit):
      3. p1 = P flag of (w & 0x68) (bits d3 d2 d0); p0 = P flag of (w & 0x58)
         (bits d3 d1 d0). The masks address the SHIFTED data-bit positions
         (bits 6..3) and never overlap the parity-bit positions (2..0), so
         already-attached parity bits cannot contaminate a later parity.
      4. Emit the 7 codeword bits MSB-first via a counted loop (SHR #6 peels
         the MSB of the 7-bit window, AND 1 isolates it — garbage above bit 6
         is masked off by the AND, so no per-iteration window mask is needed;
         SHL #1 advances). Seven WRITE+JUMP pairs per trigger — the burst-emit
         primitive (a remote JUMP does not halt the issuing cell); the
         single-outstanding output port paces the burst (INV-20 checked: a
         straight 2-cell feed-forward chain has no reconvergent fan-in and no
         feedback corridor, so no serialize-LOCK is needed; gated saturated in
         test_pipeline_saturation RATE_1IN).

    Raw-word bit streams in and out (0/1 words), NOT Q15 — the comparison is
    BIT-EXACT (metric DECISION, tolerance 0).

    Parameters: none — Hamming(7,4) is a fixed code (n=7, k=4 are structural,
    not tunable knobs; a different code length is a different block).
    """
    CATEGORY = "fec"
    TAGS = ["hamming", "fec", "encoder", "block_code", "data_link"]

    _interface = BlockInterface(
        entry_address=1, input_registers=[0], output_registers=[0])

    # The convention pin, importable by tests and by HammingDecoderBlock's
    # verification: codeword bit layout MSB-first and the parity equations.
    CODEWORD_LAYOUT = "d3 d2 d1 d0 p2 p1 p0"  # MSB-first on the wire
    # parity masks over the nibble N = (d3 d2 d1 d0):
    #   p2 = parity(N & 0b1110), p1 = parity(N & 0b1101), p0 = parity(N & 0b1011)
    P2_NIBBLE_MASK = 0b1110
    P1_NIBBLE_MASK = 0b1101
    P0_NIBBLE_MASK = 0b1011

    def __init__(self, name: str):
        super().__init__(name)

    @property
    def cell_count(self) -> int:
        return 2

    @property
    def interface(self) -> BlockInterface:
        return self._interface

    # ------------------------------------------------------------------ build
    def build_cell_programs(self) -> dict:
        cells = {}

        # (1) pack — accumulate 4 bits MSB-first; on the 4th, shift the nibble
        # to bits 6..3, attach p2 (P flag of word & 0x70), forward to expand.
        # The DataWord ``four`` doubles as the p2 bit value (1 << 2 == 4) and
        # the counter reload — one word, two uses (the INV-19 merge trick).
        cells["pack"] = CellProgram(
            inputs=[Port("sample", register=0)],
            outputs=[Port("cw"), Port("trig")],
            entries=[EntryPoint("default")],
            data=[
                DataWord("one", 1, address=1),
                DataWord("four", 4, address=2),      # p2 bit AND count reload
                DataWord("m_p2", 0x70, address=3),   # d3 d2 d1 at bits 6..4
                DataWord("zero", 0, address=4),
            ],
            state=[
                StateVar("bit", register=5),
                StateVar("word", register=6),
                StateVar("count", register=7, initial_value=4),
            ],
            assembly_template="""\
start:
    ; bit = sample & 1 (GR pack convention: only the input LSB is data)
    AND R{in:sample}, R{data:one}
    MOVE R{state:bit}, R0
    ; word = (word << 1) | bit   (MSB-first accumulate: first bit -> d3)
    SHL R{state:word}, #1
    OR R0, R{state:bit}
    MOVE R{state:word}, R0
    ; count down; only every 4th bit emits
    SUB R{state:count}, R{data:one}
    MOVE R{state:count}, R0
    BR.NZ done
    ; word <<= 3: nibble d3 d2 d1 d0 now at bits 6..3 (codeword frame)
    SHL R{state:word}, #3
    MOVE R{state:word}, R0
    ; p2 = d3^d2^d1 == P flag of (word & 0x70); BR.NP skips when parity even
    AND R{state:word}, R{data:m_p2}
    BR.NP _send
    OR R{state:word}, R{data:four}
    MOVE R{state:word}, R0
_send:
    MOVE R0, R{state:word}
    {write:cw}
    {jump:trig}
    ; reset for the next nibble
    MOVE R{state:word}, R{data:zero}
    MOVE R{state:count}, R{data:four}
done:
    HALT
""",
        )

        # (2) expand — attach p1 (mask 0x68 = d3 d2 d0) and p0 (mask 0x58 =
        # d3 d1 d0), then emit the 7 bits MSB-first with a counted loop. The
        # masks only cover bits 6..3, so p2 (bit 2) / p1 (bit 1) never feed a
        # later parity. The backward BR.NZ is separated from {jump:trig} by the
        # shift/advance instructions (the UnpackKBits lesson).
        cells["expand"] = CellProgram(
            inputs=[Port("cw", register=0)],
            outputs=[Port("out"), Port("trig")],
            entries=[EntryPoint("default")],
            data=[
                DataWord("one", 1, address=1),
                DataWord("two", 2, address=2),       # p1 bit
                DataWord("seven", 7, address=3),     # burst length
                DataWord("m_p1", 0x68, address=4),   # d3 d2 d0 at bits 6,5,3
                DataWord("m_p0", 0x58, address=5),   # d3 d1 d0 at bits 6,4,3
            ],
            state=[
                StateVar("w", register=6),
                StateVar("cnt", register=7),
            ],
            assembly_template="""\
start:
    ; copy the incoming partial codeword before any ALU op clobbers R0 (INV-33)
    MOVE R{state:w}, R{in:cw}
    ; p1 = d3^d2^d0 == P flag of (w & 0x68)
    AND R{state:w}, R{data:m_p1}
    BR.NP _p0
    OR R{state:w}, R{data:two}
    MOVE R{state:w}, R0
_p0:
    ; p0 = d3^d1^d0 == P flag of (w & 0x58)
    AND R{state:w}, R{data:m_p0}
    BR.NP _emit
    OR R{state:w}, R{data:one}
    MOVE R{state:w}, R0
_emit:
    ; emit 7 bits MSB-first — counted-loop burst (UnpackKBits pattern)
    MOVE R{state:cnt}, R{data:seven}
loop:
    SHR R{state:w}, #6
    AND R0, R{data:one}
    {write:out}
    {jump:trig}
    SHL R{state:w}, #1
    MOVE R{state:w}, R0
    SUB R{state:cnt}, R{data:one}
    MOVE R{state:cnt}, R0
    BR.NZ loop
    HALT
""",
        )
        return cells

    # ------------------------------------------------------- multi-cell wiring
    def _chain(self):
        return ["pack", "expand"]

    def internal_connections(self):
        return [("pack", "cw", "expand", "cw")]

    def internal_jumps(self):
        return [("pack", "trig", "expand", "default")]

    def output_cell_ids(self):
        return ["expand"]

    def default_layout(self):
        # 2x1 even-column fold: input (pack) and output (expand) co-located on
        # the same bus-facing edge (INV-8/14).
        return {"pack": (0, 0, "east"), "expand": (1, 0, "east")}

    # -------------------------------------------------------------- reference
    @classmethod
    def encode_nibble(cls, nibble: int) -> list:
        """The golden per-nibble encoder — the convention pin in executable
        form. ``nibble`` = (d3<<3)|(d2<<2)|(d1<<1)|d0. Returns the 7 codeword
        bits MSB-first: [d3, d2, d1, d0, p2, p1, p0] with
        p2 = d3^d2^d1, p1 = d3^d2^d0, p0 = d3^d1^d0 (even parity)."""
        n = int(nibble) & 0xF
        d3, d2, d1, d0 = (n >> 3) & 1, (n >> 2) & 1, (n >> 1) & 1, n & 1
        p2 = d3 ^ d2 ^ d1
        p1 = d3 ^ d2 ^ d0
        p0 = d3 ^ d1 ^ d0
        return [d3, d2, d1, d0, p2, p1, p0]

    def process_reference_q15(self, x_q15) -> list:
        """Bit-exact reference: group input words 4 at a time (LSB of each word
        is the data bit, first bit = d3), emit 7 codeword bits per group
        MSB-first. A trailing partial group (< 4 bits) is not emitted."""
        bits = [int(w) & 1 for w in x_q15]
        out = []
        for j in range(len(bits) // 4):
            d3, d2, d1, d0 = bits[4 * j: 4 * j + 4]
            nib = (d3 << 3) | (d2 << 2) | (d1 << 1) | d0
            out.extend(self.encode_nibble(nib))
        return out

    def process_reference(self, input_samples) -> np.ndarray:
        """Float reference (0.0/1.0 bits): same grouping as
        :meth:`process_reference_q15`."""
        words = [int(round(float(v))) & 0xFFFF for v in input_samples]
        return np.asarray(self.process_reference_q15(words), dtype=np.float32)

    def reset(self):
        """No cross-call host state (each reference call is a fresh stream)."""
        pass
