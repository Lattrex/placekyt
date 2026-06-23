"""ViterbiBranchMetricBlock — see :class:`ViterbiBranchMetricBlock`."""
import numpy as np
from ..block import CellProgram, Port, EntryPoint, StateVar, DataWord
from typing import Dict
from ._base import KyttarBlock, BlockInterface, assemble_to_words, float_to_q15, q15_to_float


class ViterbiBranchMetricBlock(KyttarBlock):
    """
    Viterbi Branch Metric Computation Block.

    Computes branch metrics for K=7 rate 1/2 Viterbi decoding. This handles
    the computationally intensive metric calculation that benefits from
    parallel execution, while the full ACS and traceback operations are
    typically done externally (too complex for Kyttar cells).

    For each pair of soft bits (LLR0, LLR1), computes 4 branch metrics:
        BM(0,0) = +LLR0 + LLR1  (expected output 0,0)
        BM(0,1) = +LLR0 - LLR1  (expected output 0,1)
        BM(1,0) = -LLR0 + LLR1  (expected output 1,0)
        BM(1,1) = -LLR0 - LLR1  (expected output 1,1)

    These metrics represent the "distance" between received soft bits and
    each possible encoder output. Lower metric = better match.

    This is a single-cell block. For a full Viterbi decoder, multiple
    parallel BMU cells can compute metrics for different trellis stages.

    Interface:
        - Entry: R1
        - Input: R31 (receives both LLR0 and LLR1 sequentially)
        - Output: BM(0,0) to downstream (other BMs stored in memory)
    """
    CATEGORY = "fec"
    TAGS = ["viterbi", "branch_metric", "fec"]

    _interface = BlockInterface(entry_address=1, input_registers=[31], output_registers=[31])

    def __init__(self, name: str):
        """
        Initialize Viterbi Branch Metric block.

        Args:
            name: Block name
        """
        super().__init__(name)

    @property
    def cell_count(self) -> int:
        return 1

    @property
    def interface(self) -> BlockInterface:
        return self._interface

    def build_cell_programs(self) -> Dict[int, CellProgram]:
        """New-style Viterbi BMU: takes 2 sequential inputs, outputs BM00=LLR0+LLR1."""
        return {0: CellProgram(
            inputs=[Port("sample", register=0)],
            outputs=[Port("out")],
            entries=[EntryPoint("default")],
            data=[
                DataWord("zero", 0, address=1),
                DataWord("one", 1, address=2),
            ],
            state=[StateVar("llr0"), StateVar("counter"),
                   StateVar("bm00"), StateVar("bm01"),
                   StateVar("bm10"), StateVar("bm11")],
            assembly_template="""\
start:
    CMP R{state:counter}, R{data:zero}
    BR.NZ have_llr0
    MOVE R{state:llr0}, R{in:sample}
    MOVE R{state:counter}, R{data:one}
    HALT
have_llr0:
    MOVE R{state:counter}, R{data:zero}
    ADD R{state:llr0}, R{in:sample}
    MOVE R{state:bm00}, R0
    SUB R{state:llr0}, R{in:sample}
    MOVE R{state:bm01}, R0
    SUB R{in:sample}, R{state:llr0}
    MOVE R{state:bm10}, R0
    SUB R{data:zero}, R{state:bm00}
    MOVE R{state:bm11}, R0
    MOVE R0, R{state:bm00}
    {write:out}
    {jump:out}
""",
        )}

    def process_reference(self, soft_bits: np.ndarray) -> np.ndarray:
        """
        Reference implementation of branch metric computation.

        Args:
            soft_bits: Pairs of soft bits [LLR0, LLR1, LLR0, LLR1, ...]

        Returns:
            Branch metrics [BM00_0, BM00_1, ...] (only BM00 output)
        """
        n_pairs = len(soft_bits) // 2
        output = np.zeros(n_pairs, dtype=np.float32)

        for i in range(n_pairs):
            llr0 = soft_bits[i * 2]
            llr1 = soft_bits[i * 2 + 1]

            # BM(0,0) = LLR0 + LLR1
            bm00 = llr0 + llr1
            output[i] = bm00

        return output

    def reset(self):
        """Reset branch metric state."""
        pass
