"""IIRBiquadBlock — see :class:`IIRBiquadBlock`."""
import numpy as np
from ..block import CellProgram, Port, EntryPoint, StateVar, DataWord
from typing import Dict, List
from ._base import KyttarBlock, BlockInterface, assemble_to_words, float_to_q15, q15_to_float


class IIRBiquadBlock(KyttarBlock):
    """
    IIR Biquad filter block.

    Implements second-order IIR filter using Direct Form I:
    y[n] = b0*x[n] + b1*x[n-1] + b2*x[n-2] - a1*y[n-1] - a2*y[n-2]

    Can implement lowpass, highpass, bandpass, notch, etc.

    Interface (defaults):
    - Entry: R1
    - Input: R31 (single sample)
    """
    CATEGORY = "filtering"
    TAGS = ["iir", "biquad", "filter", "filtering"]

    _interface = BlockInterface(entry_address=1, input_registers=[31], output_registers=[31])

    def __init__(self, name: str, b_coeffs: List[float], a_coeffs: List[float]):
        """
        Initialize IIR biquad block.

        Args:
            name: Block name
            b_coeffs: Feedforward coefficients [b0, b1, b2]
            a_coeffs: Feedback coefficients [a1, a2] (a0 is assumed to be 1)
        """
        super().__init__(name, b_coeffs=b_coeffs, a_coeffs=a_coeffs)
        self._b_coeffs = list(b_coeffs)
        self._a_coeffs = list(a_coeffs)

        # Convert to Q15, but handle potential overflow for a coeffs > 1
        self._b_q15 = [float_to_q15(b) for b in b_coeffs]
        self._a_q15 = [float_to_q15(min(1.0, max(-1.0, a))) for a in a_coeffs]

    @property
    def cell_count(self) -> int:
        return 1

    @property
    def b_coefficients(self) -> List[float]:
        return self._b_coeffs

    @property
    def a_coefficients(self) -> List[float]:
        return self._a_coeffs

    @property
    def interface(self) -> BlockInterface:
        return self._interface

    def build_cell_programs(self) -> Dict[int, CellProgram]:
        """New-style IIR biquad: y[n] = b0*x[n]+b1*x1+b2*x2-a1*y1-a2*y2."""
        return {0: CellProgram(
            inputs=[Port("sample", register=0)],
            outputs=[Port("out")],
            entries=[EntryPoint("default")],
            data=[
                DataWord("b0", self._b_q15[0], address=1),
                DataWord("b1", self._b_q15[1], address=2),
                DataWord("b2", self._b_q15[2], address=3),
                DataWord("a1", self._a_q15[0], address=4),
                DataWord("a2", self._a_q15[1], address=5),
            ],
            state=[
                StateVar("x1"), StateVar("x2"),
                StateVar("y1"), StateVar("y2"),
                StateVar("x_save"), StateVar("y_save"),
            ],
            assembly_template="""\
start:
    MOVE R{state:x_save}, R{in:sample}
    MULQ R{in:sample}, R{data:b0}
    MACQ R{state:x1}, R{data:b1}
    MACQ R{state:x2}, R{data:b2}
    MSUQ R{state:y1}, R{data:a1}
    MSUQ R{state:y2}, R{data:a2}
    MOVE R{state:y_save}, R0
    MOVE R{state:x2}, R{state:x1}
    MOVE R{state:x1}, R{state:x_save}
    MOVE R{state:y2}, R{state:y1}
    MOVE R{state:y1}, R{state:y_save}
    {write:out}
    {jump:out}
""",
        )}

    def process_reference(self, input_samples: np.ndarray) -> np.ndarray:
        """Reference implementation."""
        output = np.zeros_like(input_samples, dtype=np.float64)
        x_hist = [0.0, 0.0]  # x[n-1], x[n-2]
        y_hist = [0.0, 0.0]  # y[n-1], y[n-2]

        b0, b1, b2 = self._b_coeffs
        a1, a2 = self._a_coeffs

        for i, x_n in enumerate(input_samples):
            y_n = (b0 * x_n + b1 * x_hist[0] + b2 * x_hist[1]
                   - a1 * y_hist[0] - a2 * y_hist[1])

            # Update history
            x_hist[1] = x_hist[0]
            x_hist[0] = float(x_n)
            y_hist[1] = y_hist[0]
            y_hist[0] = y_n

            output[i] = y_n

        return output.astype(np.float32)

    @classmethod
    def lowpass(cls, name: str, cutoff: float, sample_rate: float, q: float = 0.707) -> 'IIRBiquadBlock':
        """Create a lowpass biquad filter."""
        omega = 2.0 * np.pi * cutoff / sample_rate
        alpha = np.sin(omega) / (2.0 * q)

        b0 = (1.0 - np.cos(omega)) / 2.0
        b1 = 1.0 - np.cos(omega)
        b2 = (1.0 - np.cos(omega)) / 2.0
        a0 = 1.0 + alpha
        a1 = -2.0 * np.cos(omega)
        a2 = 1.0 - alpha

        # Normalize by a0
        return cls(name, [b0/a0, b1/a0, b2/a0], [a1/a0, a2/a0])

    @classmethod
    def highpass(cls, name: str, cutoff: float, sample_rate: float, q: float = 0.707) -> 'IIRBiquadBlock':
        """Create a highpass biquad filter."""
        omega = 2.0 * np.pi * cutoff / sample_rate
        alpha = np.sin(omega) / (2.0 * q)

        b0 = (1.0 + np.cos(omega)) / 2.0
        b1 = -(1.0 + np.cos(omega))
        b2 = (1.0 + np.cos(omega)) / 2.0
        a0 = 1.0 + alpha
        a1 = -2.0 * np.cos(omega)
        a2 = 1.0 - alpha

        return cls(name, [b0/a0, b1/a0, b2/a0], [a1/a0, a2/a0])

    @classmethod
    def bandpass(cls, name: str, center: float, bandwidth: float, sample_rate: float) -> 'IIRBiquadBlock':
        """Create a bandpass biquad filter."""
        omega = 2.0 * np.pi * center / sample_rate
        alpha = np.sin(omega) * np.sinh(np.log(2.0) / 2.0 * bandwidth * omega / np.sin(omega))

        b0 = alpha
        b1 = 0.0
        b2 = -alpha
        a0 = 1.0 + alpha
        a1 = -2.0 * np.cos(omega)
        a2 = 1.0 - alpha

        return cls(name, [b0/a0, b1/a0, b2/a0], [a1/a0, a2/a0])
