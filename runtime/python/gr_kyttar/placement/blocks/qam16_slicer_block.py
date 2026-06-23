"""QAM16SlicerBlock — see :class:`QAM16SlicerBlock`."""
from ..block import CellProgram, Port, EntryPoint, StateVar, DataWord
from typing import Dict
from ._base import KyttarBlock, BlockInterface, assemble_to_words, float_to_q15, q15_to_float


class QAM16SlicerBlock(KyttarBlock):
    """
    16-QAM Hard-Decision Slicer / Demapper Block (1 cell).

    Turns a received (I, Q) sample into the 4-bit Gray-coded symbol index 0..15.
    16-QAM is separable, so each axis is sliced independently by 4-PAM thresholds
    into a 2-bit Gray value, then combined: symbol = (I_bits << 2) | Q_bits.

    Per-axis Gray 4-PAM decode (levels {-3,-1,+1,+3}/sqrt(10), threshold
    t = 2/sqrt(10)):

        bits = (v >= 0 ? 2 : 0) | (|v| < t ? 1 : 0)
        # -3 -> 00, -1 -> 01, +1 -> 11, +3 -> 10   (Gray, matches the mapper)

    This is the receiver's final decision stage; composed with the mapper it is the
    identity on a clean channel: 4 bits -> (I,Q) -> 4 bits.

    Interface:
        - Entry: R1 (cell 0)
        - Inputs: I (cell 0, R0), Q (cell 1, carried via the partial handoff)
        - Output: 4-bit symbol index (0..15)

    2 cells: cell 0 slices the I axis to the high 2 bits and forwards them (+ Q);
    cell 1 slices the Q axis to the low 2 bits and combines. Splitting the work
    keeps each cell within the register budget (one PAM slice fits comfortably).
    """
    CATEGORY = "demodulation"
    TAGS = ["qam16", "slicer", "hard_decision", "demodulation"]

    # The landing cell (cell 0) reads in_i at R0 and in_q at R1 (see
    # build_cell_programs). The interface MUST match those actual cell-input
    # registers so a placeKYT block→block connection (which routes to
    # input_registers) lands the I/Q where the slicer reads them — declaring R31/R30
    # here sent the mapper's I/Q to registers the slicer never reads (all-zero
    # inputs → a stuck symbol). Output: the 4-bit index, written to R0.
    _interface = BlockInterface(entry_address=1, input_registers=[0, 1],
                                output_registers=[0])

    def __init__(self, name: str):
        super().__init__(name)
        # 4-PAM decision threshold t = 2/sqrt(10) in Q15.
        self._thresh_q15 = float_to_q15(2.0 / (10.0 ** 0.5))

    @property
    def cell_count(self) -> int:
        return 2

    @property
    def interface(self) -> BlockInterface:
        return self._interface

    @staticmethod
    def _slice_axis_template(value_reg, partial_in, partial_out, write_q=False):
        """One PAM axis -> 2-bit Gray value, OR-shifted into a running symbol.
        Per axis: msb=(v>=0), lsb=(|v|<t); sym = (sym<<2) | (msb<<1 | lsb)."""
        pass  # (inlined below per cell; kept here for documentation)

    def build_cell_programs(self) -> Dict[int, CellProgram]:
        thr = self._thresh_q15
        # Cell 0: slice I -> 2 bits, send them (sym so far) + the raw Q onward.
        cell0 = CellProgram(
            inputs=[Port("in_i", register=0), Port("in_q", register=1)],
            outputs=[Port("sym_partial"), Port("q_fwd"), Port("fwd_trigger")],
            entries=[EntryPoint("default")],
            data=[
                DataWord("zero", 0, address=2),
                DataWord("thresh", thr, address=3),
                DataWord("one", 1, address=4),
            ],
            state=[StateVar("sym"), StateVar("absv"), StateVar("q_save"),
                   StateVar("i_save")],
            assembly_template="""\
start:
    MOVE R{state:q_save}, R{in:in_q}
    MOVE R{state:i_save}, R{in:in_i}        ; save I before R0 is clobbered
    MOVE R{state:sym}, R{data:zero}
    ; I msb = (I >= 0)
    SHL R{state:sym}, #1
    CMP R{state:i_save}, R{data:zero}
    BR.N i_lsb
    OR R0, R{data:one}
i_lsb:
    MOVE R{state:sym}, R0
    ; |I| into absv
    MOVE R{state:absv}, R{state:i_save}
    CMP R{state:i_save}, R{data:zero}
    BR.NN i_thr
    SUB R{data:zero}, R{state:i_save}
    MOVE R{state:absv}, R0
i_thr:
    SHL R{state:sym}, #1
    CMP R{state:absv}, R{data:thresh}
    BR.NN i_emit
    OR R0, R{data:one}
i_emit:
    MOVE R{state:sym}, R0
    {write:sym_partial}
    MOVE R0, R{state:q_save}
    {write:q_fwd}
    {jump:fwd_trigger}
""",
        )

        # Cell 1: receive (sym_partial=R0, q=R1); slice Q -> low 2 bits; combine.
        cell1 = CellProgram(
            inputs=[Port("sym_partial", register=0), Port("q_in", register=1)],
            outputs=[Port("out")],
            entries=[EntryPoint("default")],
            data=[
                DataWord("zero", 0, address=2),
                DataWord("thresh", thr, address=3),
                DataWord("one", 1, address=4),
            ],
            state=[StateVar("sym"), StateVar("absv")],
            assembly_template="""\
start:
    MOVE R{state:sym}, R{in:sym_partial}
    ; Q msb = (Q >= 0)
    SHL R{state:sym}, #1
    CMP R{in:q_in}, R{data:zero}
    BR.N q_lsb
    OR R0, R{data:one}
q_lsb:
    MOVE R{state:sym}, R0
    MOVE R{state:absv}, R{in:q_in}
    CMP R{in:q_in}, R{data:zero}
    BR.NN q_thr
    SUB R{data:zero}, R{in:q_in}
    MOVE R{state:absv}, R0
q_thr:
    SHL R{state:sym}, #1
    CMP R{state:absv}, R{data:thresh}
    BR.NN emit
    OR R0, R{data:one}
emit:
    {write:out}
    {jump:out}
""",
        )
        return {0: cell0, 1: cell1}

    def process_reference(self, samples):
        """Reference: (I,Q) Q15 pairs -> 4-bit Gray symbol index list."""
        def s16(v):
            return v - 0x10000 if v & 0x8000 else v
        t = s16(self._thresh_q15)

        def pam2(v):
            v = s16(v)
            return (2 if v >= 0 else 0) | (1 if abs(v) < t else 0)
        out = []
        for (i, q) in samples:
            out.append(((pam2(i) << 2) | pam2(q)) & 0xF)
        return out

    def reset(self):
        pass
