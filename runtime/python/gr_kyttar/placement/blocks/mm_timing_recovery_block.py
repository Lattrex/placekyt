# SPDX-License-Identifier: GPL-3.0-or-later
"""MMTimingRecoveryBlock — see :class:`MMTimingRecoveryBlock`."""
import math
import numpy as np
from typing import Any, Dict, List, Tuple

from ..block import CellProgram, Port, EntryPoint, StateVar, DataWord
from ._base import KyttarBlock, BlockInterface, float_to_q15


class MMTimingRecoveryBlock(KyttarBlock):
    """
    Mueller & Müller decision-directed symbol-timing recovery for multilevel QAM —
    the timing stage GNU Radio's ``digital.symbol_sync_cc`` runs with
    ``TED_MUELLER_AND_MULLER``. Unlike Gardner (a BPSK/QPSK non-decision-directed
    detector whose shallow, self-noisy S-curve leaves ~3% jitter on 16-QAM's 4-level
    axes), M&M is DECISION-DIRECTED and locks 16-QAM cleanly at 2 samples/symbol.

    The loop is the canonical Rice "Digital Communications: A Discrete-Time Approach"
    Ch.8 structure (verified against GR ``symbol_sync_cc`` to grid-distance parity):

      1. A **modulo-1 interpolator-control counter**: per input sample the counter
         decrements by ``W = 1/L + v`` (L = 2 sps, v = loop-filter output); when it
         underflows (``cnt < W``) a STROBE fires. This ties the interpolation instant
         to the sample-consumption count via one comparison — the fix for the
         "conflated symbol-clock/interpolator phase" jitter that plagues the older
         ``mm_clock_recovery`` (Andy Walls, GRCon17).
      2. On a strobe, the fractional interval ``mu = cnt / W`` (Rice Eq. 8.89) drives
         a **cubic Farrow interpolator** (continuous mu — NO polyphase phase-snap
         deadzone). The Farrow sub-filter coefficients exceed the Q15 range (|c| up to
         2.5), so they are stored in Q13 (÷4) and the result shifted back ``<<2`` — the
         only way a cubic Farrow fits the Q15 datapath without overflow.
      3. A **decision-directed M&M TED**: slice each rail to the nearest 4-PAM level,
         then ``e = Σ_rail (â_prev·y − â·y_prev)`` (esign −1 for a stable negative-slope
         S-curve zero).
      4. A **2nd-order PI loop filter** (GR ``control_loop`` gains from loop_bw + damping)
         whose output ``v`` adjusts ``W`` — kept in a WIDE accumulator (SC = 2^20) so the
         integral term does not underflow (the Gardner-block integrator lesson).

    Interface: COMPLEX (xi @R0, xq @R1) 2-sps input; emits the recovered symbol-center
    (yi, yq) pair, one per symbol, for a downstream QAM16 slicer.

    SATURATION- AND ORIENTATION-SAFE (UNCONDITIONAL). In an asynchronous streaming
    architecture a block that produces wrong output under back-to-back saturated drive is
    broken, so the serialize-LOCK (INV-19) is a CORRECTNESS REQUIREMENT, not an option:
    the ``counter`` (the NCO landing cell) LOCKs its input arbiter to the feedback face on
    EVERY sample so the next input is HELD until the ``period_relay`` closes the loop and
    clears the lock — one sample fully traverses the fan-out interior (land -> two Farrow
    rails -> the decision-directed ted) before the next is admitted, so no two samples
    co-reside and corrupt ted's decision state. The lock is fully ORIENTATION-INVARIANT
    (INV-23): the LOCK_FACE is written explicitly from an ``is_face`` DataWord (SOUTH at
    identity, the feedback corridor's entry face) so it D4-transforms with the block in all
    8 orientations; the period_relay's lock-clear WRITE.CFG rides the (rotated) feedback
    corridor that build._apply_internal_feedback co-patches with the pout data edge. The
    lock only PACES the input; the per-sample recovered result is byte-identical to the
    GR-verified reference (verified bit-exact: saturated == per-sample; all 8 orientations
    identical).

    OPERATING POINT: decision-directed → SCALE-SENSITIVE. The input constellation MUST
    be at nominal (outer level = 3/sqrt(10) ≈ 0.949, i.e. RMS-matched), NOT peak-scaled.
    Gain-stage upstream (ComplexGainBlock) so the outer symbols sit at 0.949 — a wrong
    scale biases every slicer decision and the loop walks off lock.
    """
    CATEGORY = "recovery"
    TAGS = ["mueller_muller", "mm", "timing_recovery", "ted", "symbol_sync",
            "qam", "decision_directed", "recovery"]

    # --- GR control_loop PI gains (loop_bw=0.02, damping=1.0), the settled operating
    # point verified in proto_mm_authoritative.py. alpha/beta from the standard 2nd-order
    # mapping; K1=alpha/2, K2=beta/2 (the /2 folds the ted_gain normalization k0=2/ted_gain
    # with ted_gain=1).
    #
    # ISA-FRIENDLY reformulation (verified identical to the wide SC=2^20 model AND to GR):
    #   * Counter is Q15 16-bit (ONE=32768, nominal half-period Wnom=ONE/sps=16384) — fits
    #     int16, no wide accumulator, no ADC carry chain.
    #   * mu = cnt << 1 (single SHL) — since W ≈ 0.5, mu = cnt/W ≈ 2·cnt; bit-identical to the
    #     divide-based mu across the offset sweep (the ISA has no divide; this dodges it).
    #   * Loop filter in Q15 (vp = MULQ(e,K1), vi += MULQ(e,K2)) — no integral underflow here
    #     (verified same eye as the wide integrator).
    _ONE = 1 << 15               # counter full-scale (Q15)
    _LOOP_BW = 0.02
    _DAMPING = 1.0

    # Cubic (Catmull-Rom) Farrow polynomial coefficients, stored in Q13 (÷4) so the
    # out-of-range values (up to 2.5) fit int16; the interpolation result is shifted
    # back <<2. Rows are the mu^3, mu^2, mu^1, mu^0 sub-filters over (xm1,x0,x1,x2).
    _C3 = (-0.5, 1.5, -1.5, 0.5)
    _C2 = (1.0, -2.5, 2.0, -0.5)
    _C1 = (-0.5, 0.0, 0.5, 0.0)
    _C0 = (0.0, 1.0, 0.0, 0.0)

    _interface = BlockInterface(entry_address=1, input_registers=[0, 1],
                                output_registers=[0, 1])

    def __init__(self, name: str, sps: int = 2, loop_bw: float = None,
                 damping: float = None):
        """Args: name; sps (samples/symbol, 2); loop_bw / damping (GR symbol_sync
        control-loop parameters; defaults 0.02 / 1.0, the verified operating point).

        The timing loop is UNCONDITIONALLY serialized under saturated drive (INV-19) AND
        orientation-invariant (INV-23) — see the class docstring and the ``counter`` cell.
        The serialize-LOCK is a correctness requirement, not an option: there is no knob to
        turn it off. It only paces the input; the per-sample recovered result is identical
        to the GR-verified reference."""
        super().__init__(name, sps=sps)
        self._sps = int(sps)
        if self._sps != 2:
            raise ValueError("MMTimingRecoveryBlock currently supports sps=2 only")
        self._loop_bw = self._LOOP_BW if loop_bw is None else float(loop_bw)
        self._damping = self._DAMPING if damping is None else float(damping)
        self._K1i, self._K2i = self._pi_gains()

    def _pi_gains(self) -> Tuple[int, int]:
        th = 2 * math.pi * self._loop_bw / self._sps
        dn = 1 + 2 * self._damping * th + th * th
        al = (4 * self._damping * th) / dn
        be = (4 * th * th) / dn
        K1 = al / 2.0
        K2 = be / 2.0
        # Q15 gains (applied as MULQ on the Q15 error).
        return int(round(K1 * self._ONE)), int(round(K2 * self._ONE))

    @property
    def interface(self) -> BlockInterface:
        return self._interface

    # Farrow polynomial coefficients in Q13 (÷4 so the out-of-range |c|<=2.5 fit int16).
    def _farrow_coeffs_q13(self):
        def q13(c):
            return int(round(c * 32768 / 4)) & 0xFFFF
        return ([q13(c) for c in self._C3], [q13(c) for c in self._C2],
                [q13(c) for c in self._C1], [q13(c) for c in self._C0])

    # 13-cell decomposition (the winning hybrid: B's delay-line ownership +
    # C's Horner economy) restructured to the PROVEN complex-Gardner SINGLE-LINEAR-
    # THREAD reconvergence idiom (reconverge_design.md). Per RAIL: a landing cell
    # that OWNS the 4-tap delay line (iland/qland), a Farrow-hi cell (v3,v2 + Horner
    # stage A), a Farrow-lo cell (v1,v0 + Horner finish + saturating <<2), and a
    # branchless 4-PAM slice. The two rails' decisions merge in ONE ted cell (the
    # M&M cross product), then a PI loop_filter, a period_relay (data-only feedback
    # into the counter), and a qout egress cell.
    #
    # ONE linear trigger thread (each cell TRIGGERS exactly ONE next cell, mirroring
    # complex Gardner's qdelay->resampler->ted->loop_filter->qout ring):
    #   counter -> qland -> fq_hi -> fq_lo -> slice_q -> iland -> fi_hi -> fi_lo ->
    #   slice_i -> ted -> loop_filter -> qout   (+ loop_filter -> period_relay fb)
    # The `land` splitter is GONE: the NCO `counter` (complex landing) fans (mu,xi)
    # to iland and (mu,xq) to qland DIRECTLY (1-hop each, opposite faces) and starts
    # the thread by triggering ONLY qland. slice_q deposits (sq,dq) at ted as PURE
    # DATA then TRIGGERS iland (the thread continues linearly up the I rail). slice_i
    # deposits (si,di) at ted then TRIGGERS ted (the reconvergence — by then sq,dq
    # are already in place). ted is ADJACENT to BOTH slices (opposite faces).
    _CELL_IDS = ["counter", "land", "qland", "farrow_q_hi", "farrow_q_lo",
                 "slice_q", "iland", "farrow_i_hi", "farrow_i_lo", "slice_i",
                 "ted", "loop_filter", "period_relay", "qout"]

    # Face codes: S=0, E=1, W=2, N=3.
    # In the RING fold (see default_layout) loop_filter sits at (5,0): its egress
    # cell ``qout`` is directly EAST (6,0) and the feedback ``period_relay`` is
    # directly SOUTH (5,1). So yi/yq egress EAST and v egresses SOUTH.
    _FACE_OUT = 1   # east  (loop_filter forwards yi/yq to qout, which sits E)
    _FACE_FB = 0    # south (loop_filter forwards v to period_relay, which sits S)

    @property
    def cell_count(self) -> int:
        return len(self._CELL_IDS)

    # Q13 (÷4) Farrow coefficients as raw uint16 words, plus the shared consts.
    def _coeff_words(self):
        def q13(c):
            return int(round(c * 32768 / 4)) & 0xFFFF
        c3 = [q13(c) for c in self._C3]
        c2 = [q13(c) for c in self._C2]
        c1 = [q13(c) for c in self._C1]
        return c3, c2, c1

    def build_cell_programs(self) -> Dict[Any, CellProgram]:
        ONE = self._ONE                       # 32768
        Wnom = ONE // self._sps               # 16384
        M7FFF = 0x7FFF                         # mu clamp AND (cnt-W) mod mask
        K1i, K2i = self._K1i, self._K2i
        c3, c2, c1 = self._coeff_words()
        N = 1.0 / math.sqrt(10.0)
        p1 = int(round(1 * N * 32767)); p3 = int(round(3 * N * 32767))
        thr = int(round(2 * N * 32767))
        C0Q = int(round(1.0 * 32768 / 4)) & 0xFFFF   # 8192 = Q13 unity (v0 MAC)

        # ============================================================ counter
        # Q15 M&M NCO + complex landing (xi@R0, xq@R1). Runs EVERY input sample and
        # triggers the `land` fan cell (which fans to both rails and starts the
        # linear thread by triggering qland).
        #   W = Wnom + v                (v = PI output, fed back as pure data)
        #   strobe = cnt < W            (both non-negative)
        #   mu = min(0x7FFF, cnt<<1)    (clamp BINDS; a plain SHL diverges)
        #     CLAMP via the OVERFLOW bit, NOT a signed compare: when cnt >= 0x4000
        #     (a valid strobe with an inflated W, e.g. two strobes close together),
        #     cnt<<1 >= 0x8000 sets bit15, so the SHL result reads NEGATIVE. A
        #     ``CMP R0,0x7FFF; BR.LT`` then treats the overflowed value as < 0x7FFF
        #     and KEEPS it (== the no-strobe sentinel 0x8000!), so the downstream
        #     rail drops that strobe's symbol (the double-strobe / consecutive-center
        #     bug). Instead test the SHL's N flag directly: ``SHL; BR.NN emit`` keeps
        #     the value only when bit15 is CLEAR, else clamps to 0x7FFF — matching the
        #     reference ``min(32767, cnt<<1)`` exactly.
        #   NO-STROBE SENTINEL: mu = 0x8000 (bit15 set) — every downstream cell
        #     gates its compute on ``mu >= 0`` (BR.N on bit15). On a real strobe the
        #     clamp guarantees mu in [0, 0x7FFF], bit15 CLEAR.
        #   cnt = (cnt - W) & 0x7FFF    (== (cnt-W) % ONE)
        # SINGLE-FACE landing: forwards (mu, xi, xq) to the `land` fan cell (one
        # face, EAST), keeping the NCO cell inside budget. `land` fans (mu,xi)->iland
        # + (mu,xq)->qland (pure DATA, 1-hop each) and triggers qland ONLY (the
        # single-trigger discipline that avoids the old dual-trigger runaway).
        #
        # SERIALIZE-LOCK (INV-19), UNCONDITIONAL: the counter is the loop's LANDING cell
        # (input lands + the strobe fires here) and its ``v`` state is the data-only
        # feedback the period_relay writes back. Under SATURATED drive the counter would
        # process the next sample BEFORE the previous sample's feedback closes — and, worse
        # than the simple Gardner loop, the MM interior FANS OUT (`land` -> two parallel
        # Farrow rails -> ted) so two co-resident samples corrupt ted's DECISION-DIRECTED
        # state (lxi/lxq/api/apq) and the Farrow delay-line reads, decoupling the loop (v
        # walks off, the recovered symbols diverge from the per-sample reference). A block
        # that produces wrong output under back-to-back drive is simply BROKEN, so the lock
        # is a correctness requirement, not a knob: LOCK the arbiter to the FEEDBACK face on
        # EVERY sample (not just strobes — the M&M PI loop_filter + period_relay run on EVERY
        # sample, strobe via ted and no-strobe via qland.ns_trig->loop_filter.nostrobe, so
        # the feedback closes and UNLOCKS every sample regardless): one sample fully
        # traverses the interior and closes the loop before the next is admitted.
        #
        # ORIENTATION-SAFE (INV-23): the feedback corridor enters counter on its SOUTH face
        # at IDENTITY (see default_layout: the return corridor's last transit sits directly
        # BELOW counter, resting NORTH). We do NOT rely on SOUTH being the CONFIG reset
        # default — under rotation/mirroring the corridor moves off SOUTH, so we WRITE the
        # LOCK_FACE explicitly from an ``is_face=True`` DataWord (``lock_face``=SOUTH(0)):
        # build._apply_orientation_face_words D4-transforms it (exactly like the `land`
        # cell's face_e/face_n is_face words), so the gate holds the right face in all 8
        # orientations. The lock tail (at ``emit``, where BOTH the strobe and no-strobe
        # paths reconverge) is ``MOVE [LOCK_FACE], R{data:lock_face}`` then
        # ``MOVE [LOCK], R{data:nstrobe}``. Register-reclaim to fit the extra face word +
        # instruction: ``xis`` is ELIMINATED — xi (live in R0 at entry) is forwarded to
        # `land` FIRST via {write:xif} before the W/mu math clobbers R0, so no capture/
        # reload state is needed (a WRITE latches R0 at issue time). And the LOCK-enable
        # source must have BIT0 SET (``MOVE [LOCK],Rn`` sets LOCK=(Rn&1)):
        # the no-strobe sentinel ``nstrobe`` is 0x8001 (bit15 still tags no-strobe for every
        # downstream cell — they gate on mu's SIGN only — and bit0=1 engages the lock), so
        # nstrobe doubles as the lock-enable word, no extra data slot.
        # period_relay CLEARS the lock with a backward WRITE.CFG @N,4 inline with its pout
        # data feedback (the pout->counter.v edge co-patches the WRITE.CFG hop in
        # build._apply_internal_feedback, so it follows the rotated corridor automatically).
        # NOTE: ``MOVE [LOCK],Rn`` / ``MOVE [LOCK_FACE],Rn`` do not touch R0, so mu (live in
        # R0) survives to {write:muf}.
        counter = CellProgram(
            inputs=[Port("xi", register=0), Port("xq", register=1)],
            outputs=[Port("muf"), Port("xif"), Port("xqf"), Port("ltrig")],
            entries=[EntryPoint("default")],
            # nstrobe: the no-strobe mu sentinel = 0x8001. bit15 tags no-strobe (downstream
            # cells gate on mu's SIGN only); bit0=1 ALSO engages the arbiter LOCK
            # (``MOVE [LOCK]`` reads bit0) — reused as the lock-enable word, no extra data
            # slot. The clamp guarantees a real strobe's mu is in [0,0x7FFF] (bit15 clear),
            # so 0x8001 stays an unambiguous no-strobe sentinel. lock_face: the feedback
            # face the UNLOCK enters counter on (SOUTH at identity); is_face so it
            # D4-transforms with the block for orientation safety.
            data=[DataWord("Wnom", Wnom, address=2),
                  DataWord("m7fff", M7FFF, address=3),
                  DataWord("nstrobe", 0x8001, address=4),
                  DataWord("lock_face", self._FACE_FB, address=5, is_face=True)],
            # LOOP MEMORY: the NCO count and the PI output v (fed back). Scratch Ws
            # written before read; mu kept live in R0. xi is forwarded to `land` at entry
            # while still in R0 (no capture state), then R0 is reused for the W/mu math.
            state=[StateVar("cnt", reset_per_batch=True),
                   StateVar("v", reset_per_batch=True),
                   StateVar("Ws")],
            # xi arrives at R0 (the block's pinned input reg 0) but R0 is also the NCO
            # accumulator — so FORWARD xi to `land` FIRST (the WRITE latches R0 at issue),
            # THEN clobber R0 with the W/mu math. xq@R1 is untouched by the math and is
            # forwarded near the end directly from R1.
            assembly_template="""\
start:
    {write:xif}
    MOVE R0, R{data:Wnom}
    ADD R0, R{state:v}
    MOVE R{state:Ws}, R0
    MOVE R0, R{data:nstrobe}
    CMP R{state:cnt}, R{state:Ws}
    BR.GE emit
    MOVE R0, R{state:cnt}
    SHL R0, #1
    BR.NN emit
    MOVE R0, R{data:m7fff}
emit:
    MOVE [LOCK_FACE], R{data:lock_face}
    MOVE [LOCK], R{data:nstrobe}
    {write:muf}
    MOVE R0, R{in:xq}
    {write:xqf}
    MOVE R0, R{state:cnt}
    SUB R0, R{state:Ws}
    AND R0, R{data:m7fff}
    MOVE R{state:cnt}, R0
    {jump:ltrig}
""",
        )

        # ================================================================ land
        # Complex-landing FAN. Receives (mu, xi, xq) from counter on ONE face and
        # fans them to the two rails 1-hop each as PURE DATA: (mu, xq) to qland (Q
        # landing, EAST) and (mu, xi) to iland (I landing, NORTH). Then triggers the
        # TWO PARALLEL rails: qtrig (qland) FIRST, then itrig (iland). Both rails run
        # every sample (their delay lines must shift); the Q rail is triggered first
        # so its slice_q DEPOSITS (sq,dq) at ted BEFORE the I rail's slice_i triggers
        # ted (the shipped-Gardner upstream-parallel-rail ordering — qdelay triggered
        # before resampler, its yq lands before qout fires). mu written to BOTH rails
        # while still in R0 (a WRITE does NOT clobber R0). Resting face EAST (to
        # qland); flip NORTH for the iland fan. face_e address 3, face_n address 4.
        land = CellProgram(
            inputs=[Port("mu", register=0), Port("xi", register=1),
                    Port("xq", register=2)],
            outputs=[Port("muqf"), Port("xqf"), Port("muif"), Port("xif"),
                     Port("qtrig"), Port("itrig")],
            entries=[EntryPoint("default")],
            data=[DataWord("face_e", 1, address=3, is_face=True),
                  DataWord("face_n", 3, address=4, is_face=True)],
            state=[StateVar("mus")],
            # mu arrives at R0 (accumulator) — CAPTURE it into ``mus`` FIRST (the sample
            # loads clobber R0), then fan each rail (mu,x) from the saved value. face is
            # set EXPLICITLY at entry (EAST) — a JUMP/face-flip in a PRIOR sample leaves
            # the runtime face dangling. Fan qland EAST, flip NORTH, fan iland, then
            # RESTORE EAST so the next sample starts clean.
            assembly_template="""\
start:
    MOVE R{state:mus}, R{in:mu}
    MOVE [FACE], R{data:face_e}
    MOVE R0, R{state:mus}
    {write:muqf}
    MOVE R0, R{in:xq}
    {write:xqf}
    {jump:qtrig}
    MOVE [FACE], R{data:face_n}
    MOVE R0, R{state:mus}
    {write:muif}
    MOVE R0, R{in:xi}
    {write:xif}
    {jump:itrig}
    MOVE [FACE], R{data:face_e}
""",
        )

        # ============================================================== qland
        # Q landing (FIRST cell of the thread; owns the Q 4-tap delay line, shifts
        # EVERY sample). Triggered by counter every sample. On a STROBE forwards the
        # 4 Q taps + mu to farrow_q_hi (the Q rail). On a NO-STROBE it triggers iland
        # (which shifts the I line then goes to the loop_filter's nostrobe entry) —
        # so BOTH delay lines shift every sample regardless. mu/(xi taps) for I come
        # from counter directly (iland is fed by counter).
        def _qland():
            return CellProgram(
                inputs=[Port("xq", register=0), Port("mu", register=1)],
                outputs=[Port("t0f"), Port("t1f"), Port("t2f"),
                         Port("t3f"), Port("muf"), Port("hi_trig"),
                         Port("ns_trig")],
                entries=[EntryPoint("default")],
                state=[StateVar("d0", register=2, reset_per_batch=True),
                       StateVar("d1", register=3, reset_per_batch=True),
                       StateVar("d2", register=4, reset_per_batch=True),
                       StateVar("d3", register=5, reset_per_batch=True)],
                assembly_template="""\
start:
    MOVE R{state:d0}, R{state:d1}
    MOVE R{state:d1}, R{state:d2}
    MOVE R{state:d2}, R{state:d3}
    MOVE R{state:d3}, R{in:xq}
    MOVE R0, R{in:mu}
    AND R0, R0
    BR.N nostrobe
    MOVE R0, R{state:d0}
    {write:t0f}
    MOVE R0, R{state:d1}
    {write:t1f}
    MOVE R0, R{state:d2}
    {write:t2f}
    MOVE R0, R{state:d3}
    {write:t3f}
    MOVE R0, R{in:mu}
    {write:muf}
    {jump:hi_trig}
    HALT
nostrobe:
    {jump:ns_trig}
""",
            )

        # ============================================================== iland
        # I landing (MID-thread; owns the I 4-tap delay line, shifts EVERY sample).
        # Triggered on STROBE by slice_q (after the Q rail + reconvergence-feed to
        # ted), and on NO-STROBE by qland. On a STROBE forwards the 4 I taps + mu to
        # farrow_i_hi (the I rail). On a NO-STROBE it triggers the loop_filter's
        # nostrobe entry (the PI still runs every sample). Gates on mu's sign
        # (BR.N on the no-strobe sentinel 0x8000). Fed (xi, mu) by counter directly.
        iland = CellProgram(
            inputs=[Port("xi", register=0), Port("mu", register=1)],
            outputs=[Port("t0f"), Port("t1f"), Port("t2f"), Port("t3f"),
                     Port("muf"), Port("hi_trig"), Port("ns_trig")],
            entries=[EntryPoint("default")],
            state=[StateVar("d0", register=2, reset_per_batch=True),
                   StateVar("d1", register=3, reset_per_batch=True),
                   StateVar("d2", register=4, reset_per_batch=True),
                   StateVar("d3", register=5, reset_per_batch=True)],
            assembly_template="""\
start:
    MOVE R{state:d0}, R{state:d1}
    MOVE R{state:d1}, R{state:d2}
    MOVE R{state:d2}, R{state:d3}
    MOVE R{state:d3}, R{in:xi}
    MOVE R0, R{in:mu}
    AND R0, R0
    BR.N nostrobe
    MOVE R0, R{state:d0}
    {write:t0f}
    MOVE R0, R{state:d1}
    {write:t1f}
    MOVE R0, R{state:d2}
    {write:t2f}
    MOVE R0, R{state:d3}
    {write:t3f}
    MOVE R0, R{in:mu}
    {write:muf}
    {jump:hi_trig}
    HALT
nostrobe:
    {jump:ns_trig}
""",
        )

        # ========================================================= farrow_i_hi
        # v2 = Σ c2·t (saved) ; v3 = Σ c3·t (left in R0) ; A = mq(v3,mu)+v2.
        # (No sat — the A stage never binds.) Forwards A + mu + t1(x0) to
        # farrow_i_lo. c3/c2 share values so only 7 distinct Q13 coeff words are
        # stored. Strobe-path only. Computing v2 FIRST then v3 lets A be formed
        # directly from R0 (=v3) with a single v2 save word (no v3 save).
        def _farrow_hi(name_ns):
            return CellProgram(
                # t0 pinned to R1, NOT R0: R0 is the accumulator. An input at R0
                # races the cell's own R0 usage / the incoming-WRITE ordering, so the
                # oldest tap read stale (verified: farrow read t0=46 where the tap was
                # -95). The error is INVISIBLE at mu=0 (the v3·mu term vanishes) but
                # corrupts every strobe once mu grows — the drift past the first
                # loop-adjusted symbol. All four taps + mu now live off R0.
                inputs=[Port("t0", register=1), Port("t1"), Port("t2"),
                        Port("t3"), Port("mu")],
                outputs=[Port("Af"), Port("muf"), Port("v1f"), Port("t1f"),
                         Port("trig")],
                entries=[EntryPoint("default")],
                # 7 distinct Q13 coeff words shared across the c3 and c2 rows.
                data=[DataWord("cN20480", (-20480) & 0xFFFF, address=2),
                      DataWord("cN12288", (-12288) & 0xFFFF, address=3),
                      DataWord("cN4096", (-4096) & 0xFFFF, address=4),
                      DataWord("cP4096", 4096, address=5),
                      DataWord("cP8192", 8192, address=6),
                      DataWord("cP12288", 12288, address=7),
                      DataWord("cP16384", 16384, address=8)],
                # A = mq(v3,mu)+v2 folded into ONE R0 chain (v3, MULQ mu, MACQ
                # the c2 taps onto R0 = +v2). Forward A + mu + t1 while R0 still
                # holds A, THEN compute v1 = mq(-4096,t0)+mq(4096,t2) (clobbers
                # R0) and forward it. No save word. lo does v0=mq(8192,t1) + the
                # Horner finish + saturating <<2.
                # c3 = [-4096,12288,-12288,4096]; c2 = [8192,-20480,16384,-4096].
                assembly_template="""\
start:
    MULQ R{in:t0}, R{data:cN4096}
    MACQ R{in:t1}, R{data:cP12288}
    MACQ R{in:t2}, R{data:cN12288}
    MACQ R{in:t3}, R{data:cP4096}
    MULQ R0, R{in:mu}
    MACQ R{in:t0}, R{data:cP8192}
    MACQ R{in:t1}, R{data:cN20480}
    MACQ R{in:t2}, R{data:cP16384}
    MACQ R{in:t3}, R{data:cN4096}
    {write:Af}
    MOVE R0, R{in:mu}
    {write:muf}
    MOVE R0, R{in:t1}
    {write:t1f}
    MULQ R{in:t0}, R{data:cN4096}
    MACQ R{in:t2}, R{data:cP4096}
    {write:v1f}
    {jump:trig}
""",
            )

        # ========================================================= farrow_i_lo
        # B = mq(A,mu)+v1 ; v0 = mq(8192,t1) ; C = mq(B,mu)+v0 ; si = SAT(C<<2).
        # v1 (from the landing cell) and t1(x0) arrive as inputs. The ONLY
        # saturation that binds is the final <<2 (verified 0 binds on the 3
        # intermediates). Emits si. bias(=8192) shares the c8192 coeff word.
        def _farrow_lo():
            return CellProgram(
                inputs=[Port("A", register=0), Port("mu"), Port("v1"),
                        Port("t1")],
                outputs=[Port("sif"), Port("trig")],
                entries=[EntryPoint("default")],
                data=[DataWord("cP8192", C0Q, address=2),
                      DataWord("satpos", 0x7FFF, address=3)],
                state=[StateVar("Bacc"), StateVar("v0s"),
                       StateVar("acc_save")],
                # B = mq(A,mu)+v1 (v1 from hi); v0 = mq(8192,t1);
                # C = mq(B,mu)+v0; si = SAT(C<<2). The final <<2 is the ONLY
                # binding saturation (verified). bias(=8192) == the cP8192 word.
                assembly_template="""\
start:
    MOVE R0, R{in:A}
    MULQ R0, R{in:mu}
    ADD R0, R{in:v1}
    MOVE R{state:Bacc}, R0
    MULQ R{in:t1}, R{data:cP8192}
    MOVE R{state:v0s}, R0
    MOVE R0, R{state:Bacc}
    MULQ R0, R{in:mu}
    ADD R0, R{state:v0s}
    MOVE R{state:acc_save}, R0
    ADD R{state:acc_save}, R{data:cP8192}
    SHR R0, #14
    BR.NZ satlo
    SHL R{state:acc_save}, #2
    GOTO emit
satlo:
    SHR R{state:acc_save}, #15
    ADD R0, R{data:satpos}
emit:
    {write:sif}
    {jump:trig}
""",
            )

        # ============================================================= slice_i
        # Branchless 4-PAM slice of si -> di (levels ±1,±3 /√10). LAST cell of the
        # linear thread's compute: writes its (si, di) pair as PURE DATA 1-hop to the
        # ADJACENT ted (the reconvergence point) and TRIGGERS ted. Because the
        # trigger arrives from THIS (the last) rail, sq/dq (written earlier by
        # slice_q) are already in place at ted — reconvergence is ordered by the
        # chain (the shipped complex-Gardner qout idiom). thr == (p3 - p1) so the ADD
        # reuses thr (one fewer data word).
        def _slice_i():
            return CellProgram(
                inputs=[Port("s", register=0)],
                outputs=[Port("sf"), Port("df"), Port("trig")],
                entries=[EntryPoint("default")],
                data=[DataWord("p1", p1, address=2),
                      DataWord("thr", thr, address=3),
                      DataWord("neg1", 0x8000, address=4)],
                state=[StateVar("ss"), StateVar("mag")],
                # |si| -> level magnitude; sign applied via mq(x,0x8000) = -x. Emit
                # (di, si) as DATA to ted, then JUMP ted (the trigger). Both egress
                # on the resting fwd_face toward ted (1-hop, same neighbour) — safe.
                # ABS-OVERFLOW guard (``AND R0,R0; BR.N big``): negating s=-32768 via
                # mq(s,0x8000) OVERFLOWS int16 back to -32768 (reads NEGATIVE), so the
                # ``CMP |s|,thr`` would wrongly pick p1. The reference's ``-ys`` yields
                # 32768 (>= thr) -> p3. A saturated Farrow sample (s=-32768, common on
                # the outer 16-QAM levels at some timing offsets) MUST slice to the
                # OUTER level; else the decision, the M&M error, and the whole loop
                # trajectory diverge. When the abs reads negative, jump straight to the
                # p3 (``big``) magnitude.
                assembly_template="""\
start:
    MOVE R{state:ss}, R{in:s}
    MOVE R0, R{state:ss}
    AND R0, R0
    BR.NN pos
    MULQ R0, R{data:neg1}
    AND R0, R0
    BR.N big
pos:
    CMP R0, R{data:thr}
    MOVE R0, R{data:p1}
    BR.LT small
big:
    MOVE R0, R{data:p1}
    ADD R0, R{data:thr}
small:
    MOVE R{state:mag}, R0
    MOVE R0, R{state:ss}
    AND R0, R0
    BR.NN emit
    MOVE R0, R{state:mag}
    MULQ R0, R{data:neg1}
    MOVE R{state:mag}, R0
emit:
    MOVE R0, R{state:mag}
    {write:df}
    MOVE R0, R{state:ss}
    {write:sf}
    {jump:trig}
""",
            )

        # ============================================================= slice_q
        # 4-PAM slice of sq -> dq (levels ±1,±3 /√10). Tail of the PARALLEL Q rail:
        # writes its (sq, dq) pair as PURE DATA 1-hop to the ADJACENT ted (they wait
        # there for slice_i's trigger), then HALTs (no trigger). ted is fired later by
        # slice_i (the MAIN I rail), by which time sq/dq are already in place (the
        # shipped-Gardner upstream-parallel-rail ordering — the Q rail is triggered
        # first). (sq,dq) egress on the resting fwd_face toward ted. thr == (p3-p1) so
        # the ADD reuses thr.
        def _slice_q():
            return CellProgram(
                inputs=[Port("s", register=0)],
                outputs=[Port("sqf"), Port("dqf")],
                entries=[EntryPoint("default")],
                data=[DataWord("p1", p1, address=2),
                      DataWord("thr", thr, address=3),
                      DataWord("neg1", 0x8000, address=4)],
                state=[StateVar("ss"), StateVar("mag")],
                assembly_template="""\
start:
    MOVE R{state:ss}, R{in:s}
    MOVE R0, R{state:ss}
    AND R0, R0
    BR.NN pos
    MULQ R0, R{data:neg1}
    AND R0, R0
    BR.N big
pos:
    CMP R0, R{data:thr}
    MOVE R0, R{data:p1}
    BR.LT small
big:
    MOVE R0, R{data:p1}
    ADD R0, R{data:thr}
small:
    MOVE R{state:mag}, R0
    MOVE R0, R{state:ss}
    AND R0, R0
    BR.NN emit
    MOVE R0, R{state:mag}
    MULQ R0, R{data:neg1}
    MOVE R{state:mag}, R0
emit:
    MOVE R0, R{state:ss}
    {write:sqf}
    MOVE R0, R{state:mag}
    {write:dqf}
    HALT
""",
            )

        qland = _qland()

        # =========================================================== ted (M&M)
        # Merged both-rail decision-directed TED. Receives si,di (slice_i) and
        # sq,dq (slice_q). The esign=-1 is folded into the SUBTRACTION ORDER (no
        # negate): e = (mq(di,lxi)-mq(api,si)) + (mq(dq,lxq)-mq(apq,sq)).
        # Carries the previous symbol's samples (lxi,lxq) and decisions
        # (api,apq). Each input feeds exactly ONE MULQ so it is read directly (no
        # copy-to-state needed). NOTE the update of lxi/api reads si/di AFTER the
        # error MULQs, so si/di are captured into sic/dic first (the loop-memory
        # update must see THIS symbol's values, and R0-live inputs are consumed).
        # Emits e -> loop_filter. Strobe-path only (triggered then).
        ted = CellProgram(
            inputs=[Port("si", register=1), Port("di", register=2),
                    Port("sq", register=3), Port("dq", register=4)],
            outputs=[Port("ef"), Port("yif"), Port("yqf"), Port("trig")],
            entries=[EntryPoint("default")],
            # Inputs pinned to R1-R4 (NOT R0): R0 is the accumulator, clobbered by the
            # first MULQ — an input at R0 would be destroyed before use. State above.
            state=[StateVar("lxi", initial_value=0, register=5,
                            reset_per_batch=True),
                   StateVar("lxq", initial_value=0, register=6,
                            reset_per_batch=True),
                   StateVar("api", initial_value=0, register=7,
                            reset_per_batch=True),
                   StateVar("apq", initial_value=0, register=8,
                            reset_per_batch=True),
                   StateVar("es", register=9)],
            # es = mq(di,lxi)-mq(api,si) + mq(dq,lxq)-mq(apq,sq). si/sq (@R0/R2)
            # are captured into lxi/lxq LAST (after the MULQ that reads them),
            # then re-emitted as yi/yq from lxi/lxq. di/dq feed one MULQ each.
            # ALL THREE outputs (e, yi, yq) go to loop_filter (EAST, 1-hop) which
            # runs the PI, forwards (yi,yq) to qout, and triggers qout on a strobe —
            # the shipped-Gardner ted->loop_filter->qout linear thread (yi rides
            # THROUGH loop_filter to the egress cell). ted triggers loop_filter's
            # STROBE entry. Order the reads so each input feeds only one MULQ.
            assembly_template="""\
start:
    MULQ R{in:di}, R{state:lxi}
    MSUQ R{in:si}, R{state:api}
    MOVE R{state:es}, R0
    MULQ R{in:dq}, R{state:lxq}
    MSUQ R{in:sq}, R{state:apq}
    ADD R0, R{state:es}
    MOVE R{state:es}, R0
    MOVE R{state:api}, R{in:di}
    MOVE R{state:apq}, R{in:dq}
    MOVE R{state:lxi}, R{in:si}
    MOVE R{state:lxq}, R{in:sq}
    MOVE R0, R{state:es}
    {write:ef}
    MOVE R0, R{state:lxi}
    {write:yif}
    MOVE R0, R{state:lxq}
    {write:yqf}
    {jump:trig}
""",
        )

        # ========================================================= loop_filter
        # PI: vp = mq(e,K1i); vi += mq(e,K2i); v = vp+vi. Runs EVERY sample (on
        # no-strobe e=0 so vp=0, vi unchanged, v=vi). It is the linear thread's
        # penultimate cell: it forwards the recovered (yi,yq) pair THROUGH to qout
        # (SOUTH) + triggers qout on a strobe, and forwards v -> period_relay (EAST)
        # for the PI feedback closure (the shipped-Gardner loop_filter->qout +
        # loop_filter->period_relay pattern). TWO ENTRIES encode the strobe:
        # ``strobe`` (from ted, egresses the symbol) and ``nostrobe`` (from iland, e
        # forced 0, no qout). Both run the PI + feedback. ``stb`` records which entry
        # so the qout trigger fires only on a strobe. On nostrobe yi/yq are stale (not
        # egressed). DUAL-FACE: face_out=SOUTH (yi/yq -> qout), face_fb=EAST
        # (v -> period_relay); the two rails are PERPENDICULAR.
        loop_filter = CellProgram(
            inputs=[Port("e_in", register=0), Port("yi"), Port("yq")],
            outputs=[Port("yif"), Port("yqf"), Port("vf"),
                     Port("fb_trig"), Port("otrig")],
            entries=[EntryPoint("strobe"), EntryPoint("nostrobe")],
            data=[DataWord("K1i", K1i, address=2),
                  DataWord("K2i", K2i, address=3),
                  DataWord("zero", 0, address=4),
                  DataWord("face_out", self._FACE_OUT, address=5, is_face=True),
                  DataWord("face_fb", self._FACE_FB, address=6, is_face=True)],
            state=[StateVar("vi", reset_per_batch=True), StateVar("es")],
            # STROBE entry: first flip SOUTH and forward (yi,yq) -> qout + trigger
            # qout (the recovered symbol is independent of the PI, so egress it up
            # front; yi/yq arrive in input regs). Then capture e, run the PI, and
            # feed v back EAST -> period_relay. NOSTROBE entry: skip the egress, force
            # e=0 (v=vi), still run the PI + feedback (the loop tracks every sample).
            # v = vp + vi where vp = mq(e,K1), vi += mq(e,K2): integral first
            # (vi += mq(e,K2), saved), then proportional (vp in R0) + ADD vi. Resting
            # face left EAST (the feedback face) so the tracer finds period_relay.
            assembly_template="""\
strobe:
    MOVE R{state:es}, R{in:e_in}
    MOVE [FACE], R{data:face_out}
    MOVE R0, R{in:yi}
    {write:yif}
    MOVE R0, R{in:yq}
    {write:yqf}
    {jump:otrig}
    GOTO pi
nostrobe:
    MOVE R{state:es}, R{data:zero}
pi:
    MOVE R0, R{state:es}
    MULQ R0, R{data:K2i}
    ADD R0, R{state:vi}
    MOVE R{state:vi}, R0
    MOVE R0, R{state:es}
    MULQ R0, R{data:K1i}
    ADD R0, R{state:vi}
    MOVE [FACE], R{data:face_fb}
    {write:vf}
    {jump:fb_trig}
""",
        )

        # ========================================================= period_relay
        # Pure-data feedback closure: writes v BACKWARD into counter.v (no
        # trigger — read by the counter on its next sample, the Costas/period
        # model). One word forwarded.
        #
        # SERIALIZE-LOCK release (INV-19): after writing v back into counter.v (the
        # {write:pout} data feedback), CLEAR the counter's arbiter LOCK with a backward
        # ``WRITE.CFG @N, 4`` (R0=0 -> counter CONFIG[4]=LOCK), releasing the input
        # sample HELD since the last strobe. ``pout`` AND the WRITE.CFG both travel the
        # SAME period_relay->counter return corridor; the authored @1 hop is re-patched
        # to the real resolved corridor hop by build._apply_internal_feedback (it patches
        # the pout feedback WRITE hop AND this WRITE.CFG hop TOGETHER — the same idiom the
        # shipped Gardner period_relay + the Costas pd_pi use). ``lzero`` provides R0=0
        # for the config clear. UNCONDITIONAL (the lock is a correctness requirement); this
        # cell has ample budget — 6 words used of 32.
        period_relay = CellProgram(
            inputs=[Port("v_in", register=0)],
            outputs=[Port("pout")],
            entries=[EntryPoint("relay")],
            data=[DataWord("lzero", 0, address=2)],
            # vs @R1 so it does not alias R0 (accumulator) in this dataless cell.
            state=[StateVar("vs", register=1)],
            assembly_template="""\
relay:
    MOVE R{state:vs}, R{in:v_in}
    MOVE R0, R{state:vs}
    {write:pout}
    MOVE R0, R{data:lzero}
    WRITE.CFG @1, 4
""",
        )

        # ================================================================ qout
        # Single external output. Emits yi (si from slice_i) then yq (sq from
        # slice_q) with ONE trigger — the complex-output contract (yi@R0, yq@R1).
        qout = CellProgram(
            inputs=[Port("yi", register=0), Port("yq", register=1)],
            outputs=[Port("yi_e"), Port("yq_e"), Port("trig")],
            entries=[EntryPoint("default")],
            assembly_template="""\
start:
    MOVE R0, R{in:yi}
    {write:yi_e}
    MOVE R0, R{in:yq}
    {write:yq_e}
    {jump:trig}
""",
        )

        # DICT ORDER == the single linear trigger thread (positional-next default
        # trigger falls through the chain): counter -> qland -> Q rail -> slice_q ->
        # iland -> I rail -> slice_i -> ted -> loop_filter -> qout, then the
        # period_relay feedback cell last.
        return {
            "counter": counter,
            "land": land,
            "qland": qland,
            "farrow_q_hi": _farrow_hi("q"),
            "farrow_q_lo": _farrow_lo(),
            "slice_q": _slice_q(),
            "iland": iland,
            "farrow_i_hi": _farrow_hi("i"),
            "farrow_i_lo": _farrow_lo(),
            "slice_i": _slice_i(),
            "ted": ted,
            "loop_filter": loop_filter,
            "qout": qout,
            "period_relay": period_relay,
        }

    def internal_connections(self) -> List[Tuple[Any, str, Any, str]]:
        # SINGLE LINEAR THREAD (counter -> qland -> Q rail -> slice_q -> iland ->
        # I rail -> slice_i -> ted -> loop_filter -> qout). The NCO `counter` (the
        # complex landing) fans (mu,xi)->iland (EAST) and (mu,xq)->qland (SOUTH)
        # 1-hop each — NO `land` splitter. Every internal DATA WRITE is 1-hop; ted is
        # ADJACENT to BOTH slices (opposite faces); (yi,yq) ride ted->loop_filter->
        # qout.
        return [
            # counter -> land fan (mu, xi, xq), one face (EAST).
            ("counter", "muf", "land", "mu"),
            ("counter", "xif", "land", "xi"),
            ("counter", "xqf", "land", "xq"),
            # land fans to the two landing cells 1-hop each (pure DATA).
            ("land", "xif", "iland", "xi"),
            ("land", "muif", "iland", "mu"),
            ("land", "xqf", "qland", "xq"),
            ("land", "muqf", "qland", "mu"),
            # Q rail: qland -> farrow_q_hi -> farrow_q_lo -> slice_q (all 1-hop).
            ("qland", "t0f", "farrow_q_hi", "t0"),
            ("qland", "t1f", "farrow_q_hi", "t1"),
            ("qland", "t2f", "farrow_q_hi", "t2"),
            ("qland", "t3f", "farrow_q_hi", "t3"),
            ("qland", "muf", "farrow_q_hi", "mu"),
            ("farrow_q_hi", "Af", "farrow_q_lo", "A"),
            ("farrow_q_hi", "muf", "farrow_q_lo", "mu"),
            ("farrow_q_hi", "v1f", "farrow_q_lo", "v1"),
            ("farrow_q_hi", "t1f", "farrow_q_lo", "t1"),
            ("farrow_q_lo", "sif", "slice_q", "s"),
            # slice_q -> ted (sq, dq) 1-hop; they wait for slice_i's trigger.
            ("slice_q", "sqf", "ted", "sq"),
            ("slice_q", "dqf", "ted", "dq"),
            # I rail: iland -> farrow_i_hi -> farrow_i_lo -> slice_i (all 1-hop).
            ("iland", "t0f", "farrow_i_hi", "t0"),
            ("iland", "t1f", "farrow_i_hi", "t1"),
            ("iland", "t2f", "farrow_i_hi", "t2"),
            ("iland", "t3f", "farrow_i_hi", "t3"),
            ("iland", "muf", "farrow_i_hi", "mu"),
            ("farrow_i_hi", "Af", "farrow_i_lo", "A"),
            ("farrow_i_hi", "muf", "farrow_i_lo", "mu"),
            ("farrow_i_hi", "v1f", "farrow_i_lo", "v1"),
            ("farrow_i_hi", "t1f", "farrow_i_lo", "t1"),
            ("farrow_i_lo", "sif", "slice_i", "s"),
            # slice_i -> ted (si, di) 1-hop; slice_i's trigger reconverges ted.
            ("slice_i", "sf", "ted", "si"),
            ("slice_i", "df", "ted", "di"),
            # ted -> loop_filter (error e + the recovered yi/yq pair, all 1-hop EAST).
            ("ted", "ef", "loop_filter", "e_in"),
            ("ted", "yif", "loop_filter", "yi"),
            ("ted", "yqf", "loop_filter", "yq"),
            # loop_filter forwards (yi,yq) -> qout (EAST) + v -> period_relay (SOUTH).
            ("loop_filter", "yif", "qout", "yi"),
            ("loop_filter", "yqf", "qout", "yq"),
            ("loop_filter", "vf", "period_relay", "v_in"),
            # The fb_trig JUMP is ALSO declared as a connection so its ENTRY resolves
            # to period_relay's ``relay`` entry (28), NOT the positional-next cell
            # qout (entry 26). Without this, _find_output_target falls through to the
            # positional default (loop_filter's next cell = qout) and stamps qout's
            # entry onto fb_trig, so period_relay enters at addr 26 (two empty words
            # that HALT before the real ``relay:`` body at 28) and its pout WRITE
            # never fires — counter.v stays 0, the loop never converges. This mirrors
            # the shipped complex Gardner (which declares its fb_trig as a connection
            # too, gardner_timing_recovery.py ~line 710).
            ("loop_filter", "fb_trig", "period_relay", "relay"),
            # PI feedback: period_relay -> counter.v (backward, pure data).
            ("period_relay", "pout", "counter", "v"),
        ]

    def internal_jumps(self) -> List[Tuple[Any, str, Any, str]]:
        # SINGLE LINEAR TRIGGER THREAD — each cell TRIGGERS exactly ONE next cell.
        return [
            # counter triggers the land fan every sample; land fans data to both
            # rails then triggers BOTH: qtrig (qland, Q rail) FIRST, then itrig
            # (iland, I rail) — the Q rail leads so slice_q deposits (sq,dq) at ted
            # before slice_i fires ted (upstream-parallel-rail ordering).
            ("counter", "ltrig", "land", "default"),
            ("land", "qtrig", "qland", "default"),
            ("land", "itrig", "iland", "default"),
            # qland: strobe -> farrow_q_hi (Q rail); no-strobe -> loop_filter's
            # nostrobe entry (the PI still runs every sample; the Q rail owns the
            # nostrobe path since it is triggered first).
            ("qland", "hi_trig", "farrow_q_hi", "default"),
            ("qland", "ns_trig", "loop_filter", "nostrobe"),
            ("farrow_q_hi", "trig", "farrow_q_lo", "default"),
            ("farrow_q_lo", "trig", "slice_q", "default"),
            # slice_q has NO trigger — it just deposits (sq,dq) at ted and HALTs.
            # iland: strobe -> farrow_i_hi (I rail); no-strobe -> terminate (the
            # nostrobe PI already ran via qland; iland's nostrobe just shifts the I
            # delay line and stops).
            ("iland", "hi_trig", "farrow_i_hi", "default"),
            ("iland", "ns_trig", "__terminate__", "default"),
            ("farrow_i_hi", "trig", "farrow_i_lo", "default"),
            ("farrow_i_lo", "trig", "slice_i", "default"),
            # slice_i triggers ted (the reconvergence — sq,dq are already in place).
            ("slice_i", "trig", "ted", "default"),
            # ted triggers the loop_filter's STROBE entry.
            ("ted", "trig", "loop_filter", "strobe"),
            # loop_filter closes feedback (fb_trig) + egresses on strobe (otrig).
            ("loop_filter", "fb_trig", "period_relay", "relay"),
            ("loop_filter", "otrig", "qout", "default"),
        ]

    def default_layout(self) -> Dict[Any, Tuple[int, int, str]]:
        # RING FOLD (mirrors the shipped complex Gardner's compact fold + declared
        # feedback transit corridor). The forward chain snakes RIGHT along rows 0/1,
        # reconverges at ted, drops into loop_filter/period_relay on the right, and the
        # PI feedback returns LEFT along row 2 through a chain of FACE-ONLY
        # ``transit_fb_*`` cells (the stable faces the router NEVER overrides) back
        # into ``counter`` — so ``_apply_internal_feedback`` can TRACE the backward
        # period_relay->counter edge along the authored transit faces (the whole point:
        # the old flat layout had period_relay ~7 diagonal hops from counter with NO
        # transit, so the trace dead-ended and the feedback WRITE was left un-patched;
        # period_relay never ran, counter.v stayed 0, the loop never converged).
        #
        #   col:    0        1        2        3        4        5        6
        #  row0:  iland    fi_hi    fi_lo    slice_i  ted      loop_flt  qout
        #  row1:  land     qland    fq_hi    fq_lo    slice_q  period_r
        #  row2:  counter  <-t_fb4  <-t_fb3  <-t_fb2  <-t_fb1  <-t_fb0
        #
        # FORWARD (every internal DATA write 1-hop, INV-8/9/14 ≤8 across):
        #   counter(0,2) --N--> land(0,1); land fans (mu,xi)--N-->iland(0,0) +
        #     (mu,xq)--E-->qland(1,1).
        #   I RAIL row0: iland(0,0)->fi_hi(1,0)->fi_lo(2,0)->slice_i(3,0)->ted(4,0).
        #   Q RAIL row1: qland(1,1)->fq_hi(2,1)->fq_lo(3,1)->slice_q(4,1).
        #   RECONVERGENCE ted(4,0): slice_i(3,0) --E--> ted (si,di)+trigger;
        #     slice_q(4,1) --N--> ted (sq,dq) [deposited first, HALT]. ted abuts both.
        #   ted(4,0) --E--> loop_filter(5,0). loop_filter DUAL-FACE: (yi,yq) --E-->
        #     qout(6,0) [_FACE_OUT=east] + v --S--> period_relay(5,1) [_FACE_FB=south].
        # FEEDBACK RETURN CORRIDOR (period_relay -> counter, pure DATA, backward):
        #   period_relay(5,1) rests SOUTH into transit_fb_0(5,2); the transits relay
        #   WEST along row 2: t_fb0(5,2)->t_fb1(4,2)->t_fb2(3,2)->t_fb3(2,2)->
        #   t_fb4(1,2)->counter(0,2). ``_apply_internal_feedback`` traces this path
        #   (@6) and patches period_relay's ``pout`` WRITE to that hop into counter.v.
        # DICT ORDER MUST MATCH build_cell_programs() key order: the router/placer
        # index ``pb.cells[cell_pos]`` positionally against the cell_programs dict, so
        # a default_layout in a DIFFERENT order silently mis-resolves every internal
        # handoff (iland->farrow_i_hi lands 2 cells downstream). Positions/faces are
        # the RING fold above; only the emission ORDER matches the program dict.
        return {
            "counter": (0, 2, "north"),
            "land": (0, 1, "east"),
            "qland": (1, 1, "east"),
            "farrow_q_hi": (2, 1, "east"),
            "farrow_q_lo": (3, 1, "east"),
            "slice_q": (4, 1, "north"),
            "iland": (0, 0, "east"),
            "farrow_i_hi": (1, 0, "east"),
            "farrow_i_lo": (2, 0, "east"),
            "slice_i": (3, 0, "east"),
            "ted": (4, 0, "east"),
            "loop_filter": (5, 0, "south"),
            "qout": (6, 0, "south"),
            "period_relay": (5, 1, "south"),
            # FACE-ONLY feedback return corridor (period_relay -> counter). The corridor
            # relays WEST along row 2, then BENDS DOWN into row 3 and comes UP into the
            # counter from BELOW so the feedback (the pout data write AND the co-located
            # serialize-LOCK WRITE.CFG) enters counter on its SOUTH face at IDENTITY. The
            # counter WRITES its LOCK_FACE explicitly from an is_face DataWord (=SOUTH here)
            # so it D4-transforms with the block — the lock gates the correct feedback face
            # in ALL 8 orientations (it does NOT rely on SOUTH being the CONFIG reset
            # default). The counter's INPUT arrives on a DIFFERENT face (WEST, from the
            # x16_in corridor) and its forward output triggers `land` to the NORTH, so
            # gate-all-but-lock_face holds the next input while still admitting the unlock.
            # Each transit rests toward the NEXT cell so the feedback tracer walks the
            # lane: period_relay(5,1) --S--> t0(5,2); t0..t3 relay WEST along row 2;
            # t4(1,2) --S--> t5(1,3); t5(1,3) --W--> t6(0,3); t6(0,3) --N--> counter(0,2).
            "transit_fb_0": (5, 2, "west"),
            "transit_fb_1": (4, 2, "west"),
            "transit_fb_2": (3, 2, "west"),
            "transit_fb_3": (2, 2, "west"),
            "transit_fb_4": (1, 2, "south"),
            "transit_fb_5": (1, 3, "west"),
            "transit_fb_6": (0, 3, "north"),
        }

    def output_cell_id(self) -> Any:
        return "qout"

    def output_face_addr(self) -> Any:
        return None

    # ------------------------------------------------------------ reference
    def process_reference(self, input_samples: np.ndarray) -> np.ndarray:
        """Bit-exact Q15/wide-fixed reference (identical to
        verification/kyttar/tests/proto_mm_authoritative.mm_q15, itself verified
        against GR symbol_sync_cc(M&M) to grid-distance parity). ``input_samples`` is a
        complex (or (N,2) real Q15) 2-sps stream ALREADY gain-staged so the outer
        constellation level is ~0.949. Returns the recovered (yi, yq) center pairs as
        (N_sym, 2) int16."""
        def s16(v):
            v = int(v) & 0xFFFF
            return v - 0x10000 if v & 0x8000 else v

        def sat(v):
            return max(-32768, min(32767, int(v)))

        def mq(a, b):
            return (s16(a) * s16(b)) >> 15

        N = 1.0 / math.sqrt(10.0)
        p1 = int(round(1 * N * 32767)); p3 = int(round(3 * N * 32767))
        thr = int(round(2 * N * 32767))

        def slice_pam(y):
            ys = s16(y); av = -ys if ys < 0 else ys
            mag = p1 + ((p3 - p1) if av >= thr else 0)
            return -mag if ys < 0 else mag

        c3 = [int(round(c * 32768 / 4)) for c in self._C3]
        c2 = [int(round(c * 32768 / 4)) for c in self._C2]
        c1 = [int(round(c * 32768 / 4)) for c in self._C1]
        c0 = [int(round(c * 32768 / 4)) for c in self._C0]

        def farrow(xm1, x0, x1, x2, mu):
            x = (xm1, x0, x1, x2)
            v3 = sum(mq(c, xx) for c, xx in zip(c3, x))
            v2 = sum(mq(c, xx) for c, xx in zip(c2, x))
            v1 = sum(mq(c, xx) for c, xx in zip(c1, x))
            v0 = sum(mq(c, xx) for c, xx in zip(c0, x))
            acc = sat(mq(v3, mu) + v2)
            acc = sat(mq(acc, mu) + v1)
            acc = sat(mq(acc, mu) + v0)
            return sat(acc << 2)

        arr = np.asarray(input_samples)
        if np.iscomplexobj(arr):
            xi = [float_to_q15(float(c.real)) for c in arr]
            xq = [float_to_q15(float(c.imag)) for c in arr]
        elif arr.ndim == 2 and arr.shape[1] == 2:
            xi = [int(a) & 0xFFFF for a, _ in arr]
            xq = [int(b) & 0xFFFF for _, b in arr]
        else:
            xi = [float_to_q15(float(v)) for v in arr]
            xq = [0] * len(xi)

        ONE = self._ONE
        Wnom = ONE // self._sps            # nominal half-period, Q15 (16384 at sps=2)
        K1i, K2i = self._K1i, self._K2i
        cnt = 0; vi = 0; v = 0
        dl_i = [0] * 8; dl_q = [0] * 8
        out = []
        lxi = lxq = 0; api = apq = 0
        for n in range(len(xi)):
            dl_i.append(s16(xi[n])); dl_i.pop(0)
            dl_q.append(s16(xq[n])); dl_q.pop(0)
            W = Wnom + v
            strobe = cnt < W
            e = 0
            if strobe:
                mu = min(32767, cnt << 1)          # mu = cnt/W ≈ 2·cnt (W≈0.5); one SHL
                si = farrow(dl_i[-4], dl_i[-3], dl_i[-2], dl_i[-1], mu)
                sq = farrow(dl_q[-4], dl_q[-3], dl_q[-2], dl_q[-1], mu)
                di = slice_pam(si); dq = slice_pam(sq)
                e = -1 * ((mq(api, si) - mq(di, lxi)) + (mq(apq, sq) - mq(dq, lxq)))
                out.append((si & 0xFFFF, sq & 0xFFFF))
                lxi, lxq = si, sq; api, apq = di, dq
            vp = mq(e, K1i)                          # proportional (Q15 MULQ)
            vi = vi + mq(e, K2i)                     # integral (Q15 MULQ, accumulate)
            v = vp + vi
            cnt = (cnt - W) % ONE
        return np.array([(s16(a), s16(b)) for a, b in out], dtype=np.int16)

    def reset(self):
        pass
