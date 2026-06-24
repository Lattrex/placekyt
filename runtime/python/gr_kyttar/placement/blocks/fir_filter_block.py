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

    For ≤MAX_SINGLE_CELL_TAPS taps: single cell (v2 template, compact).
    Larger: multi-cell chained partial-sum (systolic) wavefront, TAPS_PER_CELL
    taps per cell.

    SATURATING Q15 ACCUMULATION — clamp ONCE, at the END (production DSP)
    --------------------------------------------------------------------
    The cell ALU's MACQ/ADD are 16-bit and WRAP on signed overflow (modulo
    2^16), which flips the sign on overload and produces garbage — the opposite
    of every production fixed-point FIR (TI C5x/C6x, etc.) which SATURATE the
    accumulator (clamp to ±full-scale). GNU Radio's ``fir_filter_fff`` is float
    and never overflows, so the correct Q15 equivalent of "what GR computes,
    clamped to the representable range" is a SATURATING accumulator.

    This block clamps the accumulator EXACTLY ONCE — on the FINAL accumulation,
    just before the output WRITE — NOT after every tap. The whole filter (single
    cell, or the entire cross-cell wavefront) is ONE logical accumulator; only
    its final value is saturated. Intermediate partial sums (the running MACQ
    taps, and each cell's forwarded cross-cell partial) are left WRAPPED — they
    are allowed to swing past full-scale and back. This is both correct DSP and
    cheaper:

      * Clamping per tap would alter the math (re-normalising legitimate mid-sum
        excursions) and so MASK real overload — an overdriven filter would emit a
        clean rescaled signal instead of the flat-topped ±full-scale rails it
        must show.
      * Per-tap clamping also costs ~3 extra instructions PER TAP, which
        collapses the per-cell tap density (it dropped TAPS_PER_CELL to 2 and
        the single-cell ceiling to 3, exploding a 20-tap FIR to ~10 cells).
        END-only clamping costs the chain just ONE clamp, restoring the density.

    The hardware has no auto-saturating ALU mode; the final MACQ/ADD leaves the
    WRAPPED value in R0 but sets the V (signed-overflow) flag and an N flag
    reflecting the wrapped result's sign. On overflow the wrapped sign is
    INVERTED relative to the true sum, so:

        true sum > +full-scale  ⇒  wrapped is NEGATIVE (N=1)  ⇒  clamp to +0x7FFF
        true sum < −full-scale  ⇒  wrapped is POSITIVE (N=0)  ⇒  clamp to −0x8000

    The 3-instruction software clamp exploits ``0x8000 − (R0>>15)``:

        BR.NV +2              ; common path: no overflow → skip the clamp
        SHR   R0, #15         ; R0 = wrapped-result top bit (0 or 1), logical
        SUB   R{satneg}, R0   ; R0 = 0x8000 − bit  ⇒  N? 0x7FFF : 0x8000

    i.e. one branch on the hot path, two instructions on the (rare) overflow
    path, and a single shared 0x8000 data word per cell carrying the clamp. The
    priming MULQ is NEVER clamped: a single Q15 product (a·b)>>15 is always
    representable, but MULQ sets V from the RAW 32-bit product, so clamping there
    would saturate spuriously.

    BUDGET (END-only clamp, re-derived against the resolver's own allocator)
    -----------------------------------------------------------------------
    Because the clamp is paid ONCE, the per-cell tap density is back to the old
    wrapping FIR's. A cell holds, at addr 0..31: L coeffs + (optionally) satneg +
    L delay regs (+ old_save on non-last cells) + the input reg + the program.

      * Single cell (MAX_SINGLE_CELL_TAPS = 6): the whole filter is one cell, so
        it carries N coeffs + satneg + N delay regs + 1 input + ≈(2N+2) program
        instructions + the 3-instruction clamp. N=6 fits the 32-word cell; N=7
        overflows (the 7th delay register has no free gap register). This is one
        tap below the wrapping FIR's 7 — the single, one-time clamp costs it.
      * Multi-cell (TAPS_PER_CELL = 5): the densest role is a MID cell — L coeffs
        + L delay regs + old_save + partial-input + program — which fits at L=5
        and overflows at L=6. The LAST cell additionally carries the clamp but
        has NO old_save register, so a FULL last segment (L=5) + clamp still
        fits. Hence 7+ taps fold to ⌈N/5⌉ cells (a 20-tap FIR = 4 cells).

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

    # END-ONLY clamping costs the chain only ONE 3-instruction clamp (on the
    # last cell / final accumulation), NOT 3 per tap — so the per-cell tap
    # density is restored to the old wrapping FIR's. Re-derived against the
    # resolver's own allocator (see class docstring for the budget derivation):
    # a mid cell at L=5 (5 coeffs + 5 delay regs + old_save + input + program)
    # is the densest role that fits the 32-word cell; L=6 overflows. The LAST
    # cell carries the clamp but has NO old_save reg, so a full last segment
    # (L=5) + clamp still fits.
    TAPS_PER_CELL = 5
    # Largest tap count that fits one cell's 32-word budget. The single cell
    # additionally carries the END-ONLY clamp (the whole filter is one cell), so
    # its ceiling is one tap below the wrapping FIR's 7: N=6 coeffs + N delay
    # regs + satneg + input + (~2N + clamp) instructions fits; N=7 overflows
    # (verified: the 7th delay reg has no gap register). 7+ taps fold to
    # multi-cell, where the clamp lives only on the last cell.
    MAX_SINGLE_CELL_TAPS = 6
    # Cells per column in the multi-cell placement FOLD (see default_layout).
    FOLD_HEIGHT = 4
    # Full-scale clamp constant: 0x8000 = -32768. The 3-instruction clamp
    # computes 0x8000 - (R0>>15) so this one word yields both +0x7FFF and
    # -0x8000 (see _clamp_lines / class docstring).
    SAT_NEG_Q15 = 0x8000

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
        # satneg gets an EXPLICIT address right after the coeffs; an auto address
        # would pack at 0 (R0 / the accumulator) and corrupt it.
        data.append(DataWord("satneg", self.SAT_NEG_Q15, address=self._num_taps + 1))
        state = [StateVar(f"d{i}") for i in range(self._num_taps)]

        lines = []
        for i in range(self._num_taps - 1):
            lines.append(f"    MOVE R{{state:d{i}}}, R{{state:d{i+1}}}")
        lines.append(f"    MOVE R{{state:d{self._num_taps - 1}}}, R{{in:sample}}")
        # The priming MULQ needs NO clamp: a single Q15 product (a*b)>>15 is
        # always representable, and MULQ sets V from the RAW 32-bit product (which
        # almost always exceeds i16) — clamping on it would saturate spuriously.
        lines.append(f"    MULQ R{{state:d0}}, R{{data:c0}}")
        for i in range(1, self._num_taps):
            lines.append(f"    MACQ R{{state:d{i}}}, R{{data:c{i}}}")
        # Clamp ONCE, on the FINAL accumulation, just before output (NOT per tap).
        # The last MACQ leaves V set iff the final sum overflowed the representable
        # range → saturate to ±full-scale (production-DSP semantics, and what shows
        # as flat-topped rails on an overdriven filter). Clamping per-tap instead
        # would (a) cost ~3 instr × every tap, exploding the cell count, and (b)
        # alter the math / hide real overload by re-normalising intermediate
        # partial sums that legitimately swing past full-scale and return.
        lines.extend(self._clamp_lines())
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

    def _clamp_lines(self):
        """The 3-instruction END-ONLY saturating clamp (see class docstring).

        Emitted ONCE, after the FINAL accumulation op (the last MACQ in the
        single cell, or the cross-cell ADD on the last multi-cell cell), just
        before the output WRITE — never per tap. If the signed final sum
        overflowed the 16-bit accumulator (V set), replace the WRAPPED R0 with
        the correct full-scale value: ``0x8000 - (R0>>15)`` = +0x7FFF when the
        wrapped result is negative (true overflow positive) else -0x8000 (true
        overflow negative). On no overflow the BR.NV skips both clamp
        instructions, so the common path costs one branch. SUB clobbers the
        flags, but the terminal WRITE/JUMP read no flags, so that is harmless.
        """
        return [
            "    BR.NV +2",
            "    SHR R0, #15",
            "    SUB R{data:satneg}, R0",
        ]

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
            # satneg at an EXPLICIT address after the coeffs (auto would land on
            # R0). Coeffs occupy 1..L, so satneg lives at L+1.
            data.append(DataWord("satneg", self.SAT_NEG_Q15, address=L + 1))

            state = [StateVar(f"d{i}") for i in range(L)]
            if not is_last:
                state.append(StateVar("old_save"))  # oldest sample, forwarded

            if is_first:
                inputs = [Port("sample", register=0)]
            else:
                # Coeffs at 1..L, satneg at L+1, then state regs, then the
                # explicit partial-input register after them.
                partial_reg = (L + 2) + len(state)
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
            # Priming MULQ needs no clamp (single Q15 product is always in
            # range; MULQ's V reflects the raw product, not an acc overflow).
            lines.append("    MULQ R{state:d0}, R{data:c0}")
            for i in range(1, L):
                lines.append(f"    MACQ R{{state:d{i}}}, R{{data:c{i}}}")
            if not is_first:
                lines.append("    ADD R0, R{in:partial}")
            if is_last:
                # Clamp ONCE, on the LAST cell's final accumulation, before the
                # block output. Intermediate cells forward their (wrapped) partial
                # sum unclamped — the whole chain is one logical accumulator and
                # only its final value is saturated (production-DSP semantics, and
                # what makes an overdriven filter show flat-topped rails). Per-tap
                # / per-cell clamping would explode the cell count and re-normalise
                # legitimate mid-sum excursions, hiding real overload.
                lines.extend(self._clamp_lines())
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

    # --- Q15 saturating reference (models the hardware datapath EXACTLY) -------

    @staticmethod
    def _to_s16(v: int) -> int:
        v &= 0xFFFF
        return v - 0x10000 if v >= 0x8000 else v

    @classmethod
    def _wrap_acc(cls, acc: int, addend: int) -> int:
        """One WRAPPING accumulation step in Q15, bit-exact with the hardware
        ``ADD/MACQ`` WITHOUT a clamp: the 16-bit ALU simply wraps (modulo 2^16,
        sign-extended). Intermediate sums are left wrapped — only the FINAL
        accumulation is clamped (see :meth:`_clamp_final`)."""
        return cls._to_s16((acc + addend) & 0xFFFF)

    @classmethod
    def _clamp_final(cls, acc: int, last_addend: int) -> int:
        """The END-ONLY saturating clamp, bit-exact with the hardware BR.NV /
        SHR / SUB sequence applied to the FINAL accumulation only.

        ``acc`` is the running sum BEFORE the last addend; ``last_addend`` is the
        final MACQ tap (single cell) or cross-cell partial (last multi-cell
        cell). The hardware computes the wrapped final sum and sets V iff that
        addition overflowed the signed 16-bit range; on V it replaces R0 with
        ``0x8000 - (wrapped>>15)`` = ±full-scale. The V flag reflects the LAST
        op only, exactly as the chip does — intermediate wrap is invisible to it.
        """
        true_sum = acc + last_addend
        wrapped = cls._to_s16(true_sum & 0xFFFF)
        if -32768 <= true_sum <= 32767:
            return wrapped
        bit = (wrapped & 0xFFFF) >> 15               # SHR R0,#15 (logical)
        return cls._to_s16((0x8000 - bit) & 0xFFFF)  # SUB satneg, R0

    @staticmethod
    def _macq(a_q15: int, b_q15: int) -> int:
        """Q15 product term: (a*b) >> 15, arithmetic, as the ALU computes it."""
        return (FIRFilterBlock._to_s16(a_q15) * FIRFilterBlock._to_s16(b_q15)) >> 15

    def process_reference_q15(self, input_q15) -> list:
        """Bit-exact Q15 END-ONLY-SATURATING reference, in the SAME accumulation
        order as the built datapath (single-cell vs multi-cell wavefront).

        Mirrors the hardware exactly: every intermediate MACQ tap and every
        cross-cell partial-sum ADD simply WRAPS (16-bit modulo, sign-extended);
        the single saturating clamp is applied ONLY to the FINAL accumulation
        (the last MACQ in a single cell, or the cross-cell ADD on the last
        multi-cell cell). This — NOT the float ideal — is what a correct
        end-only-saturating fixed-point FIR produces, so it is the golden
        predictor the overload test compares the DUT against. (For in-range
        stimulus no accumulation overflows, so it equals GNU Radio's float output
        clipped to the Q15 range, and the existing GR comparison still holds.)

        Returns one signed Q15 int per input sample.
        """
        coeffs = self._coeff_q15
        N = self._num_taps
        delay = [0] * N
        out = []
        if N <= self.MAX_SINGLE_CELL_TAPS:
            # Single cell — mirrors build_cell_programs EXACTLY. The delay line is
            # shifted ``MOVE d{i}, d{i+1}`` then ``MOVE d{N-1}, sample``, so d0
            # holds the OLDEST sample and d{N-1} the newest; register d{i} is
            # multiplied by coeff c{i}. Model that with the newest sample at the
            # END of ``delay`` (delay[i] == d{i}). Then: acc = d0*c0 (priming
            # MULQ, no clamp); for 0<i<N-1 acc = wrap(acc + di*ci); the LAST tap
            # is the only clamped op: acc = clamp_final(acc, d{N-1}*c{N-1}).
            for s in input_q15:
                delay = delay[1:] + [self._to_s16(int(s) & 0xFFFF)]
                acc = self._macq(delay[0], coeffs[0])     # priming MULQ, no clamp
                if N == 1:
                    out.append(acc & 0xFFFF)              # (not reached: N>=2)
                    continue
                for i in range(1, N - 1):
                    acc = self._wrap_acc(acc, self._macq(delay[i], coeffs[i]))
                acc = self._clamp_final(acc, self._macq(delay[N - 1], coeffs[N - 1]))
                out.append(acc & 0xFFFF)
            return out

        # Multi-cell wavefront — a CELL-ACCURATE model of the systolic datapath.
        # Each cell holds its OWN segment delay line; per input sample the
        # wavefront runs cell 0 → cell N-1. A cell: saves its oldest sample
        # (old_save = d0), shifts its segment ingesting the INCOMING sample into
        # d{L-1}, MACs its taps (all WRAPPING), ADDs the partial sum forwarded
        # from the previous cell (WRAPPING), and forwards the new partial AND its
        # saved oldest sample on to the next cell. ONLY the LAST cell's final
        # accumulation is clamped — every intermediate cell forwards its wrapped
        # partial sum unclamped (the whole chain is one logical accumulator). The
        # last cell's final op is its cross-cell ADD, so that is where the clamp
        # lands. Cell 0's incoming sample is x[n]; every later cell's incoming
        # sample is the previous cell's shifted-out oldest sample (NOT a
        # global-delay-line index — the inter-cell forwarding IS the delay).
        # Cell m owns coefficients coeff[N-offset_{m+1} : N-offset_m] in forward
        # order — mirrors :meth:`_build_multicell_programs` exactly.
        K = self.TAPS_PER_CELL
        n_cells = self.cell_count
        offsets = [min(m * K, N) for m in range(n_cells + 1)]
        offsets[n_cells] = N
        seg = [[0] * (offsets[m + 1] - offsets[m]) for m in range(n_cells)]
        for s in input_q15:
            incoming = self._to_s16(int(s) & 0xFFFF)
            partial = None
            for m in range(n_cells):
                start, end = offsets[m], offsets[m + 1]
                L = end - start
                is_last = (m == n_cells - 1)
                seg_coeffs = coeffs[N - end:N - start]   # forward order
                d = seg[m]
                old = d[0]                               # MOVE old_save, d0
                for i in range(L - 1):                   # shift segment
                    d[i] = d[i + 1]
                d[L - 1] = incoming                      # ingest incoming sample
                acc = self._macq(d[0], seg_coeffs[0])    # priming MULQ, no clamp
                for i in range(1, L):                    # taps all WRAP
                    acc = self._wrap_acc(acc, self._macq(d[i], seg_coeffs[i]))
                # Every multi-cell non-first cell receives a partial; the last
                # cell (always index >= 1) ends on its cross-cell ADD, the one
                # clamped op. Intermediate cells just wrap and forward.
                if partial is not None:
                    if is_last:
                        acc = self._clamp_final(acc, partial)
                    else:
                        acc = self._wrap_acc(acc, partial)
                partial = acc
                incoming = old                           # forward oldest onward
            out.append(partial & 0xFFFF)
        return out

    def process_reference(self, input_samples: np.ndarray) -> np.ndarray:
        """Float reference (legacy / diagnostic). For the bit-exact saturating
        predictor used by the verification gate, see :meth:`process_reference_q15`.
        """
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
