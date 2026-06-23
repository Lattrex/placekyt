"""FIRFilterBlock — see :class:`FIRFilterBlock`."""
import numpy as np
import math
from ..block import CellProgram, Port, EntryPoint, StateVar, DataWord
from typing import Dict, List
from ._base import KyttarBlock, BlockInterface, assemble_to_words, float_to_q15, q15_to_float
from .rrc_pulse_shaper_block import RRCPulseShaperBlock


class FIRFilterBlock(KyttarBlock):
    """
    FIR Filter Block — General-Purpose Multi-Cell FIR.

    output = sum(coeff[i] * delay[i]) for i in 0..N-1

    For ≤12 taps: single cell (v2 template, compact).
    For >12 taps: multi-cell wavefront using the same chained partial-sum
    architecture as RRCPulseShaperBlock (TAPS_PER_CELL=5).

    Supports arbitrary coefficients for any FIR application (channel
    filter, matched filter, anti-alias, interpolation, etc.).

    Interface (defaults):
    - Entry: R1
    - Input: R31 (single sample)
    """
    CATEGORY = "filtering"
    TAGS = ["fir", "filter", "filtering"]

    _interface = BlockInterface(entry_address=1, input_registers=[31], output_registers=[31])

    def __init__(self, name: str, coefficients: List[float]):
        """
        Initialize FIR filter.

        Args:
            name: Block name
            coefficients: Filter coefficients (length determines tap count)
        """
        super().__init__(name, coefficients=coefficients)
        self._coefficients = coefficients
        self._num_taps = len(coefficients)

        # Convert to Q15
        self._coeff_q15 = [float_to_q15(c) for c in coefficients]

        # Initialize delay line
        self._delay_line = [0.0] * self._num_taps

    TAPS_PER_CELL = 5  # Same as RRCPulseShaperBlock

    @property
    def cell_count(self) -> int:
        import math
        if self._num_taps <= 12:
            return 1  # Single-cell fits up to ~12 taps
        return math.ceil(self._num_taps / self.TAPS_PER_CELL)

    @property
    def num_taps(self) -> int:
        return self._num_taps

    @property
    def interface(self) -> BlockInterface:
        return self._interface

    def build_cell_programs(self) -> Dict[int, CellProgram]:
        """FIR filter: single-cell for ≤12 taps, multi-cell for larger.

        Multi-cell uses the same chained partial-sum architecture as
        RRCPulseShaperBlock. Each cell handles TAPS_PER_CELL taps.
        """
        if self._num_taps > 12:
            # Multi-cell: use RRC's generic multi-cell FIR architecture.
            # Create a temporary RRC-like object with our coefficients.
            rrc = RRCPulseShaperBlock.__new__(RRCPulseShaperBlock)
            rrc._name = self._name
            rrc._kwargs = {}
            rrc._connections = []
            rrc._metrics = None
            rrc._num_taps = self._num_taps
            rrc._coeff_q15 = self._coeff_q15
            rrc.TAPS_PER_CELL = self.TAPS_PER_CELL
            return rrc.build_cell_programs()

        # Single-cell: compact version for small filters
        data = [DataWord(f"c{i}", c, address=i+1) for i, c in enumerate(self._coeff_q15)]
        state = [StateVar(f"d{i}") for i in range(self._num_taps)]

        lines = []
        for i in range(self._num_taps - 1):
            lines.append(f"    MOVE R{{state:d{i}}}, R{{state:d{i+1}}}")
        lines.append(f"    MOVE R{{state:d{self._num_taps - 1}}}, R{{in:sample}}")
        lines.append(f"    MULQ R{{state:d0}}, R{{data:c0}}")
        for i in range(1, self._num_taps):
            lines.append(f"    MACQ R{{state:d{i}}}, R{{data:c{i}}}")
        lines.append("    {write:out}")
        lines.append("    {jump:out}")

        template = "start:\n" + "\n".join(lines) + "\n"

        return {0: CellProgram(
            inputs=[Port("sample", register=0)],
            outputs=[Port("out")],
            entries=[EntryPoint("default")],
            data=data,
            state=state,
            assembly_template=template,
        )}

    def process_reference(self, input_samples: np.ndarray) -> np.ndarray:
        """Reference implementation."""
        output = np.zeros(len(input_samples), dtype=np.float32)

        for i, sample in enumerate(input_samples):
            # Shift delay line
            self._delay_line = [float(sample)] + self._delay_line[:-1]

            # Compute output
            acc = 0.0
            for j, coeff in enumerate(self._coefficients):
                acc += coeff * self._delay_line[j]

            output[i] = acc

        return output

    def reset(self):
        """Reset delay line."""
        self._delay_line = [0.0] * self._num_taps
