"""ConvEncoderK7Block — see :class:`ConvEncoderK7Block`."""
import numpy as np
from ..block import CellProgram, Port, EntryPoint, StateVar, DataWord
from typing import Dict
from ._base import KyttarBlock, BlockInterface, assemble_to_words, float_to_q15, q15_to_float


class ConvEncoderK7Block(KyttarBlock):
    """
    K=7 Rate 1/2 Convolutional Encoder Block (1 cell).

    Implements the MIL-STD-188-110B / NASA standard K=7 convolutional
    encoder with generator polynomials G1=0x79, G2=0x5B.

    Architecture: Single Cell (1 cell)
    ==================================

    The encoder is a 6-bit shift register with two output taps that
    XOR subsets of the register bits.

    Structure:
    ```
         ┌───┬───┬───┬───┬───┬───┐
    In ──│ 0 │ 1 │ 2 │ 3 │ 4 │ 5 │
         └─┬─┴─┬─┴─┬─┴─┬─┴─┬─┴─┬─┘
           │   │   │   │   │   │
    G1 = ──⊕───⊕───⊕───⊕───────⊕── = 0x79 (171 octal)
           │       │   │   │   │
    G2 = ──⊕───────⊕───⊕───⊕───⊕── = 0x5B (133 octal)
    ```

    Memory Layout (32 words):
    - R0: Accumulator
    - R1-R10: Program code
    - R16: Shift register state (6-bit)
    - R17: G1 polynomial (0x79)
    - R18: G2 polynomial (0x5B)
    - R19: Output bit 1
    - R20: Output bit 2
    - R31: Input bit

    Total: 1 cell

    Interface:
        - Entry: R1
        - Input: R31 (data bit)
        - Output: Two encoded bits (rate 1/2)
    """
    CATEGORY = "fec"
    TAGS = ["convolutional", "encoder", "fec"]

    _interface = BlockInterface(entry_address=1, input_registers=[31], output_registers=[31])

    G1 = 0x79  # Generator 1: 1111001
    G2 = 0x5B  # Generator 2: 1011011
    K = 7      # Constraint length

    def __init__(self, name: str):
        """
        Initialize K=7 convolutional encoder.

        Args:
            name: Block name
        """
        super().__init__(name)
        self._state = 0  # 6-bit shift register

    @property
    def cell_count(self) -> int:
        return 1

    @property
    def interface(self) -> BlockInterface:
        return self._interface

    def build_cell_programs(self) -> Dict[int, CellProgram]:
        """V2 K=7 convolutional encoder using parity flag for output bits.

        Shift register is 7 bits (K=7). New bit enters at bit 6, shifts right.
        Output = (parity(state & G1) << 1) | parity(state & G2), packed as
        a single 2-bit value (0-3).
        """
        return {0: CellProgram(
            inputs=[Port("sample", register=0)],
            outputs=[Port("out")],
            entries=[EntryPoint("default")],
            data=[
                DataWord("g1", self.G1, address=1),       # 0x79
                DataWord("g2", self.G2, address=2),       # 0x5B
                DataWord("mask7", 0x7F, address=3),       # 7-bit mask
                DataWord("one", 1, address=4),
                DataWord("zero", 0, address=5),
            ],
            state=[
                StateVar("shift"),       # 7-bit shift register
                StateVar("in_save"),     # saved input
                StateVar("shift_tmp"),   # temp for shift computation
                StateVar("out1"),        # G1 parity output
                StateVar("out2"),        # G2 parity output
            ],
            assembly_template="""\
start:
    MOVE R{state:in_save}, R{in:sample}
    SHR R{state:shift}, 1
    MOVE R{state:shift_tmp}, R0
    SHL R{state:in_save}, 6
    OR R0, R{state:shift_tmp}
    AND R0, R{data:mask7}
    MOVE R{state:shift}, R0
    MOVE R{state:out1}, R{data:zero}
    MOVE R{state:out2}, R{data:zero}
    AND R{state:shift}, R{data:g1}
    BR.NP g2_check
    MOVE R{state:out1}, R{data:one}
g2_check:
    AND R{state:shift}, R{data:g2}
    BR.NP output
    MOVE R{state:out2}, R{data:one}
output:
    SHL R{state:out1}, 1
    OR R0, R{state:out2}
    {write:out}
    {jump:out}
""",
        )}

    @staticmethod
    def _parity(x: int) -> int:
        """Compute parity (XOR of all bits)."""
        x ^= x >> 16
        x ^= x >> 8
        x ^= x >> 4
        x ^= x >> 2
        x ^= x >> 1
        return x & 1

    def process_reference(self, input_bits: np.ndarray) -> np.ndarray:
        """
        Reference implementation of K=7 convolutional encoder.

        Args:
            input_bits: Input data bits

        Returns:
            Encoded bits (2x input length, rate 1/2)
        """
        n_bits = len(input_bits)
        output = np.zeros(n_bits * 2, dtype=np.int32)

        for i in range(n_bits):
            # Shift in new bit
            self._state = ((self._state >> 1) | (int(input_bits[i]) << 5)) & 0x3F

            # Compute outputs
            out1 = self._parity(self._state & self.G1)
            out2 = self._parity(self._state & self.G2)

            output[i * 2] = out1
            output[i * 2 + 1] = out2

        return output

    def reset(self):
        """Reset encoder state."""
        self._state = 0
