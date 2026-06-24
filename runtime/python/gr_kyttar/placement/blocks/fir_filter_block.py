"""FIRFilterBlock — see :class:`FIRFilterBlock`."""
import numpy as np
import math
from ..block import CellProgram, Port, EntryPoint, StateVar, DataWord
from typing import Dict, List
from ._base import KyttarBlock, BlockInterface, assemble_to_words, float_to_q15, q15_to_float


class FIRFilterBlock(KyttarBlock):
    """
    FIR Filter Block — General-Purpose Multi-Cell FIR.

    output = sum(coeff[i] * delay[i]) for i in 0..N-1

    For ≤7 taps: single cell (v2 template, compact).
    For >7 taps: multi-cell wavefront using the same chained partial-sum
    architecture as RRCPulseShaperBlock (TAPS_PER_CELL=5).

    Single-cell ceiling (MAX_SINGLE_CELL_TAPS = 7) is the register budget, NOT a
    tuned number: a 1-cell FIR of N taps packs N coefficient data words + N
    delay-line state regs + 1 input reg + its program (≈2N+2 instructions) into
    one 32-word cell (R31 reserved for HALT). N=7 fits; N=8 overflows ("No
    register space for state d4"). The old <=12 threshold tried single-cell for
    8..12 taps and the build failed; those now fold to multi-cell.

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
    # Largest tap count that fits one cell's ~31-register budget (see class
    # docstring). 8+ taps overflow → fold to the multi-cell wavefront.
    MAX_SINGLE_CELL_TAPS = 7
    # Cells per column in the multi-cell placement FOLD (see default_layout).
    FOLD_HEIGHT = 4

    @property
    def cell_count(self) -> int:
        import math
        if self._num_taps <= self.MAX_SINGLE_CELL_TAPS:
            return 1  # Single-cell fits within the register budget
        return math.ceil(self._num_taps / self.TAPS_PER_CELL)

    def default_layout(self):
        """Place the multi-cell wavefront as a column-major serpentine FOLD, NOT
        the base class's single straight row.

        The wavefront snakes DOWN a column of ``FOLD_HEIGHT`` cells, OVER one, and
        UP the next column, repeating. This keeps a large FIR COMPACT (8 cells →
        a 2×4 block, not a 1×8 strip stretched across the array) and — for a
        2-column fold — lands the INPUT cell (cell 0, top of column 0) and the
        OUTPUT cell (last cell, top of column 1) SIDE BY SIDE on the top edge, so
        both the ingress and egress corridors leave from the same face. Each
        cell's face points at its successor in the chain (its forwarding / JUMP
        target): south down a column, east across the turn, north back up.

        Single-cell FIRs (≤ MAX_SINGLE_CELL_TAPS) use the trivial 1-cell layout.
        """
        n = self.cell_count
        if n <= 1:
            return {0: (0, 0, "east")}
        H = self.FOLD_HEIGHT
        pos = {}
        for i in range(n):
            col, r = divmod(i, H)
            dy = r if (col % 2 == 0) else (H - 1 - r)
            pos[i] = (col, dy)
        layout = {}
        for i in range(n):
            dx, dy = pos[i]
            if i + 1 in pos:
                nx, ny = pos[i + 1]
                face = ("east" if nx > dx else "west" if nx < dx
                        else "south" if ny > dy else "north")
            else:
                # Last cell: continue along the column's travel direction (north
                # for an up-going column) so the output egress leaves cleanly.
                face = "north" if (i // H) % 2 == 1 else "south"
            layout[i] = (dx, dy, face)
        return layout

    @property
    def num_taps(self) -> int:
        return self._num_taps

    @property
    def interface(self) -> BlockInterface:
        return self._interface

    def build_cell_programs(self) -> Dict[int, CellProgram]:
        """FIR filter: single-cell for ≤MAX_SINGLE_CELL_TAPS, multi-cell larger.

        The multi-cell path is FIR's OWN chained partial-sum (systolic) wavefront
        — NOT borrowed from RRCPulseShaperBlock, whose per-segment coefficient
        REVERSAL is only correct for symmetric taps (it silently mis-convolves a
        general/asymmetric FIR). See :meth:`_build_multicell_programs`.
        """
        if self._num_taps > self.MAX_SINGLE_CELL_TAPS:
            return self._build_multicell_programs()

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

    def _build_multicell_programs(self) -> Dict[int, CellProgram]:
        """Multi-cell systolic FIR: a single delay line spread across cells.

        Architecture (a chained partial-sum wavefront, one cell per
        ``TAPS_PER_CELL`` taps). Per input sample the wavefront runs cell 0 →
        cell 1 → … → cell M-1, each cell:
          * shifts its delay-line SEGMENT, ingesting this cell's incoming sample;
          * MACs its segment against its coefficients;
          * ADDs the partial sum arriving from the previous cell;
          * forwards (a) the new partial sum and (b) the sample SHIFTED OUT of its
            segment to the next cell, then JUMPs to trigger it.
        The last cell WRITEs/JUMPs its final sum to the block output.

        Cell ``m`` therefore sees the input stream delayed by ``offset_m`` samples
        (the total length of all preceding segments), so its register ``i`` (after
        the shift) holds ``x[n - offset_m - (L_m-1-i)]`` — i.e. delay
        ``d = offset_m + L_m-1-i``. To match the single-cell / GNU Radio
        convention ``y[n] = Σ_d coeff[N-1-d]·x[n-d]``, register ``i`` must be
        multiplied by ``coeff[N-1-d] = coeff[N - offset_{m+1} + i]``. Hence cell
        ``m`` takes the coefficient segment ``coeff[N-offset_{m+1} : N-offset_m]``
        in FORWARD order — segments assigned from the END of the tap array, the
        LAST cell getting the FIRST taps. (RRC instead reversed
        ``coeff[m*K:(m+1)*K]``; that coincides only for symmetric taps.)
        """
        K = self.TAPS_PER_CELL
        N = self._num_taps
        n_cells = self.cell_count
        # offset_m = number of taps in all cells before m (= m*K for full cells).
        offsets = [min(m * K, N) for m in range(n_cells + 1)]
        offsets[n_cells] = N

        programs: Dict[int, CellProgram] = {}
        for m in range(n_cells):
            start, end = offsets[m], offsets[m + 1]
            L = end - start
            is_first = (m == 0)
            is_last = (m == n_cells - 1)

            # Coefficients: coeff[N-end : N-start], forward order (derived above).
            cell_coeffs = self._coeff_q15[N - end:N - start]
            data = [DataWord(f"c{i}", cell_coeffs[i], address=i + 1)
                    for i in range(L)]

            state = [StateVar(f"d{i}") for i in range(L)]
            if not is_last:
                state.append(StateVar("old_save"))  # oldest sample, forwarded

            if is_first:
                inputs = [Port("sample", register=0)]
            else:
                partial_reg = (L + 1) + len(state)
                inputs = [Port("sample", register=0),
                          Port("partial", register=partial_reg)]

            if is_last:
                outputs = [Port("out")]
            else:
                outputs = [Port("partial"), Port("sample_out"), Port("fwd")]

            lines = []
            if not is_last:
                lines.append("    MOVE R{state:old_save}, R{state:d0}")
            for i in range(L - 1):
                lines.append(f"    MOVE R{{state:d{i}}}, R{{state:d{i+1}}}")
            lines.append(f"    MOVE R{{state:d{L-1}}}, R{{in:sample}}")
            lines.append("    MULQ R{state:d0}, R{data:c0}")
            for i in range(1, L):
                lines.append(f"    MACQ R{{state:d{i}}}, R{{data:c{i}}}")
            if not is_first:
                lines.append("    ADD R0, R{in:partial}")
            if is_last:
                lines.append("    {write:out}")
                lines.append("    {jump:out}")
            else:
                lines.append("    {write:partial}")
                lines.append("    MOVE R0, R{state:old_save}")
                lines.append("    {write:sample_out}")
                lines.append("    {jump:fwd}")

            template = "start:\n" + "\n".join(lines) + "\n"
            programs[m] = CellProgram(
                inputs=inputs,
                outputs=outputs,
                entries=[EntryPoint("default")],
                data=data,
                state=state,
                assembly_template=template,
            )
        return programs

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
