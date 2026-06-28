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
        gain: float = 1.0,
        sampling_freq: float = 4.0,
        symbol_rate: float = 1.0,
        alpha: float = 0.35,
        ntaps: int = 33,
    ):
        """
        Initialize RRC Pulse Shaper — matches GNU Radio's
        ``filter.firdes.root_raised_cosine(gain, sampling_freq, symbol_rate,
        alpha, ntaps)`` (the GRC **Root Raised Cosine Taps** a user would feed a
        FIR) VERBATIM. The coefficients are computed by the bit-exact firdes port
        in ``_firdes.root_raised_cosine`` and run on the verified FIR datapath.

        Args:
            name: Block name.
            gain: passband/scale gain — the taps are scaled so their SUM == gain
                (GR firdes convention), default 1.0.
            sampling_freq: sample rate; ``sampling_freq/symbol_rate`` =
                samples-per-symbol. Default 4.0 (with symbol_rate 1.0 → 4 sps).
            symbol_rate: symbol rate (same units as sampling_freq). Default 1.0.
            alpha: rolloff / excess-bandwidth factor, default 0.35.
            ntaps: number of taps (forced ODD, GR adds 1 if even). Default 33
                (= the old span-8 @ 4 sps length, for back-compat defaults).

        (Previously this took ``alpha, span`` and a hand-rolled formula normalized
        to DC gain 1 — that did NOT match firdes on any axis but the default. Now
        it mirrors firdes.root_raised_cosine exactly. ``samples_per_symbol`` is
        derived from sampling_freq/symbol_rate, not hardcoded.)
        """
        super().__init__(name, gain=gain, sampling_freq=sampling_freq,
                         symbol_rate=symbol_rate, alpha=alpha, ntaps=ntaps)
        self._gain = float(gain)
        self._sampling_freq = float(sampling_freq)
        self._symbol_rate = float(symbol_rate)
        self._alpha = float(alpha)
        ntaps = int(ntaps)
        if ntaps % 2 == 0:
            ntaps += 1
        self._num_taps = ntaps
        # samples-per-symbol derived from the GR params (not hardcoded).
        self._sps = self._sampling_freq / self._symbol_rate

        # firdes.root_raised_cosine taps (bit-exact port).
        self._coefficients = self._generate_rrc_coefficients()
        self._coeff_q15 = [float_to_q15(c) for c in self._coefficients]

    def _generate_rrc_coefficients(self) -> List[float]:
        """firdes.root_raised_cosine(gain, sampling_freq, symbol_rate, alpha,
        ntaps) taps — the bit-exact port in ``_firdes`` (NOT a hand-rolled formula).
        The taps are scaled so their SUM == gain (GR firdes convention)."""
        from . import _firdes
        return _firdes.root_raised_cosine(
            self._gain, self._sampling_freq, self._symbol_rate,
            self._alpha, self._num_taps)

    # Fold the FIR pipeline into a compact ~4-wide serpentine instead of one flat
    # row, matching the other multi-cell blocks. The default 33-tap shaper is 7
    # cells; at width 4 it folds to a 4x2 footprint (row 0 east, row 1 west) rather
    # than sprawling 1x7 across the array — so auto-place can pack it in a band and
    # the bus taps it without a 7-wide wall. Geometry only: the cells still abut
    # consecutively along the snake, so the linear cell N -> N+1 @1 handoff (the
    # feed-forward partial-sum chain, no internal feedback) lands on the next cell
    # exactly as it did flat. The DSP/program is unchanged.
    FOLD_LAYOUT_WIDTH = 4

    @property
    def cell_count(self) -> int:
        import math
        return math.ceil(self._num_taps / self.TAPS_PER_CELL)

    def default_layout(self):
        """Compact folded serpentine (FOLD_LAYOUT_WIDTH-wide) for this feed-forward
        FIR, so the shaper occupies ~2 rows instead of a flat 1xN line. Reuses the
        base boustrophedon helper (east on row 0, south at the turn-down cell, west
        on row 1), which keeps consecutive cells abutting so the linear @1 handoff
        between cells still lands on the next cell. Geometry only."""
        return self._serpentine_layout(self.cell_count, self.FOLD_LAYOUT_WIDTH)

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
