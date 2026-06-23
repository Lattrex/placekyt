"""RRCPulseShaperBlock — see :class:`RRCPulseShaperBlock`."""
import numpy as np
import math
from ..block import CellProgram, Port, EntryPoint, StateVar, DataWord
from typing import Dict, List
from ._base import KyttarBlock, BlockInterface, assemble_to_words, float_to_q15, q15_to_float


class RRCPulseShaperBlock(KyttarBlock):
    """
    Root Raised Cosine (RRC) Pulse Shaper Block (2 cells).

    Implements pulse shaping for MIL-STD-188-110B with excess bandwidth
    factor alpha = 0.35. This provides Nyquist filtering to minimize ISI.

    Architecture: 2-Cell FIR Filter
    ===============================

    Uses a spatial FIR filter with RRC coefficients. The filter is truncated
    to fit in 2 cells (16 taps each = 32 taps total).

    Cell Layout:
    ```
        In → [FIR_A] → [FIR_B] → Out
    ```

    Components:
    - FIR_A (1 cell): First N RRC taps
    - FIR_B (1 cell): Remaining RRC taps

    RRC Filter Parameters:
        - Alpha (excess bandwidth): 0.35
        - Filter length: span*sps+1 taps (odd-length, matches GNURadio)
        - Sampling rate: 4× symbol rate = 9600 Hz

    Interface:
        - Entry: R1
        - Input: R31 (upsampled symbols)
        - Output: Pulse-shaped samples
    """
    CATEGORY = "filtering"
    TAGS = ["rrc", "pulse_shaper", "filtering"]

    _interface = BlockInterface(entry_address=1, input_registers=[31], output_registers=[31])

    ALPHA = 0.35
    TAPS_PER_CELL = 5  # Max taps per cell given register budget constraints
    SAMPLES_PER_SYMBOL = 4

    def __init__(
        self,
        name: str,
        alpha: float = 0.35,
        span: int = 8,
    ):
        """
        Initialize RRC Pulse Shaper.

        Args:
            name: Block name
            alpha: Excess bandwidth factor (default 0.35)
            span: Filter span in symbols (default 8)
        """
        super().__init__(name, alpha=alpha, span=span)
        self._alpha = alpha
        self._span = span
        # Odd-length FIR (standard for symmetric filters, matches GNURadio)
        self._num_taps = span * self.SAMPLES_PER_SYMBOL + 1

        # Generate RRC coefficients
        self._coefficients = self._generate_rrc_coefficients()
        self._coeff_q15 = [float_to_q15(c) for c in self._coefficients]

    def _generate_rrc_coefficients(self) -> List[float]:
        """Generate RRC filter coefficients."""
        n_taps = self._num_taps
        taps = []

        for i in range(n_taps):
            t = (i - n_taps // 2) / self.SAMPLES_PER_SYMBOL
            if abs(t) < 1e-10:
                # t = 0
                h = 1.0 + self._alpha * (4 / np.pi - 1)
            elif abs(abs(t) - 1 / (4 * self._alpha)) < 1e-10:
                # t = ±1/(4α)
                h = (self._alpha / np.sqrt(2)) * (
                    (1 + 2 / np.pi) * np.sin(np.pi / (4 * self._alpha)) +
                    (1 - 2 / np.pi) * np.cos(np.pi / (4 * self._alpha))
                )
            else:
                # General case
                num = np.sin(np.pi * t * (1 - self._alpha)) + 4 * self._alpha * t * np.cos(np.pi * t * (1 + self._alpha))
                den = np.pi * t * (1 - (4 * self._alpha * t) ** 2)
                if abs(den) > 1e-10:
                    h = num / den
                else:
                    h = 0.0
            taps.append(h)

        # Normalize to DC gain = 1 (sum of taps = 1.0), matching GNURadio
        # convention. This prevents output clipping for any symbol amplitude
        # within Q15 range.
        tap_sum = sum(taps)
        if abs(tap_sum) > 1e-10:
            taps = [h / tap_sum for h in taps]

        return taps

    @property
    def cell_count(self) -> int:
        import math
        return math.ceil(self._num_taps / self.TAPS_PER_CELL)

    @property
    def interface(self) -> BlockInterface:
        return self._interface

    @property
    def coefficients(self) -> List[float]:
        return self._coefficients

    def build_cell_programs(self) -> Dict[int, CellProgram]:
        """Multi-cell FIR filter with chained partial accumulation.

        Each cell processes TAPS_PER_CELL taps of the delay line.
        Cell 0 receives the new sample, shifts its delay segment, computes
        a partial MAC, then forwards the oldest sample and partial sum to
        cell 1.  Each subsequent cell does the same, adding the incoming
        partial sum to its own MAC result.  The last cell outputs the final
        filtered value.
        """
        n_cells = self.cell_count
        programs = {}

        for cell_idx in range(n_cells):
            start_tap = cell_idx * self.TAPS_PER_CELL
            end_tap = min(start_tap + self.TAPS_PER_CELL, self._num_taps)
            n_taps = end_tap - start_tap
            is_first = (cell_idx == 0)
            is_last = (cell_idx == n_cells - 1)

            # Coefficients as data words — reversed within each cell.
            # In the MAC loop, c[0] multiplies d[0] (oldest sample in
            # this cell's segment) and c[N-1] multiplies d[N-1] (newest).
            # Cell 0 holds x[n]..x[n-4], cell 1 holds x[n-5]..x[n-9], etc.
            # For y[n]=sum(h[k]*x[n-k]): d[N-1-j]=x[n-start_tap-j], so
            # c[N-1-j] should equal h[start_tap+j], i.e. reversed.
            cell_coeffs = list(reversed(
                self._coeff_q15[start_tap:end_tap]
            ))
            data = [DataWord(f"c{i}", cell_coeffs[i], address=i + 1)
                    for i in range(n_taps)]

            # State: delay line for this cell's segment
            state = [StateVar(f"d{i}") for i in range(n_taps)]
            if not is_last:
                # Need a register to save the oldest sample before shifting
                state.append(StateVar("old_save"))

            # Input ports — compute explicit register for partial to allow
            # the test/resolver to know where to WRITE the partial sum
            if is_first:
                inputs = [Port("sample", register=0)]
            else:
                # partial register goes right after data + state in the gap
                n_state = len(state)
                partial_reg = (n_taps + 1) + n_state  # max_data_addr+1 + state_count
                inputs = [Port("sample", register=0), Port("partial", register=partial_reg)]

            # Output ports
            outputs = []
            if not is_last:
                outputs.append(Port("partial"))
                outputs.append(Port("sample_out"))
                outputs.append(Port("fwd"))  # jump target
            else:
                outputs.append(Port("out"))

            # Build assembly
            lines = []

            # Save oldest delay value before shift (for forwarding)
            if not is_last:
                lines.append(f"    MOVE R{{state:old_save}}, R{{state:d0}}")

            # Shift delay line: d[0]=d[1], ..., d[N-2]=d[N-1], d[N-1]=sample
            for i in range(n_taps - 1):
                lines.append(f"    MOVE R{{state:d{i}}}, R{{state:d{i+1}}}")
            lines.append(f"    MOVE R{{state:d{n_taps - 1}}}, R{{in:sample}}")

            # MAC accumulation
            lines.append(f"    MULQ R{{state:d0}}, R{{data:c0}}")
            for i in range(1, n_taps):
                lines.append(f"    MACQ R{{state:d{i}}}, R{{data:c{i}}}")

            # Add incoming partial sum (non-first cells)
            if not is_first:
                lines.append(f"    ADD R0, R{{in:partial}}")

            if is_last:
                # Output final result
                lines.append("    {write:out}")
                lines.append("    {jump:out}")
            else:
                # Forward partial sum and oldest sample to next cell
                lines.append("    {write:partial}")
                lines.append(f"    MOVE R0, R{{state:old_save}}")
                lines.append("    {write:sample_out}")
                lines.append("    {jump:fwd}")

            template = "start:\n" + "\n".join(lines) + "\n"

            programs[cell_idx] = CellProgram(
                inputs=inputs,
                outputs=outputs,
                entries=[EntryPoint("default")],
                data=data,
                state=state,
                assembly_template=template,
            )

        return programs

    def process_reference(self, input_samples: np.ndarray) -> np.ndarray:
        """
        Reference implementation of RRC pulse shaping.

        Args:
            input_samples: Input samples (typically upsampled symbols)

        Returns:
            Pulse-shaped output samples
        """
        # Convolve with RRC filter
        coeffs = np.array(self._coefficients, dtype=np.float32)
        output = np.convolve(input_samples, coeffs, mode='same')
        return output.astype(np.float32)

    def reset(self):
        """Reset pulse shaper state (stateless for FIR)."""
        pass
