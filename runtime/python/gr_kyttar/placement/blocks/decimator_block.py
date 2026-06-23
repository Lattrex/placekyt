"""DecimatorBlock — see :class:`DecimatorBlock`."""
import numpy as np
from ..block import CellProgram, Port, EntryPoint, StateVar, DataWord
from typing import Dict, List
from ._base import KyttarBlock, BlockInterface, assemble_to_words, float_to_q15, q15_to_float


class DecimatorBlock(KyttarBlock):
    """
    Decimator block - FIR filter with downsampling.

    output = FIR(input) every M samples

    Combines a lowpass FIR filter with downsampling to reduce sample rate.
    Only outputs one sample for every M input samples.

    Interface (defaults):
    - Entry: R1
    - Input: R31 (single sample)
    """
    CATEGORY = "filtering"
    TAGS = ["decimator", "downsample", "filtering"]

    _interface = BlockInterface(entry_address=1, input_registers=[31], output_registers=[31])

    def __init__(self, name: str, coefficients: List[float], decimation_factor: int = 2):
        """
        Initialize decimator block.

        Args:
            name: Block name
            coefficients: FIR filter coefficients (lowpass, should prevent aliasing)
            decimation_factor: Decimation factor M (output_rate = input_rate / M)
        """
        super().__init__(name, coefficients=coefficients, decimation_factor=decimation_factor)
        self._coefficients = list(coefficients)
        self._decimation_factor = decimation_factor
        self._coefficients_q15 = [float_to_q15(c) for c in coefficients]

    @property
    def cell_count(self) -> int:
        return 1

    @property
    def coefficients(self) -> List[float]:
        return self._coefficients

    @property
    def decimation_factor(self) -> int:
        return self._decimation_factor

    @property
    def interface(self) -> BlockInterface:
        return self._interface

    def build_cell_programs(self) -> Dict[int, CellProgram]:
        """Decimator: FIR anti-alias filter + downsampling by M.

        For ≤4 taps: single cell (FIR + counter in one cell).
        For >4 taps: use a separate FIRFilterBlock for the anti-alias
        filter followed by a DecimatorBlock with simple [1.0] coefficient.
        """
        n_taps = len(self._coefficients)

        data = [DataWord(f"c{i}", c, address=i + 1)
                for i, c in enumerate(self._coefficients_q15)]
        data.append(DataWord("decim", self._decimation_factor, address=n_taps + 1))
        data.append(DataWord("one", 1, address=n_taps + 2))

        state = [StateVar(f"d{i}") for i in range(n_taps)]
        # Counter starts at M-1 so first sample triggers output (phase 0,
        # matching GNURadio's fir_filter_fff decimation convention)
        state.append(StateVar("counter", initial_value=self._decimation_factor - 1))

        # Build assembly dynamically
        lines = []
        # Shift delay line (oldest out first)
        for i in range(n_taps - 1, 0, -1):
            lines.append(f"    MOVE R{{state:d{i}}}, R{{state:d{i-1}}}")
        lines.append(f"    MOVE R{{state:d0}}, R{{in:sample}}")

        # Increment counter
        lines.append("    ADD R{state:counter}, R{data:one}")
        lines.append("    MOVE R{state:counter}, R0")

        # Check if counter == M
        lines.append("    CMP R{state:counter}, R{data:decim}")
        lines.append("    BR.NZ done")

        # Reset counter
        lines.append("    XOR R{state:counter}, R{state:counter}")
        lines.append("    MOVE R{state:counter}, R0")

        # Compute FIR: MULQ first tap, MACQ rest
        lines.append("    MULQ R{state:d0}, R{data:c0}")
        for i in range(1, n_taps):
            lines.append(f"    MACQ R{{state:d{i}}}, R{{data:c{i}}}")

        lines.append("    {write:out}")
        lines.append("    {jump:out}")
        lines.append("done:")
        lines.append("    HALT")

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
        # Apply FIR filter
        filtered = np.convolve(input_samples, self._coefficients, mode='full')[:len(input_samples)]
        # Decimate
        return filtered[::self._decimation_factor].astype(np.float32)
