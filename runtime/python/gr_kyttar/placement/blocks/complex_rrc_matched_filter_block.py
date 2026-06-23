"""ComplexRRCMatchedFilterBlock — see :class:`ComplexRRCMatchedFilterBlock`."""
import numpy as np
import math
from ..block import CellProgram, Port, EntryPoint, StateVar, DataWord
from typing import Dict, List, Tuple, Any
from ._base import KyttarBlock, BlockInterface, assemble_to_words, float_to_q15, q15_to_float


class ComplexRRCMatchedFilterBlock(KyttarBlock):
    """
    Complex RRC matched filter — coherent BPSK RX front end (8 cells).

    Two INDEPENDENT real sqrt-RRC FIR rails (one for I, one for Q) applied to the
    chip's interleaved I/Q input stream. This sits at the FRONT of the coherent RX,
    BEFORE the Costas carrier-recovery loop:

        x16_in(xi, xq) -> [ComplexRRCMatchedFilter] -> (yi, yq)
                       -> [ComplexCostasLoop] -> recovered I -> [Gardner] -> [slicer]

    Production / ADC-grade properties (the gating spec is
    the internal reference implementation):

    * FULL-SPAN sqrt-RRC (span=8, sps=2 => 17 taps), matching the TX pulse shaper,
      so there is NO residual-ISI BER floor at high SNR.
    * EXACT on-chip MACQ semantics: the chip accumulates in a 16-bit register that
      WRAPS on overflow (simkyt alu.rs macq:
      ``R0 = _s16((R0 + ((a*b)>>15)) & 0xFFFF)``). The taps are the UNIT-ENERGY
      sqrt-RRC PRE-SCALED DOWN by ``2**headroom_shift`` (default 1, i.e. /2) so a
      full-scale (~+-1.0) ADC input never wraps the running partial sum. The
      downstream Costas normalizes amplitude via its phase detector, so a known,
      bounded attenuation is harmless — what matters is NO partial-sum wrap.

    Architecture — chained-partial-sum FIR (identical to RRCPulseShaperBlock):
    ==========================================================================

    Each rail is ``ceil(17/4) = 5`` cells of ``4+4+4+4+1`` taps. Cell 0 of a rail
    takes the new sample, shifts its 4-tap delay segment, computes a wrapping
    MULQ/MACQ partial sum, and forwards ``(partial_sum, oldest_sample)`` to the
    next cell; each subsequent cell adds the incoming partial to its own MAC; the
    last cell emits the filtered rail output. Because addition is associative under
    the 16-bit wrap and each product is ``>>15``-scaled before summing, the chained
    cells reproduce the reference ``fir_macq`` EXACTLY (verified bit-for-bit). 4
    taps/cell (not 5) so the carry-carrying cells fit the 32-register budget.

    SERIALIZED single chain (so the downstream Costas phase cell fires EXACTLY ONCE
    per sample with both operands fresh — the input-port complex-sample contract):
    a 1-cell HEAD lands the complex sample (xi@R0, xq@R1 — the ComplexCostasLoopBlock
    complex-input convention the auto-P&R complex injector targets) and feeds the
    SINGLE chain entry q0: xq as the Q-rail FIR sample and the UNFILTERED xi as a
    ``carry`` passenger. The Q rail filters xq while ferrying xi; its last cell q4
    hands (yq=carry, xi=sample) to the I rail's first cell i0 and triggers it. The I
    rail filters xi while carrying yq; its last cell i4 emits yi (→Costas.xi) then yq
    (→Costas.xq) then ONE trigger. The head is the block's single landing cell and the
    group-delay reference point.

    Cell layout (11 cells: head + two 5-cell rails)::

        col:    0       1     2     3     4     5
        row 0: head-> q0 -> q1 -> q2 -> q3 -> q4    (Q rail, EAST, filters xq)
        row 1:        i4 <- i3 <- i2 <- i1 <- i0    (I rail, WEST, filters xi)

    head(0,0) feeds q0 EAST; q4(5,0) hands SOUTH to i0(5,1); the I rail flows WEST to
    i4(1,1), the single external output cell (emits yi + yq + one trigger). Every
    internal handoff is to an orthogonally-adjacent cell. (Exact placement/faces are
    the auto-P&R router's job; the block declares the relative ``default_layout`` and
    the internal handoffs/jumps.)

    Interface: COMPLEX input mirroring ComplexCostasLoopBlock — xi at R0, xq at R1
    of the HEAD landing cell. ONE external output cell (i4) emits both ``yi``
    (filtered I) and ``yq`` (filtered Q), which feed Costas's ``xi``/``xq`` as a
    single complex-sample delivery. The chain runs once per input sample.

    GROUP DELAY: each rail is a linear-phase FIR of length L=17, so its inherent
    group delay is ``(L-1)/2 = 8`` samples. On-chip the filter runs continuously and
    is NOT padded/trimmed (the reference's ``mf_complex`` drops the first 8 samples
    only to align an offline buffer). Both rails share the SAME delay, so I and Q
    stay aligned and the downstream Gardner loop simply absorbs the fixed 8-sample
    latency — exactly as it tolerates any fixed group delay.

    Register budget: the worst-case (non-first) FIR cell holds 5 coeffs + 5 delay
    regs + 1 old_save + 1 partial-input + program; ~17 of 32 registers. The head is
    trivial. Comfortable on every cell.
    """
    CATEGORY = "filtering"
    TAGS = ["rrc", "matched_filter", "complex", "filtering", "receiver"]

    BETA = 0.35           # excess-bandwidth (alpha), matches the TX pulse shaper
    SPS = 2               # samples per symbol
    SPAN = 8              # filter span in symbols -> span*sps+1 = 17 taps
    HEADROOM_SHIFT = 1    # tap pre-scale (/2) so the wrapping MACQ never overflows
    # Partial-sum chaining (same as RRCPulseShaperBlock). 4 taps/cell (not 5) so the
    # I-rail cells, which ALSO carry yq forward in the serialized-rail design (an
    # extra state reg + 2 forwarding instructions per cell), stay within the 32-reg
    # budget. 17 taps -> ceil(17/4) = 5 cells per rail (4+4+4+4+1).
    TAPS_PER_CELL = 4

    # Landing cell takes xi at R0 (I rail) and xq at R1 (Q rail), mirroring
    # ComplexCostasLoopBlock so the auto-P&R router can wire MF.yi -> Costas.xi.
    _interface = BlockInterface(
        entry_address=1, input_registers=[0, 1], output_registers=[0, 1]
    )

    def __init__(
        self,
        name: str,
        beta: float = BETA,
        sps: int = SPS,
        span: int = SPAN,
        headroom_shift: int = HEADROOM_SHIFT,
    ):
        """
        Args:
            name: Block instance name.
            beta: RRC excess-bandwidth factor (0.35 matches the TX shaper).
            sps: Samples per symbol (2 for the coherent BPSK RX).
            span: Filter span in symbols (8 = full span, no ISI floor).
            headroom_shift: Tap down-scale exponent (1 => /2) to prevent the
                16-bit MACQ accumulator wrapping on a full-scale input.
        """
        super().__init__(name, beta=beta, sps=sps, span=span,
                         headroom_shift=headroom_shift)
        self._beta = beta
        self._sps = sps
        self._span = span
        self._headroom_shift = headroom_shift
        self._num_taps = span * sps + 1
        # The EXACT production Q15 taps (unit-energy sqrt-RRC, pre-scaled DOWN).
        self._coeff_q15 = self._rrc_taps_q15()

    def _rrc_taps_q15(self) -> List[int]:
        """Full-span sqrt-RRC taps in Q15, unit-energy then pre-scaled DOWN by
        ``2**headroom_shift``. BIT-IDENTICAL to ``rrc_taps_q15`` in
        ``proto_rrc_mf_production.py`` (the gating reference)."""
        n = self._span * self._sps
        taps = []
        beta = self._beta
        sps = self._sps
        for i in range(n + 1):
            t = (i - n / 2) / sps
            if abs(t) < 1e-8:
                v = 1 - beta + 4 * beta / math.pi
            elif abs(abs(4 * beta * t) - 1.0) < 1e-8:
                v = (beta / math.sqrt(2)) * (
                    (1 + 2 / math.pi) * math.sin(math.pi / (4 * beta))
                    + (1 - 2 / math.pi) * math.cos(math.pi / (4 * beta)))
            else:
                num = (math.sin(math.pi * t * (1 - beta))
                       + 4 * beta * t * math.cos(math.pi * t * (1 + beta)))
                den = math.pi * t * (1 - (4 * beta * t) ** 2)
                v = num / den
            taps.append(v)
        e = math.sqrt(sum(v * v for v in taps)) or 1.0
        taps = [v / e for v in taps]                       # unit-energy sqrt-RRC
        scale = 1.0 / (2 ** self._headroom_shift)
        # int(round(...)) & 0xFFFF — EXACTLY the reference (NOT float_to_q15,
        # which rounds with *32768 and saturates; the reference uses *32767).
        return [int(round(t * scale * 32767)) & 0xFFFF for t in taps]

    @property
    def coeff_q15(self) -> List[int]:
        """The exact Q15 taps used by both rails (signed-wrapped 16-bit)."""
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

    # Cell ids: head "head", I rail "i0".."i3", Q rail "q0".."q3".
    def _rail_ids(self, rail: str) -> List[str]:
        return [f"{rail}{i}" for i in range(self.cells_per_rail)]

    def _build_head(self) -> CellProgram:
        """Complex landing cell.

        Lands the complex sample (xi@R0, xq@R1 — the ComplexCostasLoopBlock
        complex-input convention the auto-P&R complex injector targets) and feeds
        the SINGLE downstream chain entry, the Q-rail head ``q0`` (its EAST neighbor):

          * ``xq_out`` -> q0's ``sample`` (the Q rail filters xq);
          * ``xi_pass`` -> q0's ``xi_pass`` register (the UNFILTERED xi, carried as a
            passenger ALONG the Q rail to its end, where it is handed to the I rail);
          * ``qtrig``  -> triggers q0.

        This is the SERIALIZED-RAIL design: a single linear chain
        ``head -> q0..q4 -> i0..i4 -> Costas`` so the downstream Costas phase cell
        fires EXACTLY ONCE per sample with BOTH operands fresh (the input-port
        complex-sample contract). The Q rail runs first (filtering xq while ferrying
        xi); its last cell hands (yq, xi) to the I rail's first cell and triggers it;
        the I rail filters xi (carrying yq), and its last cell emits yi + yq to
        Costas with one trigger. Every handoff is to an orthogonally-adjacent cell.

        Both emits go to the SAME neighbor (q0, EAST), so NO face flip is needed."""
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

        Bit-identical arithmetic to ``RRCPulseShaperBlock.build_cell_programs``
        (and thus to the reference ``fir_macq``): within-cell coeffs reversed,
        delay line shifts newest into d[N-1], MULQ/MACQ wraps in R0, the partial
        sum chains cell-to-cell. The first cell reads its forwarded sample from R0
        (the head distributes xi/xq there), so there is no input/coeff collision.
        """
        n_cells = self.cells_per_rail
        ids = self._rail_ids(rail)
        progs: Dict[str, CellProgram] = {}

        for cell_idx in range(n_cells):
            start_tap = cell_idx * self.TAPS_PER_CELL
            end_tap = min(start_tap + self.TAPS_PER_CELL, self._num_taps)
            n_taps = end_tap - start_tap
            is_first = (cell_idx == 0)
            is_last = (cell_idx == n_cells - 1)

            # Coeffs reversed within each cell: for y[n]=sum(h[k]*x[n-k]),
            # d[N-1-j]=x[n-start_tap-j] so c[N-1-j]=h[start_tap+j].
            cell_coeffs = list(reversed(self._coeff_q15[start_tap:end_tap]))
            data = [DataWord(f"c{i}", cell_coeffs[i], address=i + 1)
                    for i in range(n_taps)]

            state = [StateVar(f"d{i}") for i in range(n_taps)]
            if not is_last:
                state.append(StateVar("old_save"))
            # SERIALIZED single chain: head -> q0..q4 -> i0..i4 -> Costas. EVERY cell
            # carries one passenger value forward alongside its FIR partial sum:
            #   * Q rail carries the UNFILTERED xi (`carry`), so the chain delivers xi
            #     to the I rail's first cell to filter (the head feeds only q0);
            #   * I rail carries the FILTERED yq (`carry`), so the I rail's last cell
            #     can emit yi AND yq to Costas with ONE trigger.
            # The Q rail's last cell hands (carry=xi, plus its FIR result yq) to the
            # I rail's first cell; the I rail's last cell emits yi+yq+1 trigger. This
            # makes the downstream Costas phase cell fire EXACTLY ONCE per sample with
            # both operands fresh (the input-port complex-sample contract).
            state.append(StateVar("cs"))   # the carried passenger (xi on Q, yq on I)

            # Every rail cell reads its FIR sample from R0. Non-first cells also
            # receive the incoming partial sum at an explicit register past the
            # data+state block; every cell receives the carried passenger one slot
            # further on (it never collides with the FIR data/state).
            n_state = len(state)
            partial_reg = (n_taps + 1) + n_state
            carry_reg = partial_reg + 1
            if is_first:
                inputs = [Port("sample", register=0),
                          Port("carry_in", register=carry_reg)]
            else:
                inputs = [Port("sample", register=0),
                          Port("partial", register=partial_reg),
                          Port("carry_in", register=carry_reg)]

            # Output topology:
            #   * non-last (either rail): partial + sample_out + carry_out + fwd.
            #   * Q rail last (q4): hand (result yq, carry xi) to the I rail's first
            #     cell and trigger it. The I rail filters the handed xi.
            #   * I rail last (i4): the block's SINGLE output cell — yi -> Costas.xi
            #     (R0), the carried yq -> Costas.xq (R1), then ONE trigger JUMP.
            outputs = []
            if not is_last:
                outputs.append(Port("partial"))
                outputs.append(Port("sample_out"))
                outputs.append(Port("carry_out"))
                outputs.append(Port("fwd"))
            elif rail == "q":
                # q4: its FIR result (R0) is yq; emit it as the I rail's carry, and
                # emit the passenger xi (in cs) as the I rail's FIR sample, + trigger.
                outputs.append(Port("yq_handoff"))   # yq -> i0.carry_in
                outputs.append(Port("xi_handoff"))   # xi -> i0.sample
                outputs.append(Port("itrig"))        # -> i0 (start the I rail)
            else:
                outputs.append(Port("yi"))      # -> Costas.xi (R0)
                outputs.append(Port("yq"))      # -> Costas.xq (R1)
                outputs.append(Port("trig"))    # the SINGLE Costas trigger

            lines = []
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
            # R0 now holds this cell's FIR result.
            if is_last and rail == "i":
                # Emit yi (R0) -> Costas.xi, then the carried yq (cs) -> Costas.xq,
                # then the single trigger.
                lines.append("    {write:yi}")
                lines.append("    MOVE R0, R{state:cs}")
                lines.append("    {write:yq}")
                lines.append("    {jump:trig}")
            elif is_last:  # q4: hand yq (R0) and the passenger xi (cs) to i0, trigger
                lines.append("    {write:yq_handoff}")
                lines.append("    MOVE R0, R{state:cs}")
                lines.append("    {write:xi_handoff}")
                lines.append("    {jump:itrig}")
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

        The dict order encodes the serial dataflow head -> q0..q4 -> i0..i4 so the
        build resolves every handoff as a FORWARD abutment (the q4 -> i0 corner
        included): each handoff's destination register is taken from the consumer
        cell's input port, so q4's TWO WRITEs land on i0.carry_in and i0.sample
        distinctly. (Listing the I rail first would make q4 -> i0 a BACKWARD edge,
        routed by the feedback pass, which patches a single WRITE per dst reg and
        cannot split the two same-cell handoffs — yielding both at R0.)"""
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
        """The SINGLE serialized chain head -> q0..q4 -> i0..i4:
          * head -> q0: xq (sample) + xi (carry passenger);
          * each rail's partial/sample/carry chain;
          * q4 -> i0: its FIR result yq (the I rail's carry) + the passenger xi (the
            I rail's FIR sample) — so the I rail filters xi while carrying yq, and its
            last cell emits yi+yq to Costas with one trigger."""
        i_first = self._rail_ids("i")[0]
        q_last = self._rail_ids("q")[-1]
        return [
            ("head", "xq_out", "q0", "sample"),
            ("head", "xi_pass", "q0", "carry_in"),
            (q_last, "yq_handoff", i_first, "carry_in"),
            (q_last, "xi_handoff", i_first, "sample"),
        ] + self._rail_connections("i") + self._rail_connections("q")

    def output_cell_ids(self) -> List[str]:
        """The block has ONE external output cell: the I rail's last cell (i4),
        which emits BOTH ``yi`` (-> Costas.xi) and ``yq`` (-> Costas.xq) with a
        SINGLE trigger — the input-port complex-sample contract."""
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
        """SERIALIZED-RAIL layout (so the downstream Costas phase cell fires once
        per sample with both operands fresh). Two rows::

            col:    0       1     2     3     4
            row 0:        i3 <- i2 <- i1 <- i0        (I rail, flows WEST)
            row 1: head-> q0 -> q1 -> q2 -> q3        (Q rail, flows EAST)

            col:    0       1     2     3     4     5
            row 0: head-> q0 -> q1 -> q2 -> q3 -> q4   (Q rail, flows EAST)
            row 1:        i4 <- i3 <- i2 <- i1 <- i0   (I rail, flows WEST)

        SINGLE serial chain, every handoff orthogonally adjacent:
        * ``head`` at (0,0) feeds its EAST neighbor q0 (xq=sample, xi=carry, trigger).
        * The Q rail flows EAST on row 0; its last cell q4 at (n,0) sits DIRECTLY
          ABOVE i0 at (n,1) — q4 hands (yq=carry, xi=sample) SOUTH to i0 and triggers
          the I rail.
        * The I rail flows WEST on row 1, carrying yq; its last cell i4 at (1,1)
          emits yi + yq + ONE trigger to Costas. All coordinates are non-negative
          (head is the block's top-left anchor)."""
        n = self.cells_per_rail
        q_ids = self._rail_ids("q")
        i_ids = self._rail_ids("i")
        layout: Dict[Any, Tuple[int, int, str]] = {"head": (0, 0, "east")}
        # Q rail flows EAST on row 0: q0 at col 1, q_last at col n. The LAST q cell
        # turns the corner — it does NOT forward east; it hands (yq, xi) SOUTH to the
        # I rail's first cell (directly below) and triggers it, so its resting
        # fwd_face is SOUTH (the @1 abutment then delivers to i0).
        for k, cid in enumerate(q_ids):
            face = "south" if cid == q_ids[-1] else "east"
            layout[cid] = (k + 1, 0, face)
        # I rail flows WEST on row 1: i0 at the east end (col n), i_last at col 1.
        for k, cid in enumerate(i_ids):
            layout[cid] = (n - k, 1, "west")
        return layout

    def process_reference(self, input_samples: np.ndarray) -> np.ndarray:
        """Reference complex matched filter matching the on-chip cells EXACTLY.

        ``input_samples`` is a complex array (or (N,2) real [xi,xq]) of Q15
        SIGNED samples. Returns an (N,2) int array of [yi,yq] (the filtered I/Q),
        bit-identical to running both rails through simkyt and to the
        reference ``fir_macq``/``mf_complex`` (NO group-delay trimming — on-chip
        the filter runs continuously)."""
        def s16(v):
            return v - 0x10000 if v & 0x8000 else v
        taps = [s16(t) for t in self._coeff_q15]

        arr = np.asarray(input_samples)
        if np.iscomplexobj(arr):
            xs = [(int(round(c.real)), int(round(c.imag))) for c in arr]
        elif arr.ndim == 2 and arr.shape[1] == 2:
            xs = [(s16(int(x) & 0xFFFF), s16(int(y) & 0xFFFF)) for x, y in arr]
        else:
            xs = [(s16(int(x) & 0xFFFF), 0) for x in arr]

        xi = [v[0] for v in xs]
        xq = [v[1] for v in xs]

        def fir(x):
            L = len(taps)
            out = []
            for n in range(len(x)):
                acc = 0
                for k in range(L):
                    s = x[n - k] if 0 <= n - k < len(x) else 0
                    acc = s16((acc + ((s * taps[k]) >> 15)) & 0xFFFF)
                out.append(acc)
            return out

        fi = fir(xi)
        fq = fir(xq)
        return np.array([(a & 0xFFFF, b & 0xFFFF) for a, b in zip(fi, fq)],
                        dtype=np.int32)

    def reset(self):
        """Reset (stateless FIR — delay lines live in cell state)."""
        pass
