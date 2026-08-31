# SPDX-License-Identifier: GPL-3.0-or-later
"""PIControllerBlock — see :class:`PIControllerBlock`."""
from typing import Any, Dict, List, Tuple

import numpy as np

from ..block import CellProgram, DataWord, EntryPoint, Port, StateVar
from ._base import BlockInterface, KyttarBlock


def _s16(v: int) -> int:
    v &= 0xFFFF
    return v - 0x10000 if v >= 0x8000 else v


class PIControllerBlock(KyttarBlock):
    """Discrete PI controller with a 32-bit anti-windup integrator — the FOC
    current-loop primitive (placeKYT-native; no stock GNU Radio streaming
    counterpart).

    One Q15 error stream in, one Q15 command out, per sample::

        u[n]   = sat(Kp*e[n] + acc_hi[n],  +/-limit)
        acc   += (Ki*e[n])                 -- 32-bit, UNLESS the output is
                                              already saturated in the
                                              direction this step integrates
                                              (conditional-skip anti-windup)

    where ``acc_hi`` is the high 16-bit word of the 32-bit accumulator — the
    integral term at Q15 scale with 16 extra FRACTIONAL bits underneath it.

    THE POINT OF THIS BLOCK (measured, not argued): it answers "is 16 bits
    enough for FOC?". Q15 I/O is at or above industry practice (12-14-bit
    ADCs, 12-16-bit PWM), so the resolution risk concentrates in ONE place:
    the integrator, where ``Ki*e`` per step can be far below one Q15 LSB and
    would silently vanish in a 16-bit accumulator — the loop then never
    cancels small steady-state error. The fix is the 32-BIT ACCUMULATOR as a
    register pair (high half FIRST per INV-58 — ``MULHI``/any ALU op destroys
    the carry, and the wrong order leaves the LOW word bit-exact, invisible
    to a one-word gate — with ``ADC`` carrying the low-word overflow), the
    exact idiom :class:`Poly1305MACBlock` ships in its MAC cells. MEASURED on
    the real placed+routed+built chip (``verification/tests/
    test_pi_controller.py``, the RESOLUTION gate): at ``Ki*e = 0.030
    LSB/step`` (ki=0.001, e=30 LSB), 1200 steps grow the command by 35 LSB —
    an integral drift of 0.97 LSB against the double-precision reference
    (command-level 1.97 LSB; the extra LSB is the Kp product floor) — while
    the 16-bit-only-accumulator mutant, built and run on the same chip,
    integrates EXACTLY NOTHING (growth 0 LSB for the whole run: the 16-bit
    failure, demonstrated). Contributions down to ``2**-16`` LSB per step
    accumulate; smaller ones truncate (floor).

    Fixed-point parameter mapping (derived internally — INV-0: the user sees
    only ``kp``/``ki``/``limit``):

    * each gain is stored as a Q15 MANTISSA plus a RIGHT-SHIFT count
      ``g = m * 2**-s`` with ``|m|`` normalised into ``[0.5, 1)`` (``s`` up to
      15), so a tiny ``ki`` keeps full mantissa precision;
    * ``p = asr(MULQ(e, kp_m), kp_s)`` — Q15 multiply then arithmetic shift;
    * the integrator increment is computed at FULL precision BEFORE the
      accumulator: ``inc32 = (2 * kim * e) >> ki_s`` as a 32-bit value
      (``MULHI``+``MUL`` product pair, cross-word shift), so the ``ki`` shift
      costs no integral RANGE — ``acc_hi`` spans the full Q15 output scale at
      every ``ki``, and only sub-``2**-16``-LSB contributions are lost.

    Anti-windup (conditional skip on BR flags): the saturation CASE is decided
    where ``u`` is formed (three dispatch entries: in-range / clamped-high /
    clamped-low), and the accumulator cell skips the add when the increment's
    SIGN (the pair's high word, one ``OR``+``BR.N``) points further INTO the
    clamped rail; an increment pointing back OUT of saturation always
    integrates, so the loop unwinds immediately on error reversal.

    DATAPATH (3 cells, serialize-LOCKed per INV-19):

    * ``front`` (landing) — ``p`` term (MULQ + bias-trick ASR), then the
      32-bit increment pair: ``MULHI`` high half FIRST (parked), ``MUL`` low
      half, the ``>> ki_s`` cross-word shift (or the ``<<1`` doubling via
      ``SHL``+``ADC t,t`` at ``ki_s = 0``); forwards ``p`` to ``sat`` and
      ``(inc_hi, inc_lo)`` through ``sat`` to ``acc``.
    * ``sat`` — holds the fed-back integral term (a pinned StateVar, the AGC
      ``ginc`` idiom), forms ``u~ = p + iterm`` (V-flag overflow pinned to the
      rails), clamps to ``+/-limit``, sends ``u`` to ``acc`` and dispatches
      one of three ``acc`` entries (integrate / sat-high / sat-low), then
      LOCKs its arbiter to the ``acc`` face — the next sample is HELD until
      the loop closes (INV-19; the lock tail runs inside the same atomic cell
      execution, so there is no unlocked window).
    * ``acc`` — gates on the increment sign, does the 32-bit add (``ADD`` low,
      flag-preserving ``MOVE``, ``ADC`` high — INV-58 order), writes
      ``acc_hi`` back into ``sat``'s ``iterm`` state (backward @1 on the
      resting face), clears ``sat``'s LOCK inline (backward ``WRITE.CFG @1,
      4`` — the proven pd_pi/AGC-upd structure), then emits ``u`` on the
      dual-FACE tap (``face_tap`` is rewritten to the routed egress direction
      via ``output_face_addr``).

    Hardware deviations (INV-0 — placeKYT-native block, limits stated loudly):
    -------------------------------------------------------------------------
    HW-DEVIATION (Q15 datapath):
      1. ``|kp| < 1`` and ``|ki| < 1`` (Q15 mantissa, right-shift-only
         scaling): values with magnitude >= 1 RAISE. Fold plant scaling into
         the per-unit normalisation instead.
      2. ``0 < limit <= 1`` (Q15 command range); ``limit`` quantizes to
         ``min(32767, round(limit*32768))``.
      3. a nonzero gain whose normalised mantissa still quantizes to 0
         (``|g| < ~2**-30``) RAISES rather than silently running with the
         term dead.
    """

    CATEGORY = "control"
    TAGS = ["pi", "controller", "control_loop", "foc", "motor", "integrator",
            "anti_windup", "control"]

    _interface = BlockInterface(entry_address=1, input_registers=[0],
                                output_registers=[0])

    # INV-22: ``pipeline_lock`` is a BUILD/substrate hint (the INV-19
    # serialize-LOCK), NOT a DSP parameter — the per-sample result is identical
    # with it on or off; only saturated (pipelined) drive needs it.
    GRC_UNSUPPORTED_PARAMS = ("pipeline_lock",)

    def __init__(
        self,
        name: str,
        kp: float = 0.25,
        ki: float = 0.01,
        limit: float = 1.0,
        pipeline_lock: bool = True,
    ):
        """Initialize the PI controller.

        Args:
            name: Block name
            kp: proportional gain (|kp| < 1 — Q15 mantissa + right-shift)
            ki: integral gain per sample (|ki| < 1; tiny values keep full
                mantissa precision via the derived right-shift)
            limit: symmetric output saturation bound, 0 < limit <= 1
            pipeline_lock: engage the INV-19 serialize-LOCK (default True —
                the iterm feedback loop is only saturation-correct locked).
        """
        super().__init__(name, kp=kp, ki=ki, limit=limit)
        self._pipeline_lock = bool(pipeline_lock)
        self._kp = float(kp)
        self._ki = float(ki)
        self._limit = float(limit)

        # HARDWARE DEVIATION (Q15 mantissa + right-shift): gains with |g| >= 1
        # are not representable — RAISE (INV-0: never silently clamp).
        if abs(self._kp) >= 1.0:
            raise ValueError(
                f"HARDWARE LIMIT: pi_controller kp={kp} outside (-1, 1) — the "
                f"Q15 mantissa + right-shift scaling covers |kp| < 1 only. "
                f"Fold plant scaling into the per-unit normalisation.")
        if abs(self._ki) >= 1.0:
            raise ValueError(
                f"HARDWARE LIMIT: pi_controller ki={ki} outside (-1, 1) — the "
                f"Q15 mantissa + right-shift scaling covers |ki| < 1 only.")
        if not (0.0 < self._limit <= 1.0):
            raise ValueError(
                f"HARDWARE LIMIT: pi_controller limit={limit} outside (0, 1] "
                f"— the Q15 command word cannot exceed full scale, and a "
                f"non-positive limit pins the output at a rail.")

        self._kp_m, self._kp_s = self._mant_shift(self._kp, "kp")
        self._ki_m, self._ki_s = self._mant_shift(self._ki, "ki")
        self._limit_q15 = min(32767, int(round(self._limit * 32768.0)))

    @staticmethod
    def _mant_shift(g: float, label: str) -> Tuple[int, int]:
        """(q15_mantissa, right_shift) with |mantissa| normalised into
        [0.5, 1) — ``g = (mantissa/32768) * 2**-shift``. Shift caps at 15 (the
        ISA immediate shift field)."""
        if g == 0.0:
            return 0, 0
        s = 0
        a = abs(g)
        while a < 0.5 and s < 15:
            a *= 2.0
            s += 1
        m = g * (2.0 ** s)
        q = max(-32768, min(32767, int(round(m * 32768.0))))
        if q == 0:
            raise ValueError(
                f"HARDWARE LIMIT: pi_controller {label}={g} quantizes to 0 "
                f"even at the maximum right-shift (|{label}| < ~2**-30) — the "
                f"term would be silently dead. Use 0 explicitly if that is "
                f"intended.")
        return q, s

    # ------------------------------------------------------------------ props
    @property
    def cell_count(self) -> int:
        return 3

    @property
    def interface(self) -> BlockInterface:
        return self._interface

    @property
    def kp(self) -> float:
        return self._kp

    @property
    def ki(self) -> float:
        return self._ki

    @property
    def limit(self) -> float:
        return self._limit

    @property
    def kp_mantissa_q15(self) -> int:
        return self._kp_m

    @property
    def kp_shift(self) -> int:
        return self._kp_s

    @property
    def ki_mantissa_q15(self) -> int:
        return self._ki_m

    @property
    def ki_shift(self) -> int:
        return self._ki_s

    @property
    def limit_q15(self) -> int:
        return self._limit_q15

    @property
    def kp_effective(self) -> float:
        """The gain the chip actually applies (quantized mantissa * 2**-shift)."""
        return (_s16(self._kp_m) / 32768.0) * (2.0 ** -self._kp_s)

    @property
    def ki_effective(self) -> float:
        return (_s16(self._ki_m) / 32768.0) * (2.0 ** -self._ki_s)

    def output_face_addr(self) -> Any:
        """``acc`` is a DUAL-FACE output cell: its resting face carries the
        iterm feedback + lock-clear (north, into ``sat``); the external ``out``
        WRITE fires on an in-program ``face_tap`` flip. Declare the face word's
        address so the build rewrites it to the routed egress direction."""
        return 4  # DataWord("face_tap") in the acc cell

    # ------------------------------------------------------- cell programs
    def build_cell_programs(self) -> Dict[str, CellProgram]:
        kp_m = self._kp_m & 0xFFFF
        ki_m = self._ki_m & 0xFFFF
        lim = self._limit_q15 & 0xFFFF
        nlim = (-self._limit_q15) & 0xFFFF

        # --- front: p term + the 32-bit increment pair -----------------------
        # p = asr(MULQ(e, kp_m), kp_s) via the bias trick:
        #   asr(x, s) == ((x ^ 0x8000) >> s) - (0x8000 >> s)   (logical SHR)
        # (identity at s == 0, so the template is uniform in kp_s).
        # inc32 = (2 * ki_m * e) >> ki_s at FULL precision:
        #   ki_s == 0 : (hi:lo) << 1  — SHL lo (carry out), ADC t,t (carry in)
        #   ki_s == 1 : (hi:lo) unchanged (2*p >> 1 == p)
        #   ki_s >= 2 : 32-bit ASR by J = ki_s - 1 (cross-word OR + bias trick)
        # INV-58: MULHI (an ALU op) runs FIRST and its result is PARKED, so no
        # later flag-setting op sits between the low-word math and its carry
        # consumer.
        ki_s = self._ki_s
        if ki_s == 0:
            inc_block = """\
    MULHI R{state:es}, R{data:kim}
    MOVE R{state:th}, R0
    MUL R{state:es}, R{data:kim}
    SHL R0, #1
    {write:lo}
    ADC R{state:th}, R{state:th}
    {write:hi}
"""
        elif ki_s == 1:
            inc_block = """\
    MULHI R{state:es}, R{data:kim}
    MOVE R{state:th}, R0
    MUL R{state:es}, R{data:kim}
    {write:lo}
    MOVE R0, R{state:th}
    {write:hi}
"""
        else:
            j = ki_s - 1  # 1..14
            inc_block = f"""\
    MULHI R{{state:es}}, R{{data:kim}}
    MOVE R{{state:th}}, R0
    MUL R{{state:es}}, R{{data:kim}}
    SHR R0, #{j}
    MOVE R{{state:tl}}, R0
    SHL R{{state:th}}, #{16 - j}
    OR R0, R{{state:tl}}
    {{write:lo}}
    XOR R{{state:th}}, R{{data:pbias}}
    SHR R0, #{j}
    SUB R0, R{{data:jbias}}
    {{write:hi}}
"""
        front_data = [
            DataWord("kpm", kp_m, address=1),
            DataWord("pbias", 0x8000, address=2),
            DataWord("pkbias", (0x8000 >> self._kp_s) & 0xFFFF, address=3),
            DataWord("kim", ki_m, address=4),
        ]
        front_state = [StateVar("es", register=6), StateVar("th", register=7)]
        if ki_s >= 2:
            front_data.append(
                DataWord("jbias", (0x8000 >> (ki_s - 1)) & 0xFFFF, address=5))
            front_state.append(StateVar("tl", register=8))
        front = CellProgram(
            inputs=[Port("sample", register=0)],
            outputs=[Port("p"), Port("hi"), Port("lo"), Port("trig")],
            entries=[EntryPoint("default")],
            data=front_data,
            state=front_state,
            assembly_template=(
                """\
start:
    MOVE R{state:es}, R0
    MULQ R{state:es}, R{data:kpm}
    XOR R0, R{data:pbias}
    SHR R0, #""" + str(self._kp_s) + """
    SUB R0, R{data:pkbias}
    {write:p}
""" + inc_block + """\
    {jump:trig}
"""),
        )

        # --- sat: u~ = p + iterm, V-pinned; clamp; dispatch; serialize-LOCK --
        # iterm is a PINNED STATE register (never an input Port): the acc->sat
        # feedback WRITE resolves to it by name, and a port-input broker can
        # never relay a stale operand into it (the AGC ginc lesson). Every
        # always-taken branch below is flag-proven:
        #   BR.GE @9  — SLT clear (the BR.LT at @6 fell through);
        #   BR.LT @14 — SLT set on BOTH entries (CMP took it, or ovf with
        #               N=0,V=1 so SLT=N^V=1);
        #   WRITE/JUMP/MOVE never touch flags.
        # The LOCK tail runs INSIDE the same atomic cell execution on every
        # path (all three converge on ``lock``), so there is no window in
        # which the next sample's p/hi/lo/trigger can slip past: they are
        # held on the WEST arbiter until acc's backward WRITE.CFG clears the
        # lock, while acc's own traffic enters on the SOUTH lock face.
        lock_data = ([DataWord("lock_face", 0, address=4, is_face=True),
                      DataWord("one", 1, address=5)]
                     if self._pipeline_lock else [])
        lock_tail = ("""\
lock:
    MOVE R0, R{data:lock_face}
    MOVE [LOCK_FACE], R0
    MOVE R0, R{data:one}
    MOVE [LOCK], R0
""" if self._pipeline_lock else """\
lock:
    HALT
""")
        sat = CellProgram(
            inputs=[Port("p", register=1)],
            outputs=[Port("u_f"), Port("j_int"), Port("j_hi"), Port("j_lo")],
            entries=[EntryPoint("default")],
            data=[DataWord("lim", lim, address=2),
                  DataWord("nlim", nlim, address=3)] + lock_data,
            # LOOP MEMORY: the integral term at Q15 scale (acc_hi), written
            # back by acc each sample; reset per batch = a fresh burst starts
            # with a zero integrator (cold-start semantics).
            state=[StateVar("iterm", register=6, reset_per_batch=True)],
            assembly_template="""\
start:
    ADD R{in:p}, R{state:iterm}
    BR.V ovf
    CMP R{data:lim}, R0
    BR.LT vpos
    CMP R0, R{data:nlim}
    BR.LT vneg
    {write:u_f}
    {jump:j_int}
    BR.GE lock
ovf:
    BR.N vpos
vneg:
    MOVE R0, R{data:nlim}
    {write:u_f}
    {jump:j_lo}
    BR.LT lock
vpos:
    MOVE R0, R{data:lim}
    {write:u_f}
    {jump:j_hi}
""" + lock_tail,
        )

        # --- acc: the 32-bit accumulator + anti-windup gate + emit + unlock --
        # Entries: sat-high / sat-low test the increment SIGN (the pair's high
        # word: OR sets N) and skip the add when it points further INTO the
        # clamped rail; in-range always integrates. The add is INV-58-ordered:
        # ADD low, flag-preserving MOVE, ADC high. The face choreography:
        # face_n re-asserted at the head of the shared tail (undoing the
        # previous sample's face_tap flip) so the iterm WRITE and the
        # WRITE.CFG ride the resting north face into sat; the external pair
        # rides the face_tap flip (rewritten to the routed egress via
        # output_face_addr); {write:out}/{jump:u_trig} stay the
        # HIGHEST-addressed WRITE/JUMP so the route patch targets them and
        # never the lock-clear (INV-63).
        acc = CellProgram(
            inputs=[Port("u", register=1), Port("hi", register=2),
                    Port("lo", register=3)],
            outputs=[Port("iterm"), Port("out"), Port("u_trig")],
            entries=[EntryPoint("sathi"), EntryPoint("satlo"),
                     EntryPoint("integ")],
            data=[DataWord("face_tap", 3, address=4, is_face=True),
                  DataWord("face_n", 3, address=5, is_face=True)],
            # LOOP MEMORY: the 32-bit accumulator pair; reset per batch with
            # the iterm mirror in sat (fresh burst = zero integrator).
            state=[StateVar("acch", register=6, reset_per_batch=True),
                   StateVar("accl", register=7, reset_per_batch=True)],
            assembly_template="""\
sathi:
    OR R{in:hi}, R{in:hi}
    BR.N integ
    BR.NN post
satlo:
    OR R{in:hi}, R{in:hi}
    BR.N post
integ:
    ADD R{in:lo}, R{state:accl}
    MOVE R{state:accl}, R0
    ADC R{in:hi}, R{state:acch}
    MOVE R{state:acch}, R0
post:
    MOVE [FACE], R{data:face_n}
    MOVE R0, R{state:acch}
    {write:iterm}
    SUB R0, R0
    WRITE.CFG @1, 4
    MOVE [FACE], R{data:face_tap}
    MOVE R0, R{in:u}
    {write:out}
    {jump:u_trig}
""",
        )

        return {"front": front, "sat": sat, "acc": acc}

    # ------------------------------------------------------------- wiring
    def internal_connections(self) -> List[Tuple[Any, str, Any, str]]:
        return [
            ("front", "p", "sat", "p"),
            ("front", "hi", "acc", "hi"),
            ("front", "lo", "acc", "lo"),
            ("sat", "u_f", "acc", "u"),
            # FEEDBACK: the integral term, acc -> sat (loop closure; lands in
            # the pinned iterm STATE register by name).
            ("acc", "iterm", "sat", "iterm"),
        ]

    def internal_jumps(self) -> List[Tuple[Any, str, Any, str]]:
        return [
            ("front", "trig", "sat", "default"),
            ("sat", "j_int", "acc", "integ"),
            ("sat", "j_hi", "acc", "sathi"),
            ("sat", "j_lo", "acc", "satlo"),
            # acc's external output trigger; unconsumed it terminates, a route
            # retargets it (_patch_last_jump_handoff).
            ("acc", "u_trig", "__terminate__", "default"),
        ]

    def default_layout(self) -> Dict[Any, Tuple[int, int, str]]:
        """2x2 L-fold, input at (0,0) on the top edge::

            front(E)  sat(S)
              .       acc(N)

        Forward trace: front -> sat (p + trigger, east @1); front -> acc
        (inc_hi/inc_lo, east @2 TRANSITING sat southward on sat's resting
        face — sat never flips, and while sat is LOCKED the west arbiter
        holds them, so they only transit an idle south-facing cell);
        sat -> acc (u + the dispatch jump, south @1). Feedback: acc -> sat
        (iterm + the WRITE.CFG lock-clear) is the final @1 NORTH hop on
        acc's RESTING face — the QPSK-Costas pd_pi->phase / AGC upd->hold
        shape, no transit cell, no flip (INV-64 §2 does not bite). The
        unlock enters sat on its SOUTH face (= lock_face); the next
        sample's traffic enters on the WEST face and is held. acc is the
        exit cell (dual-FACE tap at the block corner, both non-block faces
        free for the egress route). Positional pairing (INV-33): dict order
        == build_cell_programs order."""
        return {
            "front": (0, 0, "east"),
            "sat": (1, 0, "south"),
            "acc": (1, 1, "north"),
        }

    # -------------------------------------------------------------- reference
    def process_reference_q15(self, input_q15) -> List[int]:
        """Bit-exact predictor of the on-chip datapath. Every operation mirrors
        the shipped cell programs: MULQ truncation (floor), the bias-trick ASR,
        the full-precision 32-bit increment ``(2*ki_m*e) >> ki_s``, the V-pinned
        16-bit add, strict-inequality clamps, the sign-gated (anti-windup)
        wrapping 32-bit accumulate, and ``iterm = acc_hi``. Returns one uint16
        command word per input sample."""
        kp_m = _s16(self._kp_m)
        ki_m = _s16(self._ki_m)
        lim = self._limit_q15
        acc = 0            # 32-bit accumulator, unsigned representation
        out: List[int] = []
        for w in input_q15:
            e = _s16(int(w) & 0xFFFF)
            # front: p = asr(MULQ(e, kp_m), kp_s)
            p_raw = _s16(((kp_m * e) >> 15) & 0xFFFF)
            p = p_raw >> self._kp_s          # python >> on signed int == ASR
            # front: inc32 = (2 * ki_m * e) >> ki_s  (exact, sign-preserving)
            inc = (2 * ki_m * e) >> self._ki_s
            inc_u = inc & 0xFFFFFFFF
            inc_hi = _s16((inc_u >> 16) & 0xFFFF)
            # sat: u~ = p + iterm with 16-bit V pinning, then strict clamps
            iterm = _s16((acc >> 16) & 0xFFFF)
            usum = p + iterm
            wrapped = _s16(usum & 0xFFFF)
            if usum != wrapped:              # V flag: pin to the true-sign rail
                case = "hi" if usum > 0 else "lo"
                u = lim if usum > 0 else -lim
            elif wrapped > lim:
                case, u = "hi", lim
            elif wrapped < -lim:
                case, u = "lo", -lim
            else:
                case, u = None, wrapped
            # acc: anti-windup gate on the increment's high-word sign
            if case is None:
                do_int = True
            elif case == "hi":
                do_int = inc_hi < 0          # unwinding is always allowed
            else:
                do_int = inc_hi >= 0
            if do_int:
                acc = (acc + inc_u) & 0xFFFFFFFF
            out.append(u & 0xFFFF)
        return out

    def process_reference(self, input_samples: np.ndarray) -> np.ndarray:
        """Double-precision reference of the IDENTICAL discretization (same
        order of operations, same strict saturation points, same conditional-
        skip anti-windup), run at the chip's QUANTIZED constants — the
        regime-mirrored golden law. Float error in -> float command out."""
        arr = np.asarray(input_samples, dtype=np.float64)
        kp = self.kp_effective
        ki = self.ki_effective
        lim = self._limit_q15 / 32768.0
        i_term = 0.0
        out = np.empty(len(arr), dtype=np.float64)
        for n, e in enumerate(arr):
            u_un = kp * float(e) + i_term
            if u_un > lim:
                u = lim
            elif u_un < -lim:
                u = -lim
            else:
                u = u_un
            d = ki * float(e)
            if (u_un > lim and d > 0.0) or (u_un < -lim and d < 0.0):
                pass                          # anti-windup: skip
            else:
                i_term += d
            out[n] = u
        return out.astype(np.float32)

    def reset(self):
        pass
