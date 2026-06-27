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

    SATURATING Q15 ACCUMULATION — COEFFICIENT HEADROOM (production DSP)
    ------------------------------------------------------------------
    The cell ALU's MACQ/ADD are 16-bit and WRAP on signed overflow (modulo
    2^16), which flips the sign on overload and produces garbage — the opposite
    of every production fixed-point FIR (TI C5x/C6x, etc.) which SATURATE the
    accumulator (clamp to ±full-scale). GNU Radio's ``fir_filter_fff`` is float
    and never overflows, so the correct Q15 equivalent of "what GR computes,
    clamped to the representable range" is a SATURATING accumulator.

    The naive fixes all FAIL on a high-gain filter, because the ALU's V flag is
    NOT sticky: a sum can overflow a mid-chain MACQ and WRAP BACK into range by
    the final op, so a final-only (or per-cell) clamp never sees the overflow and
    the output ROLLS OVER (sign flips) instead of saturating. (Concretely: a
    40-tap, all-0.5 filter — gain 20 — driven with a steady 0.9 input rolled to a
    sign-flipping ±0.x mess instead of pinning at +1.0.) A per-TAP clamp is
    correct but costs ~3 instructions per tap, collapsing TAPS_PER_CELL to 1 (a
    40-tap FIR → 40 cells) — unacceptable.

    The correct AND dense fix is COEFFICIENT HEADROOM (accumulator scaling):
    pre-scale the coefficients so the running sum can NEVER overflow internally,
    then restore the gain with ONE shift + a final saturating clamp.

      1. ``S = max(0, ceil(log2(Σ|coeff|)))`` (computed at construction from the
         ORIGINAL coeffs). For a normalized filter (Σ|coeff| ≤ 1) ``S = 0`` — a
         no-op. For a high-gain filter (Σ|coeff| > 1) ``S > 0``.
      2. Every coefficient is scaled by ``2^-S`` before Q15 conversion (the
         block's ``_coeff_q15`` holds the SCALED taps). Now
         ``Σ|scaled coeff| ≤ 1``, so the running MACQ sum is bounded by
         ``Σ|scaled·input| ≤ 1`` and CANNOT overflow at any tap or any cell —
         intermediate wrap is IMPOSSIBLE. The whole chain stays in range.
      3. At the very END (single cell: after the last MACQ; multi-cell: on the
         LAST cell after its final ADD) the gain is restored with a SATURATING
         left shift by S. The shift is where a true overdrive overflows, and the
         clamp saturates it to ±full-scale. When ``S = 0`` no shift is emitted
         (unchanged behaviour for normalized filters — the in-range path is then
         identical to a plain Q15 FIR).

    SATURATING LEFT SHIFT (the END restore, S > 0)
    ----------------------------------------------
    ``SHL`` does NOT set the V flag (the barrel shifter reports no overflow), so
    the gain restore CANNOT use SHL + a V-flag clamp — it would never fire. The
    restore detects shift overflow with a bias-and-shift test that is O(1) in S:
    ``acc<<S`` overflows the signed 16-bit range iff
    ``acc ∉ [-2^(15-S), 2^(15-S)-1]``, which biasing by ``2^(15-S)`` turns into
    ``(acc + bias) >> (16-S) != 0`` (unsigned). On overflow it pins to the rail of
    the ORIGINAL accumulator sign via ``0x7FFF + signbit``:

        MOVE  R{acc_save}, R0        ; keep the in-range acc (its sign = output sign)
        ADD   R{acc_save}, R{bias}   ; t = acc + 2^(15-S)   (R0, wraps mod 2^16)
        SHR   R0, #(16-S)            ; t >> (16-S), logical; 0 ⟺ in range
        BR.NZ _fir_sat               ; nonzero ⟹ overflow → saturate
        SHL   R{acc_save}, #S        ; in range → shifted result in R0
        <emit> ; HALT                ; emit then STOP (don't fall into the sat block)
      _fir_sat:                      ; pin to ±full-scale by the original sign
        SHR   R{acc_save}, #15       ; R0 = sign bit (1 if acc was negative, logical)
        ADD   R0, R{satpos}          ; R0 = 0x7FFF + bit  ⇒  pos? 0x7FFF : 0x8000
        <emit>

    The rail trick uses ``0x7FFF + signbit`` — one shared 0x7FFF word per restore
    cell yields both +0x7FFF and -0x8000. The two-path structure (duplicated
    ``<emit>`` + a terminal ``HALT``, NOT a ``GOTO`` over the sat block) is forced
    by a build-engine quirk: a GOTO/branch whose target LABELS a ``{write}``/
    ``{jump}`` placeholder is miscompiled into a stray output JUMP, so ``_fir_sat``
    must label a REAL instruction. The in-range path's HALT is REQUIRED — a remote
    JUMP does NOT stop local execution, so without it the in-range path would fall
    into the sat block and double-emit. (A doubling loop ``ADD R0,R0`` ×S + ``BR.V``
    also works but its 2·S instructions overflow the last cell's budget at large
    S.) Exhaustively verified equal to ``clamp(acc·2^S)`` for all acc, S∈0..15.

    BUDGET (coefficient headroom — S=0 is the plain wrapping FIR's density)
    ----------------------------------------------------------------------
    For a NORMALIZED filter (S=0) the headroom is a NO-OP and the per-cell tap
    density is exactly the plain wrapping FIR's. For a high-gain filter (S>0) the
    one cell that carries the saturating-shift restore (the single cell, or the
    LAST multi-cell cell) needs ≈10 extra words, so ITS segment is capped (other
    cells are unaffected — they forward an in-range scaled partial with no
    clamp/shift). A cell holds, at addr 0..31: L coeffs + (S>0: bias + satpos) +
    L delay regs (+ old_save on non-last cells) (+ S>0 on the restore cell:
    acc_save) + the input/partial reg(s) + the program.

      * Single cell: N=6 for S=0 (MAX_SINGLE_CELL_TAPS); N=4 for S>0
        (MAX_SINGLE_CELL_TAPS_WITH_SHIFT, budget 4N+16 ≤ 32). Above the ceiling it
        folds to multi-cell.
      * Multi-cell (TAPS_PER_CELL = 5): a MID cell (no restore) fits a full L=5
        segment. For S=0 the LAST cell also fits L=5, so 7+ taps fold to ⌈N/5⌉
        cells (20-tap = 4, 40-tap = 8, 64-tap = 13). For S>0 the LAST cell carries
        the restore so its segment caps at L=3 (LAST_CELL_TAPS_WITH_SHIFT, budget
        4L+18 ≤ 32) and the fold rebalances the tail — a high-gain FIR may use one
        extra cell (a 40-tap gain-20 FIR is 9 cells vs 8 normalized). See
        :meth:`_segment_offsets`.

    Supports arbitrary coefficients for any FIR application (channel
    filter, matched filter, anti-alias, interpolation, etc.).

    Interface (defaults):
    - Entry: R1
    - Input: R31 (single sample)
    """
    CATEGORY = "filtering"
    TAGS = ["fir", "filter", "filtering"]

    _interface = BlockInterface(entry_address=1, input_registers=[31], output_registers=[31])

    def __init__(self, name: str, coefficients: List[float],
                 decimation: int = 1, interpolation: int = 1):
        """
        Initialize FIR filter — matches GNU Radio's GRC **FIR Filter** /
        **interp_fir_filter** parameterization.

        GRC's ``filter_fir_filter_xxx`` exposes ``decim`` + ``taps``; its
        ``interp_fir_filter_xxx`` exposes ``interp`` + ``taps``; and the GRC
        convenience filters (Low/High/Band-pass, Band-reject) expose BOTH
        ``decim`` and ``interp`` (selecting fir_filter vs interp_fir_filter). So
        this block exposes both as first-class parameters:

        Args:
            name: Block name
            coefficients: Filter coefficients / GR ``taps`` (length = tap count).
            decimation: GR ``decim`` (M). Output rate = input rate / M; the FIR
                runs every input sample and emits only every M-th output (phase 0,
                ``y_full[0::M]``) — exactly GR ``fir_filter_fff(M, taps)``. M=1 is
                a plain FIR (no gating). decimation>1 and interpolation>1 together
                is rejected (GR uses one or the other per block instance).
            interpolation: GR ``interp`` (L). Output rate = input rate * L; the
                input is zero-stuffed by L (sample then L-1 zeros) and filtered —
                exactly GR ``interp_fir_filter_fff(L, taps)``. L=1 is a plain FIR.
        """
        decimation = int(decimation)
        interpolation = int(interpolation)
        if decimation < 1:
            raise ValueError(f"decimation must be >= 1, got {decimation}")
        if interpolation < 1:
            raise ValueError(f"interpolation must be >= 1, got {interpolation}")
        if decimation > 1 and interpolation > 1:
            raise ValueError(
                "decimation>1 and interpolation>1 cannot be combined on one FIR "
                "block (GR uses fir_filter for decim, interp_fir_filter for "
                f"interp); got decim={decimation}, interp={interpolation}. Use "
                "two cascaded blocks.")
        self._decimation = decimation
        self._interpolation = interpolation
        super().__init__(name, coefficients=coefficients)
        self._coefficients = coefficients  # ORIGINAL coeffs (reference / display)
        self._num_taps = len(coefficients)

        # COEFFICIENT HEADROOM (accumulator-scaling saturation).
        # The 16-bit accumulator WRAPS on intermediate overflow and the ALU's V
        # flag is NOT sticky, so a high-gain filter (Σ|coeff| > 1) can overflow a
        # mid-chain MACQ and wrap back into range by the final op — a final-only
        # (or per-cell) clamp then MISSES the overflow and the output ROLLS OVER
        # (sign flips) instead of saturating. The fix is to pre-scale the
        # coefficients by 2^-S so the running sum can NEVER overflow internally
        # (Σ|scaled coeff| ≤ 1 ⇒ |Σ scaled·input| ≤ 1, in range at every tap and
        # every cell), then restore the gain with ONE saturating left shift by S
        # at the very END. S = max(0, ceil(log2(Σ|coeff|))):
        #   * normalized filter (Σ|coeff| ≤ 1) → S = 0, a no-op (no shift, no
        #     overflow possible) — behaviour unchanged from a plain Q15 FIR.
        #   * high-gain filter (Σ|coeff| > 1) → S > 0, the scaled coeffs keep the
        #     accumulator in range and the final saturating shift restores the
        #     gain, pinning a true overdrive at ±full-scale.
        sum_abs = sum(abs(c) for c in coefficients)
        if sum_abs <= 1.0:
            S = 0
        else:
            S = int(math.ceil(math.log2(sum_abs)))
        # SHL's immediate count is 0..15 (one barrel-shifter pass). A filter whose
        # Σ|coeff| needs S > 15 (gain > 32768) is a documented limit and will not
        # occur for any sane filter; clamp S so the block still builds.
        self._head_shift = max(0, min(15, S))

        # Store the SCALED coeffs as _coeff_q15 (the datapath uses these). After
        # scaling Σ|scaled coeff| ≤ 1, so no MACQ tap or cross-cell partial can
        # overflow. The original coeffs remain in self._coefficients for the float
        # reference / display.
        scale = float(1 << self._head_shift)
        self._coeff_q15 = [float_to_q15(c / scale) for c in coefficients]

        # HARDWARE DEVIATION (decimation>1 only): the mod-M output-gate counter
        # SHARES the output cell with the saturating-shift restore, so a
        # decimating filter needing more than MAX_HEADROOM_SHIFT_DECIM bits of
        # coefficient headroom (Σ|h| > 2^MAX_HEADROOM_SHIFT_DECIM) cannot fit one
        # cell. RAISE loudly rather than silently mis-build. A plain or
        # interpolating FIR has no counter on the output cell and is NOT subject to
        # this — full headroom range (S≤15) applies there.
        if self._decimation > 1 and self._head_shift > self.MAX_HEADROOM_SHIFT_DECIM:
            raise ValueError(
                f"HARDWARE LIMIT: a DECIMATING FIR (decimation={self._decimation}) "
                f"needs {self._head_shift} bits of coefficient headroom "
                f"(Σ|h|={sum_abs:.2f}), but the decimator supports at most "
                f"{self.MAX_HEADROOM_SHIFT_DECIM} (Σ|h| ≤ "
                f"{1 << self.MAX_HEADROOM_SHIFT_DECIM}) because the mod-M output "
                f"counter shares the output cell with the saturating-gain restore. "
                f"Scale the taps down (Σ|h| ≤ {1 << self.MAX_HEADROOM_SHIFT_DECIM}) "
                f"and apply the gain in a separate stage, or decimate a normalized "
                f"filter. (A non-decimating FIR has no such limit.)")

        # Initialize delay line
        self._delay_line = [0.0] * self._num_taps

    # COEFFICIENT HEADROOM keeps the per-cell tap density at the plain wrapping
    # FIR's: the saturating-shift restore is paid ONCE (on the last cell), and
    # intermediate cells forward an in-range scaled partial with NO clamp/shift.
    # Densest role is a MID cell at L=5 (5 coeffs + 5 delay regs + old_save +
    # input + program), which fits the 32-word cell; L=6 overflows. The LAST cell
    # carries the shift restore but has NO old_save reg, so a full last segment
    # (L=5) + restore still fits.
    TAPS_PER_CELL = 5
    # Largest tap count that fits one cell's 32-word budget. The single cell
    # carries the headroom restore (when S>0) in addition to the filter: N=6
    # coeffs + N delay regs + acc_save + satpos + input + (~2N + restore)
    # instructions fits; N=7 overflows. 7+ taps fold to multi-cell, where the
    # restore lives only on the last cell.
    MAX_SINGLE_CELL_TAPS = 6
    # Maximum column HEIGHT (rows) in the multi-cell placement FOLD. The fold
    # chooser (see default_layout / _fold_geometry) snakes the datapath cells
    # column-major up to this many rows tall, preferring the TALLEST (most
    # compact) fold that keeps an EVEN number of columns — the parity that
    # co-locates the block's input and output on the SAME edge (INV-14).
    FOLD_HEIGHT = 4
    # INV-9: keep a block ≤ 8 cells across on the 10x12 array (a bus needs one
    # channel of cells on each side). The fold chooser never accepts an
    # even-column fold wider than this — otherwise a cell count whose only
    # even-quotient divisor is H=1 (e.g. 26 → 26×1) would lay out as a full-width
    # line that runs off the array and silently fails to route.
    MAX_CELLS_ACROSS = 8
    # Saturating-shift rail constant: 0x7FFF = +32767. The headroom restore pins
    # to a rail with ``0x7FFF + signbit`` so this one word yields both +0x7FFF
    # (positive overflow) and -0x8000 (negative overflow) — see _satshift_and_emit /
    # the class docstring.
    SAT_POS_Q15 = 0x7FFF
    # When COEFFICIENT HEADROOM is active (S>0) the LAST multi-cell cell carries
    # the saturating-shift restore (≈12 program words + bias/satpos/acc_save), so
    # its segment is capped here. Its budget is 4*L + 18 ≤ 32 ⇒ L ≤ 3. Non-last
    # cells (no restore) keep the full TAPS_PER_CELL. For a NORMALIZED filter
    # (S=0) there is no restore and every cell holds up to TAPS_PER_CELL —
    # identical to a plain wrapping FIR.
    LAST_CELL_TAPS_WITH_SHIFT = 3
    # Largest single-cell FIR WHEN the headroom restore is present (S>0). The one
    # cell carries the whole filter plus the restore: budget 4*N + 16 ≤ 32 ⇒
    # N ≤ 4. Above this (but still ≤ MAX_SINGLE_CELL_TAPS) a high-gain FIR folds
    # to the multi-cell wavefront so the restore has room. A normalized filter
    # (S=0) keeps the full MAX_SINGLE_CELL_TAPS single-cell ceiling.
    MAX_SINGLE_CELL_TAPS_WITH_SHIFT = 4
    # DECIMATION (M>1) caps: the output cell also carries the mod-M counter
    # (≈9 words), so its tap room shrinks. Re-derived against the resolver
    # allocator (probed real builds, inherited from the former DecimatorBlock):
    #   * single cell + counter (S=0): 4 taps; (S>0): 2 taps.
    #   * multi-cell last cell + counter (S=0): 3 taps; (S=1): 2 taps.
    # Non-output cells keep the full TAPS_PER_CELL (no counter). These apply ONLY
    # when decimation>1; a plain/interpolating FIR uses the un-capped values above.
    MAX_SINGLE_CELL_TAPS_DECIM = 4            # S=0 single cell + counter
    MAX_SINGLE_CELL_TAPS_DECIM_WITH_SHIFT = 2  # S>0 single cell + counter + restore
    LAST_CELL_TAPS_DECIM = 3                  # S=0 multi-cell last cell + counter
    LAST_CELL_TAPS_DECIM_WITH_SHIFT = 2       # S=1 multi-cell last cell + counter + restore

    def _single_cell_max(self) -> int:
        """Largest tap count that still fits ONE cell, accounting for the headroom
        restore (S>0) and — when decimation>1 — the mod-M output counter that
        shares the cell."""
        if self._decimation > 1:
            if self._head_shift > 0:
                return self.MAX_SINGLE_CELL_TAPS_DECIM_WITH_SHIFT
            return self.MAX_SINGLE_CELL_TAPS_DECIM
        if self._head_shift > 0:
            return self.MAX_SINGLE_CELL_TAPS_WITH_SHIFT
        return self.MAX_SINGLE_CELL_TAPS

    def _segment_offsets(self) -> List[int]:
        """Tap-array partition boundaries for the multi-cell wavefront.

        Returns ``offsets`` (length cells+1) with cell ``m`` owning taps
        ``[offsets[m], offsets[m+1])``. Non-last cells hold up to TAPS_PER_CELL.
        When S>0 the LAST cell additionally carries the saturating-shift restore,
        so its segment is capped at LAST_CELL_TAPS_WITH_SHIFT; an extra cell is
        added if needed and the tail is rebalanced so the last segment is in
        ``[1, LAST_CELL_TAPS_WITH_SHIFT]``. When S=0 this is the plain
        ⌈N/TAPS_PER_CELL⌉ packing (every cell up to TAPS_PER_CELL)."""
        import math
        N, K = self._num_taps, self.TAPS_PER_CELL
        if N <= self._single_cell_max():
            return [0, N]
        # The LAST cell is capped when it carries the saturating-shift restore
        # (S>0) and/or — when decimation>1 — the mod-M output counter. A plain
        # normalized FIR (S=0, decim=1) has no cap → simple ⌈N/K⌉ packing.
        S = self._head_shift
        if self._decimation > 1:
            # Output cell always carries the counter (every S); tighter with the
            # restore. S=0 → 3, S=1 → 2, S≥2 → 1 (S≤2 enforced in __init__).
            last_max = (self.LAST_CELL_TAPS_DECIM if S == 0
                        else self.LAST_CELL_TAPS_DECIM_WITH_SHIFT if S == 1 else 1)
        elif S == 0:
            c = math.ceil(N / K)
            offs = [min(m * K, N) for m in range(c + 1)]
            offs[c] = N
            return offs
        else:
            last_max = self.LAST_CELL_TAPS_WITH_SHIFT
        c = math.ceil((N - last_max) / K) + 1
        segs = [K] * (c - 1) + [N - K * (c - 1)]
        # Rebalance so the last segment lands in [1, last_max].
        while segs[-1] < 1:
            j = max(range(c - 1), key=lambda i: segs[i])
            segs[j] -= 1
            segs[-1] += 1
        while segs[-1] > last_max:
            j = next((i for i in range(c - 1) if segs[i] < K), None)
            if j is None:
                break
            segs[j] += 1
            segs[-1] -= 1
        offs = [0]
        for s in segs:
            offs.append(offs[-1] + s)
        return offs

    @property
    def cell_count(self) -> int:
        if self._num_taps <= self._single_cell_max():
            return 1  # Single-cell fits within the register budget
        return len(self._segment_offsets()) - 1

    def _fold_geometry(self):
        """Choose the (cols, rows) of the compact serpentine fold for ``n =
        cell_count`` datapath cells.

        INV-14 — a column-major serpentine snake co-locates the block's INPUT
        (cell 0, top of column 0) and its OUTPUT (the last datapath cell) on the
        SAME edge when the COLUMN COUNT is EVEN: column 0 snakes DOWN, column 1
        UP, …, so an even number of FULL columns ends travelling UP and lands the
        last cell back at the TOP edge beside the input. We pick the most compact
        fold (tallest column ≤ ``FOLD_HEIGHT`` ⇒ fewest columns) and PREFER one
        whose cells fill an even number of full columns, so I/O co-locates with
        NO padding. When ``n`` doesn't fold into full even columns we do NOT pad
        (that complicates the egress) — we take the compact fold and let the
        router hook up the output from wherever the last cell lands.

        Returns ``(cols, rows)``; the snake fills ``n`` of the ``cols*rows``
        positions left-to-right column-major (a partial last column is fine).
        """
        import math
        n = self.cell_count
        # Best EVEN-column full-rectangle fold (perfect I/O co-location, no pad):
        # the tallest H≤FOLD_HEIGHT that divides n with an even quotient. The
        # column count (n//H) MUST stay within the ≤8-across cap (INV-9) — without
        # it a cell count whose only even-quotient divisor is H=1 (e.g. n=26 →
        # 26×1) would pick a degenerate full-width LINE that runs off the array and
        # cannot route. When no in-cap even fold exists we fall through to the
        # compact fallback below (tallest column ⇒ fewest columns).
        for H in range(self.FOLD_HEIGHT, 0, -1):
            if n % H == 0 and (n // H) % 2 == 0 and (n // H) <= self.MAX_CELLS_ACROSS:
                return n // H, H
        # No clean even fold — take the most compact one (tallest column ⇒ fewest
        # columns) and accept the last cell may land a row off the input edge; the
        # router connects the output from there.
        H = min(self.FOLD_HEIGHT, n)
        C = math.ceil(n / H)
        return C, H

    def default_layout(self):
        """Place the multi-cell wavefront as a compact column-major serpentine
        FOLD, NOT the base class's single straight row.

        The wavefront snakes DOWN a column of up to ``FOLD_HEIGHT`` cells, OVER
        one, and UP the next, repeating. This keeps a large FIR COMPACT (8 cells →
        a 2×4 block, not a 1×8 strip across the array). When the cell count fills
        an EVEN number of full columns, the snake ends travelling UP and lands the
        OUTPUT cell (last cell) back on the INPUT's top edge — both I/O corridors
        then leave from the same face (INV-14). When it doesn't fold into full
        even columns the last cell lands wherever the snake ends (a row off the
        input edge at worst); we keep the compact fold and let the router connect
        the output from there. Each cell's face points at its successor in the
        chain (south down a column, east across a turn, north back up); the final
        cell continues its column's travel direction so its egress leaves cleanly.

        Single-cell FIRs (≤ MAX_SINGLE_CELL_TAPS) use the trivial 1-cell layout.
        """
        n = self.cell_count
        if n <= 1:
            return {0: (0, 0, "east")}
        C, H = self._fold_geometry()

        def snake_pos(i):
            col, r = divmod(i, H)
            dy = r if (col % 2 == 0) else (H - 1 - r)
            return col, dy

        pos = {i: snake_pos(i) for i in range(n)}
        layout = {}
        for i in range(n):
            dx, dy = pos[i]
            nxt = pos.get(i + 1)
            if nxt is not None:
                nx, ny = nxt
                face = ("east" if nx > dx else "west" if nx < dx
                        else "south" if ny > dy else "north")
            else:
                # Last cell: continue the column's travel direction (north up an
                # up-going column, south down a down-going one) so the output
                # egress leaves the block cleanly for the router to pick up.
                face = "north" if (i // H) % 2 == 1 else "south"
            layout[i] = (dx, dy, face)
        return layout

    @property
    def num_taps(self) -> int:
        return self._num_taps

    @property
    def decimation(self) -> int:
        return self._decimation

    @property
    def interpolation(self) -> int:
        return self._interpolation

    @property
    def interface(self) -> BlockInterface:
        return self._interface

    # ---- decimation gate (GR fir_filter_fff(M, taps)) -----------------------
    # When decimation M>1 the FIR runs EVERY input sample but emits only every
    # M-th output (phase 0, y_full[0::M]) — exactly GR. The output emit is gated
    # by a mod-M counter on the last cell (the cell that produces the output). The
    # counter (decim, one) + a few instructions cost ≈9 words, so when M>1 the
    # output cell's tap segment is capped tighter (see _single_cell_max /
    # _segment_offsets). HARDWARE DEVIATION (see __init__ / class docstring):
    # because the mod-M counter SHARES the output cell with the saturating-shift
    # restore, a high-gain decimating filter (Σ|h| needing S>MAX_HEADROOM_SHIFT)
    # cannot fit one cell and RAISES — documented loudly, never silent.
    MAX_HEADROOM_SHIFT_DECIM = 2   # Σ|h| ≤ 4 when M>1 (counter + restore share a cell)
    SAT_NEG_Q15 = 0x8000           # rail for the doubling-restore form used with the gate

    def _counter_data(self, base_addr: int):
        """The two mod-M counter DataWords (``decim``=M, ``one``=1) at explicit
        addresses just past ``base_addr`` (the last coeff/bias/satpos word)."""
        return [DataWord("decim", self._decimation, address=base_addr + 1),
                DataWord("one", 1, address=base_addr + 2)]

    @staticmethod
    def _counter_gate_open():
        """Lines run EVERY sample after the delay shift: bump the mod-M counter
        and, until it reaches M, branch past the MAC+emit to a HALT (state updated,
        no output). Targets a REAL instruction (``_decim_skip`` on a HALT), never a
        {write}/{jump} placeholder (the build-engine GOTO miscompile, INV-13)."""
        return [
            "    ADD R{state:counter}, R{data:one}",
            "    MOVE R{state:counter}, R0",
            "    CMP R{state:counter}, R{data:decim}",
            "    BR.NZ _decim_skip",
            "    XOR R{state:counter}, R{state:counter}",
            "    MOVE R{state:counter}, R0",
        ]

    @staticmethod
    def _counter_gate_close():
        """End the emit path and provide the skip target. The emit path must HALT
        before falling into the skip block (a remote {jump} does NOT stop local
        execution)."""
        return ["    HALT", "_decim_skip:", "    HALT"]

    def _decim_satshift_and_emit(self, S: int, emit_lines):
        """Decim-path gain restore: a SATURATING left shift by ``S`` done as ``S``
        DOUBLINGS (``ADD R0,R0`` + a V-flag clamp each), then emit. Equivalent to
        ``clamp(acc·2^S)`` — bit-identical to :meth:`_sat_shl`, so the inherited
        Q15 reference still predicts the DUT exactly. The doubling form is CHEAPER
        in fixed overhead than the bias-shift restore, which is what lets the
        restore COEXIST with the mod-M counter on one cell for the small S a
        decimation filter needs (S∈{0,1,2})."""
        if S <= 0:
            return list(emit_lines)
        lines = []
        for _ in range(S):
            lines.append("    ADD R0, R0")
            lines.append("    BR.NV +2")
            lines.append("    SHR R0, #15")
            lines.append("    SUB R{data:satneg}, R0")
        lines.extend(emit_lines)
        return lines

    def build_cell_programs(self) -> Dict[int, CellProgram]:
        """FIR filter: single-cell for ≤MAX_SINGLE_CELL_TAPS, multi-cell larger.

        The multi-cell path is FIR's OWN chained partial-sum (systolic) wavefront
        — NOT borrowed from RRCPulseShaperBlock, whose per-segment coefficient
        REVERSAL is only correct for symmetric taps (it silently mis-convolves a
        general/asymmetric FIR). See :meth:`_build_multicell_programs`.

        With ``decimation`` M>1 the output emit is gated by a mod-M counter
        (:meth:`_build_decim_programs`); with ``interpolation`` L>1 the landing
        cell zero-stuffs by L and emits a burst of L outputs per input
        (:meth:`_build_interp_programs`). M=L=1 is the plain FIR below.
        """
        if self._decimation > 1:
            return self._build_decim_programs()
        if self._interpolation > 1:
            return self._build_interp_programs()
        if self._num_taps > self._single_cell_max():
            return self._build_multicell_programs()
        return self._build_plain_single_cell()

    def _build_plain_single_cell(self) -> Dict[int, CellProgram]:
        """The plain (non-decim, non-interp) single-cell FIR — compact version for
        small filters. The decim builder reuses this for its non-gated base.

        TAP ORDER: after the shift, ``d{i}`` holds x[n-(N-1-i)] (d0=oldest,
        d{N-1}=newest). GR's convention y[n]=Σ_k h[k]·x[n-k] then requires register
        ``d{i}`` to be multiplied by ``h[N-1-i]`` — i.e. the coefficients REVERSED.
        (The multi-cell path documents the same reversal. The single-cell path
        previously used forward order, which is correct ONLY for symmetric taps and
        silently mis-convolved asymmetric filters — fixed here, asymmetric-tap
        regression test added.)"""
        S = self._head_shift
        rev = list(reversed(self._coeff_q15))  # d{i} multiplies h[N-1-i]
        data = [DataWord(f"c{i}", c, address=i+1) for i, c in enumerate(rev)]
        if S > 0:
            # bias (2^(15-S)) + satpos (0x7FFF) carry the saturating-shift restore.
            # EXPLICIT addresses right after the coeffs; an auto address would pack
            # at 0 (R0 / the accumulator) and corrupt it.
            data.append(DataWord("bias", 1 << (15 - S), address=self._num_taps + 1))
            data.append(DataWord("satpos", self.SAT_POS_Q15, address=self._num_taps + 2))
        state = [StateVar(f"d{i}") for i in range(self._num_taps)]
        if S > 0:
            state.append(StateVar("acc_save"))  # holds the in-range acc for the restore

        lines = []
        for i in range(self._num_taps - 1):
            lines.append(f"    MOVE R{{state:d{i}}}, R{{state:d{i+1}}}")
        lines.append(f"    MOVE R{{state:d{self._num_taps - 1}}}, R{{in:sample}}")
        # With COEFFICIENT HEADROOM the coeffs are pre-scaled so Σ|scaled|≤1: the
        # MACQ chain CANNOT overflow at any tap. The priming MULQ is a single Q15
        # product (always representable). So no per-tap or final wrap-clamp is
        # needed — the accumulator stays in range through the whole chain.
        lines.append(f"    MULQ R{{state:d0}}, R{{data:c0}}")
        for i in range(1, self._num_taps):
            lines.append(f"    MACQ R{{state:d{i}}}, R{{data:c{i}}}")
        # Restore the gain ONCE at the END with the saturating left shift by S,
        # then emit. When S==0 (normalized filter) the in-range value in R0 is
        # already the output — just emit.
        lines.extend(self._satshift_and_emit(S, ["    {write:out}", "    {jump:out}"]))

        template = "start:\n" + "\n".join(lines) + "\n"

        return {0: CellProgram(
            inputs=[Port("sample", register=0)],
            outputs=[Port("out")],
            entries=[EntryPoint("default")],
            data=data,
            state=state,
            assembly_template=template,
        )}

    def _build_decim_programs(self) -> Dict[int, CellProgram]:
        """Decimating FIR (GR fir_filter_fff(M, taps)): the plain FIR wavefront
        with the OUTPUT cell's emit gated by a mod-M counter. The FIR runs every
        input sample; only every M-th output (phase 0) is emitted. Non-output
        cells are reused verbatim from the plain builder; only the output cell is
        rebuilt to add the counter (so the register allocator accounts for it).
        Mirrors the (now-removed) DecimatorBlock exactly."""
        # Build the un-gated FIR programs first (single- or multi-cell), then
        # rebuild the output cell with the counter. We temporarily run the plain
        # path by dispatching on tap count (decimation>1 already routed us here).
        if self._num_taps > self._single_cell_max():
            base = self._build_multicell_programs()
        else:
            base = self._build_plain_single_cell()
        programs = dict(base)
        last = max(programs.keys())
        n_cells = len(programs)
        S = self._head_shift
        N = self._num_taps
        emit = ["    {write:out}", "    {jump:out}"]

        if n_cells == 1:
            coeffs = list(reversed(self._coeff_q15))  # d{i} multiplies h[N-1-i]
            data = [DataWord(f"c{i}", c, address=i + 1) for i, c in enumerate(coeffs)]
            base_addr = N
            if S > 0:
                data.append(DataWord("satneg", self.SAT_NEG_Q15, address=N + 1))
                base_addr = N + 1
            data += self._counter_data(base_addr)
            state = [StateVar(f"d{i}") for i in range(N)]
            if S > 0:
                state.append(StateVar("acc_save"))
            state.append(StateVar("counter", initial_value=self._decimation - 1))
            lines = []
            for i in range(N - 1):
                lines.append(f"    MOVE R{{state:d{i}}}, R{{state:d{i+1}}}")
            lines.append(f"    MOVE R{{state:d{N-1}}}, R{{in:sample}}")
            lines += self._counter_gate_open()
            lines.append("    MULQ R{state:d0}, R{data:c0}")
            for i in range(1, N):
                lines.append(f"    MACQ R{{state:d{i}}}, R{{data:c{i}}}")
            lines.extend(self._decim_satshift_and_emit(S, emit))
            lines += self._counter_gate_close()
            programs[0] = CellProgram(
                inputs=[Port("sample", register=0)],
                outputs=[Port("out")],
                entries=[EntryPoint("default")],
                data=data, state=state,
                assembly_template="start:\n" + "\n".join(lines) + "\n")
            return programs

        # Multi-cell: rebuild ONLY the last (output) cell with the counter.
        offsets = self._segment_offsets()
        start, end = offsets[last], offsets[last + 1]
        L = end - start
        cell_coeffs = list(reversed(self._coeff_q15[start:end]))  # GR tap convention
        data = [DataWord(f"c{i}", cell_coeffs[i], address=i + 1) for i in range(L)]
        base_addr = L
        if S > 0:
            data.append(DataWord("satneg", self.SAT_NEG_Q15, address=L + 1))
            base_addr = L + 1
        data += self._counter_data(base_addr)
        state = [StateVar(f"d{i}") for i in range(L)]
        if S > 0:
            state.append(StateVar("acc_save"))
        state.append(StateVar("counter", initial_value=self._decimation - 1))
        last_data_addr = max(dw.address for dw in data)
        partial_reg = last_data_addr + len(state) + 1
        inputs = [Port("sample", register=0),
                  Port("partial", register=partial_reg)]
        lines = []
        for i in range(L - 1):
            lines.append(f"    MOVE R{{state:d{i}}}, R{{state:d{i+1}}}")
        lines.append(f"    MOVE R{{state:d{L-1}}}, R{{in:sample}}")
        lines += self._counter_gate_open()
        lines.append("    MULQ R{state:d0}, R{data:c0}")
        for i in range(1, L):
            lines.append(f"    MACQ R{{state:d{i}}}, R{{data:c{i}}}")
        lines.append("    ADD R0, R{in:partial}")
        lines.extend(self._decim_satshift_and_emit(S, emit))
        lines += self._counter_gate_close()
        programs[last] = CellProgram(
            inputs=inputs,
            outputs=[Port("out")],
            entries=[EntryPoint("default")],
            data=data, state=state,
            assembly_template="start:\n" + "\n".join(lines) + "\n")
        return programs

    def _build_interp_programs(self) -> Dict[int, CellProgram]:
        """Interpolating FIR (GR interp_fir_filter_fff(L, taps)): the input is
        zero-stuffed by L (sample then L-1 zeros) and filtered. Per input sample
        the landing cell runs the FIR L times — once on the real sample, L-1 times
        on a zero — emitting a BURST of L outputs (rate-EXPANDING). The L passes
        are UNROLLED in the cell (a remote JUMP does not halt the issuer, so one
        entry can emit the whole burst then HALT).

        Single-cell (≤ single-cell tap budget after the unrolled burst): one cell
        holds the delay line + the L-pass burst. Larger filters need the burst to
        drive a multi-cell wavefront L times, which is a separate datapath — until
        that lands, a multi-cell interpolating FIR RAISES with a clear message
        (compose UpsamplerBlock -> FIRFilterBlock) rather than silently
        mis-building. (HONEST LIMIT, documented — see __init__.)"""
        L = self._interpolation
        S = self._head_shift
        N = self._num_taps
        # The unrolled L-pass burst costs ~ (shift + N MACs + restore + emit) * L
        # words. Cap the single-cell interp tap count so the unrolled program +
        # delay line + coeffs fit one 32-word cell. Derived conservatively: coeffs
        # (N) + delay (N) + zero const + (S>0: bias/satpos/acc_save) + program.
        single_cap = self._interp_single_cell_max()
        if N > single_cap:
            raise ValueError(
                f"HARDWARE LIMIT (current): an INTERPOLATING FIR "
                f"(interpolation={L}) with {N} taps exceeds the single-cell "
                f"unrolled-burst budget ({single_cap} taps for L={L}). A "
                f"multi-cell interpolating wavefront is not yet built. Compose "
                f"UpsamplerBlock(sps={L}) -> FIRFilterBlock(taps) instead (exactly "
                f"GR interp_fir_filter), or reduce the tap count.")

        coeffs = list(reversed(self._coeff_q15))  # d{i} multiplies h[N-1-i]
        data = [DataWord(f"c{i}", c, address=i + 1) for i, c in enumerate(coeffs)]
        addr = N
        data.append(DataWord("zero", 0, address=addr + 1)); addr += 1
        if S > 0:
            data.append(DataWord("bias", 1 << (15 - S), address=addr + 1))
            data.append(DataWord("satpos", self.SAT_POS_Q15, address=addr + 2))
            addr += 2
        state = [StateVar(f"d{i}") for i in range(N)]
        if S > 0:
            state.append(StateVar("acc_save"))

        emit = ["    {write:out}", "    {jump:out}"]

        def fir_pass(sample_ref: str):
            """One FIR pass: shift in ``sample_ref`` (a reg ref), MAC, restore,
            emit. ``sample_ref`` is R{in:sample} for the real sample or
            R{data:zero} for a stuffed zero."""
            ls = []
            for i in range(N - 1):
                ls.append(f"    MOVE R{{state:d{i}}}, R{{state:d{i+1}}}")
            ls.append(f"    MOVE R{{state:d{N-1}}}, {sample_ref}")
            ls.append("    MULQ R{state:d0}, R{data:c0}")
            for i in range(1, N):
                ls.append(f"    MACQ R{{state:d{i}}}, R{{data:c{i}}}")
            ls.extend(self._satshift_and_emit(S, emit))
            return ls

        lines = []
        lines += fir_pass("R{in:sample}")          # pass 0: the real sample
        for _ in range(L - 1):                       # passes 1..L-1: stuffed zeros
            lines += fir_pass("R{data:zero}")
        lines.append("    HALT")

        return {0: CellProgram(
            inputs=[Port("sample", register=0)],
            outputs=[Port("out")],
            entries=[EntryPoint("default")],
            data=data, state=state,
            assembly_template="start:\n" + "\n".join(lines) + "\n")}

    def _interp_single_cell_max(self) -> int:
        """Largest tap count that fits ONE cell for an interpolating FIR, given the
        L-pass unrolled burst (each pass is a full shift+MAC+emit). The cell holds
        N coeffs + N delay regs + a zero const (+ S>0: bias/satpos/acc_save) plus
        the unrolled program (≈(2N+4)·L words). The limit shrinks as L grows.

        EMPIRICALLY MEASURED against real builds (probed N up to 8, both S=0 and
        S>0): the largest N that builds AND computes per L. Above this the
        interpolating FIR must be composed as Upsampler(L) -> FIR(taps), or use a
        multi-cell interpolating wavefront (not yet built — see
        :meth:`_build_interp_programs`)."""
        L = self._interpolation
        # Measured single-cell ceilings (normalized taps, S=0), probed against
        # real builds: largest N that builds AND computes per L. The headroom
        # restore (S>0) costs a few more words; subtract one tap to stay safe.
        table = {1: 6, 2: 4, 3: 2, 4: 2}
        cap = table.get(L, 1)  # L>=5: very tight bursts, 1 tap (or compose)
        if self._head_shift > 0:
            cap = max(1, cap - 1)
        return cap

    def _satshift_and_emit(self, S: int, emit_lines):
        """The END-ONLY COEFFICIENT-HEADROOM gain restore (a SATURATING left
        shift by ``S``) followed by the block output ``emit_lines``
        (``{write:out}`` + ``{jump:out}``). See the class docstring.

        Emitted ONCE, after the FINAL accumulation op (the last MACQ in the
        single cell, or the cross-cell ADD on the last multi-cell cell). The
        accumulator in R0 is GUARANTEED in range here (scaled coeffs ⇒
        Σ|scaled·input| ≤ 1), so the only place a true overdrive can overflow is
        this restore. When ``S == 0`` there is no gain to restore — just emit.

        SHL does NOT set V, so overflow is detected with a bias-and-shift test
        that is O(1) in S (a doubling loop cost 2·S instructions, overflowing the
        last cell's budget for large S). ``acc<<S`` overflows the signed 16-bit
        range iff ``acc ∉ [-2^(15-S), 2^(15-S)-1]``; biasing by ``2^(15-S)`` maps
        that to ``(acc+bias) >> (16-S) != 0`` (unsigned):

            MOVE R{acc_save}, R0       ; keep the in-range acc (its sign = output sign)
            ADD  R{acc_save}, R{bias}  ; t = acc + 2^(15-S)   (R0, wraps mod 2^16)
            SHR  R0, #(16-S)           ; t >> (16-S), logical; 0 ⟺ in range
            BR.NZ _fir_sat             ; nonzero ⟹ overflow → saturate
            SHL  R{acc_save}, #S       ; in range → shifted result in R0
            <emit> ; HALT              ; emit then STOP (don't fall into the sat block)
          _fir_sat:                    ; pin to ±full-scale by the ORIGINAL sign
            SHR  R{acc_save}, #15      ; R0 = sign bit (1 if acc negative, logical)
            ADD  R0, R{satpos}         ; 0x7FFF + bit ⟹ pos? 0x7FFF : 0x8000
            <emit>

        NOTE the two-path structure with a duplicated <emit> and a HALT, NOT a
        GOTO over the sat block: the build engine miscompiles a GOTO/branch whose
        target LABELS a ``{write}``/``{jump}`` placeholder (it rewrites the jump
        with the placeholder's output routing — observed as a stray output JUMP).
        So ``_fir_sat`` labels a REAL instruction (the SHR), the in-range path
        emits then HALTs, and the sat path emits at the end of the program (its
        natural auto-HALT). A relative ``BR.NZ`` to a real-instruction label
        resolves correctly. The in-range path's terminal HALT is REQUIRED — a
        remote JUMP does NOT stop local execution, so without it the in-range path
        would fall into the sat block and double-emit.

        Exhaustively verified equal to ``clamp(acc·2^S)`` for all acc, S∈0..15.
        """
        if S == 0:
            return list(emit_lines)
        return [
            "    MOVE R{state:acc_save}, R0",
            "    ADD R{state:acc_save}, R{data:bias}",
            f"    SHR R0, #{16 - S}",
            "    BR.NZ _fir_sat",
            f"    SHL R{{state:acc_save}}, #{S}",
            *emit_lines,
            "    HALT",
            "_fir_sat:",
            "    SHR R{state:acc_save}, #15",
            "    ADD R0, R{data:satpos}",
            *emit_lines,
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
        ``d = offset_m + (L_m-1-i)``. GR's convention is ``y[n] = Σ_d h[d]·x[n-d]``
        (NOT the reversed ``h[N-1-d]``), so register ``i`` must be multiplied by
        ``h[d] = h[offset_m + L_m-1-i]``. As ``i`` runs 0..L_m-1, that index runs
        ``offset_{m+1}-1`` down to ``offset_m`` — i.e. cell ``m`` takes the segment
        ``h[offset_m : offset_{m+1}]`` in REVERSE order. (Cell 0 gets the FIRST
        taps, the last cell the LAST taps — the natural left-to-right split, each
        segment internally reversed for the delay-line order.)

        BUG HISTORY: this previously used ``coeff[N-end:N-start]`` forward (the
        reversed-whole-filter convention), which silently convolved ASYMMETRIC
        filters BACKWARDS vs real GR. It was masked because the FIR test's golden
        reversed the taps before feeding GR, and all FIR tests used SYMMETRIC taps.
        Fixed to match real ``fir_filter_fff``; asymmetric multi-tap regression
        tests added.

        Segment boundaries come from :meth:`_segment_offsets` — TAPS_PER_CELL per
        cell, except the LAST cell is capped (and the tail rebalanced) when the
        COEFFICIENT-HEADROOM restore is present (S>0), so it has room for the
        saturating shift. For a normalized filter (S=0) this is the plain
        ⌈N/TAPS_PER_CELL⌉ packing.
        """
        N = self._num_taps
        S = self._head_shift
        offsets = self._segment_offsets()
        n_cells = len(offsets) - 1

        programs: Dict[int, CellProgram] = {}
        for m in range(n_cells):
            start, end = offsets[m], offsets[m + 1]
            L = end - start
            is_first = (m == 0)
            is_last = (m == n_cells - 1)

            # Coefficients: cell m owns h[start:end], REVERSED so register d{i}
            # (holding x[n-(offset_m+L-1-i)]) multiplies h[offset_m+L-1-i] — the
            # real GR y[n]=Σ_d h[d]x[n-d] convention (see the class derivation).
            cell_coeffs = list(reversed(self._coeff_q15[start:end]))
            data = [DataWord(f"c{i}", cell_coeffs[i], address=i + 1)
                    for i in range(L)]
            # bias + satpos live ONLY on the LAST cell (and only when S>0 — they
            # carry the saturating-shift restore). EXPLICIT addresses after the
            # coeffs (auto would land on R0). Coeffs occupy 1..L, so bias is L+1,
            # satpos L+2.
            if is_last and S > 0:
                data.append(DataWord("bias", 1 << (15 - S), address=L + 1))
                data.append(DataWord("satpos", self.SAT_POS_Q15, address=L + 2))

            state = [StateVar(f"d{i}") for i in range(L)]
            if not is_last:
                state.append(StateVar("old_save"))  # oldest sample, forwarded
            if is_last and S > 0:
                state.append(StateVar("acc_save"))  # in-range acc for the restore

            if is_first:
                inputs = [Port("sample", register=0)]
            else:
                # The partial-input register follows the coeffs, the (optional)
                # satpos data word, and the state regs. Derive its address from
                # the highest data address actually present (= L if no satpos,
                # L+1 if satpos), then the state block, then +1 for partial.
                last_data_addr = max(dw.address for dw in data)
                partial_reg = last_data_addr + len(state) + 1
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
                # Restore the gain ONCE on the LAST cell, after its final ADD,
                # with the saturating left shift by S, then emit. Intermediate
                # cells forward their (in-range, scaled) partial sum WITHOUT any
                # clamp or shift — with COEFFICIENT HEADROOM (Σ|scaled coeff| ≤ 1)
                # no cell can overflow, so there is nothing to clamp there. The
                # only place a true overdrive overflows is this final restore.
                # When S==0 the shift is omitted (the in-range partial is already
                # the output).
                lines.extend(self._satshift_and_emit(
                    S, ["    {write:out}", "    {jump:out}"]))
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
        ``ADD/MACQ``: the 16-bit ALU wraps (modulo 2^16, sign-extended). With
        COEFFICIENT HEADROOM (scaled coeffs, Σ|scaled|≤1) the running sum never
        actually leaves range, so no wrap occurs — but the model wraps exactly as
        the silicon would, matching it bit-for-bit regardless."""
        return cls._to_s16((acc + addend) & 0xFFFF)

    @classmethod
    def _sat_shl(cls, acc: int, S: int) -> int:
        """The END-ONLY COEFFICIENT-HEADROOM gain restore: a SATURATING left
        shift of the (in-range) accumulator by ``S``, bit-exact with the hardware
        bias-and-shift restore (see :meth:`_satshift_and_emit`).

        ``acc`` is the final in-range accumulator (scaled coeffs guarantee
        |acc| ≤ full-scale). Detect overflow via ``(acc + 2^(15-S)) >> (16-S)``
        (logical, on the 16-bit pattern): 0 ⟺ in range, in which case the result
        is ``acc << S``; nonzero ⟹ overflow, pin to the rail of the ORIGINAL sign
        ``0x7FFF + signbit`` = +0x7FFF (positive) or -0x8000 (negative). When S==0
        it is a no-op. Exhaustively equal to ``clamp(acc·2^S)`` for S∈0..15."""
        acc = cls._to_s16(acc & 0xFFFF)
        if S == 0:
            return acc
        bias = 1 << (15 - S)
        t = (acc + bias) & 0xFFFF                    # ADD R{acc_save}, R{bias}
        hi = t >> (16 - S)                           # SHR R0, #(16-S), logical
        if hi != 0:                                  # BR.NZ → saturate
            sign_bit = (acc & 0xFFFF) >> 15          # SHR R{acc_save}, #15
            return cls._to_s16((0x7FFF + sign_bit) & 0xFFFF)  # ADD R0, R{satpos}
        return cls._to_s16(((acc & 0xFFFF) << S) & 0xFFFF)  # SHL R{acc_save}, #S

    @staticmethod
    def _macq(a_q15: int, b_q15: int) -> int:
        """Q15 product term: (a*b) >> 15, arithmetic, as the ALU computes it."""
        return (FIRFilterBlock._to_s16(a_q15) * FIRFilterBlock._to_s16(b_q15)) >> 15

    def process_reference_q15(self, input_q15) -> list:
        """Bit-exact Q15 COEFFICIENT-HEADROOM reference, in the SAME accumulation
        order as the built datapath (single-cell vs multi-cell wavefront).

        Mirrors the hardware exactly: the SCALED coeffs (Σ|scaled coeff| ≤ 1) are
        accumulated (every MACQ tap and cross-cell partial-sum ADD WRAPS in 16-bit
        modulo, sign-extended — though with the headroom it never actually leaves
        range), then the gain is restored at the very END with a single SATURATING
        left shift by ``S`` (the ``ADD R0,R0`` ×S / ``BR.V`` / rail restore). This
        — NOT the float ideal — is what the headroom datapath produces, so it is
        the golden predictor the overload test compares the DUT against. For
        in-range stimulus the saturating shift never clips, so the output equals
        GNU Radio's float output clipped to the Q15 range and the existing GR
        comparison still holds. An overdrive overflows the final shift and pins at
        ±full-scale.

        Returns one signed Q15 int per OUTPUT sample. For a plain FIR
        (decim=interp=1) that is one per input. With ``interpolation`` (L) the
        input is first zero-stuffed by L (so L outputs per input); with
        ``decimation`` (M) only every M-th FIR output is emitted (phase 0).
        """
        # GR interp_fir_filter_fff(L, taps): zero-stuff input by L, then filter.
        L = self._interpolation
        if L > 1:
            stuffed = []
            for s in input_q15:
                stuffed.append(int(s) & 0xFFFF)
                stuffed.extend([0] * (L - 1))
            input_q15 = stuffed
        full = self._full_fir_q15(input_q15)
        # GR fir_filter_fff(M, taps): emit only every M-th output (phase 0).
        M = self._decimation
        return full[::M] if M > 1 else full

    def _full_fir_q15(self, input_q15) -> list:
        """The core Q15 COEFFICIENT-HEADROOM FIR (no decim/interp) — one output
        per input sample, in the SAME accumulation order as the built datapath
        (single-cell vs multi-cell wavefront). ``process_reference_q15`` wraps this
        with the interp zero-stuff (input side) and decim subsample (output side).
        """
        coeffs = self._coeff_q15
        S = self._head_shift
        N = self._num_taps
        delay = [0] * N
        out = []
        if N <= self._single_cell_max():
            # Single cell — mirrors _build_plain_single_cell EXACTLY. The delay
            # line is shifted ``MOVE d{i}, d{i+1}`` then ``MOVE d{N-1}, sample``,
            # so d0 holds the OLDEST sample (x[n-(N-1)]) and d{N-1} the newest;
            # register d{i} = x[n-(N-1-i)] is multiplied by h[N-1-i] — i.e. the
            # coefficients REVERSED (the GR y[n]=Σ_k h[k]x[n-k] convention). Model
            # that with the newest sample at the END of ``delay`` (delay[i]==d{i})
            # and reversed coeffs. (Forward order is correct only for symmetric
            # taps — the single-cell asymmetric-tap bug fixed alongside.)
            rev = list(reversed(coeffs))               # rev[i] == h[N-1-i]
            for s in input_q15:
                delay = delay[1:] + [self._to_s16(int(s) & 0xFFFF)]
                acc = self._macq(delay[0], rev[0])        # priming MULQ
                if N == 1:
                    out.append(self._sat_shl(acc, S) & 0xFFFF)  # (not reached: N>=2)
                    continue
                for i in range(1, N):
                    acc = self._wrap_acc(acc, self._macq(delay[i], rev[i]))
                acc = self._sat_shl(acc, S)               # END-only gain restore
                out.append(acc & 0xFFFF)
            return out

        # Multi-cell wavefront — a CELL-ACCURATE model of the systolic datapath.
        # Each cell holds its OWN segment delay line; per input sample the
        # wavefront runs cell 0 → cell N-1. A cell: saves its oldest sample
        # (old_save = d0), shifts its segment ingesting the INCOMING sample into
        # d{L-1}, MACs its SCALED taps (all WRAPPING), ADDs the partial sum
        # forwarded from the previous cell (WRAPPING), and forwards the new
        # partial AND its saved oldest sample on to the next cell. With
        # COEFFICIENT HEADROOM (Σ|scaled coeff| ≤ 1) NO cell overflows — every
        # cell forwards an in-range scaled partial unclamped — and the gain is
        # restored only at the very END (after the LAST cell's cross-cell ADD)
        # with a single SATURATING left shift by S. Cell 0's incoming sample is
        # x[n]; every later cell's incoming sample is the previous cell's
        # shifted-out oldest sample (NOT a global-delay-line index — the
        # inter-cell forwarding IS the delay). Cell m owns coefficients
        # h[offset_m:offset_{m+1}] REVERSED (the real GR tap convention) — mirrors
        # :meth:`_build_multicell_programs` exactly.
        offsets = self._segment_offsets()
        n_cells = len(offsets) - 1
        seg = [[0] * (offsets[m + 1] - offsets[m]) for m in range(n_cells)]
        for s in input_q15:
            incoming = self._to_s16(int(s) & 0xFFFF)
            partial = None
            for m in range(n_cells):
                start, end = offsets[m], offsets[m + 1]
                L = end - start
                is_last = (m == n_cells - 1)
                seg_coeffs = list(reversed(coeffs[start:end]))  # GR tap convention
                d = seg[m]
                old = d[0]                               # MOVE old_save, d0
                for i in range(L - 1):                   # shift segment
                    d[i] = d[i + 1]
                d[L - 1] = incoming                      # ingest incoming sample
                acc = self._macq(d[0], seg_coeffs[0])    # priming MULQ
                for i in range(1, L):                    # taps all WRAP
                    acc = self._wrap_acc(acc, self._macq(d[i], seg_coeffs[i]))
                # Every multi-cell non-first cell receives a partial; ADD it
                # (WRAPPING). No cell overflows under headroom, so no clamp here.
                if partial is not None:
                    acc = self._wrap_acc(acc, partial)
                partial = acc
                incoming = old                           # forward oldest onward
            # Gain restore ONCE, at the very END, on the last cell's final acc.
            out.append(self._sat_shl(partial, S) & 0xFFFF)
        return out

    def process_reference(self, input_samples: np.ndarray) -> np.ndarray:
        """Float reference (legacy / diagnostic). For the bit-exact saturating
        predictor used by the verification gate, see :meth:`process_reference_q15`.

        Honors ``interpolation`` (zero-stuff input by L, GR interp_fir_filter) and
        ``decimation`` (emit every M-th output, phase 0, GR fir_filter(M,taps)).
        """
        # interp: zero-stuff input by L before filtering (GR interp_fir_filter).
        L = self._interpolation
        if L > 1:
            stuffed = np.zeros(len(input_samples) * L, dtype=np.float32)
            stuffed[::L] = np.asarray(input_samples, dtype=np.float32)
            stream = stuffed
        else:
            stream = np.asarray(input_samples, dtype=np.float32)

        full = np.zeros(len(stream), dtype=np.float32)
        delay = [0.0] * self._num_taps
        for i, sample in enumerate(stream):
            delay = [float(sample)] + delay[:-1]
            acc = 0.0
            for j, coeff in enumerate(self._coefficients):
                acc += coeff * delay[j]
            full[i] = acc

        # decim: emit every M-th output (phase 0, GR fir_filter(M,taps)).
        M = self._decimation
        return full[::M] if M > 1 else full

    def reset(self):
        """Reset delay line."""
        self._delay_line = [0.0] * self._num_taps
