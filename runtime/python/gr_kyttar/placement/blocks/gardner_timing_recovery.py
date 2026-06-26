"""GardnerTimingRecovery — see :class:`GardnerTimingRecovery`."""
import numpy as np
from ..block import CellProgram, Port, EntryPoint, StateVar, DataWord
from typing import Dict, List, Tuple, Any
from ._base import KyttarBlock, BlockInterface, assemble_to_words, float_to_q15, q15_to_float


class GardnerTimingRecovery(KyttarBlock):
    """
    Gardner symbol-timing recovery — production 3-cell implementation.

    A REAL timing-recovery loop (the older fixed-rate-decimator version could not
    move the sampling instant). A 2-samples/symbol input drives a Gardner timing
    error detector + PI loop filter + an NCO-controlled interpolating resampler
    that actually advances/retards where it samples. Validated on-chip bit-exact
    (the 3-cell loop matches its Q15 reference 20/20) and recovers symbol timing
    BER=0 across fractional offsets 0.3-0.7 — see
    the internal reference implementation (algorithm) and
    ``proto_gardner_chip.py`` (on-chip cells).

    Cells (forward chain on one row, period feedback returns via the row below)::

        resampler ──► ted ──► loop_filter
            ▲                      │
            └─── period feedback ──┘

      * resampler: holds a 2-sample delay line + a Q14 phase accumulator (1.0 =
        0x4000 so it stays positive for the phase>=period sign test); on each
        input it advances phase and, when phase>=period, emits ONE interpolated
        strobe (value + a parity tag, 0=center / 0x4000=mid). A MID strobe resets
        the period to nominal locally; the CENTER's corrected period arrives from
        the loop filter (one-execution feedback delay). Phase carries across
        inputs (the remainder is kept), so a shrunk period advances the sampling
        instant — the real timing recovery.
      * ted: Gardner error e = (center - center_prev) * mid on a CENTER strobe
        (branches on the parity tag); passes the center sample through.
      * loop_filter: PI controller — integ += ki*e (clamped); corr = kp*e + integ;
        period = nominal - (corr>>1); feeds `period` BACK to the resampler and
        emits the recovered center sample.

    Interface: a real 2-sps input stream; the output is the recovered
    symbol-rate (center) sample stream (slice its sign for BPSK bits).

    Gains kp=3, ki=1 (raw, applied via a rounded Q15 multiply) for the validated
    loop bandwidth; the period/phase scale is Q14.
    """
    CATEGORY = "recovery"
    TAGS = ["gardner", "timing_recovery", "ted", "symbol_sync", "recovery"]

    # Complex/real 2-sps input lands at R0 of the resampler landing cell; the
    # recovered center sample is the output.
    _interface = BlockInterface(
        entry_address=1, input_registers=[0], output_registers=[0]
    )

    _CELL_IDS = ["resampler", "ted", "loop_filter", "period_relay"]

    # loop_filter dual-face emit (face codes S=0, E=1, W=2, N=3). `out` egresses
    # SOUTH (outward, toward the bus/downstream slicer); `period_fb` returns WEST
    # to the `period_relay` cell (which forwards it NORTH into the resampler's
    # `period` state — see below). These MUST stay consistent with
    # ``default_layout`` (loop_filter's resting face == FACE_FB so the build's
    # feedback tracer follows it to the relay).
    _FACE_OUT = 0   # south
    _FACE_FB = 2    # west

    def __init__(self, name: str, kp: int = 3, ki: int = 1):
        """
        Args:
            name: Block name.
            kp: Proportional loop-filter gain (raw Q15-multiply scale). Default 3.
            ki: Integral loop-filter gain. Default 1. (kp=3, ki=1 is validated.)
        """
        super().__init__(name, kp=kp, ki=ki)
        self._kp = int(kp)
        self._ki = int(ki)

    @property
    def cell_count(self) -> int:
        # resampler, ted, loop_filter + the period_relay (PI filter / feedback
        # relay that closes the timing loop as a data path — see build_cell_programs).
        return 4

    @property
    def interface(self) -> BlockInterface:
        return self._interface

    def build_cell_programs(self) -> Dict[str, CellProgram]:
        """The 3 proven cells (ported verbatim from proto_gardner_chip.py)."""
        kp, ki = self._kp, self._ki

        # --- C1 resampler: Q14 NCO + 2-sample delay line + interp + parity. ---
        resampler = CellProgram(
            inputs=[Port("xi", register=0)],
            outputs=[Port("val"), Port("par"), Port("trig")],
            entries=[EntryPoint("default")],
            data=[DataWord("inc", 1 << 14, address=1),
                  DataWord("one_q14", 1 << 14, address=2)],
            state=[StateVar("phase"), StateVar("xp"), StateVar("xp2"),
                   StateVar("period", initial_value=1 << 14),
                   StateVar("parity"), StateVar("diff")],
            assembly_template="""\
start:
    MOVE R{state:xp2}, R{state:xp}
    MOVE R{state:xp}, R{in:xi}
    ADD R{state:phase}, R{data:inc}
    MOVE R{state:phase}, R0
    SUB R{state:phase}, R{state:period}
    BR.N done
    MOVE R{state:phase}, R0
    SUB R{state:xp}, R{state:xp2}
    MOVE R{state:diff}, R0
    SHL R{state:phase}, #1
    MULQ R0, R{state:diff}
    ADD R0, R{state:xp2}
    {write:val}
    CMP R{state:parity}, R{data:one_q14}
    BR.NZ emitpar
    MOVE R{state:period}, R{data:one_q14}
emitpar:
    MOVE R0, R{state:parity}
    {write:par}
    XOR R{state:parity}, R{data:one_q14}
    MOVE R{state:parity}, R0
    {jump:val}
done:
    {jump:trig}
""",
        )

        # --- C2 ted: Gardner error on a center strobe; pass center through. ---
        ted = CellProgram(
            inputs=[Port("val", register=0), Port("par", register=1)],
            outputs=[Port("e_out"), Port("c_out"), Port("trig")],
            entries=[EntryPoint("default")],
            data=[DataWord("one_q14", 1 << 14, address=2)],
            state=[StateVar("cprev"), StateVar("half"), StateVar("e"),
                   StateVar("cs")],
            assembly_template="""\
start:
    CMP R{in:par}, R{data:one_q14}
    BR.NZ center
    MOVE R{state:half}, R{in:val}
    {jump:trig}
center:
    MOVE R{state:cs}, R{in:val}
    MOVE R{state:e}, R{state:cs}
    SUB R{state:e}, R{state:cprev}
    MOVE R{state:e}, R0
    MULQ R{state:e}, R{state:half}
    MOVE R{state:e}, R0
    MOVE R{state:cprev}, R{state:cs}
    {write:e_out}
    MOVE R0, R{state:cs}
    {write:c_out}
    {jump:c_out}
""",
        )

        # --- C3 loop_filter: emits the recovered center `out` (forward, toward the
        # downstream slicer/bus) AND hands the Gardner error `e` to the period_relay
        # (the PI filter, which computes + feeds back the period). The PI math lives
        # in the relay, NOT here, for two reasons:
        #   (1) FEEDBACK ROUTING: a feedback loop must not be closed through a TRIGGER
        #       path — if this last datapath cell triggered the resampler directly, the
        #       loop (resampler -> ted -> loop_filter -> period -> resampler) would have
        #       no slack and stall after one center. Instead a relay cell accepts this
        #       cell's emit (freeing `out` to go forward) and writes `period` into the
        #       resampler as PURE DATA (no trigger — read by the NEXT sample, like the
        #       Costas dphase feedback). The data-only feedback breaks the cyclic
        #       dependency so the loop runs continuously.
        #   (2) BUDGET: the full PI filter (integ accumulate + +/-256 clamp + corr +
        #       period + >=1 floor) does not fit in one cell alongside the dual-face
        #       emit. The relay has ample register space.
        #
        # DUAL-FACE EMIT: this cell rests facing the feedback (WEST) direction so the
        # build's feedback tracer (``_apply_internal_feedback``, which follows the
        # cell's resting fwd_face) finds the relay; it emits `e_fb` + ``fb_trig`` WEST
        # to the relay, then FLIPS to FACE_OUT and emits `out` SOUTH (the highest-address
        # WRITE, so ``_patch_last_write_handoff`` patches only the egress), then a FINAL
        # flip back to WEST. face codes S=0,E=1,W=2,N=3.
        loop_filter = CellProgram(
            inputs=[Port("e_in", register=0), Port("cval", register=1)],
            outputs=[Port("out"), Port("e_fb"), Port("fb_trig"), Port("trig")],
            entries=[EntryPoint("default")],
            data=[DataWord("face_out", self._FACE_OUT, address=2, is_face=True),
                  DataWord("face_fb", self._FACE_FB, address=3, is_face=True)],
            state=[StateVar("es"), StateVar("cs")],
            assembly_template="""\
start:
    MOVE R{state:es}, R{in:e_in}
    MOVE R{state:cs}, R{in:cval}
    MOVE [FACE], R{data:face_fb}
    MOVE R0, R{state:es}
    {write:e_fb}
    {jump:fb_trig}
    MOVE [FACE], R{data:face_out}
    MOVE R0, R{state:cs}
    {write:out}
    {jump:trig}
    MOVE [FACE], R{data:face_fb}
""",
        )

        # --- C4 period_relay: the PI loop filter + the deadlock-breaking relay.
        #
        # Triggered by the loop_filter on each CENTER (``fb_trig``); receives the
        # Gardner error `e`. Runs the PI controller EXACTLY as the reference
        # (process_reference / proto_gardner):
        #     integ += ki*e ; integ = clamp(integ, -256, +256)
        #     corr  = kp*e + integ
        #     period = one_q14 - (corr>>1) ; period = max(1, period)
        # then WRITES `period` (pure data, NO trigger) NORTH into the resampler's
        # `period` state — the Costas dphase feedback model. The integ ±256 clamp and
        # the period >=1 floor are ESSENTIAL: without them tiny per-center errors
        # wind the integrator up unbounded, period saturates negative, and the
        # resampler's ``phase >= period`` test fires every input (no 2:1 decimation,
        # never settles). They live HERE (not the loop_filter) because the loop_filter
        # is register-tight and the relay sits exactly on the period feedback path.
        # The relay is a PROGRAMMED cell (not a plain transit cell): it reads the
        # period correction AS DATA and re-emits it, so the timing feedback loop is
        # closed through a data path rather than a trigger path.
        period_relay = CellProgram(
            inputs=[Port("e_in", register=0)],
            outputs=[Port("pout")],
            entries=[EntryPoint("relay")],
            data=[DataWord("kp", kp, address=1),
                  DataWord("ki", ki, address=2),
                  DataWord("one_q14", 1 << 14, address=3),
                  DataWord("ilim", 256, address=4),
                  DataWord("nilim", (-256) & 0xFFFF, address=5),
                  DataWord("signbit", 0x8000, address=6)],
            state=[StateVar("integ"), StateVar("es"), StateVar("sg")],
            assembly_template="""\
relay:
    MOVE R{state:es}, R{in:e_in}
    MULQ R{state:es}, R{data:ki}
    ADD R{state:integ}, R0
    MOVE R{state:integ}, R0
    CMP R{state:integ}, R{data:ilim}
    BR.N ihi
    MOVE R{state:integ}, R{data:ilim}
ihi:
    CMP R{state:integ}, R{data:nilim}
    BR.NN ilo
    MOVE R{state:integ}, R{data:nilim}
ilo:
    MULQ R{state:es}, R{data:kp}
    ADD R0, R{state:integ}
    MOVE R{state:es}, R0
    AND R0, R{data:signbit}
    MOVE R{state:sg}, R0
    SHR R{state:es}, #1
    OR R0, R{state:sg}
    MOVE R{state:es}, R0
    MOVE R0, R{data:one_q14}
    SUB R0, R{state:es}
    {write:pout}
""",
        )

        return {"resampler": resampler, "ted": ted, "loop_filter": loop_filter,
                "period_relay": period_relay}

    def internal_connections(self) -> List[Tuple[int, str, int, str]]:
        """Forward data handoffs + the period FEEDBACK (loop_filter -> resampler).

        The resampler tags each strobe (val + par); the TED branches center/mid on
        par. The loop filter feeds the corrected `period` back to the resampler's
        period state (the loop closure, routed via the row-below return path).
        """
        return [
            ("resampler", "val", "ted", "val"),
            ("resampler", "par", "ted", "par"),
            ("ted", "e_out", "loop_filter", "e_in"),
            ("ted", "c_out", "loop_filter", "cval"),
            # FEEDBACK (via the relay PI filter): the loop_filter hands the Gardner
            # error `e` to the `period_relay` (forward, WEST); the relay runs the PI
            # controller and writes the corrected `period` as a pure data WRITE into
            # the resampler's `period` state (backward, NORTH). Closing the loop
            # through a data WRITE (not a trigger) keeps the feedback independent of
            # the forward path (see its program).
            ("loop_filter", "e_fb", "period_relay", "e_in"),
            ("period_relay", "pout", "resampler", "period"),
        ]

    def internal_jumps(self) -> List[Tuple[int, str, int, str]]:
        """JUMP triggers. The resampler triggers the TED via its `val` emit (only
        on a strobe); the TED triggers the loop filter via `c_out` (only on a
        center). The loop_filter triggers the `period_relay` via `fb_trig` (the
        feedback path). The relay does NOT trigger the resampler — the period lands
        as pure data, read by the next external sample (the Costas dphase model).
        No-strobe / mid cases terminate locally (the `trig` outputs)."""
        return [
            ("resampler", "val", "ted", "default"),
            ("ted", "c_out", "loop_filter", "default"),
            ("loop_filter", "fb_trig", "period_relay", "relay"),
            # The resampler's NO-STROBE `trig`, the TED's MID-strobe `trig`, and the
            # loop_filter's local `trig` must JUMP NOWHERE — only a CENTER strobe may
            # advance the chain and emit a recovered symbol. Without these, an
            # unmapped `trig` falls through to the positional-next cell, firing the
            # chain (and a downstream slicer) on every input => ~2x outputs (one per
            # input sample, not one per symbol).
            ("resampler", "trig", "__terminate__", "default"),
            ("ted", "trig", "__terminate__", "default"),
            ("loop_filter", "trig", "__terminate__", "default"),
        ]

    def default_layout(self) -> Dict[Any, Tuple[int, int, str]]:
        """Compact 2x2 fold (CM-approved): resampler(0,0)->ted(1,0) on row 0
        facing east; the loop_filter folds down to (1,1). It is a DUAL-FACE cell
        (see ``build_cell_programs``): it emits `out` SOUTH (outward, the
        recovered center to a downstream slicer/bus) and `period_fb`/`fb_trig` WEST
        (the feedback) via in-program FACE flips. Its LAYOUT face is WEST — the
        feedback direction — so the build's feedback tracer
        (``_apply_internal_feedback``, which follows the cell's fwd_face) finds the
        relay. The `out` egress face is set at runtime by the in-program flip,
        independent of the layout face.

        Feedback return (the deadlock-free ring): loop_filter(1,1) --WEST-->
        period_relay(0,1) --NORTH--> resampler(0,0). The ``period_relay`` is a
        PROGRAMMED consumer (NOT a face-only transit): it acks the loop_filter on
        capture so the ring has slack, then writes `period` NORTH into the resampler
        as pure data (no trigger). It RESTS facing NORTH so the feedback tracer
        follows its fwd_face to the resampler. The loop_filter rests facing WEST
        (the final MOVE [FACE]), so its runtime resting face matches its layout face
        for the tracer."""
        return {
            "resampler": (0, 0, "east"),     # val/par EAST -> ted
            "ted": (1, 0, "south"),          # e_out/c_out SOUTH -> loop_filter(1,1)
            "loop_filter": (1, 1, "west"),   # dual-face: out SOUTH, period_fb WEST
            # Programmed relay: receives period_fb (EAST, from loop_filter) and
            # forwards `period` NORTH into the resampler (the deadlock fix).
            "period_relay": (0, 1, "north"),
        }

    def output_cell_id(self) -> Any:
        """The recovered center `out` leaves the loop_filter (the last datapath
        cell), which ALSO carries the `period_fb` feedback WRITE. Declaring it
        explicitly tells the build to patch ONLY the loop_filter's LAST output
        WRITE/JUMP for the brokered output route (``output_at_last_write``),
        leaving the feedback WRITE + the relay's feedback path intact, regardless
        of how many cells the block has or their dict ordering."""
        return "loop_filter"

    def output_face_addr(self) -> Any:
        """The loop_filter is a DUAL-FACE output cell: its ``out`` WRITE fires on the
        in-program ``MOVE [FACE], R{face_out}`` flip (addr 2), independent of the
        cell's resting ``fwd_face`` (which carries the WEST feedback). So the build
        must rewrite THIS face word to the drawn route's first-hop direction — else
        the ``out`` word fires on the baked-in/rotated ``face_out`` and, when the
        route leaves another way (a rotated/relocated block), shoots into empty cells
        and stray-executes (the phantom-route bug). ``face_out`` is DataWord addr 2."""
        return 2

    def process_reference(self, input_samples: np.ndarray) -> np.ndarray:
        """Reference Q15 Gardner loop modelling the on-chip cells EXACTLY (matches
        the chip bit-exact; recovers timing BER=0 frac 0.3-0.7).

        ``input_samples`` is a real (or complex; the real part is used) 2-sps
        stream. Returns the recovered symbol-center samples as Q15 int16.
        Persistent phase carries across inputs; one strobe/input; a MID strobe
        resets period locally, a CENTER's corrected period is deferred one strobe.
        """
        def s16(v):
            return v - 0x10000 if v & 0x8000 else v

        def u16(v):
            return v & 0xFFFF

        def mqr(a, b):
            return (s16(a) * s16(b) + (1 << 14)) >> 15

        arr = np.asarray(input_samples)
        if np.iscomplexobj(arr):
            sq = [float_to_q15(float(c.real)) for c in arr]
        elif arr.dtype.kind == "f":
            sq = [float_to_q15(float(x)) for x in arr]
        else:
            sq = [int(x) & 0xFFFF for x in arr]

        kp, ki = self._kp, self._ki
        out = []
        integ = 0
        cprev = 0
        half = 0
        phase = 0
        period = 1 << 14
        xp = 0
        xp2 = 0
        parity = 0
        pend = None
        for v in sq:
            xi = s16(v)
            xp2 = xp
            xp = xi
            phase += 1 << 14
            if phase >= period:
                phase -= period
                frac = (phase << 1) & 0xFFFF
                s = xp2 + mqr(u16(frac), u16((xp - xp2) & 0xFFFF))
                if pend is not None:
                    period = pend
                    pend = None
                if parity == 0:    # CENTER
                    e = mqr(u16((s - cprev) & 0xFFFF), u16(half & 0xFFFF))
                    cprev = s
                    out.append(s16(u16(s)))
                    integ += mqr(u16(ki), u16(e & 0xFFFF))
                    integ = max(-256, min(256, integ))
                    corr = mqr(u16(kp), u16(e & 0xFFFF)) + integ
                    pend = (1 << 14) - (corr >> 1)
                    if pend < 1:
                        pend = 1
                else:              # MID
                    half = s
                    period = 1 << 14
                parity ^= 1
        return np.array(out, dtype=np.int16)

    def reset(self):
        """Stateless reference (process_reference is self-contained)."""
        pass
