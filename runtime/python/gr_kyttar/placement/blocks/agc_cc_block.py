# SPDX-License-Identifier: GPL-3.0-or-later
"""AGCCCBlock — see :class:`AGCCCBlock`."""
from typing import Any, Dict, List, Tuple

import numpy as np

from ..block import CellProgram, DataWord, EntryPoint, Port, StateVar
from ._base import BlockInterface, KyttarBlock
from .cordic_blocks import NITER, _CordicBase, _s16, cordic_mag_word


class AGCCCBlock(KyttarBlock):
    """Complex AGC — drop-in for GNU Radio ``analog.agc_cc`` (VERBATIM params
    ``rate``, ``reference``, ``gain``, ``max_gain``).

    GNU Radio's complex AGC loop (pinned against LIVE GR before authoring)::

        out[n]  = in[n] * gain                       (complex * real scalar)
        gain   += rate * (reference - |out[n]|)      (|.| = TRUE complex magnitude)
        if max_gain > 0: gain = min(gain, max_gain)

    The FIRST sample is scaled by the INITIAL gain; ``max_gain == 0`` means
    unclamped; GR has no lower clamp (in the attenuating regime the loop never
    drives gain negative — see the datapath notes below).

    DATAPATH (20 cells): a serialize-LOCKED feedback composite (INV-19), the
    ComplexCostasLoop recipe applied to a gain loop:

      * ``hold`` (landing cell) — applies the fed-back gain INCREMENT
        (``g += ginc``, the dphase idiom: ginc rides an input register, cold 0
        = GR's first-sample semantics), pins a 16-bit ADD overflow to the gain
        ceiling (V flag), clamps ``g`` to ``[0, max_gain_q]``, forwards
        (xi, xq, g), then LOCKs its arbiter to the feedback face (the next
        sample is HELD until the loop closes — INV-19).
      * ``tap`` (the block OUTPUT cell, mid-block like the Costas rotate) —
        ``yi = MULQ(xi, g)``, ``yq = MULQ(xq, g)``; dual-FACE: the pair goes
        INTERNALLY into the magnitude chain (face_internal) and EXTERNALLY as
        the block output (face_tap, route-overridden at build).
      * ``pre1 .. mag`` — the PROVEN ComplexToMagBlock CORDIC vectoring chain
        VERBATIM (prescale 1/4, |x|,|y| pre-fold, 14 unrolled iterations,
        MULQ 1/K + saturating <<2 restore): the TRUE ``|out|``, computed from
        the exact output words the block emits.
      * ``upd`` — ``ginc = rate_q * (reference_q - |out|)`` with the RMS pair's
        FULL-PRECISION ERROR-FEEDBACK accumulator (a 30-bit S = ginc<<15 +
        acc_lo kept as two words): bare ``MULQ(rate_q, err)`` truncation stalls
        the loop for every ``|err| < 2^15/rate_q`` LSB — at GR's default
        rate=1e-4 (rate_q=3) that is a third of full scale. With the error
        feedback NO increment is ever lost and the loop settles with
        ``|out|`` within +-1 LSB of the reference. ``upd`` writes ``ginc``
        back to ``hold`` (@1 abutment, the QPSK-Costas pd_pi->phase shape) and
        clears the arbiter LOCK inline (backward ``WRITE.CFG @1, 4``).

    Hardware deviations from analog.agc_cc (INV-0):
    -------------------------------------------------------------------------
    HW-DEVIATION (Q15 datapath — attenuating regime ONLY):
      1. The gain register is Q15 [0, 1): ``gain``/``max_gain``/``reference``
         above 1.0 are NOT representable — the block RAISES (never clamps
         silently). ``gain=1.0`` etc. quantize to 32767/32768. TRUE
         amplification (gain > 1, weak signal pulled UP) needs integer gain
         headroom and is out of scope (same limit as AGCBlock/agc_ff).
      2. ``max_gain = 0`` (GR: unlimited) runs with the Q15 ceiling
         32767/32768 — in the attenuating regime the loop can never need more.
      3. ``rate``/``reference``/``gain``/``max_gain`` are quantized to Q15
         (round(v*32768)/32768). ``rate`` that quantizes to 0 (< ~1.5e-5)
         RAISES (the loop would never move); GR's default 1e-4 runs as
         3/32768 (~8% slower transient; the settled level is unchanged).
      4. gain is clamped at 0 from below (GR lets it go negative; with
         reference > 0 the update ``g*(1-rate) <= g+rate*(ref-|out|)`` keeps
         g >= 0 in-regime, so the clamp only guards Q15 rounding corners).
    """
    CATEGORY = "signal_conditioning"
    TAGS = ["agc", "agc_cc", "gain", "complex", "signal_conditioning"]

    _interface = BlockInterface(
        entry_address=1, input_registers=[0, 1], output_registers=[0, 1])

    # INV-22: ``pipeline_lock`` is a BUILD/substrate hint (the INV-19 saturation
    # serialize-LOCK), NOT a GNU Radio DSP parameter — analog.agc_cc has no such
    # param and the per-sample DSP result is identical with it on or off.
    GRC_UNSUPPORTED_PARAMS = ("pipeline_lock",)

    def __init__(
        self,
        name: str,
        rate: float = 1e-4,
        reference: float = 1.0,
        gain: float = 1.0,
        max_gain: float = 0.0,
        pipeline_lock: bool = True,
    ):
        """Initialize complex AGC (GNU Radio ``analog.agc_cc`` signature).

        Args:
            name: Block name
            rate: update rate of the loop (GR default 1e-4)
            reference: reference value to adjust signal power to (GR default 1.0)
            gain: initial gain value (GR default 1.0)
            max_gain: maximum gain value; 0 means UNLIMITED (GR default 0)
            pipeline_lock: engage the INV-19 serialize-LOCK (default True —
                a data-feedback loop is only saturation-correct locked).
        """
        super().__init__(name, rate=rate, reference=reference, gain=gain,
                         max_gain=max_gain)
        self._pipeline_lock = bool(pipeline_lock)
        self._rate = float(rate)
        self._reference = float(reference)
        self._initial_gain = float(gain)
        self._max_gain = float(max_gain)

        # HARDWARE DEVIATION (Q15 attenuating regime): gain/reference/max_gain
        # above 1.0 are not representable in the Q15 gain register — RAISE
        # (INV-0: never silently clamp). See the class docstring.
        if not (0.0 <= self._initial_gain <= 1.0):
            raise ValueError(
                f"HARDWARE LIMIT: agc_cc gain={gain} outside [0, 1] — the Q15 "
                f"gain register implements the ATTENUATING regime only "
                f"(amplification needs integer gain headroom).")
        if not (0.0 < self._reference <= 1.0):
            raise ValueError(
                f"HARDWARE LIMIT: agc_cc reference={reference} outside (0, 1] "
                f"— a Q15 magnitude cannot exceed 1.0, so the loop could "
                f"never reach it.")
        if self._max_gain < 0.0 or self._max_gain > 1.0:
            raise ValueError(
                f"HARDWARE LIMIT: agc_cc max_gain={max_gain} outside [0, 1] "
                f"(0 = unlimited-within-Q15) — the Q15 gain register "
                f"implements the ATTENUATING regime only.")
        if not (0.0 < self._rate <= 1.0):
            raise ValueError(
                f"HARDWARE LIMIT: agc_cc rate={rate} outside (0, 1] — the "
                f"Q15 datapath cannot represent it.")

        def _q(v: float) -> int:
            return min(32767, int(round(v * 32768.0)))

        self._rate_q15 = _q(self._rate)
        if self._rate_q15 <= 0:
            raise ValueError(
                f"HARDWARE LIMIT: agc_cc rate={rate} quantizes to 0 in Q15 "
                f"(rate < ~1.5e-5) — the loop would never update. Smallest "
                f"representable rate is ~1.53e-5 (1/65536-rounding).")
        self._reference_q15 = _q(self._reference)
        self._gain_q15 = _q(self._initial_gain)
        # max_gain == 0 -> unlimited -> the Q15 ceiling (HW-DEVIATION 2).
        self._max_gain_q15 = (32767 if self._max_gain == 0.0
                              else _q(self._max_gain))
        if self._max_gain_q15 <= 0:
            raise ValueError(
                f"HARDWARE LIMIT: agc_cc max_gain={max_gain} quantizes to 0 "
                f"in Q15 — the gain would be pinned at zero.")

    # ------------------------------------------------------------------ props
    @property
    def cell_count(self) -> int:
        return NITER + 6          # hold, tap, pre1, pre2, xy0..13, mag, upd

    @property
    def interface(self) -> BlockInterface:
        return self._interface

    @property
    def rate(self) -> float:
        return self._rate

    @property
    def reference(self) -> float:
        return self._reference

    @property
    def gain(self) -> float:
        return self._initial_gain

    @property
    def max_gain(self) -> float:
        return self._max_gain

    def output_cell_id(self):
        """The block output (yi_tap/yq_tap) leaves the MID-block ``tap`` cell
        (the Costas-rotate shape), not the last placed cell."""
        return "tap"

    def output_cell_ids(self) -> List[str]:
        return ["tap"]

    # ------------------------------------------------------- cell programs
    def build_cell_programs(self) -> Dict[str, CellProgram]:
        # --- hold: gain state + increment application + clamps + fan-forward.
        # ``ginc`` is an INPUT register (cold 0 = GR's first-sample semantics —
        # the first output is scaled by the INITIAL gain); upd feeds it back
        # each sample (the Costas dphase idiom). The 16-bit ADD can overflow
        # (g up to 32767 plus a positive increment): ADD sets V on signed
        # overflow, and the overflow is pinned to the gain ceiling before the
        # ordinary [0, gmax] clamps run. The serialize-LOCK tail (INV-19) runs
        # AFTER {jump:trig} (a JUMP does not halt the issuer): lock_face=SOUTH
        # (0) is the face the upd feedback corridor enters on in
        # ``default_layout`` (is_face -> orientation-transformed).
        lock_data = ([DataWord("lock_face", 0, address=4, is_face=True),
                      DataWord("one", 1, address=5)]
                     if self._pipeline_lock else [])
        lock_tail = ("""\
    MOVE R0, R{data:lock_face}
    MOVE [LOCK_FACE], R0
    MOVE R0, R{data:one}
    MOVE [LOCK], R0
""" if self._pipeline_lock else "")
        # ``ginc`` (the feedback landing) is a pinned STATE register, NOT an
        # input Port: ``resolved_io`` counts every input-role register as a
        # host-injected operand, so a port-input BROKER (some orientations
        # broker the input corridor) would relay a stale third operand into a
        # ginc INPUT register every sample and erase the feedback (found at
        # cw^3: the loop ran open). The upd->hold feedback WRITE resolves to
        # the state register by name (state names match before input names),
        # and the landing operand group stays exactly [xi, xq].
        hold = CellProgram(
            inputs=[Port("xi", register=0), Port("xq", register=1)],
            outputs=[Port("xi_f"), Port("xq_f"), Port("g_f"), Port("trig")],
            entries=[EntryPoint("default")],
            data=[DataWord("gmax", self._max_gain_q15, address=3)] + lock_data,
            # LOOP MEMORY: ``g`` is the AGC gain and ``ginc`` the pending
            # increment — both reset per batch so a fresh burst cold-starts at
            # the initial gain with no pending update, exactly like a fresh GR
            # block (the Costas phase/freq convention). ginc pinned into the
            # R2 hole below the data words (INV-33: pin every StateVar).
            state=[StateVar("ginc", register=2, reset_per_batch=True),
                   StateVar("g", register=6 if self._pipeline_lock else 4,
                            initial_value=self._gain_q15,
                            reset_per_batch=True)],
            # xi lands in R0 (the accumulator, INV-33): forward it with the
            # FIRST instruction ({write} emits R0 and preserves it — the
            # Costas phase cell's {write:fwd_input}-first idiom), then xq,
            # THEN run the gain update (which clobbers R0 freely).
            assembly_template="""\
start:
    {write:xi_f}
    MOVE R0, R{in:xq}
    {write:xq_f}
    ADD R{state:g}, R{state:ginc}
    BR.NV nov
    MOVE R0, R{data:gmax}
nov:
    MOVE R{state:g}, R0
    CMP R{state:g}, R{data:gmax}
    BR.N chi
    MOVE R{state:g}, R{data:gmax}
chi:
    OR R{state:g}, R{state:g}
    BR.NN fwd
    SUB R0, R0
    MOVE R{state:g}, R0
fwd:
    MOVE R0, R{state:g}
    {write:g_f}
    {jump:trig}
""" + lock_tail,
        )

        # --- tap: the OUTPUT cell (mid-block, dual-FACE — the Costas rotate
        # idiom). yi/yq go INTERNALLY into the CORDIC chain on face_internal
        # (the resting default_layout face, re-asserted every sample so the
        # previous sample's face_tap flip is undone) and EXTERNALLY as the
        # block output on face_tap (route-overridden by the build). The
        # external WRITEs are strictly LAST (the build patches the last WRITE/
        # JUMP for the route — INV-19/20 exit-handoff rule) and the cell keeps
        # >= 1 free word for the INV-17 complex fan-out JUMP.
        tap = CellProgram(
            inputs=[Port("xi", register=0), Port("xq", register=1),
                    Port("g", register=2)],
            outputs=[Port("yi_c"), Port("yq_c"), Port("trig"),
                     Port("yi_tap"), Port("yq_tap"), Port("tap_trig")],
            entries=[EntryPoint("default")],
            data=[DataWord("face_internal", 1, address=3, is_face=True),
                  DataWord("face_tap", 1, address=4, is_face=True)],
            state=[StateVar("gs", register=5), StateVar("yis", register=6),
                   StateVar("yqs", register=7)],
            assembly_template="""\
start:
    MOVE [FACE], R{data:face_internal}
    MOVE R{state:gs}, R{in:g}
    MULQ R{in:xi}, R{state:gs}
    {write:yi_c}
    MOVE R{state:yis}, R0
    MULQ R{in:xq}, R{state:gs}
    {write:yq_c}
    MOVE R{state:yqs}, R0
    {jump:trig}
    MOVE [FACE], R{data:face_tap}
    MOVE R0, R{state:yis}
    {write:yi_tap}
    MOVE R0, R{state:yqs}
    {write:yq_tap}
    {jump:tap_trig}
""",
        )

        # --- the PROVEN CORDIC magnitude chain, verbatim (ComplexToMagBlock).
        progs: Dict[str, CellProgram] = {"hold": hold, "tap": tap,
                                         "pre1": _CordicBase._pre1_program(),
                                         "pre2": _CordicBase._pre2m_program()}
        for i in range(NITER):
            progs[f"xy{i}"] = _CordicBase._xy_program(
                i, emit_y2z=False, emit_xy=(i < NITER - 1))
        progs["mag"] = _CordicBase._mag_program()

        # --- upd: the gain update with FULL-PRECISION ERROR FEEDBACK (the RMS
        # pair's accumulator idiom): ginc_int + acc_lo track rate_q*err to the
        # last bit ((hi<<15) + lo15 == rate_q*err exactly, floor identity), so
        # small-rate increments are never truncated away. Writes ginc back to
        # hold (@1, the backward feedback edge — _apply_internal_feedback
        # patches hop + dest) and clears hold's arbiter LOCK inline (backward
        # WRITE.CFG @1, 4 — the pd_pi structure: NOT the exit cell, holds the
        # feedback WRITE alongside the config-write, so neither is clobbered).
        # trig SELF-TERMINATES (a local terminator, never a JUMP into hold).
        upd = CellProgram(
            inputs=[Port("m", register=0)],
            outputs=[Port("ginc"), Port("trig")],
            entries=[EntryPoint("default")],
            data=[DataWord("ref", self._reference_q15, address=1),
                  DataWord("rate", self._rate_q15, address=2),
                  DataWord("mask", 0x7FFF, address=3),
                  DataWord("zero", 0, address=4)],
            # LOOP MEMORY: ``acclo`` is the error-feedback residue — reset per
            # batch with the gain (fresh burst = fresh loop). d/hi/gs are
            # per-sample scratch (written before read).
            state=[StateVar("d", register=5), StateVar("hi", register=6),
                   StateVar("acclo", register=7, reset_per_batch=True),
                   StateVar("gs", register=8)],
            assembly_template=("""\
start:
    SUB R{data:ref}, R{in:m}
    MOVE R{state:d}, R0
    MULQ R{state:d}, R{data:rate}
    MOVE R{state:hi}, R0
    MUL R{state:d}, R{data:rate}
    AND R0, R{data:mask}
    ADD R0, R{state:acclo}
    MOVE R{state:d}, R0
    AND R0, R{data:mask}
    MOVE R{state:acclo}, R0
    SHR R{state:d}, #15
    ADD R0, R{state:hi}
""" + ("""\
    MOVE R{state:gs}, R0
    MOVE R0, R{data:zero}
    WRITE.CFG @1, 4
    MOVE R0, R{state:gs}
    {write:ginc}
    {jump:trig}
""" if self._pipeline_lock else """\
    {write:ginc}
    {jump:trig}
""")),
        )
        progs["upd"] = upd
        return progs

    # ------------------------------------------------------------- wiring
    def internal_connections(self) -> List[Tuple[Any, str, Any, str]]:
        conns: List[Tuple[Any, str, Any, str]] = [
            ("hold", "xi_f", "tap", "xi"),
            ("hold", "xq_f", "tap", "xq"),
            ("hold", "g_f", "tap", "g"),
            ("tap", "yi_c", "pre1", "xi"),
            ("tap", "yq_c", "pre1", "xq"),
            ("pre1", "x", "pre2", "x"), ("pre1", "y", "pre2", "y"),
            ("pre2", "x", "xy0", "x"), ("pre2", "y", "xy0", "y"),
        ]
        for i in range(NITER - 1):
            conns += [(f"xy{i}", "x", f"xy{i+1}", "x"),
                      (f"xy{i}", "y", f"xy{i+1}", "y")]
        conns += [(f"xy{NITER-1}", "x", "mag", "x"),
                  ("mag", "mag", "upd", "m"),
                  # FEEDBACK: the gain increment, upd -> hold (loop closure).
                  ("upd", "ginc", "hold", "ginc")]
        return conns

    def internal_jumps(self) -> List[Tuple[Any, str, Any, str]]:
        jumps = [("hold", "trig", "tap", "default"),
                 ("tap", "trig", "pre1", "default"),
                 ("pre1", "trig", "pre2", "default"),
                 ("pre2", "trig", "xy0", "default")]
        for i in range(NITER - 1):
            jumps.append((f"xy{i}", "trig", f"xy{i+1}", "default"))
        jumps += [(f"xy{NITER-1}", "trig", "mag", "default"),
                  ("mag", "trig", "upd", "default"),
                  # upd is the last cell of the pass: its trig SELF-TERMINATES
                  # (a stray JUMP up the feedback corridor would re-fire hold).
                  ("upd", "trig", "__terminate__", "default"),
                  # tap's external output trigger; unconsumed it terminates,
                  # a route retargets it (_patch_last_jump_handoff).
                  ("tap", "tap_trig", "__terminate__", "default")]
        return jumps

    def default_layout(self) -> Dict[Any, Tuple[int, int, str]]:
        """7x5 serpentine RING (perimeter = exactly the 20 cells), I/O
        co-located on the TOP edge (INV-8/9)::

            col:   0        1       2        3       4      5      6
            row0:  hold(E)  tap(E)  pre1(E)  pre2(E) xy0    xy1    xy2(S)
            row1:  upd(N)   .       .        .       .      .      xy3(S)
            row2:  mag(N)   .       .        .       .      .      xy4(S)
            row3:  xy13(N)  .       .        .       .      .      xy5(S)
            row4:  xy12(N)  xy11(W) xy10(W)  xy9(W)  xy8(W) xy7(W) xy6(W)

        Forward face-trace: hold -> tap -> pre1 -> pre2 -> xy0..xy2 (east),
        down col 6 (xy3..xy5), west along row4 (xy6..xy12), up col 0
        (xy13 -> mag -> upd -> hold). Every internal handoff is @1-abutted on
        the trace. The upd -> hold FEEDBACK (ginc + the WRITE.CFG lock-clear)
        is the final @1 NORTH hop — the QPSK-Costas pd_pi->phase shape, no
        transit cell. The unlock word enters hold on its SOUTH face
        (= lock_face). Input cell (0,0) and output cell (1,0) co-locate on
        the top edge; footprint 7x5 (both <= 8, INV-9). 7 wide (not 8) is
        deliberate: at 8 wide a 180-degree orientation left only 1-cell
        channels and the input corridor was forced THROUGH the x16_out port
        cell (broker-diverted there, output wrapped 22 cells around the die
        -> dead datapath); at 7 wide every orientation keeps a free channel
        clear of both port cells."""
        lay: Dict[Any, Tuple[int, int, str]] = {
            "hold": (0, 0, "east"), "tap": (1, 0, "east"),
            "pre1": (2, 0, "east"), "pre2": (3, 0, "east")}
        for i in range(3):                       # xy0..xy2 eastbound
            lay[f"xy{i}"] = (4 + i, 0, "east" if i < 2 else "south")
        lay["xy3"] = (6, 1, "south")
        lay["xy4"] = (6, 2, "south")
        lay["xy5"] = (6, 3, "south")
        for i in range(6, 13):                   # xy6..xy12 westbound row4
            lay[f"xy{i}"] = (12 - i, 4, "west" if i < 12 else "north")
        lay["xy13"] = (0, 3, "north")
        lay["mag"] = (0, 2, "north")
        lay["upd"] = (0, 1, "north")
        # Positional pairing (INV-33): dict order == build_cell_programs order.
        ordered = {"hold": lay["hold"], "tap": lay["tap"],
                   "pre1": lay["pre1"], "pre2": lay["pre2"]}
        for i in range(NITER):
            ordered[f"xy{i}"] = lay[f"xy{i}"]
        ordered["mag"] = lay["mag"]
        ordered["upd"] = lay["upd"]
        return ordered

    # -------------------------------------------------------------- reference
    def process_reference_q15(self, iq_words) -> List[Tuple[int, int]]:
        """Bit-exact predictor of the on-chip datapath: per sample, apply the
        fed-back increment (V-pinned add + [0, gmax] clamps), scale both rails
        (MULQ truncation), run the exact CORDIC magnitude chain on the emitted
        words, and form the next increment with the error-feedback accumulator.
        ``iq_words``: iterable of (i, q) uint16 word pairs."""
        g = self._gain_q15
        gmax = self._max_gain_q15
        ref = self._reference_q15
        rate = self._rate_q15
        acclo = 0
        ginc = 0
        out: List[Tuple[int, int]] = []
        for (xi, xq) in iq_words:
            s = g + ginc
            if s > 32767:            # 16-bit signed ADD overflow -> V -> gmax
                s = gmax
            if s > gmax:
                s = gmax
            if s < 0:
                s = 0
            g = s
            yi = (_s16(xi) * g) >> 15
            yq = (_s16(xq) * g) >> 15
            out.append((yi & 0xFFFF, yq & 0xFFFF))
            m = cordic_mag_word(yi & 0xFFFF, yq & 0xFFFF)
            err = ref - m                        # in [-32766, 32767]
            prod = rate * err
            hi = prod >> 15                      # MULQ truncation (floor)
            lo = prod & 0x7FFF
            t = acclo + lo
            ginc = hi + (t >> 15)
            acclo = t & 0x7FFF
        return out

    def process_reference(self, input_samples) -> np.ndarray:
        """Float reference mirroring GNU Radio ``agc_cc`` run at the CHIP's
        quantized constants (rate_q/ref_q/gain_q/gmax_q as floats) — the
        regime-mirrored golden law. Complex in -> complex out, clipped to Q15
        range."""
        arr = np.asarray(input_samples)
        if not np.iscomplexobj(arr):
            arr = arr.astype(np.complex64)
        g = self._gain_q15 / 32768.0
        gmax = self._max_gain_q15 / 32768.0
        ref = self._reference_q15 / 32768.0
        rate = self._rate_q15 / 32768.0
        out = np.empty(len(arr), dtype=np.complex64)
        for i, z in enumerate(arr):
            o = complex(z) * g
            out[i] = o
            g = g + rate * (ref - abs(o))
            if g > gmax:
                g = gmax
            if g < 0.0:
                g = 0.0
        re = np.clip(out.real, -1.0, 32767.0 / 32768.0)
        im = np.clip(out.imag, -1.0, 32767.0 / 32768.0)
        return (re + 1j * im).astype(np.complex64)

    def reset(self):
        pass
