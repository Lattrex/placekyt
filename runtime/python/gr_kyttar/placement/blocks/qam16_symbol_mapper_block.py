"""QAM16SymbolMapperBlock — see :class:`QAM16SymbolMapperBlock`."""
import numpy as np
from ..block import CellProgram, Port, EntryPoint, StateVar, DataWord
from typing import Dict
from ._base import KyttarBlock, BlockInterface, assemble_to_words, float_to_q15, q15_to_float
from ._qam16_common import _QAM16_NORM, _QAM16_PAM_LEVELS, _QAM16_PAM_Q15


class QAM16SymbolMapperBlock(KyttarBlock):
    """
    16-QAM Symbol Mapper Block (2 cells).

    Maps 4 input bits to a Gray-coded 16-QAM constellation point, emitting the I
    and Q components on separate output ports — the same 2-cell shape as the
    QPSK/8-PSK mapper. 16-QAM is separable, so cell 1 holds just a 4-entry I-PAM
    table and a 4-entry Q-PAM table (high 2 bits -> I, low 2 bits -> Q).

        bits b3 b2 b1 b0  ->  I = PAM[b3 b2],  Q = PAM[b1 b0]

    Cell 0 accumulates 4 input bits (MSB first) into an index 0..15. Cell 1 splits
    the index into I-index (bits 3:2) and Q-index (bits 1:0), LOADs each PAM level,
    and writes out_i / out_q.

    Interface:
        - Entry: R1 (cell 0)
        - Input: R0 (one bit per call; 4 bits make a symbol)
        - Outputs: out_i, out_q (Q15)
    """
    CATEGORY = "demodulation"
    TAGS = ["qam16", "symbol_mapper", "modulation", "demodulation"]

    _interface = BlockInterface(entry_address=1, input_registers=[31], output_registers=[31])

    BITS_PER_SYMBOL = 4

    def __init__(self, name: str):
        super().__init__(name)
        self._bit_buffer = 0
        self._bit_count = 0

    @property
    def cell_count(self) -> int:
        return 2

    @property
    def interface(self) -> BlockInterface:
        return self._interface

    def build_cell_programs(self) -> Dict[int, CellProgram]:
        # Cell 0: accumulate 4 bits (MSB first) -> index 0..15, send to cell 1.
        cell0 = CellProgram(
            inputs=[Port("sample", register=0)],
            outputs=[Port("idx")],
            entries=[EntryPoint("default")],
            data=[
                DataWord("mask", 15, address=1),
                DataWord("bps", 4, address=2),
                DataWord("one", 1, address=3),
                DataWord("zero", 0, address=4),
            ],
            state=[StateVar("in_save"), StateVar("bit_acc"), StateVar("bit_cnt")],
            assembly_template="""\
start:
    MOVE R{state:in_save}, R{in:sample}
    SHL R{state:bit_acc}, #1
    OR R0, R{state:in_save}
    MOVE R{state:bit_acc}, R0
    ADD R{state:bit_cnt}, R{data:one}
    MOVE R{state:bit_cnt}, R0
    CMP R{state:bit_cnt}, R{data:bps}
    BR.N done
    MOVE R{state:bit_cnt}, R{data:zero}
    AND R{state:bit_acc}, R{data:mask}
    MOVE R{state:bit_acc}, R{data:zero}
    {write:idx}
    {jump:idx}
done:
    HALT
""",
        )

        # Cell 1: split idx -> I-index (idx>>2) and Q-index (idx & 3); LOAD each
        # 4-entry PAM table; output I then Q. PAM tables share addresses 1..4
        # (I) and 5..8 (Q) — both are the same {-3,-1,+3,+1}/sqrt(10) levels.
        i_pam = [DataWord(f"ip{i}", v, address=i + 1)
                 for i, v in enumerate(_QAM16_PAM_Q15)]
        q_pam = [DataWord(f"qp{i}", v, address=i + 5)
                 for i, v in enumerate(_QAM16_PAM_Q15)]
        cell1 = CellProgram(
            inputs=[Port("index", register=0)],
            outputs=[Port("out_i"), Port("out_q"), Port("out_trigger")],
            entries=[EntryPoint("default")],
            data=i_pam + q_pam + [
                DataWord("one", 1, address=9),
                DataWord("three", 3, address=10),
                DataWord("q_base", 5, address=11),
            ],
            state=[StateVar("idx_save"), StateVar("addr_tmp")],
            assembly_template="""\
start:
    MOVE R{state:idx_save}, R{in:index}
    SHR R{state:idx_save}, #2
    ADD R0, R{data:one}
    MOVE R{state:addr_tmp}, R0
    LOAD R{state:addr_tmp}
    {write:out_i}
    MOVE R0, R{state:idx_save}
    MOVE R{state:idx_save}, R{in:index}
    AND R{state:idx_save}, R{data:three}
    ADD R0, R{data:q_base}
    MOVE R{state:addr_tmp}, R0
    LOAD R{state:addr_tmp}
    {write:out_q}
    {jump:out_trigger}
""",
        )
        return {0: cell0, 1: cell1}

    def process_reference(self, input_bits):
        """Reference: 4 bits -> (I, Q) Q15 pair per symbol (separable Gray PAM)."""
        out = []
        acc = cnt = 0
        for b in np.asarray(input_bits).ravel():
            acc = ((acc << 1) | (int(b) & 1)) & 0xF
            cnt += 1
            if cnt == 4:
                i_idx = (acc >> 2) & 3
                q_idx = acc & 3
                out.append((_QAM16_PAM_Q15[i_idx], _QAM16_PAM_Q15[q_idx]))
                acc = cnt = 0
        return out

    def reset(self):
        self._bit_buffer = 0
        self._bit_count = 0
