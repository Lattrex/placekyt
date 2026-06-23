"""EOMDetectorBlock — see :class:`EOMDetectorBlock`."""
import numpy as np
from ..block import CellProgram, Port, EntryPoint, StateVar, DataWord
from typing import Dict
from ._base import KyttarBlock, BlockInterface, assemble_to_words, float_to_q15, q15_to_float


class EOMDetectorBlock(KyttarBlock):
    """
    End-of-Message (EOM) Detector Block (1 cell).

    Detects the MIL-STD-188-110B end-of-message pattern 0x4B65A5B2 which
    signals the end of transmission.

    Architecture: Pattern Matcher (1 cell)
    ======================================

    Uses a shift register to match the 32-bit EOM pattern in incoming bits.

    Cell Layout:
    ```
        In → [MATCHER] → Out
    ```

    Components:
    - MATCHER (1 cell): 32-bit shift register with comparator

    Total: 1 cell

    EOM Pattern: 0x4B65A5B2 (binary: 01001011 01100101 10100101 10110010)

    Interface:
        - Entry: R1
        - Input: R31 (decoded bits)
        - Output: EOM detected flag
    """
    CATEGORY = "frame_sync"
    TAGS = ["eom", "detector", "frame_sync"]

    _interface = BlockInterface(entry_address=1, input_registers=[31], output_registers=[31])

    # EOM pattern split into two 16-bit words for Q15 storage
    EOM_PATTERN_HI = 0x4B65
    EOM_PATTERN_LO = 0xA5B2

    def __init__(self, name: str):
        """Initialize EOM Detector."""
        super().__init__(name)
        self._shift_reg = 0

    @property
    def cell_count(self) -> int:
        return 1

    @property
    def interface(self) -> BlockInterface:
        return self._interface

    def build_cell_programs(self) -> Dict[int, CellProgram]:
        """Production EOM detector: full 32-bit shift register + dual CMP.

        Uses SHL + ADC for 32-bit left shift across two 16-bit registers.
        SHL sets carry flag to the MSB shifted out, ADC propagates it
        into the high word. Both halves compared against the full
        0x4B65A5B2 EOM pattern.

        False positive rate: ~1 in 4 billion (vs ~1 in 65536 with 16-bit).
        """
        return {0: CellProgram(
            inputs=[Port("sample", register=0)],
            outputs=[Port("out")],
            entries=[EntryPoint("default")],
            data=[
                DataWord("pattern_hi", self.EOM_PATTERN_HI, address=1),
                DataWord("pattern_lo", self.EOM_PATTERN_LO, address=2),
                DataWord("one", 1, address=3),
                DataWord("zero", 0, address=4),
            ],
            state=[
                StateVar("shift_hi"),
                StateVar("shift_lo"),
                StateVar("in_save"),
                StateVar("result"),
            ],
            assembly_template="""\
start:
    MOVE R{state:in_save}, R0
    SHL R{state:shift_lo}, #1
    MOVE R{state:shift_lo}, R0
    ADC R{state:shift_hi}, R{state:shift_hi}
    MOVE R{state:shift_hi}, R0
    OR R{state:shift_lo}, R{state:in_save}
    MOVE R{state:shift_lo}, R0
    MOVE R{state:result}, R{data:zero}
    CMP R{state:shift_hi}, R{data:pattern_hi}
    BR.NZ output
    CMP R{state:shift_lo}, R{data:pattern_lo}
    BR.NZ output
    MOVE R{state:result}, R{data:one}
output:
    MOVE R0, R{state:result}
    {write:out}
    {jump:out}
""",
        )}

    def process_reference(self, input_bits: np.ndarray) -> np.ndarray:
        """
        Reference implementation of EOM detection.

        Args:
            input_bits: Input bits (0 or 1)

        Returns:
            Detection flags (1 when EOM pattern found)
        """
        n_bits = len(input_bits)
        detected = np.zeros(n_bits, dtype=np.int32)

        eom_pattern = (self.EOM_PATTERN_HI << 16) | self.EOM_PATTERN_LO

        for i in range(n_bits):
            bit = int(input_bits[i]) & 1
            self._shift_reg = ((self._shift_reg << 1) | bit) & 0xFFFFFFFF

            if self._shift_reg == eom_pattern:
                detected[i] = 1

        return detected

    def reset(self):
        """Reset detector state."""
        self._shift_reg = 0
