"""GainBlock — see :class:`GainBlock`."""
import numpy as np
from ..block import CellProgram, Port, EntryPoint, StateVar, DataWord
from typing import Dict
from ._base import KyttarBlock, BlockInterface, assemble_to_words, float_to_q15, q15_to_float


class GainBlock(KyttarBlock):
    """
    Simple gain/multiplier block.

    output = input * gain

    This is the simplest possible DSP block, useful for testing
    the E2E flow.

    Interface (defaults):
    - Entry: R1
    - Input: R31 (single sample)
    - Output: writes to target's input register
    """
    CATEGORY = "signal_conditioning"
    TAGS = ["gain", "multiply", "signal_conditioning"]

    # Block interface - can be customized per instance if needed
    _interface = BlockInterface(entry_address=1, input_registers=[31], output_registers=[31])

    # Gain range modes: determines the shift amount after MUL
    RANGE_Q15 = 15    # gain range [-1.0, +1.0), uses MULQ
    RANGE_Q14 = 14    # gain range [-2.0, +2.0), uses MUL + SHR #14
    RANGE_Q13 = 13    # gain range [-4.0, +4.0), uses MUL + SHR #13
    RANGE_Q12 = 12    # gain range [-8.0, +8.0), uses MUL + SHR #12

    def __init__(self, name: str, gain: float = 0.5, gain_range: int = 15):
        """
        Initialize gain block.

        Args:
            name: Block name
            gain: Gain value (range depends on gain_range)
            gain_range: Q-format shift (15=[-1,1), 14=[-2,2), 13=[-4,4), 12=[-8,8))
        """
        super().__init__(name, gain=gain, gain_range=gain_range)
        self._gain = gain
        self._gain_range = gain_range
        # Scale gain to the appropriate fixed-point format
        scale = 1 << gain_range
        gain_scaled = int(round(gain * scale))
        gain_scaled = max(-32768, min(32767, gain_scaled))
        self._gain_scaled = gain_scaled & 0xFFFF

    @property
    def cell_count(self) -> int:
        return 1

    @property
    def gain(self) -> float:
        return self._gain

    @property
    def interface(self) -> BlockInterface:
        return self._interface

    def build_cell_programs(self) -> Dict[int, CellProgram]:
        """Production gain block: output = input * gain.

        For gains within [-1.0, +1.0): uses MULQ (Q15, 3 instructions).
        For gains outside that range: uses MUL + SHR (extended Q format, 4 instructions).
        Gain range configurable via gain_range parameter.
        """
        if self._gain_range == 15:
            # Standard Q15: MULQ gives (input * gain) >> 15
            return {0: CellProgram(
                inputs=[Port("sample", register=0)],
                outputs=[Port("out")],
                entries=[EntryPoint("default")],
                data=[DataWord("gain", self._gain_scaled, address=1)],
                assembly_template="""\
start:
    MULQ R{in:sample}, R{data:gain}
    {write:out}
    {jump:out}
""",
            )}
        else:
            # Extended range: need full 32-bit product shifted right.
            # result = (input * gain_scaled) >> gain_range
            # = (MULHI << (16 - gain_range)) | (MUL_LOW >> gain_range)
            hi_shift = 16 - self._gain_range  # e.g., 2 for Q14
            lo_shift = self._gain_range       # e.g., 14 for Q14
            return {0: CellProgram(
                inputs=[Port("sample", register=0)],
                outputs=[Port("out")],
                entries=[EntryPoint("default")],
                data=[DataWord("gain", self._gain_scaled, address=1)],
                state=[StateVar("lo_bits")],
                assembly_template=f"""\
start:
    MUL R{{in:sample}}, R{{data:gain}}
    SHR R0, #{lo_shift}
    MOVE R{{state:lo_bits}}, R0
    MULHI R{{in:sample}}, R{{data:gain}}
    SHL R0, #{hi_shift}
    OR R0, R{{state:lo_bits}}
    {{write:out}}
    {{jump:out}}
""",
            )}

    def process_reference(self, input_samples: np.ndarray) -> np.ndarray:
        """Reference implementation."""
        return (input_samples * self._gain).astype(np.float32)
