"""LFSRScramblerBlock — see :class:`LFSRScramblerBlock`."""
import numpy as np
from ..block import CellProgram, Port, EntryPoint, StateVar, DataWord
from typing import Dict
from ._base import KyttarBlock, BlockInterface, assemble_to_words, float_to_q15, q15_to_float


class LFSRScramblerBlock(KyttarBlock):
    """
    LFSR Scrambler/Descrambler Block (1 cell).

    Implements the MIL-STD-188-110B scrambler using LFSR polynomial
    x^15 + x^14 + 1. The same circuit works for both scrambling and
    descrambling (self-synchronizing).

    Architecture: Single Cell (1 cell)
    ==================================

    The LFSR generates a pseudo-random sequence that is XORed with
    the input data. This provides:
    - Energy dispersal (no long runs of 0s or 1s)
    - Self-synchronization on receive

    LFSR Structure:
    ```
         ┌───┐   ┌───┐          ┌───┐
    In──⊕─│ 0 │───│ 1 │── ... ──│14 │──┐
         ▲ └───┘   └───┘          └───┘  │
         │              tap at bit 14    │
         └───────────────────────────────┘
    ```

    Memory Layout (32 words):
    - R0: Accumulator
    - R1-R8: Program code
    - R16: LFSR state (15-bit)
    - R17: Polynomial tap mask (0x4001 for x^15+x^14+1)
    - R18: Output bit
    - R31: Input bit

    Total: 1 cell

    Interface:
        - Entry: R1
        - Input: R31 (data bit)
        - Output: Scrambled/descrambled bit
    """
    CATEGORY = "fec"
    TAGS = ["lfsr", "scrambler", "fec"]

    _interface = BlockInterface(entry_address=1, input_registers=[31], output_registers=[31])

    # MIL-STD-188-110B polynomial: x^15 + x^14 + 1
    POLY_MASK = 0x4001  # Bits 14 and 0

    def __init__(
        self,
        name: str,
        is_descrambler: bool = False,
        initial_state: int = 0x0001,
    ):
        """
        Initialize LFSR Scrambler.

        Args:
            name: Block name
            is_descrambler: If True, acts as descrambler
            initial_state: Initial LFSR state (default 0x0001)
        """
        super().__init__(
            name,
            is_descrambler=is_descrambler,
            initial_state=initial_state,
        )
        self._is_descrambler = is_descrambler
        self._initial_state = initial_state
        self._lfsr_state = initial_state

    @property
    def cell_count(self) -> int:
        return 1

    @property
    def interface(self) -> BlockInterface:
        return self._interface

    def build_cell_programs(self) -> Dict[int, CellProgram]:
        """Production LFSR scrambler: x^15 + x^14 + 1 polynomial.

        feedback = bit14 XOR bit0 of LFSR state.
        Computed via AND with poly mask (0x4001) then BR.NP (parity flag).
        AND sets parity flag = XOR of all set bits in result.
        For 0x4001 mask: parity = bit14 XOR bit0, which IS the feedback.
        """
        return {0: CellProgram(
            inputs=[Port("sample", register=0)],
            outputs=[Port("out")],
            entries=[EntryPoint("default")],
            data=[
                DataWord("poly", self.POLY_MASK, address=1),
                DataWord("mask15", 0x7FFF, address=2),
                DataWord("one", 1, address=3),
                DataWord("zero", 0, address=4),
            ],
            state=[StateVar("lfsr", initial_value=self._initial_state),
                   StateVar("feedback"), StateVar("in_save")],
            assembly_template="""\
start:
    MOVE R{state:in_save}, R{in:sample}
    ; feedback = bit14 XOR bit0 via parity flag
    AND R{state:lfsr}, R{data:poly}
    ; Parity flag = XOR of all bits in result = bit14 XOR bit0
    ; BR.NP = branch if NOT parity (parity=0 means even = bits equal = feedback=0)
    MOVE R{state:feedback}, R{data:zero}
    BR.NP skip_set
    MOVE R{state:feedback}, R{data:one}
skip_set:
    ; Shift LFSR: state = (state << 1) | feedback
    SHL R{state:lfsr}, #1
    OR R0, R{state:feedback}
    AND R0, R{data:mask15}
    MOVE R{state:lfsr}, R0
    ; Output = input XOR feedback
    XOR R{state:in_save}, R{state:feedback}
    {write:out}
    {jump:out}
""",
        )}

    def process_reference(self, input_bits: np.ndarray) -> np.ndarray:
        """
        Reference implementation of LFSR scrambler.

        Args:
            input_bits: Input bits (0 or 1)

        Returns:
            Scrambled/descrambled bits
        """
        n_bits = len(input_bits)
        output = np.zeros(n_bits, dtype=np.int32)

        for i in range(n_bits):
            # Compute feedback: XOR of bits 14 and 0
            bit14 = (self._lfsr_state >> 14) & 1
            bit0 = self._lfsr_state & 1
            feedback = bit14 ^ bit0

            # Output = input XOR feedback
            output[i] = int(input_bits[i]) ^ feedback

            # Shift LFSR
            self._lfsr_state = ((self._lfsr_state << 1) | feedback) & 0x7FFF

        return output

    def reset(self):
        """Reset LFSR to initial state."""
        self._lfsr_state = self._initial_state
