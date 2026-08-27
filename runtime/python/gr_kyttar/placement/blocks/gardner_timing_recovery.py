# SPDX-License-Identifier: GPL-3.0-or-later
"""GardnerTimingRecovery — see :class:`GardnerTimingRecovery`."""
import math
import numpy as np
from ..block import CellProgram, Port, EntryPoint, StateVar, DataWord
from typing import Dict, List, Tuple, Any
from ._base import KyttarBlock, BlockInterface, float_to_q15


class GardnerTimingRecovery(KyttarBlock):
    """
    Gardner symbol-timing recovery — a drop-in for GNU Radio's
    ``digital.symbol_sync_cc`` with ``TED_GARDNER`` on the industry-standard
    receiver channel (RRC-shaped TX, fractional timing offset, RRC matched filter,
    Nyquist 2 samples/symbol).

    The loop is the canonical Rice "Digital Communications: A Discrete-Time
    Approach" Ch.8 structure — the SAME interpolator-control skeleton
    :class:`MMTimingRecoveryBlock` uses, with Gardner's non-decision-directed TED
    in place of M&M's decision-directed one:

      1. A **modulo-1 interpolator-control counter**. Per input sample the counter
         decrements by ``W = 1/L + v`` (L = 2 sps, ``v`` = loop-filter output);
         when it underflows (``cnt < W``) a STROBE fires, ONE per SYMBOL. The
         counter is ``cnt = (cnt - W) & 0x7FFF`` — bounded BY CONSTRUCTION, so no
         accumulator wrap is possible.
      2. On a strobe the fractional interval ``mu = cnt/W ~= cnt<<1`` (Rice Eq.
         8.89 at W ~= 0.5) drives **two linear interpolations off one 3-tap delay
         line**: the symbol CENTER ``c`` (between ``x[n-1]`` and ``x[n]``) and the
         MID-symbol sample ``m`` (between ``x[n-2]`` and ``x[n-1]`` — one input
         sample earlier is exactly half a symbol at 2 sps), both at the SAME mu.
      3. The **Gardner TED**: ``e = m * (c - c_prev)`` — non-decision-directed, so
         unlike M&M it needs no constellation and no slicer, at the cost of a
         shallower, self-noisier S-curve.
      4. A **2nd-order PI loop filter** (GR ``control_loop`` gains derived from
         ``loop_bw`` + ``damping``) whose output ``v`` adjusts ``W``, clamped to
         GR's ``max_deviation``.

    THE ONE STROBE PER SYMBOL POINT IS LOAD-BEARING. Gardner is often described as
    a "2 samples/symbol" detector, which invites a resampler that strobes TWICE per
    symbol and alternates a center/mid parity tag — what the 2026-07 design did and
    what both prior attempts kept. That structure ties the mid sample's phase to a
    SEPARATE strobe whose own timing the loop is still moving, so the TED's two
    operands come from different loop states and the S-curve is not well defined.
    Interpolating BOTH operands from ONE strobe at ONE mu fixes that.

    Measured on the verification channel, both forms otherwise identical (same
    modulo-1 counter, same full-precision MULQ TED, same GR gain derivation), and
    the two-strobe form given its correct strobe rate and swept over
    loop_bw x ted_scale x max_deviation: the two-strobe form's BEST result is
    **12 of 50 selection cases failing** — it reaches BER 0 on some offsets and
    0.03-0.4 on others, never 0 across the sweep. The one-strobe form is **0 of 50**,
    and 0 of 200 held out. That gap is the whole reason this block ships.

    Interface: a real 2-sps input stream; the output is the recovered symbol-rate
    (center) sample stream (slice its sign for BPSK bits). With ``complex=True``
    the block lands an (xi, xq) pair and emits the recovered (yi, yq) center pair:
    the I rail alone drives the timing loop, and the Q rail is interpolated at the
    IDENTICAL strobe and mu, so a downstream QPSK slicer can decode.

    SATURATION- AND ORIENTATION-SAFE. The ``counter`` (the NCO landing cell) LOCKs
    its input arbiter to the feedback face on EVERY sample, so the next input is
    HELD until the ``period_relay`` closes the loop and clears the lock (INV-19):
    one sample fully traverses the interior before the next is admitted. The
    LOCK_FACE is written explicitly from an ``is_face`` DataWord so it D4-transforms
    with the block in all 8 orientations (INV-23).

    OPERATING ENVELOPE (a measured limit, stated as a limit — not a claim of
    universality). The Gardner TED is NON-decision-directed, so its error is a
    product of two SIGNAL samples and its S-curve slope therefore scales with the
    SQUARE of the input level: unlike a decision-directed detector, the effective
    loop gain moves with the drive amplitude. Measured over 5 seeds x a 10-point
    offset grid per cell of the sweep:

      * peak amplitude **0.5-0.75**: 0/50 failures. At 0.4 and below the S-curve
        is too shallow to acquire within the burst (1/50 at 0.4, 7/50 at 0.2); at
        0.8 and above the TED difference ``c - c_prev`` saturates often enough to
        bias the detector (1/50 at 0.8, 25/50 at 0.9).
      * RRC rolloff **beta >= 0.35**: 0/50 failures, up to beta 0.9. Below that
        the pulse is too narrow for the mid-symbol sample to carry timing
        information (4/50 at beta 0.3, 8/50 at 0.25).
      * burst length **150-2500 symbols**: 0/50 at every length.

    COLD-ACQUISITION TRANSIENT: **up to 6 symbols.** The loop starts at the nominal
    period with ``v = 0`` and has to pull in the actual timing offset, so the first
    few recovered symbols can be wrong; from symbol 6 onward the measured BER is 0
    on every case of the sweep. Any BER gate over a burst must skip that transient
    (this suite uses 80, and GR's own output needs a transient skip too). This is a
    REAL behaviour change against the pre-2026-08-27 block, which ran its resampler
    OPEN-LOOP at the nominal period and therefore had no acquisition transient —
    and also could not track a timing offset at all, which is why it was
    quarantined. A downstream gate that counted every symbol from zero and passed
    before may now see a handful of errors at the head of the burst.

    Drive this block at ~0.7 peak — what a normalising AGC or the shipped RX chain
    produces, and where the verification channel sits. This is the same
    operating-point discipline MMTimingRecoveryBlock needs, for the same reason.

    ``loop_bw`` CEILING: the proportional gain is a Q15 MULQ multiplier and so
    cannot exceed 32767. With the x8 TED-scale normalisation folded in, that ceiling
    is reached at ``loop_bw`` ~ 0.022, above which the gain CLAMPS and the requested
    bandwidth is not actually delivered. The default 0.02 sits just inside. A wider
    loop would need the TED scale moved into a shift rather than the multiplier.

    NOT A UNIVERSAL TIMING BLOCK. Gardner is a BPSK/QPSK detector: its S-curve is
    derived assuming a two-level eye, and it does NOT lock a 4-level (4-PAM /
    16-QAM) signal — the documented limit that blocked the M17 4FSK modem. For
    multilevel constellations use MMTimingRecoveryBlock, which is decision-directed.
    """
    CATEGORY = "recovery"
    TAGS = ["gardner", "timing_recovery", "ted", "symbol_sync", "recovery"]
    # INV-22: every class param must be settable from GRC, or whitelisted here.
    # ``kp``/``ki`` are DEPRECATED and IGNORED (kept only so saved flowgraphs that
    # set them still load — the loop gains derive from loop_bw/damping), and
    # ``pipeline_lock`` is a correctness requirement rather than a user choice:
    # turning it off makes the block wrong under saturated drive, so it is not a
    # knob to expose (the same call ComplexCostasLoopBlock/AGCCCBlock make).
    GRC_UNSUPPORTED_PARAMS = ("kp", "ki", "pipeline_lock")

    # --- GR control_loop PI gains. ``loop_bw``/``damping`` map to alpha/beta by
    # GR's own 2nd-order ``control_loop`` mapping (identical to
    # MMTimingRecoveryBlock._pi_gains); K1 = alpha/2, K2 = beta/2 folds GR's
    # ted_gain normalisation k0 = 2/ted_gain at ted_gain = 1.
    #
    # TED_SCALE: Gardner's S-curve slope on this channel is ~1/8 of the
    # decision-directed M&M slope the raw mapping assumes (the TED multiplies two
    # SIGNAL samples rather than a decision by a sample, so its gain carries an
    # extra factor of the signal amplitude, and a non-decision-directed detector
    # loses a further factor to its self-noise). Re-normalising by 8 restores GR's
    # intended closed-loop bandwidth. Measured on the matched-filter channel:
    # at scale 1/2/4 the loop fails to ACQUIRE within the burst on 24-50 of the 50
    # selection cases (BER up to ~0.45 on those, i.e. never locked); at 8 it is
    # 0/50 and 0/200 held out.
    _LOOP_BW = 0.02
    _DAMPING = 1.0
    _TED_SCALE = 8.0
    _ONE = 1 << 15                # counter full-scale (Q15)
    # GR ``max_deviation`` on the loop output, in the counter's Q15 units. It is
    # BOTH the deviation clamp and the integrator anti-windup. 8192 = 0.25 of a
    # full symbol period. Measured: without it 2 of 200 held-out cases slip
    # (a self-noise excursion walks the integrator out); with it, 0.
    _MAXDEV = 8192

    # Real (BPSK) mode: one 2-sps sample lands at R0, one recovered center out.
    # COMPLEX mode overrides this with the xi/xq + yi/yq pair interface.
    _interface = BlockInterface(
        entry_address=1, input_registers=[0], output_registers=[0]
    )

    # THE TOPOLOGY THAT MAKES THE LOOP CLOSE ON SILICON (INV-44). The block's
    # EXTERNAL-EGRESS cell and the source of its INTERNAL FEEDBACK must be
    # DIFFERENT cells. Four independent build passes each claim an exit cell's
    # WRITE/JUMP words —
    # ``output_at_last_write`` (single-WRITE egress contract), ``_apply_routes``
    # (rewrites every WRITE in a routed exit cell to the output corridor),
    # ``_apply_internal_feedback`` (re-patches the highest-address JUMP of a
    # feedback source) and the ``feedback_blocks`` preserve-set — and a cell asked
    # to be both loses one role to the other. So ``loop_filter`` fans out: the
    # recovered symbol goes to a DEDICATED single-WRITE ``qout`` egress cell on one
    # face, and ``v`` goes to ``period_relay`` on a PERPENDICULAR face.
    # ``period_relay`` is ordered LAST in the program dict so its edge back into
    # ``counter`` is the block's only BACKWARD connection.
    _CELL_IDS = ["counter", "dline", "interp", "ted", "loop_filter", "qout",
                 "period_relay"]

    # Face codes: S=0, E=1, W=2, N=3. These are DERIVED FROM ``default_layout``,
    # not chosen: _FACE_OUT is the direction from ``loop_filter`` to ``qout``,
    # _FACE_FB the direction from ``loop_filter`` to ``period_relay``, and
    # _FACE_LOCK the face the feedback ENTERS ``counter`` on. INV-37: a baked
    # ``is_face`` constant PINS the fold, so keep these and the layout in step —
    # ``test_faces_match_the_layout`` asserts exactly that.
    #
    # _FACE_LOCK is a DIFFERENT thing from _FACE_FB (the face the feedback LEAVES
    # the loop_filter on), and conflating the two is a real trap: the counter's
    # arbiter LOCK gates every face except _FACE_LOCK, so if it names the wrong
    # one the lock never clears and the block emits exactly ONE symbol and goes
    # quiescent. Measured — that is precisely what a mismatched pair produces.
    _FACE_OUT = 0   # south (real fold: loop_filter(1,1) -> qout(1,2))
    _FACE_FB = 2    # west  (real fold: loop_filter(1,1) -> period_relay(0,1))
    _FACE_LOCK = 0  # south (real fold: period_relay(0,1) sits SOUTH of
    #                 counter(0,0), so the feedback ARRIVES on its south face)
    _CFACE_OUT = 1  # east  (complex fold: loop_filter(5,0) -> qout(6,0))
    _CFACE_FB = 0   # south (complex fold: loop_filter(5,0) -> period_relay(5,1))
    _CFACE_LOCK = 0  # south (complex fold: transit(0,1) -> counter(0,0))

    def __init__(self, name: str, loop_bw: float = None, damping: float = None,
                 complex: bool = False, kp: int = 3, ki: int = 1,
                 pipeline_lock: bool = True):
        """
        Args:
            name: Block name.
            loop_bw: GR ``symbol_sync`` control-loop bandwidth (default 0.02).
            damping: GR ``symbol_sync`` control-loop damping factor (default 1.0).
            complex: When True, 2-rail (I/Q) timing recovery — the SAME I-driven
                Gardner loop (counter, interpolation, TED, PI and feedback all
                identical and driven by the I rail only) plus a parallel Q rail
                interpolated at the IDENTICAL strobe and ``mu``. The block then
                lands an (xi, xq) pair and emits the recovered (yi, yq) symbol-center
                pair, so a downstream QPSKSlicer can decode QPSK.
            kp, ki: DEPRECATED and IGNORED. The loop filter derives its gains from
                ``loop_bw`` + ``damping`` through GR's ``control_loop`` mapping.
                Accepted for backward compatibility only.
            pipeline_lock: When True (default) the timing loop is SERIALIZED under
                saturated drive (INV-19): the ``counter`` LOCKs its arbiter to the
                feedback face so the next input sample is HELD until the
                ``period_relay`` closes the loop and CLEARS the lock via a backward
                ``WRITE.CFG``. Without it, under back-to-back drive the counter
                strobes again before the PI's corrected period has fed back, using a
                STALE ``v`` — the loop decouples and the recovered symbols drift
                from the per-sample reference.
        """
        super().__init__(name, loop_bw=loop_bw, damping=damping)
        self._kp = int(kp)
        self._ki = int(ki)
        self._pipeline_lock = bool(pipeline_lock)
        self._complex = bool(complex)
        self._loop_bw = self._LOOP_BW if loop_bw is None else float(loop_bw)
        self._damping = self._DAMPING if damping is None else float(damping)
        self._K1i, self._K2i = self._pi_gains()
        if self._complex:
            self._FACE_OUT = self._CFACE_OUT
            self._FACE_FB = self._CFACE_FB
            self._FACE_LOCK = self._CFACE_LOCK
            self._interface = BlockInterface(
                entry_address=1, input_registers=[0, 1], output_registers=[0, 1])

    def _pi_gains(self) -> Tuple[int, int]:
        """GR ``control_loop`` alpha/beta -> the Q15 MULQ multipliers K1/K2.

        Identical mapping to ``MMTimingRecoveryBlock._pi_gains``, with the Gardner
        S-curve re-normalisation ``_TED_SCALE`` folded in (see the class comment)."""
        sps = 2
        th = 2 * math.pi * self._loop_bw / sps
        dn = 1 + 2 * self._damping * th + th * th
        al = (4 * self._damping * th) / dn
        be = (4 * th * th) / dn
        K1 = al / 2.0 * self._TED_SCALE
        K2 = be / 2.0 * self._TED_SCALE
        return (max(1, min(32767, int(round(K1 * self._ONE)))),
                max(1, min(32767, int(round(K2 * self._ONE)))))

    @property
    def cell_count(self) -> int:
        # counter, dline, interp, ted, loop_filter, qout, period_relay.
        # COMPLEX adds a qinterp cell (the Q rail's delay line + interpolation).
        return 8 if self._complex else 7

    @property
    def interface(self) -> BlockInterface:
        return self._interface

    # ------------------------------------------------------------ cell programs
    def build_cell_programs(self) -> Dict[str, CellProgram]:
        ONE = self._ONE                      # 32768
        Wnom = ONE // 2                      # 16384 (nominal half-sample period)
        M7FFF = 0x7FFF                       # mu clamp AND the (cnt-W) mod mask
        K1i, K2i = self._K1i, self._K2i
        MD = self._MAXDEV

        # ============================================================== counter
        # The modulo-1 interpolator-control NCO (Rice Ch.8), and nothing else:
        #   W      = Wnom + v            (v = PI output, fed back as pure data)
        #   strobe = cnt < W
        #   mu     = min(0x7FFF, cnt<<1) (== cnt/W at W ~= 0.5; the ISA has no divide)
        #   cnt    = (cnt - W) & 0x7FFF  (== (cnt-W) % ONE — bounded by construction)
        #
        # ``cnt`` is bounded BY CONSTRUCTION by that AND-mask. This is the whole
        # correction over the 2026-07 design, whose plain int16 phase accumulator
        # had NO modulo: whenever the loop pulled the period below nominal the
        # accumulator gained more per sample than each strobe shed, grew without
        # bound and WRAPPED (measured reaching 32298), after which the derived
        # fraction read NEGATIVE and INVERTED the interpolation. The loop was not
        # jittering, it was SLIPPING — which is why its BER sat in the 0.04-0.12
        # band, far too good for a broken detector and far too bad for a working
        # one.
        #
        # NO-STROBE SENTINEL: mu = 0x8001. bit15 tags no-strobe (``interp`` gates
        # on mu's SIGN) and bit0 = 1 ALSO engages the arbiter LOCK (``MOVE [LOCK]``
        # reads bit0) — reused as the lock-enable word, no extra data slot. The
        # clamp guarantees a real strobe's mu is in [0, 0x7FFF] so bit15 is CLEAR
        # and the sentinel is unambiguous. Clamp via the SHL's N flag, NOT a signed
        # CMP: when cnt >= 0x4000 the shifted result reads NEGATIVE, and a
        # ``CMP R0,0x7FFF; BR.LT`` would KEEP it — colliding with the sentinel.
        #
        # SERIALIZE-LOCK (INV-19), when ``pipeline_lock``: LOCK the arbiter to the
        # FEEDBACK face on EVERY sample. The PI and the period_relay run on every
        # sample (strobe via ted, no-strobe via interp's ns_trig), so the loop
        # closes and UNLOCKS every sample regardless of the strobe.
        # ORIENTATION-SAFE (INV-23): LOCK_FACE is written from an ``is_face``
        # DataWord so build._apply_orientation_face_words D4-transforms it.
        lock_tail = ("""\
    MOVE [LOCK_FACE], R{data:lock_face}
    MOVE [LOCK], R{data:nstrobe}
""" if self._pipeline_lock else "")
        counter = CellProgram(
            inputs=[Port("xi", register=0)],
            outputs=[Port("muf"), Port("xif"), Port("trig")],
            entries=[EntryPoint("default")],
            data=[DataWord("Wnom", Wnom, address=1),
                  DataWord("m7fff", M7FFF, address=2),
                  DataWord("nstrobe", 0x8001, address=3),
                  DataWord("lock_face", self._FACE_LOCK, address=4, is_face=True)],
            # LOOP MEMORY (reset_per_batch): the NCO count and the PI output ``v``
            # — the feedback target. A fresh packet must start cold or it inherits
            # the previous packet's timing lock. ``Ws`` is per-sample scratch
            # (written before read).
            state=[StateVar("cnt", register=5, reset_per_batch=True),
                   StateVar("v", register=6, reset_per_batch=True),
                   StateVar("Ws", register=7)],
            # xi arrives at R0 (the pinned input reg) but R0 is also the NCO
            # accumulator, so FORWARD xi to ``dline`` FIRST (a WRITE latches R0 at
            # issue time), THEN clobber R0 with the W/mu math.
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
""" + lock_tail + """\
    {write:muf}
    MOVE R0, R{state:cnt}
    SUB R0, R{state:Ws}
    AND R0, R{data:m7fff}
    MOVE R{state:cnt}, R0
    {jump:trig}
""",
        )

        # =============================================================== dline
        # The 3-tap delay line. It runs on EVERY input sample (that is why it
        # cannot live in ``interp``, which runs on STROBES only) and forwards the
        # three taps plus ``mu`` to ``interp``:
        #     d0 = x[n-2]   d1 = x[n-1]   d2 = x[n]
        # The taps are forwarded BEFORE the shift, so the values ``interp``
        # receives are the ones this sample's interpolations need.
        dline = CellProgram(
            inputs=[Port("xi", register=1), Port("mu", register=2)],
            outputs=[Port("d0f"), Port("d1f"), Port("d2f"), Port("muf"),
                     Port("trig")],
            entries=[EntryPoint("default")],
            # INV-33: a cell with NO data words has max_data_address = -1, so its
            # auto-allocated state lands ON TOP of R0 and the inputs. State is
            # pinned explicitly here, and a single zero word keeps the allocator's
            # floor above R0 regardless.
            data=[DataWord("zero", 0, address=3)],
            state=[StateVar("d0", register=4, reset_per_batch=True),
                   StateVar("d1", register=5, reset_per_batch=True)],
            assembly_template="""\
start:
    MOVE R0, R{state:d0}
    {write:d0f}
    MOVE R0, R{state:d1}
    {write:d1f}
    MOVE R0, R{in:xi}
    {write:d2f}
    MOVE R0, R{in:mu}
    {write:muf}
    MOVE R{state:d0}, R{state:d1}
    MOVE R{state:d1}, R{in:xi}
    {jump:trig}
""",
        )

        # =============================================================== interp
        # The TWO linear interpolations, both at the SAME mu off the SAME 3-tap
        # window:
        #     c = d1 + MULQ(mu, d2 - d1)      the symbol CENTER
        #     m = d0 + MULQ(mu, d1 - d0)      the MID-symbol sample (one input
        #                                     sample earlier == half a symbol at
        #                                     2 sps)
        #
        # DERIVING BOTH TED OPERANDS FROM ONE STROBE AT ONE MU IS THE DESIGN. The
        # obvious alternative — a resampler that strobes TWICE per symbol and tags
        # each strobe center/mid — ties the mid sample to a SEPARATE strobe whose
        # own timing the loop is still moving, so the detector's two operands come
        # from different loop states and the S-curve is not well-defined. Measured
        # on this channel with everything else held equal and the two-strobe form
        # swept over its gains: its BEST is 12/50 selection cases failing; this form
        # is 0/50.
        #
        # NO SATURATION HERE, and that is a MEASURED claim, not an optimisation:
        # over 70 x 900-symbol bursts spanning the whole documented amplitude
        # envelope, neither interpolation difference nor either interpolation
        # result EVER leaves int16 (0 binds); only the TED's ``c - c_prev`` does
        # (4 binds at the verification amplitude, 2744 at amp 0.85). Results are
        # bit-identical with the clamps present or absent. The clamp therefore
        # lives in ``ted`` alone, where it earns its words.
        #
        # Gates on mu's SIGN (BR.N on the 0x8001 no-strobe sentinel): on a
        # no-strobe it triggers the loop_filter's ``nostrobe`` entry so the PI, the
        # feedback and the lock-clear still run on EVERY sample.
        #
        # THE ``HALT`` AFTER ``{jump:trig}`` IS LOAD-BEARING (INV-43). A remote JUMP does
        # NOT stop local execution — it kicks the target cell and the issuing
        # thread keeps running into the next word. Without the HALT the STROBE path
        # falls straight through into ``nostrobe:`` and fires ``ns_trig`` as well,
        # so the loop_filter runs BOTH its entries on every strobe and the
        # ``nostrobe`` one — which arrives second — overwrites the captured error
        # with zero. The visible symptom is subtle and misleading: the integrator
        # still tracks the reference bit for bit (it was updated by the strobe
        # entry first) while ``v`` comes out exactly equal to ``vi`` on every
        # sample, i.e. the PROPORTIONAL term silently disappears and the
        # second-order loop degrades to a pure integrator. It looks like an
        # arithmetic bug in the PI and is really a control-flow one.
        interp = CellProgram(
            inputs=[Port("mu", register=1), Port("d0", register=2),
                    Port("d1", register=3), Port("d2", register=4)],
            outputs=[Port("cf"), Port("mf"), Port("trig"), Port("ns_trig")],
            entries=[EntryPoint("default")],
            data=[DataWord("zero", 0, address=5)],
            state=[StateVar("acc", register=6)],
            assembly_template="""\
start:
    MOVE R0, R{in:mu}
    AND R0, R0
    BR.N nostrobe
    SUB R{in:d2}, R{in:d1}
    MULQ R0, R{in:mu}
    ADD R0, R{in:d1}
    MOVE R{state:acc}, R0
    SUB R{in:d1}, R{in:d0}
    MULQ R0, R{in:mu}
    ADD R0, R{in:d0}
    {write:mf}
    MOVE R0, R{state:acc}
    {write:cf}
    {jump:trig}
    HALT
nostrobe:
    {jump:ns_trig}
""",
        )

        # ================================================================== ted
        # The Gardner timing-error detector, NON-decision-directed:
        #     e = MULQ(m, SAT(c - c_prev))
        # ``c`` is also passed through as the recovered symbol.
        #
        # FULL-PRECISION MULQ. A Q15 signal*signal product is ALREADY in [-1,1),
        # so pre-halving BOTH operands to "make room" — the 2026-07 design's
        # ``MULHI(mid, (s>>1) - (cprev>>1))`` — pays exactly 2 bits of error
        # amplitude for headroom the product never needed. Only the DIFFERENCE
        # ``c - c_prev`` can leave int16, so saturate JUST that. Those 2 bits are
        # what let the loop acquire cold within the burst.
        #
        # THE SATURATION'S SIGN POLARITY IS INVERTED, AND THAT IS CORRECT. It reads
        # the SUB's own flags: overflow sets V, and on overflow the WRAPPED result's
        # sign bit is the OPPOSITE of the true sign (a difference below -32768 wraps
        # round to a POSITIVE word, and vice versa). So the branch that follows the
        # V test must clamp to 0x8000 when the wrapped result reads POSITIVE and to
        # 0x7FFF when it reads NEGATIVE — the reverse of what it looks like it
        # should do. Getting this backwards clamps the TED error to the WRONG RAIL:
        # measured, on a burst where ``c - c_prev`` overflowed negative, the chip
        # produced +32767 where the reference had -32768, the loop was kicked the
        # wrong way, and it shed two strobes over the rest of the burst. Note also
        # that MOVE does not touch the flags, so the second branch is still reading
        # N from the SUB — which is exactly what makes this two-instruction form
        # work.
        ted = CellProgram(
            inputs=[Port("c", register=1), Port("m", register=2)],
            outputs=[Port("ef"), Port("yf"), Port("trig")],
            entries=[EntryPoint("default")],
            data=[DataWord("satpos", 0x7FFF, address=3),
                  DataWord("satneg", 0x8000, address=4)],
            state=[StateVar("cprev", register=5, reset_per_batch=True),
                   StateVar("dif", register=6)],
            assembly_template="""\
start:
    SUB R{in:c}, R{state:cprev}
    BR.NV dok
    MOVE R0, R{data:satneg}
    BR.NN dok
    MOVE R0, R{data:satpos}
dok:
    MOVE R{state:dif}, R0
    MOVE R0, R{in:m}
    MULQ R0, R{state:dif}
    {write:ef}
    MOVE R{state:cprev}, R{in:c}
    MOVE R0, R{in:c}
    {write:yf}
    {jump:trig}
""",
        )

        # ========================================================== loop_filter
        # The 2nd-order PI, run on EVERY sample (on a no-strobe e = 0, so the
        # proportional term vanishes and the integrator holds):
        #     vi = vi + MULQ(e, K2)      integral
        #     v  = MULQ(e, K1) + vi      + proportional
        # GR's ``max_deviation`` clamp on BOTH terms lives downstream in
        # ``period_relay`` — see there for why that split is not arbitrary.
        #
        # THIS IS THE SPLIT-ROLE CELL, and the split is the entire point (see
        # ``_CELL_IDS``): it forwards the recovered symbol ``y`` to the DEDICATED
        # ``qout`` egress cell on _FACE_OUT and ``v`` to ``period_relay`` on the
        # PERPENDICULAR _FACE_FB. Neither of THOSE cells carries the other's role,
        # so no single cell has to satisfy the external-egress patch passes and the
        # internal-feedback patch pass at once. TWO ENTRIES encode the strobe:
        # ``strobe`` (from ted, egresses the symbol) and ``nostrobe`` (from interp,
        # e forced 0, no egress); both run the PI and close the feedback.
        loop_filter = CellProgram(
            inputs=[Port("e_in", register=0), Port("y", register=1)],
            outputs=[Port("yf"), Port("vf"), Port("fb_trig"), Port("otrig")],
            entries=[EntryPoint("strobe"), EntryPoint("nostrobe")],
            data=[DataWord("K1i", K1i, address=2),
                  DataWord("K2i", K2i, address=3),
                  DataWord("zero", 0, address=4),
                  DataWord("face_out", self._FACE_OUT, address=5, is_face=True),
                  DataWord("face_fb", self._FACE_FB, address=6, is_face=True)],
            state=[StateVar("vi", register=7, reset_per_batch=True),
                   StateVar("es", register=8)],
            # STROBE entry: capture e, flip to _FACE_OUT, forward y to qout and
            # trigger it (the recovered symbol is independent of the PI, so egress
            # it up front), then FALL THROUGH into the PI and feed v back on
            # _FACE_FB. NOSTROBE entry: force e = 0, then BRANCH OVER the egress
            # into the same PI. The resting face is left at _FACE_FB so the
            # build's feedback tracer follows it to period_relay.
            #
            # NOSTROBE IS ORDERED FIRST AND BRANCHES FORWARD; the strobe path
            # FALLS THROUGH. Do NOT write this the other way round with a ``GOTO
            # pi`` skipping the nostrobe body: a GOTO assembles to an opcode-0x7
            # word, i.e. a LOCAL (@0) JUMP, and a JUMP does not redirect local
            # execution — it QUEUES a re-entry at the target while the current
            # thread keeps running into the next word. The strobe path would then
            # fall into ``MOVE es, zero`` and lose the error it had just captured,
            # so ``v`` would come out equal to ``vi`` on EVERY sample: the
            # proportional term silently vanishes and the loop degrades to a pure
            # integrator. That was measured here (chip ``v`` == chip ``vi``
            # exactly, at every sample, while ``vi`` itself matched the reference
            # bit for bit) — a failure that looks like an arithmetic bug and is
            # really a control-flow one. ``CMP Rz, Rz`` sets Z unconditionally
            # (MOVE does not touch the flags, so the compare must be explicit).
            assembly_template="""\
nostrobe:
    MOVE R{state:es}, R{data:zero}
    CMP R{data:zero}, R{data:zero}
    BR.Z pi
strobe:
    MOVE R{state:es}, R{in:e_in}
    MOVE [FACE], R{data:face_out}
    MOVE R0, R{in:y}
    {write:yf}
    {jump:otrig}
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

        # ================================================================= qout
        # The block's SINGLE external output cell: exactly ONE WRITE, ONE JUMP,
        # ONE face, no state, no feedback. Making the egress contract
        # single-valued BY CONSTRUCTION is what lets ``output_at_last_write`` and
        # ``_apply_routes`` patch it unambiguously — there is nothing else in the
        # cell for them to clobber, and nothing in it that another pass wants.
        qout = CellProgram(
            inputs=[Port("y", register=0)],
            outputs=[Port("out"), Port("trig")],
            entries=[EntryPoint("default")],
            assembly_template="""\
start:
    MOVE R0, R{in:y}
    {write:out}
    {jump:trig}
""",
        )

        # ========================================================= period_relay
        # GR's ``max_deviation`` clamp, then the pure-data feedback closure: write
        # the clamped ``v`` BACKWARD into ``counter.v`` (no trigger — the counter
        # reads it on its next sample, the Costas-dphase model).
        #
        # WHY THE CLAMP IS HERE. It is GR's ``max_deviation``, and it doubles as
        # the integrator anti-windup, so it belongs to the loop filter
        # mathematically. It is EVALUATED here for a mundane and load-bearing
        # reason: ``loop_filter`` is the block's only dual-face cell and its word
        # budget is spent on the two entries, the two face flips and the two
        # perpendicular emits, while this relay uses 8 of 32 words. Clamping ``v``
        # on the way into ``counter.v`` is arithmetically identical to clamping it
        # on the way out of the PI — ``v`` is read nowhere else — and the
        # integrator sees the clamp on the NEXT sample through the same state.
        # Measured: without the clamp, 2 of 200 held-out cases slip when a
        # self-noise excursion walks the loop out; with it, 0.
        #
        # SERIALIZE-LOCK release (INV-19): after the data write, CLEAR the
        # counter's arbiter LOCK with a backward ``WRITE.CFG @N, 4``, releasing the
        # input sample held since this sample's landing. ``pout`` and the WRITE.CFG
        # travel the SAME return corridor; the authored @1 is a placeholder that
        # ``_apply_internal_feedback`` re-patches to the resolved corridor hop for
        # BOTH together.
        relay_tail = ("""\
    MOVE R0, R{data:lzero}
    WRITE.CFG @1, 4
""" if self._pipeline_lock else "")
        period_relay = CellProgram(
            inputs=[Port("v_in", register=0)],
            outputs=[Port("pout")],
            entries=[EntryPoint("relay")],
            data=[DataWord("lzero", 0, address=1),
                  DataWord("pdev", MD, address=2),
                  DataWord("ndev", (-MD) & 0xFFFF, address=3)],
            # ``vs`` pinned off R0 (the accumulator) in this small cell.
            state=[StateVar("vs", register=4)],
            assembly_template="""\
relay:
    MOVE R{state:vs}, R{in:v_in}
    CMP R{state:vs}, R{data:pdev}
    BR.LT vhi
    MOVE R{state:vs}, R{data:pdev}
vhi:
    CMP R{state:vs}, R{data:ndev}
    BR.GE vlo
    MOVE R{state:vs}, R{data:ndev}
vlo:
    MOVE R0, R{state:vs}
    {write:pout}
""" + relay_tail,
        )

        if self._complex:
            return self._complex_cell_programs(counter, dline, interp, ted,
                                               loop_filter, period_relay)

        # DICT ORDER == the single linear trigger thread, with period_relay LAST
        # so its edge into counter is the block's ONLY BACKWARD connection.
        return {"counter": counter, "dline": dline, "interp": interp,
                "ted": ted, "loop_filter": loop_filter, "qout": qout,
                "period_relay": period_relay}

    # ------------------------------------------------- COMPLEX (2-rail) variant
    def _complex_cell_programs(self, counter, dline, interp, ted, loop_filter,
                               period_relay) -> Dict[str, CellProgram]:
        """The 8-cell COMPLEX (I/Q) loop.

        The TIMING LOOP is the real block VERBATIM — same counter, same delay
        line, same interpolations, same TED, same PI, same feedback — driven by
        the I rail alone. Two cells add the Q rail without touching it:

          * ``qinterp`` owns the Q delay line and interpolates the Q CENTER at the
            IDENTICAL strobe and ``mu`` the I rail uses. Sharing one strobe by
            construction (rather than running a duplicate NCO, as the 2026-07
            complex path did) is what guarantees the Q sample lands at the same
            instant as the I center — a second NCO can only stay in step if every
            feedback update reaches both, which is one more thing to get wrong.
          * ``qout`` becomes a two-WRITE pair emitter (yi -> R0, yq -> R1, one
            trigger), the ComplexCostasLoop / matched-filter complex-output
            contract a downstream QPSKSlicer consumes. It is STILL the block's
            single dedicated egress cell carrying no feedback — the split that
            makes the loop close is untouched.

        ``dline`` gains an xq pass-through so ``qinterp`` sees the same sample the
        I rail does, on the same schedule.
        """
        K1i, K2i = self._K1i, self._K2i

        # --- counter (complex): forwards xq alongside xi to the delay line.
        ccounter = CellProgram(
            inputs=[Port("xi", register=0), Port("xq", register=1)],
            outputs=[Port("muf"), Port("xif"), Port("xqf"), Port("trig")],
            entries=[EntryPoint("default")],
            data=[DataWord("Wnom", self._ONE // 2, address=2),
                  DataWord("m7fff", 0x7FFF, address=3),
                  DataWord("nstrobe", 0x8001, address=4)],
            state=[StateVar("cnt", register=5, reset_per_batch=True),
                   StateVar("v", register=6, reset_per_batch=True),
                   StateVar("Ws", register=7)],
            # No serialize-LOCK on the complex path: every rendezvous is ordered by
            # the single linear thread and the complex harness drives per-sample.
            # xi (live in R0 at entry) and xq are forwarded BEFORE the W/mu math
            # clobbers R0.
            assembly_template="""\
start:
    {write:xif}
    MOVE R0, R{in:xq}
    {write:xqf}
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
    {write:muf}
    MOVE R0, R{state:cnt}
    SUB R0, R{state:Ws}
    AND R0, R{data:m7fff}
    MOVE R{state:cnt}, R0
    {jump:trig}
""",
        )

        # --- dline (complex): the I delay line as before, plus a pass-through of
        # the live xq to ``interp`` (which relays it on to the Q interpolator).
        cdline = CellProgram(
            inputs=[Port("xi", register=1), Port("mu", register=2),
                    Port("xq", register=3)],
            outputs=[Port("d0f"), Port("d1f"), Port("d2f"), Port("muf"),
                     Port("xqf"), Port("trig")],
            entries=[EntryPoint("default")],
            data=[DataWord("zero", 0, address=4)],
            state=[StateVar("d0", register=5, reset_per_batch=True),
                   StateVar("d1", register=6, reset_per_batch=True)],
            assembly_template="""\
start:
    MOVE R0, R{state:d0}
    {write:d0f}
    MOVE R0, R{state:d1}
    {write:d1f}
    MOVE R0, R{in:xi}
    {write:d2f}
    MOVE R0, R{in:mu}
    {write:muf}
    MOVE R0, R{in:xq}
    {write:xqf}
    MOVE R{state:d0}, R{state:d1}
    MOVE R{state:d1}, R{in:xi}
    {jump:trig}
""",
        )

        # --- interp (complex): the two I interpolations exactly as in the real
        # block, plus a relay of (mu, xq) one hop on to ``qinterp``.
        #
        # THE Q RAIL RIDES THE FORWARD THREAD, it does not shortcut to the egress.
        # An earlier shape here had a ``qinterp`` sitting off to the side writing
        # ``yq`` STRAIGHT to ``qout`` several cells away. It places, routes, builds
        # and runs — and the Q channel comes out ALL ZEROS, because an internal data
        # WRITE is delivered along the chain of ABUTTING forward faces and the
        # programmed cells in between are not transits: they do not relay it. The I
        # rail stayed bit-exact throughout, which makes the failure read like a
        # Q-rail arithmetic bug rather than the topology bug it is. So every
        # internal handoff here is 1 hop to an abutting cell and ``yq`` is passed
        # hand to hand (qinterp -> ted -> loop_filter -> qout), exactly the way
        # MMTimingRecoveryBlock walks its own recovered pair down to its egress.
        cinterp = CellProgram(
            inputs=[Port("mu", register=1), Port("d0", register=2),
                    Port("d1", register=3), Port("d2", register=4),
                    Port("xq", register=5)],
            outputs=[Port("cf"), Port("mf"), Port("qmuf"), Port("qxf"),
                     Port("trig"), Port("ns_trig")],
            entries=[EntryPoint("default")],
            data=[DataWord("zero", 0, address=6)],
            state=[StateVar("acc", register=7)],
            # The (mu, xq) relay happens BEFORE the strobe gate: qinterp owns the Q
            # delay line and must shift it on EVERY sample, strobe or not, exactly
            # as dline does for the I rail.
            assembly_template="""\
start:
    MOVE R0, R{in:mu}
    {write:qmuf}
    MOVE R0, R{in:xq}
    {write:qxf}
    MOVE R0, R{in:mu}
    AND R0, R0
    BR.N nostrobe
    SUB R{in:d2}, R{in:d1}
    MULQ R0, R{in:mu}
    ADD R0, R{in:d1}
    MOVE R{state:acc}, R0
    SUB R{in:d1}, R{in:d0}
    MULQ R0, R{in:mu}
    ADD R0, R{in:d0}
    {write:mf}
    MOVE R0, R{state:acc}
    {write:cf}
    {jump:trig}
    HALT
nostrobe:
    {jump:ns_trig}
""",
        )

        # --- qinterp: the Q delay line + ONE interpolation at the SHARED mu.
        # Runs on EVERY sample (the delay line must shift). On a strobe it writes
        # ``yq`` 1 hop to ``ted``, which carries it on down the thread. It uses the
        # SAME (q1, xq) pair and the SAME mu the I rail's centre uses, so both rails
        # are interpolated at ONE instant — the reason this replaced the 2026-07
        # duplicate-NCO Q rail, which could only stay in step if every feedback
        # update reached both NCOs identically.
        qinterp = CellProgram(
            inputs=[Port("mu", register=1), Port("xq", register=2),
                    Port("c", register=3), Port("m", register=4)],
            outputs=[Port("yqf"), Port("cf"), Port("mf"), Port("trig"),
                     Port("ns_trig")],
            entries=[EntryPoint("strobe"), EntryPoint("nostrobe")],
            data=[DataWord("zero", 0, address=5)],
            state=[StateVar("q1", register=6, reset_per_batch=True)],
            # SITS IN THE THREAD, between interp and ted, and RELAYS the I rail's
            # (c, m) through as well as producing yq. That relay is not decoration:
            # a data WRITE is delivered along abutting forward faces, so interp
            # cannot hand (c, m) "over" an intervening programmed cell to ted. A
            # cell placed in the middle of a linear thread must forward everything
            # the thread carries.
            #
            # TWO ENTRIES so the Q delay line shifts on EVERY sample: ``strobe``
            # interpolates and relays, ``nostrobe`` only shifts and passes the
            # no-strobe trigger on to the loop_filter. As in interp, the HALT after
            # ``{jump:trig}`` is required — a remote JUMP does not stop local
            # execution.
            assembly_template="""\
strobe:
    SUB R{in:xq}, R{state:q1}
    MULQ R0, R{in:mu}
    ADD R0, R{state:q1}
    {write:yqf}
    MOVE R{state:q1}, R{in:xq}
    MOVE R0, R{in:m}
    {write:mf}
    MOVE R0, R{in:c}
    {write:cf}
    {jump:trig}
    HALT
nostrobe:
    MOVE R{state:q1}, R{in:xq}
    {jump:ns_trig}
""",
        )

        # --- ted (complex): the real TED plus a hand-off of ``yq`` to loop_filter.
        cted = CellProgram(
            inputs=[Port("c", register=1), Port("m", register=2),
                    Port("yq", register=3)],
            outputs=[Port("ef"), Port("yf"), Port("yqf"), Port("trig")],
            entries=[EntryPoint("default")],
            data=[DataWord("satpos", 0x7FFF, address=4),
                  DataWord("satneg", 0x8000, address=5)],
            state=[StateVar("cprev", register=6, reset_per_batch=True),
                   StateVar("dif", register=7)],
            assembly_template="""\
start:
    SUB R{in:c}, R{state:cprev}
    BR.NV dok
    MOVE R0, R{data:satneg}
    BR.NN dok
    MOVE R0, R{data:satpos}
dok:
    MOVE R{state:dif}, R0
    MOVE R0, R{in:m}
    MULQ R0, R{state:dif}
    {write:ef}
    MOVE R{state:cprev}, R{in:c}
    MOVE R0, R{in:yq}
    {write:yqf}
    MOVE R0, R{in:c}
    {write:yf}
    {jump:trig}
""",
        )

        # --- loop_filter (complex): the real PI plus the ``yq`` hand-off to qout.
        # Still the SPLIT-ROLE cell: (yi, yq) on _FACE_OUT to the dedicated egress,
        # ``v`` on the perpendicular _FACE_FB to the relay. See the real-mode
        # comment for why the nostrobe entry is ordered FIRST and branches forward.
        cloop = CellProgram(
            inputs=[Port("e_in", register=0), Port("y", register=1),
                    Port("yq", register=2)],
            outputs=[Port("yf"), Port("yqf"), Port("vf"), Port("fb_trig"),
                     Port("otrig")],
            entries=[EntryPoint("strobe"), EntryPoint("nostrobe")],
            data=[DataWord("K1i", K1i, address=3),
                  DataWord("K2i", K2i, address=4),
                  DataWord("zero", 0, address=5),
                  DataWord("face_out", self._FACE_OUT, address=6, is_face=True),
                  DataWord("face_fb", self._FACE_FB, address=7, is_face=True)],
            state=[StateVar("vi", register=8, reset_per_batch=True),
                   StateVar("es", register=9)],
            assembly_template="""\
nostrobe:
    MOVE R{state:es}, R{data:zero}
    CMP R{data:zero}, R{data:zero}
    BR.Z pi
strobe:
    MOVE R{state:es}, R{in:e_in}
    MOVE [FACE], R{data:face_out}
    MOVE R0, R{in:y}
    {write:yf}
    MOVE R0, R{in:yq}
    {write:yqf}
    {jump:otrig}
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

        # --- qout (complex): emits the (yi, yq) pair with ONE trigger. Still the
        # single dedicated egress cell — no feedback, no third role.
        cqout = CellProgram(
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

        # DICT ORDER: qinterp sits between interp and ted so ALL its edges are
        # FORWARD; period_relay stays LAST so its edge into counter remains the
        # block's only BACKWARD connection.
        return {"counter": ccounter, "dline": cdline, "interp": cinterp,
                "qinterp": qinterp, "ted": cted, "loop_filter": cloop,
                "qout": cqout, "period_relay": period_relay}

    # -------------------------------------------------------------- topology
    def internal_connections(self) -> List[Tuple[Any, str, Any, str]]:
        """Forward data handoffs + the ONE backward feedback edge.

        The only BACKWARD edge is ``period_relay -> counter.v``. Every other
        handoff runs forward in program-dict order, so ``_apply_internal_feedback``
        has exactly one connection to resolve and NEVER touches the egress cell —
        which is the structural property that makes this block's loop close (see
        ``_CELL_IDS``).
        """
        base = [
            ("counter", "xif", "dline", "xi"),
            ("counter", "muf", "dline", "mu"),
            ("dline", "d0f", "interp", "d0"),
            ("dline", "d1f", "interp", "d1"),
            ("dline", "d2f", "interp", "d2"),
            ("dline", "muf", "interp", "mu"),
            ("ted", "ef", "loop_filter", "e_in"),
            ("ted", "yf", "loop_filter", "y"),
            # THE SPLIT: the recovered symbol to the DEDICATED egress cell...
            ("loop_filter", "yf", "qout", "yi" if self._complex else "y"),
            # ...and, on a PERPENDICULAR face, the loop correction to the relay.
            ("loop_filter", "vf", "period_relay", "v_in"),
            # ``fb_trig`` is declared as a connection too so its ENTRY resolves to
            # period_relay's ``relay`` entry rather than falling through to the
            # positional-next cell (which is ``qout``). Without this the relay is
            # entered at qout's entry address, lands on empty words, HALTs before
            # its ``relay:`` body, and its pout WRITE never fires — counter.v stays
            # 0 and the loop never converges. (The same trap MMTimingRecoveryBlock
            # documents at its own fb_trig.)
            ("loop_filter", "fb_trig", "period_relay", "relay"),
            # THE FEEDBACK: pure data, BACKWARD, into the NCO's ``v`` state.
            ("period_relay", "pout", "counter", "v"),
        ]
        if self._complex:
            # COMPLEX inserts ``qinterp`` INTO the thread between interp and ted,
            # so the I rail's (c, m) are relayed THROUGH it rather than handed
            # across it — see the qinterp program for why that is mandatory.
            base = ([("counter", "xqf", "dline", "xq"),
                     ("dline", "xqf", "interp", "xq"),
                     ("interp", "cf", "qinterp", "c"),
                     ("interp", "mf", "qinterp", "m"),
                     ("interp", "qmuf", "qinterp", "mu"),
                     ("interp", "qxf", "qinterp", "xq"),
                     ("qinterp", "cf", "ted", "c"),
                     ("qinterp", "mf", "ted", "m"),
                     ("qinterp", "yqf", "ted", "yq"),
                     ("ted", "yqf", "loop_filter", "yq"),
                     ("loop_filter", "yqf", "qout", "yq")] + base)
        else:
            base = ([("interp", "cf", "ted", "c"),
                     ("interp", "mf", "ted", "m")] + base)
        return base

    def internal_jumps(self) -> List[Tuple[Any, str, Any, str]]:
        """The SINGLE LINEAR TRIGGER THREAD — each cell triggers exactly ONE next
        cell. The no-strobe path re-enters the loop_filter at its ``nostrobe``
        entry so the PI, the feedback and the arbiter lock-clear run on EVERY
        sample, not only on strobes."""
        base = [
            ("counter", "trig", "dline", "default"),
            ("dline", "trig", "interp", "default"),
            ("interp", "trig", "ted", "default"),
            ("interp", "ns_trig", "loop_filter", "nostrobe"),
            ("ted", "trig", "loop_filter", "strobe"),
            ("loop_filter", "otrig", "qout", "default"),
            ("loop_filter", "fb_trig", "period_relay", "relay"),
            # The egress cell's trigger must DEAD-END. It is the last cell of the
            # thread; falling through to the positional-next cell would fire the
            # period_relay a second time per symbol and double-apply the feedback.
            ("qout", "trig", "__terminate__", "default"),
        ]
        if self._complex:
            # qinterp is IN the thread: interp triggers it (strobe) or hands it the
            # no-strobe path (so its Q delay line still shifts), and it continues on
            # to ted / the loop_filter's nostrobe entry respectively.
            base = [b for b in base
                    if b[0] not in ("interp",) or b[1] not in ("trig", "ns_trig")]
            base = base[:2] + [
                ("interp", "trig", "qinterp", "strobe"),
                ("interp", "ns_trig", "qinterp", "nostrobe"),
                ("qinterp", "trig", "ted", "default"),
                ("qinterp", "ns_trig", "loop_filter", "nostrobe"),
            ] + base[2:]
        return base

    def default_layout(self) -> Dict[Any, Tuple[int, int, str]]:
        """COMPACT SERPENTINE FOLD. The chain snakes so that the last datapath
        cell ends up with TWO free perpendicular faces — one for the dedicated
        egress, one for the feedback relay — and the relay lands back beside the
        landing cell::

            col:      0              1              2
            row 0:  counter        dline          interp
            row 1:  period_relay   loop_filter    ted
            row 2:                 qout

        Chain: counter(0,0) -E-> dline(1,0) -E-> interp(2,0) -S-> ted(2,1) -W->
        loop_filter(1,1). ``loop_filter`` is the DUAL-FACE cell: the recovered
        symbol goes SOUTH to ``qout``(1,2) and ``v`` goes WEST to
        ``period_relay``(0,1), which writes back NORTH directly into
        ``counter``(0,0).

        **SQUARE (3x3), AND THE ASPECT IS LOAD-BEARING.** The same seven-cell ring
        folds as 4x2, 2x4 or 3x3 with identical area, and ALL THREE are BER 0,
        bit-exact and orientation-invariant in this block's own suite — the
        difference is completely invisible from inside the block, and only shows up
        when you run the DESIGNS the block ships in. Two separate placer behaviours
        punish the rectangular folds:

        * a **TALL** fold trips the packer's FIT-DRIVEN ROTATION of a feedback
          block whose authored height would overflow the current band
          (``autoplace._pack_compact``: ``h > w and row_top + h > height`` ->
          rotate ``cw``). The flyline orienter deliberately leaves feedback blocks
          at identity, but this fit path overrides it, and once ONE block rotates
          the orienter re-orients everything downstream. Measured on the shipped
          duplex BPSK modem: 2x4 gives 6/11 nets at the compact reserve and 9/11
          after the whole 45 s auto-P&R sweep (FAIL; 11/11 needs a 300 s budget).
        * a **4-WIDE** fold walls the coherent-RX chain's matched-filter -> Costas
          bus channel: 5/7 nets, ``no bus path from source to the broker tap``.

        3x3 is square, so it triggers neither: **modem 11/11 in 2 s AND production
        coherent RX 7/7**. It was found by enumerating all 144 legal 3x3
        zero-transit folds and scoring candidates against BOTH design families.

        I/O co-location (layout_rules §1): the input lands on ``counter``(0,0) and
        the output leaves ``qout``(1,2) — both on the footprint perimeter, well
        inside the <=8-across convention (INV-9).

        DICT ORDER MUST MATCH build_cell_programs() key order: cells are paired BY
        INDEX, and a mismatched order assigns program A to cell B with no error.
        """
        if self._complex:
            #   col:     0        1       2        3        4      5            6
            #  row 0:  counter   dline   interp  qinterp   ted   loop_filter  qout
            #  row 1: <-t_fb4  <-t_fb3 <-t_fb2  <-t_fb1  <-t_fb0 period_relay
            #
            # The COMPLEX ring is a SEVEN-cycle (qinterp joins the chain), which is
            # ODD, so on a bipartite grid it CANNOT close by abutment — a transit
            # lane is mathematically required here, unlike the real fold. A tighter
            # 3x3 single-transit arrangement exists on paper and was built: it
            # places, routes and emits THREE symbols and then stalls, so this
            # verified 7x2 lane is what ships. The complex variant is not what
            # pressures a dense design (the shipped modem uses the real mode), so
            # the extra width is affordable here and the compaction work belongs
            # with whoever next needs a complex Gardner in a full chip.
            #
            # ``qinterp`` sits IN the chain, not off to the side: it relays the I
            # rail's (c, m) as well as producing yq, and an internal data WRITE
            # only travels along ABUTTING forward faces. A qinterp parked off the
            # spine writing straight to qout builds and routes cleanly and delivers
            # NOTHING (measured: the Q channel came out all zeros while the I rail
            # stayed bit-exact).
            return {
                "counter": (0, 0, "east"),
                "dline": (1, 0, "east"),
                "interp": (2, 0, "east"),
                "qinterp": (3, 0, "east"),
                "ted": (4, 0, "east"),
                "loop_filter": (5, 0, "south"),  # dual-face: y EAST, v SOUTH
                "qout": (6, 0, "south"),         # the single external egress
                "period_relay": (5, 1, "west"),
                "transit_fb_0": (4, 1, "west"),
                "transit_fb_1": (3, 1, "west"),
                "transit_fb_2": (2, 1, "west"),
                "transit_fb_3": (1, 1, "west"),
                "transit_fb_4": (0, 1, "north"),
            }
        return {
            "counter": (0, 0, "east"),
            "dline": (1, 0, "east"),
            "interp": (2, 0, "south"),
            "ted": (2, 1, "west"),
            "loop_filter": (1, 1, "south"),  # dual-face: y SOUTH, v WEST
            "qout": (1, 2, "east"),          # the single external egress
            "period_relay": (0, 1, "north"),  # writes v NORTH into counter
        }

    def output_cell_id(self) -> Any:
        """The DEDICATED egress cell. It carries no feedback and no second WRITE,
        so the output-patch passes have an unambiguous single target."""
        return "qout"

    def output_face_addr(self) -> Any:
        """``qout`` is a plain single-face output cell — its WRITE egresses on the
        cell's resting ``fwd_face``, which the route sets. There is no baked-in
        face word for the build to rewrite."""
        return None

    # ------------------------------------------------------------- reference
    def process_reference(self, input_samples: np.ndarray) -> np.ndarray:
        """Bit-exact Q15/ISA reference for the on-chip cells (truncating MULQ,
        wrapping int16, immediate-count shifts).

        ``input_samples`` is a real (or complex / (N,2)) 2-sps stream. Returns the
        recovered symbol-center samples as Q15 int16 — or, with ``complex=True``,
        the (N_sym, 2) recovered (yi, yq) center pairs.
        """
        def s16(v):
            v = int(v) & 0xFFFF
            return v - 0x10000 if v & 0x8000 else v

        def sat(v):
            return max(-32768, min(32767, int(v)))

        def mq(a, b):
            # The ISA MULQ TRUNCATES (arithmetic floor >>15); matching that
            # exactly is required for chip<->reference bit-exactness.
            return (s16(a) * s16(b)) >> 15

        arr = np.asarray(input_samples)
        if np.iscomplexobj(arr):
            xi = [float_to_q15(float(c.real)) for c in arr]
            xq = [float_to_q15(float(c.imag)) for c in arr]
        elif arr.ndim == 2 and arr.shape[1] == 2:
            xi = [int(a) & 0xFFFF for a, _ in arr]
            xq = [int(b) & 0xFFFF for _, b in arr]
        elif arr.dtype.kind == "f":
            xi = [float_to_q15(float(v)) for v in arr]
            xq = [0] * len(xi)
        else:
            xi = [int(v) & 0xFFFF for v in arr]
            xq = [0] * len(xi)

        ONE = self._ONE
        Wnom = ONE // 2
        K1i, K2i = self._K1i, self._K2i
        MD = self._MAXDEV
        cnt = 0
        vi = 0
        v = 0
        d0 = d1 = 0                 # x[n-2], x[n-1]
        q1 = 0                      # the Q rail's one-sample delay
        cprev = 0
        out = []
        outq = []
        for n in range(len(xi)):
            d2 = s16(xi[n])
            xq2 = s16(xq[n])
            W = Wnom + v
            strobe = cnt < W
            e = 0
            if strobe:
                mu = min(32767, cnt << 1)      # mu = cnt/W at W ~= 0.5; one SHL
                c = sat(d1 + mq(mu, sat(d2 - d1)))     # symbol CENTER
                m = sat(d0 + mq(mu, sat(d1 - d0)))     # MID-symbol
                e = mq(m, sat(c - cprev))              # the Gardner TED
                cprev = c
                out.append(c)
                if self._complex:
                    outq.append(sat(q1 + mq(mu, sat(xq2 - q1))))
            vi = max(-MD, min(MD, vi + mq(e, K2i)))    # integral + anti-windup
            v = max(-MD, min(MD, mq(e, K1i) + vi))     # + proportional, clamped
            cnt = (cnt - W) % ONE                      # modulo-1, bounded
            d0, d1 = d1, d2
            q1 = xq2
        if self._complex:
            return np.array(list(zip(out, outq)), dtype=np.int16)
        return np.array(out, dtype=np.int16)

    def reset(self):
        """Stateless reference (``process_reference`` is self-contained)."""
        pass
