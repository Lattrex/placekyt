# SPDX-License-Identifier: GPL-3.0-or-later
"""GolayEncoderBlock — see the class docstring."""
import numpy as np

from ..block import CellProgram, DataWord, EntryPoint, Port, StateVar
from ._base import BlockInterface, KyttarBlock


class GolayEncoderBlock(KyttarBlock):
    """Extended binary Golay (24,12) systematic hard-decision FEC ENCODER
    (no GNU Radio counterpart — gr-fec has no Golay factory).

    THE CONVENTION PIN (shared VERBATIM with GolayDecoderBlock — both sides
    derive from this exact statement; state it LOUDLY):

        codeword layout MSB-first on the wire = d11 d10 .. d0 p11 p10 .. p0,
        where the 12 data bits arrive MSB-first (the FIRST arriving bit is
        d11), and with the message row vector m = [d11 d10 .. d0] the parity
        bits are p11..p0 = m . B (mod 2), column 0 of B producing p11 (the
        FIRST parity bit on the wire) through column 11 producing p0.

    Bit stream in, bit stream out (one 0/1 word per sample, the Pack/Unpack
    convention; only the LSB of each input word is read, ``& 1``, exactly like
    GR ``pack_k_bits_bb``). The block consumes 12 data bits and emits the
    24-bit systematic codeword MSB-first. Rate-EXPANDING 12:24. Like GR's
    ``pack_k_bits_bb``, a trailing partial group of fewer than 12 bits at the
    end of a stream is NOT emitted.

    The generator matrix is G = [I12 | B] with the STANDARD B matrix of the
    extended Golay code (F. J. MacWilliams & N. J. A. Sloane, "The Theory of
    Error-Correcting Codes", North-Holland 1977, Ch. 2 §6, the bordered
    reverse-circulant form; the same G appears in Lin & Costello, "Error
    Control Coding"). B verbatim (row i is the parity contribution of data
    bit d(11-i); B is SYMMETRIC, so rows == columns):

        B = [ 0 1 1 1 1 1 1 1 1 1 1 1 ]   (row 0, for d11)
            [ 1 1 1 0 1 1 1 0 0 0 1 0 ]   (row 1, for d10)
            [ 1 1 0 1 1 1 0 0 0 1 0 1 ]   (row 2, for d9)
            [ 1 0 1 1 1 0 0 0 1 0 1 1 ]   (row 3, for d8)
            [ 1 1 1 1 0 0 0 1 0 1 1 0 ]   (row 4, for d7)
            [ 1 1 1 0 0 0 1 0 1 1 0 1 ]   (row 5, for d6)
            [ 1 1 0 0 0 1 0 1 1 0 1 1 ]   (row 6, for d5)
            [ 1 0 0 0 1 0 1 1 0 1 1 1 ]   (row 7, for d4)
            [ 1 0 0 1 0 1 1 0 1 1 1 0 ]   (row 8, for d3)
            [ 1 0 1 0 1 1 0 1 1 1 0 0 ]   (row 9, for d2)
            [ 1 1 0 1 1 0 1 1 1 0 0 0 ]   (row 10, for d1)
            [ 1 0 1 1 0 1 1 1 0 0 0 1 ]   (row 11, for d0)

    Structure (the citable construction): B[0][0] = 0, first row and first
    column otherwise all-ones, and the 11x11 core is the reverse circulant
    core[i][j] = c[(i+j-2) mod 11] over the indicator c of {0} u QR(11) =
    {0, 1, 3, 4, 5, 9}. This B is symmetric with B.B^T = I (the code is
    self-dual) and generates the unique [24,12,8] extended binary Golay code
    (weight distribution 1/759/2576/759/1 at weights 0/8/12/16/24 — verified
    exhaustively by the test's golden self-checks). Minimum distance 8: the
    matching syndrome decoder (GolayDecoderBlock) corrects up to 3 bit errors
    per codeword.

    The executable convention pin is :meth:`encode_word` — GolayDecoderBlock
    MUST be built against it.

    Datapath (FOUR cells, feed-forward 2x2 serpentine — I/O co-located on the
    top edge per INV-8/14; output egresses the LAST cell per INV-10):

    Cell ``pack`` (input cell — the PackKBitsBlock k=12 idiom):
      1. bit = sample & 1; D = (D << 1) | bit (MSB-first accumulate: the
         first bit of a group lands at bit 11 by the 12th shift).
      2. Counter counts DOWN from 12 (StateVar initial_value=12); on the 12th
         bit forward D to ``par1`` and reload the counter. D is deliberately
         NOT reset: stale group bits climb into D[12..15] and every downstream
         read is masked (the parity masks are 12-bit values; the emit peel is
         ``SHR #11`` + ``AND 1``) — the HammingDecoder masked-read invariant,
         covered by the multi-group stream tests.

    Cells ``par1`` / ``par2`` (7 + 5 parity bits via a LOAD-table loop —
    the ASYMMETRIC split is budget-forced: par2 carries one extra entry
    instruction to copy the incoming partial parity word, so with the 30-word
    cell budget (data + state + instructions + non-R0 input registers) it can
    only hold 5 masks while par1 holds 7; both cells resolve to exactly 30):
      3. For count = N..1: p = (p << 1) | parity(D & T[count]), where T[a]
         (data words at addresses 1..N, INSIDE the LOAD 5-bit address range;
         every input register lands OUTSIDE it — the QAM16 table-aliasing
         trap, and par2's pw register must ALSO sit below the instruction
         region: a colliding input register is NOT rejected by the resolver,
         it silently reads program words) holds the pre-computed COLUMN mask
         of B: par1 addr 7..1 = columns 0..6 (p11 first), par2 addr 5..1 =
         columns 7..11. The parity of the 12-bit masked AND is read from the
         P flag (BR.NP skips the OR of the LSB). The down-counter IS the LOAD
         address (the HammingDecoder trick). Each cell forwards D and the
         partial p; stale p bits climb above bit 11 and are never read
         (masked-read invariant again).
    Cell ``emit`` (output cell — two counted-loop bursts):
      4. Emit the 12 data bits then the 12 parity bits MSB-first (SHR #11
         peels bit 11, AND 1 isolates it — garbage above bit 11 is masked by
         the AND; SHL #1 advances). 24 WRITE+JUMP pairs per trigger — the
         burst-emit primitive; the single-outstanding output port paces the
         burst. INV-19/20 checked: a straight 4-cell feed-forward chain, no
         reconvergent fan-in (each cell has exactly one upstream cell) and no
         feedback corridor, so no serialize-LOCK is needed; gated saturated in
         test_pipeline_saturation RATE_1IN. No GOTO in the exit cell (the
         handoff pass rewrites exit-cell JUMPs): both bursts are BR.NZ loops.

    Raw-word bit streams in and out (0/1 words), NOT Q15 — the comparison is
    BIT-EXACT (metric DECISION, tolerance 0).

    Parameters: none — the extended Golay (24,12) is a fixed code (n=24, k=12
    are structural, not tunable knobs; a different code is a different block).
    """
    CATEGORY = "fec"
    TAGS = ["golay", "fec", "encoder", "block_code", "data_link"]

    _interface = BlockInterface(
        entry_address=1, input_registers=[0], output_registers=[0])

    # The convention pin, importable by tests and by GolayDecoderBlock's
    # verification: the STANDARD B matrix (MacWilliams & Sloane 1977, Ch.2 §6
    # bordered reverse circulant; symmetric, B.B^T = I). Row i = the parity
    # contribution of data bit d(11-i); column j produces parity bit p(11-j).
    B_MATRIX = (
        (0, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1),
        (1, 1, 1, 0, 1, 1, 1, 0, 0, 0, 1, 0),
        (1, 1, 0, 1, 1, 1, 0, 0, 0, 1, 0, 1),
        (1, 0, 1, 1, 1, 0, 0, 0, 1, 0, 1, 1),
        (1, 1, 1, 1, 0, 0, 0, 1, 0, 1, 1, 0),
        (1, 1, 1, 0, 0, 0, 1, 0, 1, 1, 0, 1),
        (1, 1, 0, 0, 0, 1, 0, 1, 1, 0, 1, 1),
        (1, 0, 0, 0, 1, 0, 1, 1, 0, 1, 1, 1),
        (1, 0, 0, 1, 0, 1, 1, 0, 1, 1, 1, 0),
        (1, 0, 1, 0, 1, 1, 0, 1, 1, 1, 0, 0),
        (1, 1, 0, 1, 1, 0, 1, 1, 1, 0, 0, 0),
        (1, 0, 1, 1, 0, 1, 1, 1, 0, 0, 0, 1),
    )
    CODEWORD_LAYOUT = "d11 .. d0 p11 .. p0"  # MSB-first on the wire

    @classmethod
    def _column_mask(cls, j: int) -> int:
        """Column j of B as a 12-bit AND mask over the packed data word D
        (bit 11 of D = d11 = B row 0): p(11-j) = parity(D & mask)."""
        m = 0
        for i in range(12):
            if cls.B_MATRIX[i][j]:
                m |= 1 << (11 - i)
        return m

    def __init__(self, name: str):
        super().__init__(name)

    @property
    def cell_count(self) -> int:
        return 4

    @property
    def interface(self) -> BlockInterface:
        return self._interface

    # ------------------------------------------------------------------ build
    def build_cell_programs(self) -> dict:
        cells = {}

        # (1) pack — accumulate 12 bits MSB-first; on the 12th forward D.
        cells["pack"] = CellProgram(
            inputs=[Port("sample", register=0)],
            outputs=[Port("dw"), Port("trig")],
            entries=[EntryPoint("default")],
            data=[
                DataWord("one", 1, address=1),
                DataWord("twelve", 12, address=2),   # counter reload
            ],
            state=[
                StateVar("bit", register=3),
                StateVar("word", register=4),
                StateVar("count", register=5, initial_value=12),
            ],
            assembly_template="""\
start:
    ; bit = sample & 1 (GR pack convention: only the input LSB is data)
    AND R{in:sample}, R{data:one}
    MOVE R{state:bit}, R0
    ; word = (word << 1) | bit   (MSB-first accumulate: first bit -> d11)
    SHL R{state:word}, #1
    OR R0, R{state:bit}
    MOVE R{state:word}, R0
    ; count down; only every 12th bit forwards a group
    SUB R{state:count}, R{data:one}
    MOVE R{state:count}, R0
    BR.NZ done
    ; forward D (bits 11..0 = d11..d0; bits 12+ stale — masked downstream)
    MOVE R0, R{state:word}
    {write:dw}
    {jump:trig}
    MOVE R{state:count}, R{data:twelve}
done:
    HALT
""",
        )

        # (2)/(3) par1, par2 — 7 + 5 parity bits via the LOAD-table loop.
        # T[N..1] = B columns, first column processed at the highest address
        # (p11 first, shifted in at the LSB). The down-counter doubles as the
        # LOAD address. Budget (30 words = data + state + instr + non-R0
        # inputs): par1 = 9 data + 3 state + 18 instr = 30; par2 = 7 data +
        # 3 state + 1 input (pw@11, BELOW the instruction region 12..30 and
        # OUTSIDE the LOAD table range — a colliding register is silently
        # read as program words, it is NOT rejected) + 19 instr = 30.
        for cell_id, first_col, nmask, extra_in, regs in (
                ("par1", 0, 7, [], (10, 11, 12)),
                ("par2", 7, 5, [Port("pw", register=11)], (8, 9, 10))):
            data = [DataWord(f"m{k}", self._column_mask(first_col + k),
                             address=nmask - k) for k in range(nmask)]
            data += [DataWord("one", 1, address=nmask + 1),
                     DataWord("n", nmask, address=nmask + 2)]
            entry_copy_p = (
                "    MOVE R{state:p}, R{in:pw}\n" if extra_in else "")
            d_reg, p_reg, c_reg = regs
            cells[cell_id] = CellProgram(
                inputs=[Port("dw", register=0)] + list(extra_in),
                outputs=[Port("dout"), Port("pout"), Port("trig")],
                entries=[EntryPoint("default")],
                data=data,
                state=[
                    StateVar("d", register=d_reg),
                    StateVar("p", register=p_reg),
                    StateVar("count", register=c_reg),
                ],
                assembly_template="""\
start:
    ; copy the inputs before any ALU op clobbers R0 (INV-33)
    MOVE R{state:d}, R{in:dw}
""" + entry_copy_p + """\
    MOVE R{state:count}, R{data:n}
loop:
    ; p = (p << 1) | parity(D & T[count])  — the P flag of the masked AND
    SHL R{state:p}, #1
    MOVE R{state:p}, R0
    LOAD R{state:count}
    AND R0, R{state:d}
    BR.NP _next
    OR R{state:p}, R{data:one}
    MOVE R{state:p}, R0
_next:
    SUB R{state:count}, R{data:one}
    MOVE R{state:count}, R0
    BR.NZ loop
    ; forward D and the (partial) parity word
    MOVE R0, R{state:d}
    {write:dout}
    MOVE R0, R{state:p}
    {write:pout}
    {jump:trig}
    HALT
""",
            )

        # (4) emit — burst the 12 data bits then the 12 parity bits MSB-first
        # (two counted loops; SHR #11 + AND 1 peels bit 11, masking any stale
        # garbage above it). EXIT-CELL RULE: conditional branches only.
        cells["emit"] = CellProgram(
            inputs=[Port("dw", register=0), Port("pw", register=1)],
            outputs=[Port("out"), Port("trig")],
            entries=[EntryPoint("default")],
            data=[
                DataWord("one", 1, address=2),
                DataWord("twelve", 12, address=3),
            ],
            state=[
                StateVar("w", register=4),
                StateVar("cnt", register=5),
            ],
            assembly_template="""\
start:
    ; data half: 12 bits MSB-first
    MOVE R{state:w}, R{in:dw}
    MOVE R{state:cnt}, R{data:twelve}
dloop:
    SHR R{state:w}, #11
    AND R0, R{data:one}
    {write:out}
    {jump:trig}
    SHL R{state:w}, #1
    MOVE R{state:w}, R0
    SUB R{state:cnt}, R{data:one}
    MOVE R{state:cnt}, R0
    BR.NZ dloop
    ; parity half: 12 bits MSB-first (pw still untouched in its register)
    MOVE R{state:w}, R{in:pw}
    MOVE R{state:cnt}, R{data:twelve}
ploop:
    SHR R{state:w}, #11
    AND R0, R{data:one}
    {write:out}
    {jump:trig}
    SHL R{state:w}, #1
    MOVE R{state:w}, R0
    SUB R{state:cnt}, R{data:one}
    MOVE R{state:cnt}, R0
    BR.NZ ploop
    HALT
""",
        )
        return cells

    # ------------------------------------------------------- multi-cell wiring
    def _chain(self):
        return ["pack", "par1", "par2", "emit"]

    def internal_connections(self):
        return [("pack", "dw", "par1", "dw"),
                ("par1", "dout", "par2", "dw"),
                ("par1", "pout", "par2", "pw"),
                ("par2", "dout", "emit", "dw"),
                ("par2", "pout", "emit", "pw")]

    def internal_jumps(self):
        return [("pack", "trig", "par1", "default"),
                ("par1", "trig", "par2", "default"),
                ("par2", "trig", "emit", "default")]

    def output_cell_ids(self):
        return ["emit"]

    def default_layout(self):
        # 2x2 serpentine fold (INV-14 even-column): input cell (0,0) and
        # output cell (1,0) co-locate on the top edge (INV-8); output
        # egresses the last cell (INV-10) — the RMSBlock fold.
        return {"pack": (0, 0, "south"),
                "par1": (0, 1, "east"),
                "par2": (1, 1, "north"),
                "emit": (1, 0, "east")}

    # -------------------------------------------------------------- reference
    @classmethod
    def encode_word(cls, word: int) -> list:
        """The golden per-word encoder — the convention pin in executable
        form (GolayDecoderBlock MUST be built against this). ``word`` = the
        12 data bits packed MSB-first (bit 11 = d11 = the FIRST bit on the
        wire). Returns the 24 codeword bits MSB-first:
        [d11 .. d0, p11 .. p0] with p(11-j) = parity(word & column_mask(j))."""
        w = int(word) & 0xFFF
        bits = [(w >> (11 - i)) & 1 for i in range(12)]
        for j in range(12):
            bits.append(bin(w & cls._column_mask(j)).count("1") & 1)
        return bits

    def process_reference_q15(self, x_q15) -> list:
        """Bit-exact reference: group input words 12 at a time (LSB of each
        word is the data bit, first bit = d11), emit 24 codeword bits per
        group MSB-first. A trailing partial group (< 12 bits) is not
        emitted."""
        bits = [int(w) & 1 for w in x_q15]
        out = []
        for j in range(len(bits) // 12):
            w = 0
            for b in bits[12 * j: 12 * j + 12]:
                w = (w << 1) | b
            out.extend(self.encode_word(w))
        return out

    def process_reference(self, input_samples) -> np.ndarray:
        """Float reference (0.0/1.0 bits): same grouping as
        :meth:`process_reference_q15`."""
        words = [int(round(float(v))) & 0xFFFF for v in input_samples]
        return np.asarray(self.process_reference_q15(words), dtype=np.float32)

    def reset(self):
        """No cross-call host state (each reference call is a fresh stream)."""
        pass
