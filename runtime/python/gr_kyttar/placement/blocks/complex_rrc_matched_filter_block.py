# SPDX-License-Identifier: GPL-3.0-or-later
"""ComplexRRCMatchedFilterBlock — see :class:`ComplexRRCMatchedFilterBlock`."""
import math
from typing import Any, Dict, List, Tuple

import numpy as np

from ..block import CellProgram, DataWord, EntryPoint, Port, StateVar
from ._base import BlockInterface, KyttarBlock, float_to_q15
from . import _firdes


class ComplexRRCMatchedFilterBlock(KyttarBlock):
    """
    Complex root-raised-cosine matched filter — GNU Radio ``filter.fir_filter_ccf``
    fed taps from ``filter.firdes.root_raised_cosine(...)``.

    Complex (I/Q) in, complex (I/Q) out: the SAME real sqrt-RRC tap set is applied
    independently to the in-phase and quadrature rails — exactly the semantics of
    GNU Radio's ``fir_filter_ccf`` (complex-in, complex-out, real FLOAT taps). This
    sits at the FRONT of the coherent RX, BEFORE the Costas carrier-recovery loop::

        x16_in(xi, xq) -> [ComplexRRCMatchedFilter] -> (yi, yq)
                       -> [ComplexCostasLoop] -> ... -> [Gardner] -> [slicer]

    Parameters mirror GNU Radio's ``firdes.root_raised_cosine`` VERBATIM (INV-0):

      * ``gain``      — passband gain (the RRC taps are normalised so their SUM is
        ``gain``; GR default 1.0). Matched-filtering is gain-invariant for the
        downstream carrier/timing loops (Costas normalises amplitude), so gain is a
        pure amplitude choice; the on-chip filter reproduces GR's ``fir_filter_ccf``
        output at whatever gain is set.
      * ``samp_rate`` — the sampling frequency (Hz) — GR ``sampling_freq``.
      * ``sym_rate``  — the symbol rate (Hz) — GR ``symbol_rate``. Samples-per-symbol
        is ``samp_rate / sym_rate``.
      * ``alpha``     — RRC roll-off / excess-bandwidth factor (0.35 default).
      * ``ntaps``     — number of filter taps (GR forces it ODD; so does firdes).
      * ``decimation``— output decimation factor M (GR ``fir_filter_ccf(M, taps)``
        ``decim``): the filter runs at the full input rate and the filtered output
        is EMITTED only on phase 0 — every M-th sample (``full_output[0::M]``,
        matching GR). 1 = no decimation (default).

    Q15 datapath (COEFFICIENT HEADROOM, INV-13). The sqrt-RRC has negative
    sidelobes, so ``Σ|h|`` exceeds 1 (≈1.49 at gain 1.0). A Q15 MACQ chain would
    wrap. So the taps are PRE-SCALED DOWN by ``2**S`` where
    ``S = max(0, ceil(log2 Σ|h|))`` — now ``Σ|scaled·input| ≤ 1`` and the running
    partial sum can NEVER overflow at any tap or any cell. The gain is RESTORED at
    the very end with a single SATURATING left shift by ``S`` (the exact bias-and-
    shift restore FIRFilterBlock ships), so the emitted I/Q pair matches GNU Radio's
    ``fir_filter_ccf`` output bit-for-bit (in range) and pins to ±full-scale on a
    true overdrive (never wraps). ``S`` is DERIVED from the taps — it is not a user
    parameter.

    Architecture — SERIALIZED chained-partial-sum FIR
    =================================================

    A 1-cell HEAD lands the complex sample (xi@R0, xq@R1 — the ComplexCostasLoop
    complex-input convention the auto-P&R complex injector targets) and feeds a
    SINGLE linear chain ``head -> q0..q(k-1) -> i0..i(k-1) -> Costas`` so the
    downstream Costas phase cell fires EXACTLY ONCE per sample with both operands
    fresh (the input-port complex-sample contract). The Q rail filters xq while
    ferrying the UNFILTERED xi as a passenger; its last cell hands (yq, xi) to the
    I rail's first cell; the I rail filters xi (carrying yq); its last cell emits
    yi + yq to Costas with ONE trigger.

    Each rail is ``ceil(ntaps/TAPS_PER_CELL)`` cells. Within a cell the coeffs are
    reversed, the delay line shifts the newest sample into ``d[N-1]``, and the
    MULQ/MACQ chain wraps in R0; the partial sum chains cell-to-cell UNCLAMPED (it
    is in range under the headroom); the last cell of each rail restores the gain
    (saturating ``<<S``) on its FIR result before emitting/handing it off. The
    passenger (unfiltered xi ferried by the Q rail; filtered-and-restored yq
    ferried by the I rail) is carried UNSCALED.

    Cell layout (``1 + 2*cells_per_rail`` cells: head + two rails)::

        col:    0       1     2     3     4     5
        row 0: head-> q0 -> q1 -> q2 -> q3 -> q4    (Q rail, EAST, filters xq)
        row 1:        i4 <- i3 <- i2 <- i1 <- i0    (I rail, WEST, filters xi)

    GROUP DELAY: each rail is a linear-phase FIR of length ``ntaps``, so its group
    delay is ``(ntaps-1)/2`` samples. Both rails share the SAME delay, so I and Q
    stay aligned and the downstream loop absorbs the fixed latency. The on-chip
    filter runs CONTINUOUSLY (no padding/trimming), exactly like ``fir_filter_ccf``.
    """
    CATEGORY = "filtering"
    TAGS = ["rrc", "matched_filter", "complex", "filtering", "receiver"]

    # GR firdes.root_raised_cosine defaults (the coherent BPSK/QPSK RX at 2 sps).
    GAIN = 0.7105        # matches the legacy unit-energy-/2 taps exactly (GR drop-in)
    SAMP_RATE = 2.0       # samples/sec; samp_rate/sym_rate = 2 samples/symbol
    SYM_RATE = 1.0
    ALPHA = 0.35          # excess-bandwidth (roll-off)
    NTAPS = 17            # span*sps+1 = 8*2+1 = 17 (full-span sqrt-RRC)

    # Partial-sum chaining (same as RRCPulseShaperBlock). 4 taps/cell (not 5) so the
    # rail cells, which ALSO carry a passenger forward in the serialized-rail design
    # (an extra state reg + forwarding instructions per cell), stay within the
    # 32-register budget.
    TAPS_PER_CELL = 4

    MAX_CELLS_ACROSS = 8  # INV-9: keep the fold ≤8 cells across the 10-wide chip.

    SAT_POS_Q15 = 0x7FFF  # +full-scale rail (0x7FFF + signbit => +0x7FFF / -0x8000)

    # Landing cell takes xi at R0 (I rail) and xq at R1 (Q rail), mirroring
    # ComplexCostasLoopBlock so the auto-P&R router can wire MF.yi -> Costas.xi.
    _interface = BlockInterface(
        entry_address=1, input_registers=[0, 1], output_registers=[0, 1]
    )

    # INV-22: these constructor kwargs are BACK-COMPAT ALIASES kept so older
    # ``.kyt``/GRC flowgraphs keep loading — they are NOT distinct GR params and must
    # NOT appear in GRC (the binding exposes the canonical GR-verbatim names, INV-0):
    #   beta            -> back-compat alias of ``alpha``  (GRC exposes ``alpha``)
    #   sps / span      -> back-compat aliases that DERIVE ``samp_rate``/``sym_rate``/
    #                      ``ntaps`` (GRC exposes those canonical names directly)
    #   headroom_shift  -> legacy fixed pre-scale, now DERIVED from the taps (INV-13);
    #                      the passed value is ignored, so it is not a settable param.
    GRC_UNSUPPORTED_PARAMS = ("beta", "sps", "span", "headroom_shift")

    def __init__(
        self,
        name: str,
        gain: float = GAIN,
        samp_rate: float = SAMP_RATE,
        sym_rate: float = SYM_RATE,
        alpha: float = ALPHA,
        ntaps: int = NTAPS,
        decimation: int = 1,
        # --- backward-compatible aliases (older .kyt / GRC bindings) -----------
        # The block predates the GR-verbatim param names; the shipped modem .kyt
        # files and the RRCPulseShaperBlock reuse pass beta/sps/span. Accept them
        # and derive the GR params so those flowgraphs keep loading unchanged.
        beta: float = None,
        sps: int = None,
        span: int = None,
        headroom_shift: int = None,   # legacy fixed pre-scale — now DERIVED; ignored
    ):
        """See the class docstring for the GR-verbatim parameters.

        Legacy aliases (``beta``/``sps``/``span``/``headroom_shift``) are accepted
        for backward compatibility with older flowgraphs and map onto the GR params
        (``beta`` -> ``alpha``; ``sps``/``span`` -> ``samp_rate=sps``, ``sym_rate=1``,
        ``ntaps=span*sps+1``). ``headroom_shift`` is now DERIVED from the taps (the
        INV-13 accumulator headroom) and the legacy value is ignored.
        """
        # Legacy alias resolution (only if a GR param was left at its default).
        if beta is not None:
            alpha = beta
        if sps is not None or span is not None:
            _sps = sps if sps is not None else 2
            _span = span if span is not None else 8
            samp_rate = float(_sps)
            sym_rate = 1.0
            ntaps = _span * _sps + 1

        ntaps = int(ntaps)
        if ntaps % 2 == 0:                 # GR forces an odd tap count
            ntaps += 1
        if ntaps < 1:
            raise ValueError(f"ntaps must be >= 1, got {ntaps}")
        if int(decimation) < 1:
            raise ValueError(f"decimation must be >= 1, got {decimation}")
        # HARDWARE/HARNESS LIMIT (documented, LOUD — INV-0): a decimating COMPLEX
        # matched filter (GR fir_filter_ccf(M>1, taps)) is NOT yet supported. The
        # last I-rail cell already carries a 2-word (yi/yq) complex emit + the
        # saturating-restore; adding a mod-M emit gate that must skip BOTH rail
        # WRITEs + the trigger overflows that cell's register budget (verified:
        # "No register space for d0" at decimation=2 with the headroom restore).
        # The coherent BPSK/QPSK RX runs the loops at the full sample rate
        # (decimation=1), so this is not on the critical path. Compose a separate
        # complex decimator downstream if you need rate reduction.
        if int(decimation) != 1:
            raise ValueError(
                "ComplexRRCMatchedFilterBlock: decimation > 1 is not supported "
                f"(got {decimation}). A decimating complex matched filter overflows "
                "the last I-rail cell's register budget (the 2-word yi/yq emit + the "
                "Q15 saturating-restore leave no room for the mod-M emit gate). Use "
                "decimation=1 (the coherent RX runs its loops at full rate) and "
                "compose a separate complex decimator if rate reduction is needed.")

        super().__init__(name, gain=float(gain), samp_rate=float(samp_rate),
                         sym_rate=float(sym_rate), alpha=float(alpha),
                         ntaps=ntaps, decimation=int(decimation))
        self._gain = float(gain)
        self._samp_rate = float(samp_rate)
        self._sym_rate = float(sym_rate)
        self._alpha = float(alpha)
        self._num_taps = ntaps
        self._decimation = int(decimation)

        # LAYOUT LIMIT (INV-9, ≤8 cells across on this 10×12 chip). The serialized
        # two-row fold places the head at column 0 and the Q rail at columns
        # 1..cells_per_rail, so the last column index is cells_per_rail; it must be
        # ≤ 8 to leave the bus a channel (col 9 lands off the 10-wide fabric —
        # verified: ntaps=33 → 9 cells/rail → "outside the 10x12 fabric"). With
        # TAPS_PER_CELL=4 that caps ntaps at 8*4 = 32 (→ 8 cells/rail at ntaps 29..32).
        _cells_per_rail = math.ceil(ntaps / self.TAPS_PER_CELL)
        if _cells_per_rail > self.MAX_CELLS_ACROSS:
            raise ValueError(
                f"ComplexRRCMatchedFilterBlock: ntaps={ntaps} folds to "
                f"{_cells_per_rail} cells per rail, which places the rail's last "
                f"column at col {_cells_per_rail} — off the 10-wide fabric (INV-9, "
                f"≤{self.MAX_CELLS_ACROSS} across). Reduce ntaps to "
                f"≤{self.MAX_CELLS_ACROSS * self.TAPS_PER_CELL}.")

        # GR-faithful sqrt-RRC taps (float), then the INV-13 coefficient headroom.
        self._coefficients = _firdes.root_raised_cosine(
            self._gain, self._samp_rate, self._sym_rate, self._alpha, self._num_taps)
        sum_abs = sum(abs(c) for c in self._coefficients)
        self._head_shift = max(0, min(15, math.ceil(math.log2(sum_abs))
                                      if sum_abs > 1.0 else 0))
        scale = float(1 << self._head_shift)
        self._coeff_q15 = [float_to_q15(c / scale) for c in self._coefficients]

    # ---- GR-faithful float taps (before Q15 quantisation) ------------------
    @property
    def design_taps(self) -> List[float]:
        """The firdes ``root_raised_cosine`` float taps (GR ``fir_filter_ccf`` taps)."""
        return list(self._coefficients)

    @property
    def coeff_q15(self) -> List[int]:
        """The SCALED (headroom-applied) Q15 taps used by both rails."""
        return list(self._coeff_q15)

    @property
    def cells_per_rail(self) -> int:
        return math.ceil(self._num_taps / self.TAPS_PER_CELL)

    @property
    def cell_count(self) -> int:
        # 1 head (complex landing/distributor) + two FIR rails.
        return 1 + 2 * self.cells_per_rail

    @property
    def interface(self) -> BlockInterface:
        return self._interface

    # Cell ids: head "head", I rail "i0".., Q rail "q0"..
    def _rail_ids(self, rail: str) -> List[str]:
        return [f"{rail}{i}" for i in range(self.cells_per_rail)]

    # ---- the END-only saturating gain restore (INV-13, shared with FIR) ----
    def _satshift_and_emit(self, emit_lines: List[str]) -> List[str]:
        """Restore the coefficient-headroom gain with a SATURATING left shift by
        ``S`` (=``self._head_shift``), then run ``emit_lines``. The accumulator in
        R0 is GUARANTEED in range here (scaled coeffs => Σ|scaled·input| ≤ 1); the
        only place a true overdrive overflows is this shift, which pins to
        ±full-scale. Byte-for-byte the FIRFilterBlock restore (two-path structure,
        no GOTO over a {write}/{jump} label — INV-13 build-engine gotcha)."""
        S = self._head_shift
        if S == 0:
            return list(emit_lines)
        return [
            "    MOVE R{state:acc_save}, R0",
            "    ADD R{state:acc_save}, R{data:bias}",
            f"    SHR R0, #{16 - S}",
            "    BR.NZ _mf_sat",
            f"    SHL R{{state:acc_save}}, #{S}",
            *emit_lines,
            "    HALT",
            "_mf_sat:",
            "    SHR R{state:acc_save}, #15",
            "    ADD R0, R{data:satpos}",
            *emit_lines,
        ]

    def _build_head(self) -> CellProgram:
        """Complex landing cell — lands (xi@R0, xq@R1) and feeds the Q-rail head
        ``q0`` (its EAST neighbour): ``xq`` as the Q-rail FIR sample, the UNFILTERED
        ``xi`` as a passenger carried along the Q rail, plus a trigger. Both emits
        go to the SAME neighbour (q0), so NO face flip is needed."""
        return CellProgram(
            inputs=[Port("xi", register=0), Port("xq", register=1)],
            outputs=[Port("xi_pass"), Port("xq_out"), Port("qtrig")],
            entries=[EntryPoint("default")],
            state=[StateVar("xqs", register=2)],
            assembly_template="""\
start:
    MOVE R{state:xqs}, R{in:xq}
    MOVE R0, R{in:xi}
    {write:xi_pass}
    MOVE R0, R{state:xqs}
    {write:xq_out}
    {jump:qtrig}
""",
        )

    def _build_rail(self, rail: str) -> Dict[str, CellProgram]:
        """Build one real sqrt-RRC FIR rail as a chained-partial-sum FIR.

        Within-cell coeffs reversed, delay line shifts newest into d[N-1],
        MULQ/MACQ wraps in R0, the (in-range, scaled) partial sum chains
        cell-to-cell UNCLAMPED. The last cell of each rail RESTORES the
        coefficient-headroom gain (saturating ``<<S``) on its FIR result before
        emitting/handing it off. The carried passenger is UNSCALED."""
        n_cells = self.cells_per_rail
        ids = self._rail_ids(rail)
        S = self._head_shift
        progs: Dict[str, CellProgram] = {}

        for cell_idx in range(n_cells):
            start_tap = cell_idx * self.TAPS_PER_CELL
            end_tap = min(start_tap + self.TAPS_PER_CELL, self._num_taps)
            n_taps = end_tap - start_tap
            is_first = (cell_idx == 0)
            is_last = (cell_idx == n_cells - 1)

            # Coeffs reversed within each cell (GR y[n]=Σ h[k]x[n-k] convention).
            cell_coeffs = list(reversed(self._coeff_q15[start_tap:end_tap]))
            data = [DataWord(f"c{i}", cell_coeffs[i], address=i + 1)
                    for i in range(n_taps)]
            # The last cell of a rail carries the saturating-restore constants
            # (bias + satpos), placed explicitly past the coeffs.
            if is_last and S > 0:
                data.append(DataWord("bias", 1 << (15 - S), address=n_taps + 1))
                data.append(DataWord("satpos", self.SAT_POS_Q15,
                                     address=n_taps + 2))

            # The FIR DELAY LINE d0..d{n_taps-1} IS loop memory (the running sample
            # history the matched filter convolves against); reset it cold at a
            # packet boundary so a fresh packet is not contaminated by stale tail.
            state = [StateVar(f"d{i}", reset_per_batch=True) for i in range(n_taps)]
            if not is_last:
                state.append(StateVar("old_save"))
            state.append(StateVar("cs"))   # the carried passenger (xi on Q, yq on I)
            if is_last and S > 0:
                state.append(StateVar("acc_save"))

            # The partial/carry input registers sit PAST the highest data address +
            # all state regs (the FIR block's last_data_addr + len(state) + 1
            # convention), so they never collide with the auto-packed state.
            n_state = len(state)
            data_top = max((dw.address for dw in data
                            if dw.address is not None), default=n_taps)
            partial_reg = data_top + n_state + 1
            carry_reg = partial_reg + 1
            if is_first:
                inputs = [Port("sample", register=0),
                          Port("carry_in", register=carry_reg)]
            else:
                inputs = [Port("sample", register=0),
                          Port("partial", register=partial_reg),
                          Port("carry_in", register=carry_reg)]

            outputs = []
            if not is_last:
                outputs.append(Port("partial"))
                outputs.append(Port("sample_out"))
                outputs.append(Port("carry_out"))
                outputs.append(Port("fwd"))
            elif rail == "q":
                outputs.append(Port("yq_handoff"))   # (restored) yq -> i0.carry_in
                outputs.append(Port("xi_handoff"))   # passenger xi -> i0.sample
                outputs.append(Port("itrig"))        # -> i0 (start the I rail)
            else:
                outputs.append(Port("yi"))      # -> Costas.xi (R0)
                outputs.append(Port("yq"))      # -> Costas.xq (R1)
                outputs.append(Port("trig"))    # the SINGLE Costas trigger

            lines: List[str] = []
            # Save the carried passenger into cs BEFORE the FIR MACQ chain clobbers
            # R0, so it survives to be forwarded / emitted after the FIR result.
            lines.append("    MOVE R{state:cs}, R{in:carry_in}")
            if not is_last:
                lines.append("    MOVE R{state:old_save}, R{state:d0}")
            for i in range(n_taps - 1):
                lines.append(f"    MOVE R{{state:d{i}}}, R{{state:d{i+1}}}")
            lines.append(f"    MOVE R{{state:d{n_taps - 1}}}, R{{in:sample}}")
            lines.append("    MULQ R{state:d0}, R{data:c0}")
            for i in range(1, n_taps):
                lines.append(f"    MACQ R{{state:d{i}}}, R{{data:c{i}}}")
            if not is_first:
                lines.append("    ADD R0, R{in:partial}")
            # R0 now holds this cell's (in-range, scaled) FIR partial/result.
            if is_last and rail == "i":
                # Restore the gain on yi (R0), emit yi -> Costas.xi; then the
                # carried (already restored) yq (cs) -> Costas.xq; one trigger.
                emit = ["    {write:yi}",
                        "    MOVE R0, R{state:cs}",
                        "    {write:yq}",
                        "    {jump:trig}"]
                lines += self._satshift_and_emit(emit)
            elif is_last:  # q-rail last: restore yq, hand (yq, passenger xi) to i0
                emit = ["    {write:yq_handoff}",
                        "    MOVE R0, R{state:cs}",
                        "    {write:xi_handoff}",
                        "    {jump:itrig}"]
                lines += self._satshift_and_emit(emit)
            else:
                lines.append("    {write:partial}")
                lines.append("    MOVE R0, R{state:old_save}")
                lines.append("    {write:sample_out}")
                lines.append("    MOVE R0, R{state:cs}")     # forward the passenger
                lines.append("    {write:carry_out}")
                lines.append("    {jump:fwd}")

            template = "start:\n" + "\n".join(lines) + "\n"
            progs[ids[cell_idx]] = CellProgram(
                inputs=inputs, outputs=outputs,
                entries=[EntryPoint("default")],
                data=data, state=state, assembly_template=template,
            )
        return progs

    def build_cell_programs(self) -> Dict[Any, CellProgram]:
        """Head (complex landing) + Q rail + I rail, in CHAIN ORDER.

        The dict order encodes the serial dataflow head -> q0.. -> i0.. so the
        build resolves every handoff as a FORWARD abutment (the q-last -> i0 corner
        included); each handoff's destination register is taken from the consumer
        cell's input port, so the q-last cell's TWO WRITEs land on i0.carry_in and
        i0.sample distinctly."""
        progs: Dict[Any, CellProgram] = {"head": self._build_head()}
        progs.update(self._build_rail("q"))   # Q rail filters xq (runs first)
        progs.update(self._build_rail("i"))   # I rail filters xi (runs second)
        return progs

    def _rail_connections(self, rail: str) -> List[Tuple[str, str, str, str]]:
        ids = self._rail_ids(rail)
        conns = []
        for k in range(len(ids) - 1):
            conns.append((ids[k], "partial", ids[k + 1], "partial"))
            conns.append((ids[k], "sample_out", ids[k + 1], "sample"))
            conns.append((ids[k], "carry_out", ids[k + 1], "carry_in"))
        return conns

    def internal_connections(self) -> List[Tuple[str, str, str, str]]:
        """The SINGLE serialized chain head -> q0.. -> i0..:
          * head -> q0: xq (sample) + xi (carry passenger);
          * each rail's partial/sample/carry chain;
          * q-last -> i0: its (restored) FIR result yq (the I rail's carry) + the
            passenger xi (the I rail's FIR sample)."""
        i_first = self._rail_ids("i")[0]
        q_last = self._rail_ids("q")[-1]
        return [
            ("head", "xq_out", "q0", "sample"),
            ("head", "xi_pass", "q0", "carry_in"),
            (q_last, "yq_handoff", i_first, "carry_in"),
            (q_last, "xi_handoff", i_first, "sample"),
        ] + self._rail_connections("i") + self._rail_connections("q")

    def output_cell_ids(self) -> List[str]:
        """ONE external output cell: the I rail's last cell, which emits BOTH
        ``yi`` (-> Costas.xi) and ``yq`` (-> Costas.xq) with a SINGLE trigger."""
        return [self._rail_ids("i")[-1]]

    def internal_jumps(self) -> List[Tuple[str, str, str, str]]:
        """SERIALIZED chain: head triggers q0; each cell triggers the next; the Q
        rail's last cell triggers the I rail's first cell. The I rail's last cell
        triggers Costas (an EXTERNAL net, not listed here)."""
        i_first = self._rail_ids("i")[0]
        q_last = self._rail_ids("q")[-1]
        jumps = [
            ("head", "qtrig", "q0", "default"),
            (q_last, "itrig", i_first, "default"),
        ]
        for rail in ("i", "q"):
            ids = self._rail_ids(rail)
            for k in range(len(ids) - 1):
                jumps.append((ids[k], "fwd", ids[k + 1], "default"))
        return jumps

    def default_layout(self) -> Dict[Any, Tuple[int, int, str]]:
        """SERIALIZED-RAIL layout. Two rows::

            col:    0       1     2     3     4     5
            row 0: head-> q0 -> q1 -> q2 -> q3 -> q4   (Q rail, flows EAST)
            row 1:        i4 <- i3 <- i2 <- i1 <- i0   (I rail, flows WEST)

        ``head`` at (0,0) feeds its EAST neighbour q0; the Q rail flows EAST on
        row 0; its last cell sits DIRECTLY ABOVE i0 and hands (yq, xi) SOUTH to it;
        the I rail flows WEST on row 1; its last cell (col 1) emits yi + yq + one
        trigger."""
        n = self.cells_per_rail
        q_ids = self._rail_ids("q")
        i_ids = self._rail_ids("i")
        layout: Dict[Any, Tuple[int, int, str]] = {"head": (0, 0, "east")}
        for k, cid in enumerate(q_ids):
            face = "south" if cid == q_ids[-1] else "east"
            layout[cid] = (k + 1, 0, face)
        for k, cid in enumerate(i_ids):
            layout[cid] = (n - k, 1, "west")
        return layout

    # ---- bit-exact Q15 reference (models the on-chip cells EXACTLY) ---------
    @staticmethod
    def _s16(v: int) -> int:
        v &= 0xFFFF
        return v - 0x10000 if v & 0x8000 else v

    def _sat_shl(self, acc: int) -> int:
        """The END-only saturating left shift by ``S`` — bit-exact with the
        hardware bias-and-shift restore in :meth:`_satshift_and_emit`."""
        S = self._head_shift
        acc = self._s16(acc & 0xFFFF)
        if S == 0:
            return acc
        bias = 1 << (15 - S)
        t = (acc + bias) & 0xFFFF
        if (t >> (16 - S)) != 0:                       # overflow -> pin to rail
            sign_bit = (acc & 0xFFFF) >> 15
            return self._s16((0x7FFF + sign_bit) & 0xFFFF)
        return self._s16(((acc & 0xFFFF) << S) & 0xFFFF)

    def _fir_q15(self, x: List[int]) -> List[int]:
        """One rail's wrapping Q15 MACQ FIR with the SCALED taps, plus the END-only
        saturating gain restore — the SAME accumulation order as the on-chip rail
        (coeffs reversed per the GR convention, running sum wraps in 16 bits)."""
        taps = [self._s16(t) for t in self._coeff_q15]
        L = len(taps)
        out = []
        for n in range(len(x)):
            acc = 0
            for k in range(L):
                s = x[n - k] if 0 <= n - k < len(x) else 0
                acc = self._s16((acc + ((s * taps[k]) >> 15)) & 0xFFFF)
            out.append(self._sat_shl(acc))
        return out

    def process_reference(self, input_samples: np.ndarray) -> np.ndarray:
        """Reference complex matched filter matching the on-chip cells EXACTLY.

        ``input_samples`` is a complex array (or (N,2) real [xi,xq]) of Q15 SIGNED
        samples. Returns an (N,2) int array of [yi,yq] (the filtered I/Q),
        bit-identical to running both rails through simkyt (NO group-delay
        trimming — on-chip the filter runs continuously)."""
        arr = np.asarray(input_samples)
        if np.iscomplexobj(arr):
            xs = [(int(round(c.real)), int(round(c.imag))) for c in arr]
        elif arr.ndim == 2 and arr.shape[1] == 2:
            xs = [(self._s16(int(x) & 0xFFFF), self._s16(int(y) & 0xFFFF))
                  for x, y in arr]
        else:
            xs = [(self._s16(int(x) & 0xFFFF), 0) for x in arr]

        fi = self._fir_q15([v[0] for v in xs])
        fq = self._fir_q15([v[1] for v in xs])
        out = [(a & 0xFFFF, b & 0xFFFF) for a, b in zip(fi, fq)]
        return np.array(out, dtype=np.int32)

    def reset(self):
        """Reset (stateless FIR — delay lines live in cell state)."""
        pass
