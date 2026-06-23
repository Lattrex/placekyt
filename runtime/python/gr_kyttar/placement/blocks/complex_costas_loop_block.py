"""ComplexCostasLoopBlock — see :class:`ComplexCostasLoopBlock`."""
import numpy as np
import math
from ..block import CellProgram, Port, EntryPoint, StateVar, DataWord
from typing import Dict, List, Tuple, Any
from ._base import KyttarBlock, BlockInterface, assemble_to_words, float_to_q15, q15_to_float


class ComplexCostasLoopBlock(KyttarBlock):
    """
    Complex-input Costas carrier recovery — production 7-cell implementation.

    Unlike the older real-input CostasLoopBlock (which cannot bootstrap a lock:
    a single real input carries no quadrature to drive the loop), this block
    takes COMPLEX baseband (xi, xq) and runs a decision-directed BPSK Costas
    loop that actually locks. Validated bit-exact cell-by-cell and to 50/50 lock
    over multiple frequency offsets in
    the internal reference implementation.

    Algorithm (per sample, phase is a 16-bit accumulator, full circle = 65536)::

        cosv = qw(phase + 16384) ; sinv = -qw(phase)        # NCO, quarter-wave
        yi = xi*cosv - xq*sinv                               # derotate (output)
        yq = xi*sinv + xq*cosv
        err = sign(yi) * yq                                  # decision-directed PD
        freq  += beta  * err                                 # PI loop filter
        dphase = freq + alpha * err
        phase += dphase    (applied at the START of the NEXT sample — the
                            on-chip feedback has a one-sample delay, which is
                            correct and locks)

    Gains are used DIRECTLY in Q15 (no extra scaling, k=1)::

        theta = loop_bw ; d = damping ; denom = 1 + 2*d*theta + theta^2
        alpha = 4*d*theta/denom ; beta = 4*theta^2/denom

    Cells (linear data/trigger chain on row 0, feedback returns via row 1):

        phase | sin_fold | cos_fold | table_sin | table_cos | rotate | pd_pi
          ^                                                              |
          └──────────────── dphase feedback (row-1 west return) ────────┘

      * phase:    holds phase state; phase += dphase (feedback); emits the sin
                  and cos NCO phase words (ph_sin = phase + pi so the sin table
                  yields -sin, matching the derotation); forwards xi, xq.
      * sin_fold/cos_fold: quarter-wave fold (phase -> table index + negate flag).
      * table_sin/table_cos: quarter-wave LUT (index + neg -> Q15 value).
      * rotate:   complex multiply -> yi (the recovered I / block output), yq.
      * pd_pi:    decision-directed phase detector + PI loop filter -> dphase,
                  routed BACK to the phase cell.

    Interface: COMPLEX input (xi at R0, xq at R1 of the phase landing cell);
    output is the recovered yi.

    Do NOT confuse with the legacy real-input CostasLoopBlock — that one is kept
    for reference but does not lock.
    """
    CATEGORY = "recovery"
    TAGS = ["costas", "pll", "carrier_recovery", "complex", "recovery"]

    # 17-entry quarter-wave sine table -> exact 64-entry resolution (as NCOBlock).
    QUARTER_SIZE = 17

    # Landing cell is the phase cell; complex input lands at R0 (xi) and R1 (xq);
    # the recovered yi is the output.
    _interface = BlockInterface(
        entry_address=1, input_registers=[0, 1], output_registers=[0]
    )

    # Cell ids (string keys), in data/trigger-chain order.
    _CELL_IDS = [
        "phase", "sin_fold", "cos_fold", "table_sin", "table_cos",
        "rotate", "pd_pi",
    ]

    def __init__(
        self,
        name: str,
        loop_bw: float = 0.05,
        damping: float = 1.0,
    ):
        """
        Args:
            name: Block name.
            loop_bw: Loop bandwidth (normalized). 0.05 is the validated default.
            damping: Loop damping factor. 1.0 (critically damped) is validated.
        """
        super().__init__(name, loop_bw=loop_bw, damping=damping)
        self._loop_bw = loop_bw
        self._damping = damping

        # Validated gain mapping (used DIRECTLY in Q15, k=1).
        theta = loop_bw
        d = damping
        denom = 1.0 + 2.0 * d * theta + theta * theta
        self._alpha = 4.0 * d * theta / denom
        self._beta = 4.0 * theta * theta / denom
        self._alpha_q15 = float_to_q15(min(0.999, max(-0.999, self._alpha)))
        self._beta_q15 = float_to_q15(min(0.999, max(-0.999, self._beta)))

        # Reference-model state.
        self._phase = 0
        self._freq = 0

    @property
    def cell_count(self) -> int:
        return 7

    @property
    def interface(self) -> BlockInterface:
        return self._interface

    def _quarter_wave_table(self) -> List[int]:
        """17-entry quarter-wave sine table in Q15 (sin 0..90 deg)."""
        return [
            int(round(math.sin(k / 16 * math.pi / 2) * 32767)) & 0xFFFF
            for k in range(self.QUARTER_SIZE)
        ]

    def build_cell_programs(self) -> Dict[str, CellProgram]:
        """The 7 proven cells (ported verbatim from proto_complex_costas.py).

        Returns a dict keyed by the string cell ids in ``_CELL_IDS`` order. The
        router wires them per ``internal_connections``/``internal_jumps`` and
        places them per ``default_layout``.
        """
        qt = self._quarter_wave_table()
        alpha = self._alpha_q15
        beta = self._beta_q15

        # --- phase cell: holds phase; phase += dphase (feedback); emit ph_sin
        # (= phase + pi -> -sin), ph_cos (= phase + pi/2); forward xi, xq. ---
        phase_cell = CellProgram(
            inputs=[Port("xi", register=0), Port("xq", register=1),
                    Port("dphase", register=2)],
            outputs=[Port("ph_sin"), Port("ph_cos"),
                     Port("xi_fwd"), Port("xq_fwd"), Port("trig")],
            entries=[EntryPoint("default")],
            data=[DataWord("quarter", 16384, address=3),
                  DataWord("half", 32768, address=4)],
            state=[StateVar("phase"), StateVar("xis"), StateVar("xqs")],
            assembly_template="""\
start:
    MOVE R{state:xis}, R{in:xi}
    MOVE R{state:xqs}, R{in:xq}
    ADD R{state:phase}, R{in:dphase}
    MOVE R{state:phase}, R0
    ADD R{state:phase}, R{data:half}
    {write:ph_sin}
    MOVE R0, R{state:phase}
    ADD R0, R{data:quarter}
    {write:ph_cos}
    MOVE R0, R{state:xis}
    {write:xi_fwd}
    MOVE R0, R{state:xqs}
    {write:xq_fwd}
    {jump:trig}
""",
        )

        # --- fold cell (sin & cos use identical programs): phase -> idx, neg. ---
        def _fold_cell():
            return CellProgram(
                inputs=[Port("phase", register=0)],
                outputs=[Port("neg"), Port("idx"), Port("trig")],
                entries=[EntryPoint("default")],
                data=[DataWord("thirtytwo", 32, address=1),
                      DataWord("fifteen", 15, address=2),
                      DataWord("sixteen", 16, address=3),
                      DataWord("zero", 0, address=4)],
                state=[StateVar("ph"), StateVar("fidx"), StateVar("loc")],
                assembly_template="""\
start:
    MOVE R{state:ph}, R{in:phase}
    SHR R{state:ph}, #10
    MOVE R{state:fidx}, R0
    AND R{state:fidx}, R{data:thirtytwo}
    {write:neg}
    AND R{state:fidx}, R{data:fifteen}
    MOVE R{state:loc}, R0
    AND R{state:fidx}, R{data:sixteen}
    CMP R0, R{data:zero}
    BR.Z nomir
    SUB R{data:sixteen}, R{state:loc}
    MOVE R{state:loc}, R0
nomir:
    MOVE R0, R{state:loc}
    {write:idx}
    {jump:trig}
""",
            )

        # --- table cell (sin & cos): idx, neg -> Q15 value.  The qt table MUST
        # start at R2 (idx=R0, neg=R1); placing it at R1 would let `neg` clobber
        # qt[0]=0 and leak a spurious -32 sinv at phase=0 (the convergence bug). -
        def _table_cell():
            data = [DataWord(f"qt{i}", v, address=2 + i)
                    for i, v in enumerate(qt)]
            data += [DataWord("tbase", 2, address=19),
                     DataWord("zero", 0, address=20)]
            return CellProgram(
                inputs=[Port("idx", register=0), Port("neg", register=1)],
                outputs=[Port("val"), Port("trig")],
                entries=[EntryPoint("default")],
                data=data, state=[StateVar("v")],
                assembly_template="""\
start:
    ADD R{in:idx}, R{data:tbase}
    LOAD R0
    MOVE R{state:v}, R0
    CMP R{in:neg}, R{data:zero}
    BR.Z out
    SUB R{data:zero}, R{state:v}
out:
    {write:val}
    {jump:trig}
""",
            )

        # --- rotate cell: complex multiply.  yi = xi*cosv - xq*sinv (the recovered
        # I), yq = xi*sinv + xq*cosv.  yi is BROADCAST: it drives the phase
        # detector (pd_pi, the internal loop) AND a `yi_tap` output so the recovered
        # I can leave the block (e.g. to x16_out / the downstream Gardner) WITHOUT a
        # downstream route having to rewrite the internal yi->pd_pi handoff.
        #
        # DUAL-FACE EMIT (CM-approved; same primitive as the Gardner loop_filter /
        # the CoherentBPSKRx slicer): the INTERNAL yi/yq handoffs to pd_pi and the
        # EXTERNAL yi_tap output go in DIFFERENT directions once the bus router faces
        # this cell toward the yi_tap bus tap. On ONE fwd_face they would all chase
        # the bus and starve pd_pi (the Costas mis-derotates and the downstream gets
        # garbage). So the program explicitly sets the output FACE per emit:
        #   1. MOVE [FACE], face_internal  -> emit yi, yq toward pd_pi (the loop)
        #   2. MOVE [FACE], face_tap       -> emit yi_tap (the LAST WRITE — the build's
        #      ``_patch_last_write_handoff`` patches only this one to the route hop)
        # ``face_internal`` is the resting toward-pd_pi direction (default_layout
        # WEST=2; orientation-transformed with the cell by ``_apply_orientation_face_words``).
        # ``face_tap`` defaults to the SAME value (so a STANDALONE Costas, where
        # yi_tap is unconsumed, and the CoherentBPSKRx, where yi_tap also goes WEST,
        # both keep working untouched); when the bus router faces this cell toward a
        # tap, the build OVERRIDES face_tap from the route's first-hop exit direction
        # (``_apply_rotate_tap_face`` in engine/build.py). No trailing restore is
        # needed — the next sample re-sets face_internal before yi/yq.
        #
        # The WRITE ORDER is PRESERVED from the validated single-face cell — yi FIRST
        # (→pd_pi R0), then yq (→pd_pi R1), then yi_tap, then trig — so pd_pi sees the
        # SAME internal-handoff arrival order as in the standalone Costas (reordering
        # them starves pd_pi in the abutted CoherentBPSKRx layout where yi_tap @2
        # transits pd_pi). yi is saved in `yis` so yi_tap can re-emit it after yq
        # overwrites R0. The only inserted instructions are the two FACE flips
        # (internal before yi/yq, tap before yi_tap); a redundant post-SUB `MOVE
        # acc, R0` was dropped to stay within the register budget (faces at addr 4+11).
        # TWO triggers (one JUMP each): `trig` fires pd_pi (the loop) AND a separate
        # `tap_trig` fires the DOWNSTREAM consumer of yi_tap (the Gardner, via a bus
        # broker/crossover). The rotate's output leaves a MID-block cell, so the build
        # patches the cell's LAST WRITE (yi_tap) and LAST JUMP for the route — by
        # emitting `tap_trig` LAST, the build's `_patch_last_jump_handoff` retargets it
        # to the route while `trig` -> pd_pi keeps its @1 abutment hop (without this
        # split, the single `trig` JUMP was hijacked by the route and the pd_pi loop
        # was never triggered, so the Costas never locked — the flagship dead-build).
        # When yi_tap is UNCONSUMED (a standalone Costas) `tap_trig` resolves to a
        # local terminator (`__terminate__`, harmless), exactly like pd_pi's trig.
        rotate_cell = CellProgram(
            inputs=[Port("xi", register=0), Port("xq", register=1),
                    Port("sinv", register=2), Port("cosv", register=3)],
            outputs=[Port("yi"), Port("yi_tap"), Port("yq"), Port("trig"),
                     Port("tap_trig")],
            entries=[EntryPoint("default")],
            data=[DataWord("face_internal", 2, address=4, is_face=True),
                  DataWord("face_tap", 2, address=11, is_face=True)],
            state=[StateVar("xis", register=5), StateVar("xqs", register=6),
                   StateVar("sv", register=7), StateVar("cv", register=8),
                   StateVar("acc", register=9)],
            assembly_template="""\
start:
    MOVE R{state:xis}, R{in:xi}
    MOVE R{state:xqs}, R{in:xq}
    MOVE R{state:sv}, R{in:sinv}
    MOVE R{state:cv}, R{in:cosv}
    MOVE [FACE], R{data:face_internal}
    MULQ R{state:xis}, R{state:sv}
    MOVE R{state:acc}, R0
    MULQ R{state:xqs}, R{state:cv}
    ADD R{state:acc}, R0
    {write:yq}
    MULQ R{state:xis}, R{state:cv}
    MOVE R{state:acc}, R0
    MULQ R{state:xqs}, R{state:sv}
    SUB R{state:acc}, R0
    {write:yi}
    {jump:trig}
    MOVE [FACE], R{data:face_tap}
    {write:yi_tap}
    {jump:tap_trig}
""",
        )

        # NOTE on the rotate WRITE order vs the dual-face: the dual-face rotate above
        # emits yq BEFORE yi (yi computed last so R0 holds it for the trailing yi_tap
        # WRITE without an extra `yis` save — the register budget can't hold both `yis`
        # and the two face words). That order is fine for a STANDALONE Costas / the
        # flagship (yi_tap egresses to the bus, NOT through pd_pi). It is NOT fine for
        # the abutted CoherentBPSKRx layout, where yi_tap is an INTERNAL @2 handoff that
        # TRANSITS pd_pi: emitting yi (the @1 pd_pi handoff) immediately before the
        # yi_tap @2 transit races the transit against the just-landed yi in pd_pi's R0
        # and starves the loop. CoherentBPSKRx therefore overrides rotate with
        # :meth:`_rotate_legacy_single_face` (yi-first, single fwd_face — proven), which
        # it needs anyway since its yi_tap goes the SAME direction (WEST) as pd_pi.

        # --- pd_pi cell: err = sign(yi)*yq; freq += beta*err;
        # dphase = freq + alpha*err.  freq is state; dphase feeds back. ---
        pd_pi_cell = CellProgram(
            inputs=[Port("yi", register=0), Port("yq", register=1)],
            outputs=[Port("dphase"), Port("trig")],
            entries=[EntryPoint("default")],
            data=[DataWord("zero", 0, address=2),
                  DataWord("alpha", alpha, address=3),
                  DataWord("beta", beta, address=4)],
            state=[StateVar("freq"), StateVar("err"), StateVar("yqs")],
            assembly_template="""\
start:
    MOVE R{state:yqs}, R{in:yq}
    MOVE R{state:err}, R{in:yq}
    CMP R{in:yi}, R{data:zero}
    BR.NN pos
    SUB R{data:zero}, R{state:yqs}
    MOVE R{state:err}, R0
pos:
    MULQ R{state:err}, R{data:beta}
    ADD R{state:freq}, R0
    MOVE R{state:freq}, R0
    MULQ R{state:err}, R{data:alpha}
    ADD R{state:freq}, R0
    {write:dphase}
    {jump:trig}
""",
        )

        return {
            "phase": phase_cell,
            "sin_fold": _fold_cell(),
            "cos_fold": _fold_cell(),
            "table_sin": _table_cell(),
            "table_cos": _table_cell(),
            "rotate": rotate_cell,
            "pd_pi": pd_pi_cell,
        }

    @staticmethod
    def _rotate_legacy_single_face() -> CellProgram:
        """The PROVEN single-fwd_face rotate (yi-FIRST WRITE order, no FACE flips).

        Used by :class:`CoherentBPSKRxBlock`, whose abutted layout sends yi/yq AND
        yi_tap the SAME direction (WEST) — so no dual-face flip is needed — and whose
        yi_tap is an INTERNAL @2 handoff TRANSITING pd_pi. In that transit the WRITE
        ORDER matters: yi must be emitted BEFORE yq so the yi_tap @2 transit does not
        race the just-landed yi in pd_pi's R0 (the dual-face rotate, which emits yi
        last to free the `yis` register for its two face words, starves pd_pi here).
        yi is saved in `yis` so the final yi_tap WRITE re-emits it after yq overwrites
        R0. Identical to the originally-validated rotate (proto_complex_costas)."""
        return CellProgram(
            inputs=[Port("xi", register=0), Port("xq", register=1),
                    Port("sinv", register=2), Port("cosv", register=3)],
            outputs=[Port("yi"), Port("yi_tap"), Port("yq"), Port("trig")],
            entries=[EntryPoint("default")],
            data=[DataWord("zero", 0, address=4)],
            state=[StateVar("xis"), StateVar("xqs"), StateVar("sv"),
                   StateVar("cv"), StateVar("acc"), StateVar("yis")],
            assembly_template="""\
start:
    MOVE R{state:xis}, R{in:xi}
    MOVE R{state:xqs}, R{in:xq}
    MOVE R{state:sv}, R{in:sinv}
    MOVE R{state:cv}, R{in:cosv}
    MULQ R{state:xis}, R{state:cv}
    MOVE R{state:acc}, R0
    MULQ R{state:xqs}, R{state:sv}
    SUB R{state:acc}, R0
    MOVE R{state:acc}, R0
    MOVE R{state:yis}, R0
    {write:yi}
    MULQ R{state:xis}, R{state:sv}
    MOVE R{state:acc}, R0
    MULQ R{state:xqs}, R{state:cv}
    ADD R{state:acc}, R0
    {write:yq}
    MOVE R0, R{state:yis}
    {write:yi_tap}
    {jump:trig}
""",
        )

    def internal_connections(self) -> List[Tuple[int, str, int, str]]:
        """Data handoffs between cells (src_cell, src_out, dst_cell, dst_in).

        The forward datapath plus the dphase FEEDBACK from pd_pi back to the
        phase cell (the loop closure). Every handoff is declared explicitly so
        the router does not fall back to its positional 'next cell' default.
        """
        return [
            # phase -> folds (NCO phase words) and -> rotate (forwarded xi/xq).
            ("phase", "ph_sin", "sin_fold", "phase"),
            ("phase", "ph_cos", "cos_fold", "phase"),
            ("phase", "xi_fwd", "rotate", "xi"),
            ("phase", "xq_fwd", "rotate", "xq"),
            # folds -> tables (index + negate flag).
            ("sin_fold", "idx", "table_sin", "idx"),
            ("sin_fold", "neg", "table_sin", "neg"),
            ("cos_fold", "idx", "table_cos", "idx"),
            ("cos_fold", "neg", "table_cos", "neg"),
            # tables -> rotate (sinv/cosv).
            ("table_sin", "val", "rotate", "sinv"),
            ("table_cos", "val", "rotate", "cosv"),
            # rotate -> pd_pi (yi, yq).  yi MUST reach the phase detector, not
            # just the block output, or the err sign is wrong at zero-crossings.
            ("rotate", "yi", "pd_pi", "yi"),
            ("rotate", "yq", "pd_pi", "yq"),
            # FEEDBACK: pd_pi dphase -> phase cell (loop closure, row-1 return).
            ("pd_pi", "dphase", "phase", "dphase"),
        ]

    def internal_jumps(self) -> List[Tuple[int, str, int, str]]:
        """JUMP triggers forming the linear execution chain (each cell triggers
        the next so every cell runs once per sample). pd_pi terminates the pass;
        the NEXT input sample re-triggers the phase cell (which then applies the
        fed-back dphase).

        pd_pi's trig SELF-TERMINATES (``__terminate__``): pd_pi is the last cell,
        so without this declaration the router defaults its trig JUMP to the
        sink-to-port hop. In the old straight-line layout that stray JUMP died
        harmlessly on the dead row-1 transit cells; in the compact serpentine
        fold it would loop @9 back THROUGH the live datapath (phase..pd_pi),
        coupling the feedback corridor to the forward chain and DEADLOCKING the
        loop after the first sample. Declaring the trig as ``__terminate__``
        resolves it to a LOCAL terminator (@0/entry31), exactly as the proven
        proto's ``JumpTarget(0, 31)`` does — correct in BOTH layouts."""
        return [
            ("phase", "trig", "sin_fold", "default"),
            ("sin_fold", "trig", "cos_fold", "default"),
            ("cos_fold", "trig", "table_sin", "default"),
            ("table_sin", "trig", "table_cos", "default"),
            ("table_cos", "trig", "rotate", "default"),
            ("rotate", "trig", "pd_pi", "default"),
            # rotate's SECOND JUMP triggers the downstream consumer of yi_tap. With no
            # downstream (standalone Costas) it self-terminates locally; a bus route to
            # a Gardner/x16_out retargets it (build `_patch_last_jump_handoff`).
            ("rotate", "tap_trig", "__terminate__", "default"),
            ("pd_pi", "trig", "__terminate__", "default"),
        ]

    def default_layout(self) -> Dict[Any, Tuple[int, int, str]]:
        """COMPACT 4x2 serpentine fold (replaces the 13-cell straight line).

        The datapath snakes across two rows so the dphase FEEDBACK return is a
        single short corridor instead of a 6-cell row-1 corridor::

            col:     0          1           2            3
            row 0:  phase(E)   sin_fold(E) cos_fold(E)  table_sin(S)
            row 1:  fb0(N)     pd_pi(W)    rotate(W)    table_cos(W)

        Forward trace (each cell's single fwd_face is followed by the router's
        ``_get_routing_distance``):
          phase(0,0,E) -> sin_fold(1,0,E) -> cos_fold(2,0,E) -> table_sin(3,0,S)
            -> table_cos(3,1,W) -> rotate(2,1,W) -> pd_pi(1,1,W) -> (0,1,N) -> phase.

        Every forward internal handoff stays traceable along this single
        connected face-path (sin_fold/cos_fold/table_sin/table_cos/rotate all
        lie ON the trace from their source's fwd_face), and rotate ABUTS pd_pi
        (@1) so the exit-cell @1-default never breaks the yi/yq handoff. The
        dphase feedback pd_pi(1,1,W) -> (0,1,N) -> phase(0,0) is @2 (one transit
        cell), traced backward by ``_apply_internal_feedback``.

        The lone FACE-only TRANSIT cell at (0,1) carries NO program (its id
        starts with ``"transit"`` so placeKYT materializes it as a TransitCell,
        per the project rule that transit cells never carry a program).
        """
        return {
            "phase": (0, 0, "east"),
            "sin_fold": (1, 0, "east"),
            "cos_fold": (2, 0, "east"),
            "table_sin": (3, 0, "south"),
            "table_cos": (3, 1, "west"),
            "rotate": (2, 1, "west"),     # yi_tap exits here; abuts pd_pi (@1)
            "pd_pi": (1, 1, "west"),      # dphase -> (0,1) transit -> phase (@2)
            "transit_fb_0": (0, 1, "north"),  # corner return up into phase
        }

    def output_cell_id(self) -> Any:
        """The recovered I (the ``yi_tap`` output) leaves from the ROTATE cell,
        which sits in the MIDDLE of the block (pd_pi and the row-1 feedback transit
        cells follow it in the chain). placeKYT marks/routes the output from here,
        not the last placed cell."""
        return "rotate"

    def process_reference(self, input_samples: np.ndarray) -> np.ndarray:
        """Reference Q15 complex Costas (matches the on-chip cells).

        ``input_samples`` is a complex array (or an (N,2) real array of [xi,xq]).
        Returns the recovered yi as a real Q15 int16 array.
        """
        def s16(v): return v - 0x10000 if v & 0x8000 else v
        def u16(v): return v & 0xFFFF
        def mq(a, b): return u16((s16(a) * s16(b)) >> 15)
        qt = self._quarter_wave_table()

        def qw(ph16):
            fi = (ph16 >> 10) & 0x3F
            neg = (fi & 32) != 0
            mir = (fi & 16) != 0
            lo = fi & 15
            if mir:
                lo = 16 - lo
            v = qt[lo]
            return u16(-s16(v)) if neg else v

        # Accept complex or (N,2) real input.
        arr = np.asarray(input_samples)
        if np.iscomplexobj(arr):
            iq = [(float_to_q15(c.real), float_to_q15(c.imag)) for c in arr]
        elif arr.ndim == 2 and arr.shape[1] == 2:
            iq = [(int(x) & 0xFFFF, int(y) & 0xFFFF) for x, y in arr]
        else:
            # Real input: treat as xi with xq = 0 (will NOT lock — complex in
            # is required; provided only so the reference is total).
            iq = [(float_to_q15(float(x)), 0) for x in arr]

        alpha = self._alpha_q15
        beta = self._beta_q15
        phase = 0
        freq = 0
        out = []
        for (xi, xq) in iq:
            cosv = qw(u16(phase + 16384))
            sinv = u16(-s16(qw(phase)))
            yi = u16(s16(mq(xi, cosv)) - s16(mq(xq, sinv)))
            yq = u16(s16(mq(xi, sinv)) + s16(mq(xq, cosv)))
            out.append(s16(yi))
            err = yq if s16(yi) >= 0 else u16(-s16(yq))
            freq = u16(s16(freq) + s16(mq(beta, err)))
            phase = u16(s16(phase) + s16(freq) + s16(mq(alpha, err)))
        return np.array(out, dtype=np.int16)

    def reset(self):
        """Reset reference-model loop state."""
        self._phase = 0
        self._freq = 0
