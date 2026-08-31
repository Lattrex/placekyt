# SPDX-License-Identifier: GPL-3.0-or-later
"""CordicRotateBlock — general vector rotation by a STREAMED angle.

Rotation-mode CORDIC on the machinery :mod:`cordic_blocks` shipped and proved
in VECTORING mode (same 14-iteration count, same atan(2^-i)/pi Q15 table, same
1/4 prescale, same wrapping half-turn z arithmetic, same 1/K compensation):

    x' = x*cos(s*theta) - y*sin(s*theta)
    y' = x*sin(s*theta) + y*cos(s*theta)

with ``s`` (+1 or -1) a boot parameter — ONE block class covers a motor-control
Park transform (s = -1), an inverse Park (s = +1), and any future polar/mixer
rotation by an arbitrary streamed angle. Unity gain: the CORDIC gain
K = 1.6467602581 is compensated internally (MULQ by 1/K = 0.6072529350), so a
rotation never scales the vector.

INPUTS — three Q15 streams on three arms: ``x``, ``y``, and ``theta``. The
angle convention is the shipped CORDIC/NCO one: 16-bit HALF-TURN Q15 units,
word/32768 * pi radians, the full circle = 65536 counts, so plain 16-bit WRAP
is exactly arithmetic mod 2*pi and the +-pi seam needs no special case
(theta = 0x8000 IS -pi == +pi).

The three arms fire at INDEPENDENT, ASYNCHRONOUS times, so the landing cell is
an N=3 LOCK-rotation rendezvous (the TMRVoterBlock mechanism, INV-46): the
arbiter LOCK accepts from exactly ONE face at a time and rotates
x -> y -> theta -> (barred), so no interleaving can ever mis-pair a triple.
The face budget (N + 2 = 5 > 4) forces the rendezvous to be a LEAF of the fold
and the serialize-LOCK release to ride the ONE abutting cell (``pre``).

THE RELEASE IS A BACKWARD JUMP, NOT A VALUE-CARRYING ``WRITE.CFG`` (INV-69).
``pre`` jumps the rendezvous's own ``relock`` entry, which re-points LOCK_FACE
from the rendezvous's ``face_x`` DataWord. That matters because the build's
face-reconciliation pass (``_apply_rendezvous_input_faces``) patches face words
**in the rendezvous cell only** — an authored ``unlock_face`` copy in ``pre``
is never reconciled, so a release carrying its own constant aims the lock at
whatever face the constant names rather than the face the router actually
landed arm x on. Measured on the ``examples/foc_motor`` chain, where the router
lands arm x NORTH while the authored constant says WEST: iteration 0 completes,
the release re-points LOCK_FACE at WEST, arm x's next word arrives on NORTH and
is barred forever — the chain wedges with a post-group ``Deadlock`` (INV-67).
The release is race-free by construction: the relock jump arrives on the
rendezvous's INTERNAL forward face, which the arbiter bars until ``got_t``'s
final ``LOCK_FACE = face_fwd`` has run, so it cannot outrace the arm bar.

NUMERICS (each stage mirrored bit-exactly by :func:`cordic_rotate_word`):

* PRESCALE 1/4 (``MULQ 1<<13`` = floor arithmetic shift): K times a corner
  vector reaches 2.33 and would wrap 16 bits; at 1/4 the whole trajectory is
  signed-safe (measured max |intermediate| = 19083 < 32768 over a 48k-case
  sweep including dense corner scans).
* QUADRANT PRE-ROTATION: rotation-mode CORDIC converges only for
  |z| <= 0.5549 half-turns, so ``prep1``/``prep2`` reduce z into
  [-0.5, 0.5) by the exact +-90-degree rotations
  (q = z >> 14: q==1 -> (x,y)=(-y,x), z -= 0.5; q==2 -> (x,y)=(y,-x),
  z += 0.5). All arithmetic on z is PURE 16-bit WRAP.
* ROTATION ITERATIONS (14, the shipped NITER — the residual angle
  atan(2^-13) plus table quantization is what bounds the accuracy below;
  fewer iterations was not needed and the shipped count is matched):
  sigma_i = +1 if z >= 0 else -1; z -= sigma*atan(2^-i)/pi;
  x -= sigma*asr(y, i); y += sigma*asr(x, i). The arithmetic shift is ONE
  instruction: ``MULQ v, 1<<(15-i)`` == floor(v / 2^i) for signed v (i >= 1;
  the i = 0 cell needs no shift at all). sigma is applied with the masked
  identity sigma*a = ((a ^ msk) + sgn), msk = -(z >> 15).
* GAIN RESTORE: MULQ by 1/K then two SATURATING doublings — the PROVEN
  ComplexGainBlock idiom: capture the compensated product's sign first, pin
  to 0x7FFF + signbit on the FIRST overflow (both signs — unlike the
  vectoring MAG cell, a rotated component can be negative). Saturation is
  reachable only for corner inputs (|v| > 1.0, e.g. x = y = 0x7FFF), which
  clamp exactly like the float reference saturated to Q15. No GOTO anywhere
  (INV-43: the assembler compiles a GOTO near a write/jump placeholder into
  an EXTERNAL output jump — measured to kill this cell's egress).

ACCURACY (measured, the bound the verification gate holds): against float
rotation saturated to Q15, over 48192 cases — a dense 1024-step full-circle
sweep at all four Q15 corner vectors plus 40000 uniform random
(x, y, theta, sign) cases — **max |error| = 24.75 LSB, mean 6.24 LSB**
(proto sweep 15072 cases: max 24.02, mean 5.61). The verification suite
re-measures its own sweep and asserts max <= 25.0 LSB.

OUTPUT: one complex 2-rail packet (``yi`` = x', ``yq`` = y') per triple from
the exit cell — the ComplexMixer/DualFloatToComplex egress shape
(``output_registers`` = 2 rails), so a complex consumer receives both rails
and a port egress interleaves [x'0, y'0, x'1, y'1, ...].

FOLD — 20 cells, 5x5 serpentine, the rendezvous a LEAF at (0,0) with three
free faces (west/north/south at identity) for the three arms::

    row0 (E): rdv pre prep1 prep2 it0(S)
    row1 (W):     it4(S) it3 it2 it1
    row2 (E):     it5 it6 it7 it8(S)
    row3 (W):     it12(S) it11 it10 it9
    row4 (E):     it13 postx posty

Column 0 holds ONLY the rendezvous (its south neighbour slot stays empty),
program-dict order == layout order (INV-51 positional pairing), no two cells
rest facing each other (INV-56), and the only backward edge is the CONFIG-only
``unlock`` (no backward data or jump edges — INV-53 satisfied trivially).
"""
import numpy as np
from typing import Any, Dict, List, Tuple

from ..block import CellProgram, Port, EntryPoint, StateVar, DataWord
from ._base import KyttarBlock, BlockInterface
from .cordic_blocks import NITER, ATAN_Q15, KINV_Q15, _s16


def _mulq(a: int, b: int) -> int:
    """Bit-exact MULQ: R0 = (A x B) >> 15, signed (floor)."""
    return ((_s16(a) * _s16(b)) >> 15) & 0xFFFF


def cordic_rotate_word(xi: int, yi: int, theta: int, sign: int = 1
                       ) -> Tuple[int, int]:
    """Bit-exact Q15 model of the WHOLE chain (pre -> prep1 -> prep2 ->
    it0..it13 -> postx -> posty) for ONE (x, y, theta) triple -> (x', y').

    This is the GOLDEN the on-chip words are held EXACTLY equal to; its own
    correctness is pinned by the float-rotation tolerance bound in the
    verification suite (measured max 24.75 LSB, see the module docstring)."""
    x = _mulq(int(xi), 1 << 13)                 # pre: prescale 1/4
    y = _mulq(int(yi), 1 << 13)
    z = int(theta) & 0xFFFF
    if int(sign) < 0:                           # prep1: s*theta (wrap negate;
        z = (~z + 1) & 0xFFFF                   #  0x8000 -> 0x8000: -pi == +pi)
    q = z >> 14                                 # prep1: quadrant
    if q == 1:                                  # z in [0.5, 1): rotate +90deg
        z = (z - 0x4000) & 0xFFFF
        x, y = (~y + 1) & 0xFFFF, x             # prep2: (x,y) -> (-y, x)
    elif q == 2:                                # z in [-1, -0.5): rotate -90deg
        z = (z + 0x4000) & 0xFFFF
        x, y = y, (~x + 1) & 0xFFFF             # prep2: (x,y) -> (y, -x)
    for i in range(NITER):                      # it0..it13
        sgn = z >> 15
        msk = (0 - sgn) & 0xFFFF
        if i < NITER - 1:                       # it13 skips the dead z update
            z = (z - (((ATAN_Q15[i] ^ msk) + sgn) & 0xFFFF)) & 0xFFFF
        ax = (x if i == 0 else (_s16(x) >> i)) & 0xFFFF   # MULQ 1<<(15-i)
        ay = (y if i == 0 else (_s16(y) >> i)) & 0xFFFF
        nx = (x - (((ay ^ msk) + sgn) & 0xFFFF)) & 0xFFFF
        ny = (y + (((ax ^ msk) + sgn) & 0xFFFF)) & 0xFFFF
        x, y = nx, ny

    def _comp(v: int) -> int:                   # postx / posty
        p = (_s16(KINV_Q15) * _s16(v)) >> 15    # MULQ 1/K
        acc = p
        for _ in range(2):                      # saturating <<2 restore:
            acc2 = acc + acc                    # first V pins to p's sign
            if acc2 > 32767 or acc2 < -32768:   # rail (0x7FFF + signbit) —
                return (0x7FFF + ((p >> 15) & 1)) & 0xFFFF   # ComplexGain idiom
            acc = acc2
        return acc & 0xFFFF

    return _comp(x), _comp(y)


def rotate_stream(x_words, y_words, theta_words, sign: int = 1) -> list:
    """N (x, y, theta) triples in -> the FLAT interleaved complex-packet
    stream ``[x'0, y'0, x'1, y'1, ...]`` a port egress drains. Truncates to
    the shortest arm (a starved arm stalls the rendezvous — no partial
    packet is ever emitted)."""
    n = min(len(x_words), len(y_words), len(theta_words))
    out: list = []
    for i in range(n):
        xo, yo = cordic_rotate_word(x_words[i], y_words[i], theta_words[i],
                                    sign)
        out.append(xo)
        out.append(yo)
    return out


class CordicRotateBlock(KyttarBlock):
    """General Q15 vector rotation by a streamed angle — see the module
    docstring for the numerics, the measured accuracy bound, and the fold.

    Parameters:
      * ``sign``: +1 or -1 — the block rotates by ``sign * theta``. One class,
        two motor-control instances: Park is ``sign=-1`` (rotate by -theta),
        inverse Park is ``sign=+1``. Boot-time (changes the ``prep1`` program,
        so entry addresses are params-dependent — INV-6).
      * ``face_x`` / ``face_y`` / ``face_t``: the faces the three arms arrive
        on. Pairwise DISTINCT (the face IS the stream identity) —
        placement/routing internals the build reconciles to the router's real
        geometry, NOT exposed in GRC (the TMRVoter convention).
    """

    CATEGORY = "signal_conditioning"
    TAGS = ["cordic", "rotation", "park", "inverse_park", "mixer", "complex",
            "rendezvous", "lock", "motor_control", "signal_conditioning"]

    # The defining rendezvous property (INV-46): three independent async
    # streams distinguishable ONLY by arrival face.
    NEEDS_DISTINCT_INPUT_FACES = True
    # (input port, face DataWord) pairs in FIRST-ACCEPTED order, for the
    # build's face-reconciliation pass (a missing declaration makes that pass
    # a silent no-op and the chain emits ZERO output — measured on TMRVoter).
    RENDEZVOUS_FACE_PORTS = (("x", "face_x"), ("y", "face_y"),
                             ("theta", "face_t"))
    # NOTE: no ``UNLOCK_CFG_ADDR``. The serialize-LOCK release is a backward
    # JUMP into the rendezvous's own ``relock`` entry (INV-69), not a
    # value-carrying ``WRITE.CFG``, so there is no CONFIG address for the
    # build to patch — the rendezvous re-points its own LOCK_FACE from the
    # ONE ``face_x`` copy the build's reconciliation pass actually patches.

    # face_* are placement/routing internals the placer+router choose and the
    # build reconciles — never user-facing DSP params.
    GRC_UNSUPPORTED_PARAMS = ("face_x", "face_y", "face_t")

    _FACE = {"south": 0, "east": 1, "west": 2, "north": 3}

    def __init__(self, name: str, sign: int = 1, face_x: str = "west",
                 face_y: str = "north", face_t: str = "south"):
        super().__init__(name, sign=sign, face_x=face_x, face_y=face_y,
                         face_t=face_t)
        if int(sign) not in (1, -1):
            raise ValueError(
                f"CordicRotateBlock rotates by sign*theta with sign in "
                f"{{+1, -1}} (Park = -1, inverse Park = +1); got {sign!r}")
        faces = (face_x, face_y, face_t)
        if len(set(faces)) != 3:
            raise ValueError(
                f"HARDWARE LIMIT: CordicRotateBlock distinguishes its three "
                f"arms (x, y, theta) by ARRIVAL FACE, so face_x, face_y and "
                f"face_t must be pairwise DISTINCT; got {faces}. (Two arms "
                f"sharing a face cannot be told apart by the arbiter and "
                f"would mis-pair permanently — the face IS the arm identity.)")
        self._sign = int(sign)
        self._face_x, self._face_y, self._face_t = face_x, face_y, face_t

    @property
    def cell_count(self) -> int:
        return NITER + 6            # rdv, pre, prep1, prep2, it0..13, postx, posty

    @property
    def interface(self) -> BlockInterface:
        # Inputs all land in R0, each on its own face-gated entry (got_x is
        # the first-accepted / boot-locked one). TWO output registers: the
        # exit cell emits a genuine 2-rail complex packet (yi = x', yq = y'),
        # and >1 output registers is THE build discriminator that steers the
        # two rails to consecutive downstream registers (INV-23's brokered
        # 2-rail guard) instead of collapsing them.
        return BlockInterface(entry_address=15, input_registers=[0],
                              output_registers=[0, 1])

    def output_cell_id(self):
        return "posty"

    # ------------------------------------------------------------------ cells
    def build_cell_programs(self) -> Dict[str, CellProgram]:
        fx = self._FACE.get(self._face_x, 2)    # west
        fy = self._FACE.get(self._face_y, 3)    # north
        ft = self._FACE.get(self._face_t, 0)    # south
        cells: Dict[str, CellProgram] = {}

        # (1) rdv — the N=3 LOCK-rotation rendezvous (TMRVoter mechanism,
        # INV-46 rule 3: the rotation has FOUR stops). Each arm lands in R0 on
        # its OWN face-gated entry and is latched immediately; got_t forwards
        # the triple and locks to the INTERNAL forward face — which no arm
        # ever arrives on, barring all three until `pre` releases the lock.
        # Cold start is BAKED (initial_lock_face): arming via a JUMP is a race.
        cells["rdv"] = CellProgram(
            inputs=[Port("x", register=0, entry="got_x"),
                    Port("y", register=0, entry="got_y"),
                    Port("theta", register=0, entry="got_t")],
            outputs=[Port("fx"), Port("fy"), Port("fz"), Port("ftrig")],
            # got_x / got_y / got_t (NO arm entry — boots pre-locked) plus
            # `relock`, the serialize-LOCK release target (INV-69): it
            # re-points the lock from THIS cell's own `face_x` word, the copy
            # the build's face-reconciliation pass patches to the ROUTED arm
            # geometry. An authored copy in the release cell is NEVER
            # reconciled, so a release that carries its own constant aims the
            # lock at whatever face that constant names — see the class
            # docstring.
            entries=[EntryPoint("got_x"), EntryPoint("got_y"),
                     EntryPoint("got_t"), EntryPoint("relock")],
            data=[DataWord("face_x", fx, address=1, is_face=True),
                  DataWord("face_y", fy, address=2, is_face=True),
                  DataWord("face_t", ft, address=3, is_face=True),
                  # The INTERNAL forward face (toward `pre`); locking to it
                  # bars every arm until the release. is_face -> D4 (INV-23).
                  DataWord("face_fwd", 1, address=4, is_face=True)],
            state=[StateVar("vx", register=5), StateVar("vy", register=6),
                   StateVar("vt", register=7)],
            assembly_template=(
                "got_x:\n"
                "    MOVE R{state:vx}, R{in:x}\n"
                "    MOVE [LOCK_FACE], R{data:face_y}\n"
                "    HALT\n"
                "got_y:\n"
                "    MOVE R{state:vy}, R{in:y}\n"
                "    MOVE [LOCK_FACE], R{data:face_t}\n"
                "    HALT\n"
                "got_t:\n"
                "    MOVE R{state:vt}, R{in:theta}\n"
                "    MOVE R0, R{state:vx}\n"
                "    {write:fx}\n"
                "    MOVE R0, R{state:vy}\n"
                "    {write:fy}\n"
                "    MOVE R0, R{state:vt}\n"
                "    {write:fz}\n"
                "    {jump:ftrig}\n"
                # Bar ALL THREE arms (lock to the internal face nothing
                # drives) until `pre` has dispatched and jumps `relock`. This
                # also ADMITS the release: the relock jump arrives on the
                # internal forward face, so the arbiter holds it until this
                # very instruction has run — the release cannot outrace the
                # arm bar (INV-69's race-free-by-construction property).
                "    MOVE [LOCK_FACE], R{data:face_fwd}\n"
                "    HALT\n"
                "relock:\n"
                # Re-admit the next triple, from the RECONCILED face word.
                "    MOVE [LOCK_FACE], R{data:face_x}\n"
                "    HALT\n"
            ),
            initial_lock_face=fx,
        )

        # (2) pre — prescale x, y by 1/4 (MULQ 1<<13 = floor asr 2), forward
        # theta, and carry the SERIALIZE-LOCK RELEASE (INV-19/20/46): the
        # release must ride the ONE cell abutting the rendezvous (face budget
        # N+2 = 5 > 4 — no face is free for a corridor of its own).
        # `unlock_face` serves double duty: the direction WEST back into the
        # rendezvous AND the LOCK_FACE value (arm x's face) — both WEST at
        # identity, D4-transforming identically, so ONE word (the TMR agree
        # reclaim). The tail restores the resting face (INV-52).
        cells["pre"] = CellProgram(
            inputs=[Port("x", register=1), Port("y", register=2),
                    Port("z", register=3)],
            outputs=[Port("x"), Port("y"), Port("z"), Port("trig"),
                     Port("unlock")],
            entries=[EntryPoint("default")],
            data=[DataWord("p13", 1 << 13, address=4),
                  DataWord("unlock_face", fx, address=5, is_face=True),
                  DataWord("face_tap", 1, address=6, is_face=True)],
            state=[],
            assembly_template=(
                "start:\n"
                "    MULQ R{data:p13}, R{in:x}\n"
                "    {write:x}\n"
                "    MULQ R{data:p13}, R{in:y}\n"
                "    {write:y}\n"
                "    MOVE R0, R{in:z}\n"
                "    {write:z}\n"
                "    {jump:trig}\n"
                # THE RELEASE (INV-69): flip back into the rendezvous, JUMP
                # its `relock` entry — which re-points its own LOCK_FACE from
                # its RECONCILED `face_x` word — then restore the forward
                # face. NOT a value-carrying WRITE.CFG: the LOCK_FACE value
                # must be arm x's ROUTED face, and only the rendezvous's own
                # reconciled word knows it. The jump is authored LAST so it is
                # the cell's HIGHEST-addressed JUMP (INV-53: that is the one a
                # declared backward edge patch targets). No trailing HALT —
                # R31 is always HALT.
                "    MOVE [FACE], R{data:unlock_face}\n"
                "    {jump:unlock}\n"
                "    MOVE [FACE], R{data:face_tap}\n"
            ),
        )

        # (3) prep1 — z conditioning: apply the boot-time sign (wrap negate —
        # 0x8000 negates to itself, which is exactly -pi == +pi), then the
        # quadrant dispatch on q = z >> 14 (CMP leaves R0 untouched is not
        # needed: chained SUB #one probes q-1, q-2). x and y pass through.
        neg = ("    NOT R{in:z}\n"
               "    ADD R0, R{data:one}\n"
               "    MOVE R{in:z}, R0\n") if self._sign < 0 else ""
        cells["prep1"] = CellProgram(
            inputs=[Port("x", register=1), Port("y", register=2),
                    Port("z", register=3)],
            outputs=[Port("x"), Port("y"), Port("zp"), Port("zq"), Port("zr"),
                     Port("tpass"), Port("tpos"), Port("tneg")],
            entries=[EntryPoint("default")],
            data=[DataWord("one", 1, address=4),
                  DataWord("half", 0x4000, address=5)],
            state=[],
            assembly_template=(
                "start:\n"
                + neg +
                "    MOVE R0, R{in:x}\n"
                "    {write:x}\n"
                "    MOVE R0, R{in:y}\n"
                "    {write:y}\n"
                "    SHR R{in:z}, #14\n"
                "    SUB R0, R{data:one}\n"
                "    BR.Z pos\n"
                "    SUB R0, R{data:one}\n"
                "    BR.Z neg\n"
                "    MOVE R0, R{in:z}\n"
                "    {write:zp}\n"
                "    {jump:tpass}\n"
                "    HALT\n"
                "pos:\n"
                "    SUB R{in:z}, R{data:half}\n"
                "    {write:zq}\n"
                "    {jump:tpos}\n"
                "    HALT\n"
                "neg:\n"
                "    ADD R{in:z}, R{data:half}\n"
                "    {write:zr}\n"
                "    {jump:tneg}\n"
            ),
        )

        # (4) prep2 — apply the quadrant pre-rotation to (x, y): three
        # entries, one per prep1 verdict (all three are internal_jumps
        # targets — INV-39). pass: identity; pos: (x,y) -> (-y, x);
        # neg: (x,y) -> (y, -x). z relays unchanged.
        cells["prep2"] = CellProgram(
            inputs=[Port("x", register=1), Port("y", register=2),
                    Port("z", register=3)],
            outputs=[Port("xA"), Port("yA"), Port("zA"), Port("tA"),
                     Port("xB"), Port("yB"), Port("zB"), Port("tB"),
                     Port("xC"), Port("yC"), Port("zC"), Port("tC")],
            entries=[EntryPoint("pass"), EntryPoint("pos"),
                     EntryPoint("neg")],
            data=[DataWord("one", 1, address=4)],
            state=[],
            assembly_template=(
                "pass:\n"
                "    MOVE R0, R{in:z}\n"
                "    {write:zA}\n"
                "    MOVE R0, R{in:x}\n"
                "    {write:xA}\n"
                "    MOVE R0, R{in:y}\n"
                "    {write:yA}\n"
                "    {jump:tA}\n"
                "    HALT\n"
                "pos:\n"
                "    MOVE R0, R{in:z}\n"
                "    {write:zB}\n"
                "    NOT R{in:y}\n"
                "    ADD R0, R{data:one}\n"
                "    {write:xB}\n"
                "    MOVE R0, R{in:x}\n"
                "    {write:yB}\n"
                "    {jump:tB}\n"
                "    HALT\n"
                "neg:\n"
                "    MOVE R0, R{in:z}\n"
                "    {write:zC}\n"
                "    MOVE R0, R{in:y}\n"
                "    {write:xC}\n"
                "    NOT R{in:x}\n"
                "    ADD R0, R{data:one}\n"
                "    {write:yC}\n"
                "    {jump:tC}\n"
            ),
        )

        # (5) it0..it13 — the unrolled ROTATION iterations. sigma = sign of z
        # (masked identity, the vectoring cells' idiom with the mask sourced
        # from z instead of y); the arithmetic shift is ONE MULQ by
        # 1<<(15-i) (exact floor asr for signed v, i >= 1 — i = 0 shifts by
        # nothing and i = 13 drops the dead z update).
        for i in range(NITER):
            cells[f"it{i}"] = self._iter_program(i)

        # (6) postx / posty — 1/K compensation + saturating <<2 restore on
        # each rail, the PROVEN ComplexGainBlock idiom verbatim: capture the
        # compensated product's sign BEFORE the doublings (an overflowed ADD
        # destroys it), and on the FIRST V pin to 0x7FFF + signbit — all
        # doublings share p's sign, so the first overflow is a true overload
        # in p's direction. Conditional LOCAL branches ONLY, never GOTO (the
        # assembler compiles a GOTO near a {write}/{jump} as an EXTERNAL
        # output jump — INV-43/INV-13; measured here too: a GOTO variant of
        # this cell emitted NOTHING). posty is the exit cell: it emits the
        # (yi, yq) complex packet.
        cells["postx"] = CellProgram(
            inputs=[Port("x", register=1), Port("y", register=2)],
            outputs=[Port("x"), Port("y"), Port("trig")],
            entries=[EntryPoint("default")],
            data=[DataWord("kinv", KINV_Q15, address=3),
                  DataWord("satpos", 0x7FFF, address=4)],
            state=[StateVar("sgn", register=5)],
            assembly_template=(
                "start:\n"
                "    MULQ R{data:kinv}, R{in:x}\n"
                "    MOVE R{state:sgn}, R0\n"
                "    ADD R0, R0\n"
                "    BR.V _sx\n"
                "    ADD R0, R0\n"
                "    BR.NV _wx\n"
                "_sx:\n"
                "    SHR R{state:sgn}, #15\n"
                "    ADD R0, R{data:satpos}\n"
                "_wx:\n"
                "    MOVE R0, R0\n"
                "    {write:x}\n"
                "    MOVE R0, R{in:y}\n"
                "    {write:y}\n"
                "    {jump:trig}\n"
            ),
        )
        cells["posty"] = CellProgram(
            inputs=[Port("y", register=1), Port("x", register=2)],
            outputs=[Port("yi"), Port("yq"), Port("trig")],
            entries=[EntryPoint("default")],
            data=[DataWord("kinv", KINV_Q15, address=3),
                  DataWord("satpos", 0x7FFF, address=4)],
            state=[StateVar("sgn", register=5)],
            assembly_template=(
                # yi (x', already compensated by postx) FIRST, then the
                # compensated y' — the emit order IS the packet rail order
                # (the build steers the writes to consecutive downstream
                # registers in program order, and a port egress drains them
                # in the same order: [x', y', x', y', ...]).
                "start:\n"
                "    MOVE R0, R{in:x}\n"
                "    {write:yi}\n"
                "    MULQ R{data:kinv}, R{in:y}\n"
                "    MOVE R{state:sgn}, R0\n"
                "    ADD R0, R0\n"
                "    BR.V _sy\n"
                "    ADD R0, R0\n"
                "    BR.NV _wy\n"
                "_sy:\n"
                "    SHR R{state:sgn}, #15\n"
                "    ADD R0, R{data:satpos}\n"
                "_wy:\n"
                "    MOVE R0, R0\n"
                "    {write:yq}\n"
                "    {jump:trig}\n"
            ),
        )
        return cells

    @staticmethod
    def _iter_program(i: int) -> CellProgram:
        """Rotation iteration cell i (see build_cell_programs note 5)."""
        emit_z = i < NITER - 1
        if i == 0:
            # No shift at iteration 0: sigma*y / sigma*x directly.
            body = (
                "    XOR R{in:y}, R{state:m}\n"
                "    ADD R0, R{state:t}\n"          # sigma*y
                "    MOVE R{state:tx}, R0\n"
                "    XOR R{in:x}, R{state:m}\n"
                "    ADD R0, R{state:t}\n"          # sigma*x
                "    ADD R{in:y}, R0\n"             # y' = y + sigma*x
                "    {write:y}\n"
                "    SUB R{in:x}, R{state:tx}\n"    # x' = x - sigma*y
                "    {write:x}\n"
            )
            data = [DataWord("at", ATAN_Q15[0], address=4)]
        else:
            body = (
                "    MULQ R{data:p2}, R{in:x}\n"    # floor asr(x, i)
                "    MOVE R{state:tx}, R0\n"
                "    MULQ R{data:p2}, R{in:y}\n"    # floor asr(y, i)
                "    XOR R0, R{state:m}\n"
                "    ADD R0, R{state:t}\n"          # sigma*asr(y, i)
                "    SUB R{in:x}, R0\n"             # x' = x - sigma*asr(y, i)
                "    {write:x}\n"
                "    XOR R{state:tx}, R{state:m}\n"
                "    ADD R0, R{state:t}\n"          # sigma*asr(x, i)
                "    ADD R{in:y}, R0\n"             # y' = y + sigma*asr(x, i)
                "    {write:y}\n"
            )
            data = ([DataWord("p2", 1 << (15 - i), address=4)]
                    if not emit_z else
                    [DataWord("at", ATAN_Q15[i], address=4),
                     DataWord("p2", 1 << (15 - i), address=5)])
        # sgn/msk from z's sign (the shipped -sgn idiom), then the z update.
        head = (
            "start:\n"
            "    SHR R{in:z}, #15\n"
            "    MOVE R{state:t}, R0\n"
            "    SUB R0, R{state:t}\n"
            "    SUB R0, R{state:t}\n"
            "    MOVE R{state:m}, R0\n"
        )
        if emit_z:
            head += (
                "    XOR R{data:at}, R{state:m}\n"
                "    ADD R0, R{state:t}\n"          # sigma*atan_i
                "    SUB R{in:z}, R0\n"             # z' (PURE WRAP)
                "    {write:z}\n"
            )
        outs = ([Port("z")] if emit_z else []) + [Port("x"), Port("y"),
                                                  Port("trig")]
        max_data = max(d.address for d in data)
        return CellProgram(
            inputs=[Port("x", register=1), Port("y", register=2),
                    Port("z", register=3)],
            outputs=outs,
            entries=[EntryPoint("default")],
            data=data,
            # INV-33: pin every state register explicitly, above the data.
            state=[StateVar("t", register=max_data + 1),
                   StateVar("m", register=max_data + 2),
                   StateVar("tx", register=max_data + 3)],
            assembly_template=head + body + "    {jump:trig}\n",
        )

    # ----------------------------------------------------------------- wiring
    def internal_connections(self) -> List[Tuple[Any, str, Any, str]]:
        conns: List[Tuple[Any, str, Any, str]] = [
            ("rdv", "fx", "pre", "x"), ("rdv", "fy", "pre", "y"),
            ("rdv", "fz", "pre", "z"),
            ("pre", "x", "prep1", "x"), ("pre", "y", "prep1", "y"),
            ("pre", "z", "prep1", "z"),
            ("prep1", "x", "prep2", "x"), ("prep1", "y", "prep2", "y"),
            ("prep1", "zp", "prep2", "z"), ("prep1", "zq", "prep2", "z"),
            ("prep1", "zr", "prep2", "z"),
            # prep2 -> it0, one edge set per quadrant entry
            ("prep2", "xA", "it0", "x"), ("prep2", "yA", "it0", "y"),
            ("prep2", "zA", "it0", "z"),
            ("prep2", "xB", "it0", "x"), ("prep2", "yB", "it0", "y"),
            ("prep2", "zB", "it0", "z"),
            ("prep2", "xC", "it0", "x"), ("prep2", "yC", "it0", "y"),
            ("prep2", "zC", "it0", "z"),
        ]
        for i in range(NITER - 1):
            conns += [(f"it{i}", "x", f"it{i+1}", "x"),
                      (f"it{i}", "y", f"it{i+1}", "y"),
                      (f"it{i}", "z", f"it{i+1}", "z")]
        conns += [
            (f"it{NITER-1}", "x", "postx", "x"),
            (f"it{NITER-1}", "y", "postx", "y"),
            ("postx", "x", "posty", "x"), ("postx", "y", "posty", "y"),
        ]
        return conns

    def internal_jumps(self) -> List[Tuple[Any, str, Any, str]]:
        jumps = [
            ("rdv", "ftrig", "pre", "default"),
            ("pre", "trig", "prep1", "default"),
            ("prep1", "tpass", "prep2", "pass"),
            ("prep1", "tpos", "prep2", "pos"),
            ("prep1", "tneg", "prep2", "neg"),
            ("prep2", "tA", "it0", "default"),
            ("prep2", "tB", "it0", "default"),
            ("prep2", "tC", "it0", "default"),
        ]
        for i in range(NITER - 1):
            jumps.append((f"it{i}", "trig", f"it{i+1}", "default"))
        jumps += [
            (f"it{NITER-1}", "trig", "postx", "default"),
            ("postx", "trig", "posty", "default"),
            ("posty", "trig", "__terminate__", "default"),
            # The LAST edge is the BACKWARD serialize-LOCK release,
            # pre -> rdv.relock (INV-69, see the `pre` cell's note). It
            # carries NO data, so it lives here and not in
            # internal_connections — a data edge aimed at a real input port
            # would make portmap classify it as a feedback RETURN and DROP
            # that arm from the block's external inputs (the TMRVoter trap).
            # Authored LAST so the patch targets the highest-addressed JUMP
            # (INV-53).
            ("pre", "unlock", "rdv", "relock"),
        ]
        return jumps

    def default_layout(self) -> Dict[Any, Tuple[int, int, str]]:
        """5x5 serpentine with the rendezvous a LEAF at (0, 0) — column 0
        holds ONLY the rendezvous, so its west/north/south faces stay free
        for the three arms (the N=3 face budget, INV-46/layout_rules 4b)::

            col:   0    1       2      3      4
            row0:  rdv  pre     prep1  prep2  it0(S)
            row1:  .    it4(S)  it3    it2    it1
            row2:  .    it5     it6    it7    it8(S)
            row3:  .    it12(S) it11   it10   it9
            row4:  .    it13    postx  posty

        Rows 0/2/4 flow EAST, rows 1/3 WEST; the turn cells face SOUTH.
        NARROW deliberately: a 3-row 8- or 9-wide fold walls the 10-wide
        chip into a north and a south region joined only by columns 0 and 9
        — two vertical channels for THREE independent corridors (west arm,
        south arm, egress), which cannot route (measured, both widths).
        At 5 wide the whole east half of the array stays open. No two cells
        rest facing each other (INV-56). Program-dict order == layout order
        (positional pairing, INV-51)."""
        lay: Dict[Any, Tuple[int, int, str]] = {
            "rdv": (0, 0, "east"), "pre": (1, 0, "east"),
            "prep1": (2, 0, "east"), "prep2": (3, 0, "east"),
            "it0": (4, 0, "south")}
        for i in range(1, 5):                    # it1..it4 westbound
            lay[f"it{i}"] = (5 - i, 1, "west" if i < 4 else "south")
        for i in range(5, 9):                    # it5..it8 eastbound
            lay[f"it{i}"] = (i - 4, 2, "east" if i < 8 else "south")
        for i in range(9, 13):                   # it9..it12 westbound
            lay[f"it{i}"] = (13 - i, 3, "west" if i < 12 else "south")
        lay["it13"] = (1, 4, "east")
        lay["postx"] = (2, 4, "east")
        lay["posty"] = (3, 4, "east")
        # INV-51: reindex against the program dict so both iterate in the
        # same order — the ids would hide a mismatch and whole cells would
        # load empty.
        order = list(self.build_cell_programs().keys())
        assert set(order) == set(lay)
        return {cid: lay[cid] for cid in order}

    # -------------------------------------------------------------- reference
    def process_reference(self, input_samples) -> np.ndarray:
        """Bit-exact model of the cell chain: an iterable of (x, y, theta)
        Q15 word triples -> an (N, 2) array of (x', y') words."""
        return np.array([cordic_rotate_word(int(x), int(y), int(t),
                                            self._sign)
                         for (x, y, t) in input_samples], dtype=np.uint16)

    def reset(self):
        pass
