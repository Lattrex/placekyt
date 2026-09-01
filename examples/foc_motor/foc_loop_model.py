# SPDX-License-Identifier: GPL-3.0-or-later
"""The CLOSED FOC loop, host side: the PMSM plant and the whole-loop golden.

This module is what makes ``foc_motor.grc`` a LOOP rather than a chain. It
holds two things, and nothing else:

  * ``PMSMPlant`` — the motor. Duties in, (ia, ib, theta) out. This is the
    part that is NOT on the chip and never will be: it is the physical
    machine the controller drives.
  * ``foc_loop_golden`` — one whole control iteration composed from the
    blocks' OWN pinned integer models, so the host reference and the chip
    agree bit for bit at every stage that IS on the chip.

The two together close the loop, which is the only way to answer "does this
controller actually regulate a motor" without a motor.

THE SIGNAL CHAIN, in the order this module evaluates it
-------------------------------------------------------
::

    ia, ib ──> Clarke ──────> (i_alpha, i_beta)
                                     │
                        theta ──> CordicRotate(sign=-1)  [forward Park]
                                     │
                               (i_d, i_q)          the MEASUREMENT half
    ----------------------------------------------------------------------
    e_d = i_d_ref - i_d ; e_q = i_q_ref - i_q       formed on the HOST
    ----------------------------------------------------------------------
    e_d ──> PI(d) ──> v_d ┐
                          ├──> CordicRotate(sign=+1) ──> SVPWM ──> a, b, c
    e_q ──> PI(q) ──> v_q ┘       [inverse Park]        the COMMAND half
                        theta ──────^

The measurement half and the command half are strictly SERIAL within one
sample: the duties for sample k cannot be computed until sample k's currents
have been measured and rotated. That serialization is why the whole loop's
period is the sum of the two halves' periods and not the larger of them.

THE ANGLE CONVENTION
--------------------
``theta`` is the rotor ELECTRICAL angle in the shipped 16-bit half-turn Q15
form: ``word/32768 * pi`` radians. The full circle is 65536 counts, so plain
16-bit wrap IS arithmetic mod 2*pi and there is no seam at +/-pi. Every
angle in this module is carried as that 16-bit word.

THE DISCRETIZATION, stated plainly
----------------------------------
The plant is a surface-PMSM (Ld == Lq == L) in the stationary two-phase frame,
integrated by FORWARD EULER at the control period ``dt``:

    d(i_alpha)/dt = (v_alpha - R*i_alpha - e_alpha) / L
    d(i_beta )/dt = (v_beta  - R*i_beta  - e_beta ) / L

with the back-EMF of a sinusoidal machine

    e_alpha = -ke * omega_e * sin(theta_e)
    e_beta  = +ke * omega_e * cos(theta_e)

and the electrical angle advancing at a CONSTANT electrical speed,

    theta_e(k+1) = theta_e(k) + omega_e * dt.

Forward Euler is chosen deliberately: it is the same explicit one-step update
the controller itself is discretized with, it needs no matrix exponential to
read, and at ``dt`` = 35 us against an electrical time constant L/R = 4.3 ms
it is ~0.8% of a time constant per step — comfortably inside its stability
region and accurate enough that the settle this module demonstrates is a
property of the CONTROLLER, not of the integrator.

Held constant, and honestly so: the mechanical dynamics. ``omega_e`` does not
respond to the torque the loop produces. A current loop closes 2-3 orders of
magnitude faster than the mechanical pole of any real machine, so over the
few hundred electrical samples a current-loop settle takes, constant speed is
the standard and correct modelling assumption. This module models the CURRENT
loop; it is not a drivetrain simulator.

The inverter is modelled as ideal: the duty cycles the SVPWM emits are taken
as the applied phase voltages, scaled by ``v_dc``. No dead time, no device
drop, no switching ripple. Those matter to a real drive's current THD; they
do not change whether the regulator regulates.

Everything the CHIP does is bit-exact Q15 here. Everything the MOTOR does is
double-precision floating point. That split is deliberate: the plant is
physics and does not have a word width.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import List, Sequence, Tuple

__all__ = [
    "PMSMPlant", "MotorParams", "q15", "from_q15", "wrap16",
    "measurement_half", "command_half", "foc_loop_golden", "run_closed_loop",
    "StatefulPI", "DEFAULT_PI", "I_D_REF", "SAMPLE_PERIOD_S",
]

# The control period the flowgraph is documented at. 35 us == 28.6 kHz, the
# rate the whole loop is expected to sustain (the command half alone measured
# 55.8 kHz; the two halves are serial within a sample, so the whole loop is
# roughly half that). See README.md.
SAMPLE_PERIOD_S = 35e-6

# The PI tuning. Both axes share it: on a surface PMSM Ld == Lq, so the two
# current loops are the identical plant and the symmetric tuning is correct.
DEFAULT_PI = {"kp": 0.25, "ki": 0.01, "limit": 1.0, "pipeline_lock": True}

# The d-axis current reference. ZERO for a surface PMSM: the magnets already
# supply the rotor flux, so any d-axis current is pure loss. (Field weakening
# above base speed drives it negative; that is a different operating region
# and not what this example demonstrates.)
I_D_REF = 0.0


# --------------------------------------------------------------------------- #
#  Q15 helpers                                                                 #
# --------------------------------------------------------------------------- #

def q15(x: float) -> int:
    """Float in [-1, 1) -> a Q15 WORD (uint16), saturating at the rails."""
    v = int(math.floor(float(x) * 32768.0 + 0.5))
    return max(-32768, min(32767, v)) & 0xFFFF


def from_q15(w: int) -> float:
    """A Q15 word -> its float value in [-1, 1)."""
    w = int(w) & 0xFFFF
    return (w - 0x10000 if w >= 0x8000 else w) / 32768.0


def wrap16(w: int) -> int:
    return int(w) & 0xFFFF


def _s16(w: int) -> int:
    w = int(w) & 0xFFFF
    return w - 0x10000 if w >= 0x8000 else w


# --------------------------------------------------------------------------- #
#  The two chip halves, as pure functions over WORDS                           #
# --------------------------------------------------------------------------- #

def measurement_half(ia_words: Sequence[int], ib_words: Sequence[int],
                     theta_words: Sequence[int]) -> List[Tuple[int, int]]:
    """Clarke -> forward Park. N (ia, ib, theta) word triples in, N (i_d, i_q)
    word pairs out.

    Bit-exact: ``ClarkeTransformBlock.process_reference_words`` and
    ``cordic_rotate_word`` are the blocks' OWN pinned integer models, so this
    is a contract against the chip, not an approximation of it."""
    from gr_kyttar.placement.blocks.clarke_transform_block import ClarkeTransformBlock
    from gr_kyttar.placement.blocks.cordic_rotate_block import cordic_rotate_word

    ab = ClarkeTransformBlock.process_reference_words(list(ia_words), list(ib_words))
    out: List[Tuple[int, int]] = []
    for k in range(len(ab) // 2):
        i_alpha, i_beta = ab[2 * k], ab[2 * k + 1]
        # sign = -1 is the FORWARD Park: rotate the measured stationary-frame
        # current vector by -theta into the rotor frame.
        i_d, i_q = cordic_rotate_word(i_alpha, i_beta, theta_words[k], -1)
        out.append((wrap16(i_d), wrap16(i_q)))
    return out


def command_half(e_d_words: Sequence[int], e_q_words: Sequence[int],
                 theta_words: Sequence[int], pi_params=None) -> List[int]:
    """PI(d), PI(q) -> inverse Park -> SVPWM. N error triples in, 3N duty
    words out (a, b, c per sample).

    This is exactly ``foc_motor_demo.golden`` — the command half that is on
    the shipped ``.kyt`` — re-exported here so the closed loop composes the
    two halves from one place.

    The PI integrators EVOLVE across samples, so the WHOLE sequence must be
    passed in one call; a per-sample call would cold-start each accumulator
    and disagree with the chip."""
    from gr_kyttar.placement.blocks.cordic_rotate_block import cordic_rotate_word
    from gr_kyttar.placement.blocks.pi_controller_block import PIControllerBlock
    from svpwm_golden import svpwm_duties

    p = dict(pi_params or DEFAULT_PI)
    v_d = PIControllerBlock("d", **p).process_reference_q15(list(e_d_words))
    v_q = PIControllerBlock("q", **p).process_reference_q15(list(e_q_words))
    duties: List[int] = []
    for k in range(len(theta_words)):
        v_alpha, v_beta = cordic_rotate_word(v_d[k], v_q[k], theta_words[k], 1)
        duties.extend(svpwm_duties(v_alpha, v_beta))
    return [d & 0xFFFF for d in duties]


# --------------------------------------------------------------------------- #
#  The motor                                                                   #
# --------------------------------------------------------------------------- #

@dataclass
class MotorParams:
    """A small surface-PMSM, in SI units.

    The defaults are an ordinary low-voltage servo motor: a few hundred
    milliohms of phase resistance, a fraction of a millihenry of phase
    inductance, and a back-EMF constant that puts the machine's rated speed
    inside a 24 V bus. They are chosen to be REPRESENTATIVE, not to model any
    specific part."""
    r_s: float = 0.35            # phase resistance, ohm
    l_s: float = 1.5e-3          # phase inductance, H (Ld == Lq, surface magnet)
    ke: float = 0.035            # back-EMF constant, V.s/rad (electrical)
    v_dc: float = 24.0           # DC bus, V
    i_base: float = 10.0         # the Q15 full-scale current, A
    pole_pairs: int = 4

    @property
    def tau_e(self) -> float:
        """The electrical time constant L/R, s."""
        return self.l_s / self.r_s


@dataclass
class PMSMPlant:
    """The motor, integrated by forward Euler at ``dt``.

    ``step(duty_a, duty_b, duty_c)`` takes one SVPWM duty packet (three Q15
    WORDS, the chip's own output) and returns the next ``(ia_word, ib_word,
    theta_word)`` — the three quantities a real drive senses and feeds back.
    So the plant speaks exactly the flowgraph's wire format on both sides:
    words in, words out.

    STATE: the two stationary-frame currents (SI amperes) and the electrical
    angle (radians, wrapped). Currents are floats because the motor is
    physics; only the interface is Q15.
    """
    params: MotorParams = field(default_factory=MotorParams)
    dt: float = SAMPLE_PERIOD_S
    omega_e: float = 200.0       # electrical speed, rad/s (held constant)
    i_alpha: float = 0.0
    i_beta: float = 0.0
    theta_e: float = 0.0

    # ----- the interface, in WORDS ----------------------------------------- #

    def step(self, duty_a_w: int, duty_b_w: int, duty_c_w: int
             ) -> Tuple[int, int, int]:
        """One control period. Duty WORDS in, sensed (ia, ib, theta) WORDS out."""
        da = from_q15(duty_a_w)
        db = from_q15(duty_b_w)
        dc = from_q15(duty_c_w)
        self.step_si(da, db, dc)
        return self.sensed_words()

    def sensed_words(self) -> Tuple[int, int, int]:
        """What the drive's sensors report, as Q15 words.

        The two shunt currents are scaled by ``i_base`` (the Q15 full scale)
        and the angle by the half-turn convention. This is the ADC + encoder
        model, and it is where the plant's SI floats become chip words."""
        ia, ib, _ic = self.phase_currents()
        p = self.params
        th = self.theta_e % (2.0 * math.pi)
        if th >= math.pi:
            th -= 2.0 * math.pi          # fold to [-pi, pi) for the Q15 map
        return (q15(ia / p.i_base), q15(ib / p.i_base), q15(th / math.pi))

    # ----- the physics, in SI ---------------------------------------------- #

    def phase_currents(self) -> Tuple[float, float, float]:
        """(ia, ib, ic) in amperes, from the stationary two-phase state.

        The INVERSE of the amplitude-invariant two-current Clarke the chip
        runs forward: ia = i_alpha, and i_beta = (ia + 2*ib)/sqrt(3) inverts
        to ib = (sqrt(3)*i_beta - i_alpha)/2. ic closes the star."""
        ia = self.i_alpha
        ib = (math.sqrt(3.0) * self.i_beta - ia) / 2.0
        return (ia, ib, -(ia + ib))

    def back_emf(self) -> Tuple[float, float]:
        """(e_alpha, e_beta) in volts — a sinusoidal machine's back-EMF."""
        p = self.params
        amp = p.ke * self.omega_e
        return (-amp * math.sin(self.theta_e), amp * math.cos(self.theta_e))

    def step_si(self, duty_a: float, duty_b: float, duty_c: float) -> None:
        """Advance the state one period from three duty cycles in [-1, 1].

        The duties are the SVPWM output: three phase voltages with the
        common-mode midpoint already injected. Common mode does not drive
        current in an isolated-neutral machine, so the forward Clarke of the
        duty set recovers exactly the (v_alpha, v_beta) the winding sees, and
        the injection cancels out here as it does in the motor. Multiplying
        by v_dc turns duty into volts."""
        p = self.params
        # forward Clarke of the duty set (amplitude invariant, three-current
        # form — the duties are a balanced-plus-common-mode set).
        v_alpha = (2.0 * duty_a - duty_b - duty_c) / 3.0 * p.v_dc
        v_beta = (duty_b - duty_c) / math.sqrt(3.0) * p.v_dc

        e_alpha, e_beta = self.back_emf()
        # Forward Euler on  L di/dt = v - R i - e.
        di_alpha = (v_alpha - p.r_s * self.i_alpha - e_alpha) / p.l_s
        di_beta = (v_beta - p.r_s * self.i_beta - e_beta) / p.l_s
        self.i_alpha += di_alpha * self.dt
        self.i_beta += di_beta * self.dt
        self.theta_e = (self.theta_e + self.omega_e * self.dt) % (2.0 * math.pi)

    # ----- what the loop is regulating ------------------------------------- #

    def dq_currents(self) -> Tuple[float, float]:
        """(i_d, i_q) in amperes — the true rotor-frame currents, in SI.

        This is the plant's own view, computed in double precision. It exists
        for the settle test to check against; the CONTROLLER never sees it
        (it sees the chip's Q15 measurement half instead)."""
        c, s = math.cos(self.theta_e), math.sin(self.theta_e)
        return (self.i_alpha * c + self.i_beta * s,
                -self.i_alpha * s + self.i_beta * c)


# --------------------------------------------------------------------------- #
#  A SAMPLE-AT-A-TIME PI, because a closed loop cannot be batched              #
# --------------------------------------------------------------------------- #

class StatefulPI:
    """``PIControllerBlock``'s Q15 datapath with the accumulator held ACROSS
    calls, so it can be stepped one sample at a time.

    WHY THIS EXISTS, precisely
    --------------------------
    ``PIControllerBlock.process_reference_q15`` is a BATCH function: its 32-bit
    accumulator is a local, initialised to zero on entry and discarded on
    return. Feed it a whole error sequence and it is exactly the chip. Feed it
    one sample per call — which is the only thing a CLOSED loop can do, since
    sample k's error is not known until sample k-1's duties have moved the
    motor — and the integrator is reset every step, silently. The integral
    action then does not exist: the loop runs proportional-only, settles short
    of its reference, and (the tell) changing ``ki`` changes NOTHING.

    So this class carries ``acc`` as instance state. The arithmetic below is
    transcribed operation for operation from the block's own model — the
    truncating MULQ, the bias-trick ASR, the full-precision 32-bit increment,
    the V-pinned 16-bit add, the strict-inequality clamps, and the sign-gated
    anti-windup. ``assert_matches_block_model`` proves the transcription is
    faithful by running both over the same batch and comparing word for word;
    the test suite gates it, so this cannot drift from the block.
    """

    def __init__(self, name: str, **params):
        from gr_kyttar.placement.blocks.pi_controller_block import PIControllerBlock
        self._blk = PIControllerBlock(name, **params)
        self.acc = 0                      # the 32-bit integrator, PERSISTENT

    def step(self, e_word: int) -> int:
        """One error word in, one command word out. Advances the integrator."""
        b = self._blk
        kp_m, ki_m, lim = _s16(b._kp_m), _s16(b._ki_m), b._limit_q15
        e = _s16(int(e_word) & 0xFFFF)

        p_raw = _s16(((kp_m * e) >> 15) & 0xFFFF)
        p = p_raw >> b._kp_s
        inc = (2 * ki_m * e) >> b._ki_s
        inc_u = inc & 0xFFFFFFFF
        inc_hi = _s16((inc_u >> 16) & 0xFFFF)

        iterm = _s16((self.acc >> 16) & 0xFFFF)
        usum = p + iterm
        wrapped = _s16(usum & 0xFFFF)
        if usum != wrapped:                      # V flag: pin to the true rail
            case = "hi" if usum > 0 else "lo"
            u = lim if usum > 0 else -lim
        elif wrapped > lim:
            case, u = "hi", lim
        elif wrapped < -lim:
            case, u = "lo", -lim
        else:
            case, u = None, wrapped

        if case is None:
            do_int = True
        elif case == "hi":
            do_int = inc_hi < 0                  # unwinding is always allowed
        else:
            do_int = inc_hi >= 0
        if do_int:
            self.acc = (self.acc + inc_u) & 0xFFFFFFFF
        return u & 0xFFFF

    @staticmethod
    def assert_matches_block_model(errors: Sequence[int], **params) -> None:
        """Prove the transcription: stepping this class sample by sample must
        equal the block's own batch model word for word."""
        from gr_kyttar.placement.blocks.pi_controller_block import PIControllerBlock
        batch = PIControllerBlock("chk", **params).process_reference_q15(list(errors))
        pi = StatefulPI("chk", **params)
        stepped = [pi.step(w) for w in errors]
        if stepped != batch:
            raise AssertionError(
                f"StatefulPI has drifted from PIControllerBlock: "
                f"{[hex(w) for w in stepped[:8]]} != {[hex(w) for w in batch[:8]]}")


# --------------------------------------------------------------------------- #
#  One whole loop iteration, and a closed-loop run                             #
# --------------------------------------------------------------------------- #

def foc_loop_golden(ia_w: int, ib_w: int, theta_w: int,
                    i_d_ref_w: int, i_q_ref_w: int,
                    pi_d, pi_q) -> Tuple[int, int, int, int, int]:
    """ONE whole control iteration, bit-exact for every stage that is on chip.

    Takes the three sensed words and the two references; returns
    ``(duty_a, duty_b, duty_c, i_d, i_q)``.

    ``pi_d`` and ``pi_q`` are LIVE ``StatefulPI`` instances, passed in rather
    than constructed here, because their 32-bit integrators carry state across
    samples — that state IS the integral action, and reconstructing the
    controller per sample would silently delete it (see ``StatefulPI``).
    """
    from gr_kyttar.placement.blocks.cordic_rotate_block import cordic_rotate_word
    from svpwm_golden import svpwm_duties

    # --- measurement half (on chip: Clarke + CordicRotate sign=-1) ---------- #
    (i_d, i_q) = measurement_half([ia_w], [ib_w], [theta_w])[0]

    # --- error formation (on the HOST, by construction) -------------------- #
    e_d = _sat16(_s16(i_d_ref_w) - _s16(i_d)) & 0xFFFF
    e_q = _sat16(_s16(i_q_ref_w) - _s16(i_q)) & 0xFFFF

    # --- command half (on chip: PI x2 + CordicRotate sign=+1 + SVPWM) ------ #
    v_d = pi_d.step(e_d)
    v_q = pi_q.step(e_q)
    v_alpha, v_beta = cordic_rotate_word(v_d, v_q, theta_w, 1)
    da, db, dc = svpwm_duties(v_alpha, v_beta)
    return (da & 0xFFFF, db & 0xFFFF, dc & 0xFFFF, i_d, i_q)


def _sat16(v: int) -> int:
    return max(-32768, min(32767, int(v)))


def run_closed_loop(n_steps: int = 400, i_q_ref: float = 0.30,
                    plant: PMSMPlant | None = None, pi_params=None):
    """Run the WHOLE loop closed, host-only, for ``n_steps`` control periods.

    ``i_q_ref`` is the torque command as a Q15 FRACTION of full-scale current
    (0.30 == 3 A with the default ``i_base`` = 10 A). The d-axis reference is
    ``I_D_REF`` = 0.

    Returns a dict of per-step traces (all Q15 words except the SI entries),
    which is what the settle gate asserts on and what the README quotes."""
    p = dict(pi_params or DEFAULT_PI)
    pi_d = StatefulPI("d", **p)
    pi_q = StatefulPI("q", **p)
    plant = plant or PMSMPlant()

    ref_d_w = q15(I_D_REF)
    ref_q_w = q15(i_q_ref)

    tr = {"i_d": [], "i_q": [], "duty": [], "i_d_si": [], "i_q_si": [],
          "theta": [], "ref_d": ref_d_w, "ref_q": ref_q_w}

    ia_w, ib_w, th_w = plant.sensed_words()
    for _ in range(n_steps):
        da, db, dc, i_d, i_q = foc_loop_golden(
            ia_w, ib_w, th_w, ref_d_w, ref_q_w, pi_d, pi_q)
        tr["i_d"].append(i_d)
        tr["i_q"].append(i_q)
        tr["duty"].append((da, db, dc))
        tr["theta"].append(th_w)
        ia_w, ib_w, th_w = plant.step(da, db, dc)
        sid, siq = plant.dq_currents()
        tr["i_d_si"].append(sid)
        tr["i_q_si"].append(siq)
    return tr
