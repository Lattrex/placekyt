"""DCBlockerBlock — see :class:`DCBlockerBlock`."""
import numpy as np
from ..block import CellProgram, Port, EntryPoint, StateVar, DataWord
from typing import Dict
from ._base import KyttarBlock, BlockInterface, assemble_to_words, float_to_q15, q15_to_float


class DCBlockerBlock(KyttarBlock):
    """
    DC blocker (high-pass filter).

    output = input - dc_estimate
    dc_estimate += alpha * (input - dc_estimate)

    Removes DC offset using a simple IIR filter.

    Interface (defaults):
    - Entry: R1
    - Input: R31 (single sample)
    """
    CATEGORY = "signal_conditioning"
    TAGS = ["dc_blocker", "highpass", "signal_conditioning"]

    _interface = BlockInterface(entry_address=1, input_registers=[31], output_registers=[31])

    def __init__(self, name: str, alpha: float = 0.01):
        """
        Initialize DC blocker.

        Args:
            name: Block name
            alpha: Adaptation rate (smaller = slower, more filtering)
        """
        super().__init__(name, alpha=alpha)
        self._alpha = alpha
        self._alpha_q15 = float_to_q15(alpha)
        self._dc_estimate = 0.0

    @property
    def cell_count(self) -> int:
        return 1

    @property
    def interface(self) -> BlockInterface:
        return self._interface

    def build_cell_programs(self) -> Dict[int, CellProgram]:
        """New-style DC blocker: output = input - dc_est; dc_est += alpha * error."""
        return {0: CellProgram(
            inputs=[Port("sample", register=0)],
            outputs=[Port("out")],
            entries=[EntryPoint("default")],
            data=[DataWord("alpha", self._alpha_q15, address=1)],
            state=[StateVar("dc_est"), StateVar("temp")],
            assembly_template="""\
start:
    SUB R{in:sample}, R{state:dc_est}
    MOVE R{state:temp}, R0
    MULQ R0, R{data:alpha}
    ADD R{state:dc_est}, R0
    MOVE R{state:dc_est}, R0
    MOVE R0, R{state:temp}
    {write:out}
    {jump:out}
""",
        )}

    def process_reference(self, input_samples: np.ndarray) -> np.ndarray:
        """Reference implementation."""
        output = np.zeros(len(input_samples), dtype=np.float32)

        for i, sample in enumerate(input_samples):
            error = float(sample) - self._dc_estimate
            output[i] = error
            self._dc_estimate += self._alpha * error

        return output

    def reset(self):
        """Reset DC estimate."""
        self._dc_estimate = 0.0
