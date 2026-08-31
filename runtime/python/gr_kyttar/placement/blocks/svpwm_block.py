# SPDX-License-Identifier: GPL-3.0-or-later
"""SVPWMBlock — see :class:`SVPWMBlock`."""
import numpy as np

from ..block import CellProgram, DataWord, EntryPoint, Port, StateVar
from ._base import BlockInterface, KyttarBlock


class SVPWMBlock(KyttarBlock):
    """Space-vector PWM by min-max (common-mode) injection — the (v_alpha,
    v_beta) voltage command in, the three phase duty cycles out as a 3-word
    Q15 packet per sample, fixed order **a, b, c**.

    There is no stock GNU Radio counterpart: SVPWM is a motor-drive modulator,
    not a communications block, and GR has no three-phase inverter model. The
    golden is therefore a host reference written directly from the textbook
    definition (float) plus an EXACT integer model of the shipped arithmetic
    (:meth:`duties`), compared word for word against the real chip.

    ------------------------------------------------------------------ the math

    Inverse Clarke (amplitude-invariant, the standard FOC convention):

        va = v_alpha
        vb = -v_alpha/2 + (sqrt(3)/2) * v_beta
        vc = -v_alpha/2 - (sqrt(3)/2) * v_beta

    Common-mode (min-max) injection — the zero-sequence midpoint is subtracted
    so the three-phase set is centered in the inverter's voltage window, which
    is what buys SVPWM its ~15.5% linear-range advantage over plain sine PWM:

        m      = (max(va, vb, vc) + min(va, vb, vc)) / 2
        duty_i = v_i - m

    SECTOR SELECTION IS THE MIN/MAX ITSELF: which phase is the max and which is
    the min changes every 60 electrical degrees, so the compare tree that finds
    them IS the six-sector logic — there is no separate sector switch. The
    compares are signed (CMP then BR on the SLT flag = N^V, correct over the
    full Q15 range; a bare N test is wrong across an overflowing difference).
    NOTE the ISA trap the compare tree must respect: MOVE does NOT set flags
    (only ALU ops do), so every branch is preceded by its own CMP.

    ------------------------------------------------- the exact chip arithmetic

    Everything is Q15 with these EXACT steps (mirrored by :meth:`duties`, the
    integer golden the gate compares word-for-word):

      * "/2" is ``MULQ`` by 16384 = floor((x * 16384) >> 15) — an arithmetic
        (floor) halving, verified against the chip's MULQ rounding.
      * (sqrt(3)/2) is the Q15 constant 28378 = round(0.86602540 * 32768).
      * vb and vc are SATURATING adds: ADD/SUB then, on the V flag, a clamp to
        0x7FFF/0x8000 by the wrapped result's sign (N=1 -> the true value
        overflowed positive -> 0x7FFF). |−v_alpha/2| + |t| can reach ~1.37
        full-scale, so the clamp is load-bearing, not defensive.
      * m = floor(max/2) + floor(min/2) — each half via MULQ 16384, summed with
        a plain ADD (each half is within ±16384, the sum cannot overflow).
        This differs from floor((max+min)/2) by at most 1 LSB and NEVER
        overflows, where a 16-bit (max+min) can.
      * duty_i = sat(v_i - m), the same V-flag clamp.

    THE CENTERING INVARIANT this arithmetic guarantees (stated exactly, gated
    exactly): for any sample where none of the three duty subtractions
    saturated,

        max(duties) + min(duties) == (max(v) & 1) + (min(v) & 1)  ∈ {0, 1, 2}

    i.e. the post-injection set is centered to within the two floor-halvings'
    truncation — at most 2 LSB of residual, and bit-exactly the parity sum of
    the pre-injection extremes. (Plain sine PWM — the injection dropped — is
    off by |max+min| ≈ up to half of full scale in mid-sector; the gate that
    pins this invariant is the gate that catches that mutation.)

    OVERMODULATION: |v| beyond the linear range simply drives the saturating
    clamps; the output follows :meth:`duties` exactly (pinned by the gate), so
    saturation is predictable, monotone-flat at the rails, and never wraps.

    ------------------------------------------------------------------ the I/O

    INPUT — the N=2 LOCK-by-face rendezvous (the ``DualFloatToComplexBlock`` /
    ``FeaturePairJoinBlock`` mechanism, verbatim). v_alpha and v_beta arrive
    from two INDEPENDENT producers at asynchronous times; the only stream
    identity on this clockless array is the arrival FACE, so the rendezvous
    cell locks to one face at a time (``LOCK``/``LOCK_FACE``, CONFIG 4/3):

        (cold start): LOCK=1, LOCK_FACE=face_alpha   (accept ONLY v_alpha)
        got_alpha: latch alpha ; LOCK_FACE = face_beta ; HALT
        got_beta:  latch beta ; forward (alpha, beta) ;
                   LOCK_FACE = face_fwd ; HALT

    There is NO arm entry — arming via a JUMP is a race (a word arriving before
    the arm-JUMP is accepted unlocked and mis-pairs); the cold start is baked
    into the boot CONFIG via ``initial_lock_face``.

    THE ROTATION HAS N+1 STOPS (INV-46 Rule 3 / INV-19): ``got_beta`` locks to
    the INTERNAL forward face — which no external arm arrives on, barring both
    producers — and the ``scale`` cell (the one cell abutting the rendezvous)
    releases it once it has dispatched the sample into the datapath.
    Re-locking straight to ``face_alpha`` at the end of ``got_beta`` is
    correct per-sample and deadlocks under saturated drive (the TMRVoter's
    measured failure).

    THE RELEASE IS A BACKWARD JUMP, NOT A WRITE.CFG — and the reason is
    MEASURED. The TMRVoter releases with ``WRITE.CFG @N, 3`` carrying an
    AUTHORED ``unlock_face`` value that serves as arm A's face; that is
    correct only when the router actually lands arm A on the authored face.
    The build's face-reconciliation pass (``_apply_rendezvous_input_faces``)
    patches face words in the RENDEZVOUS CELL ONLY — an authored copy in the
    release cell is NOT reconciled. For this 7-cell chain ``auto_pnr``
    relocates blocks freely, and ~70% of routed layouts landed arm alpha off
    the authored face: the release then re-pointed the lock at whatever face
    that constant named — the BETA corridor, in the measured case — and the
    next beta word barged in ahead of its alpha (a stale-alpha packet:
    ``duties(previous_alpha, beta)``), or the chain wedged. So instead
    ``scale`` JUMPs a third rendezvous entry, ``relock``, which re-points
    ``LOCK_FACE`` from the rendezvous's OWN ``face_alpha`` DataWord — the one
    copy the build DOES reconcile to the routed geometry. The jump rides the
    same forward abutment backwards (``face_back``/``face_tap`` flip), and it
    is RACE-FREE by construction: it arrives on the rendezvous's EAST face,
    which the arbiter bars until ``got_beta``'s final ``LOCK_FACE=face_fwd``
    (EAST) admits it — the release cannot land before the arms are barred.

    OUTPUT — a 3-word PACKET STREAM: per sample, THREE SEQUENTIAL WRITE+JUMP
    bursts into the SAME downstream register and entry, fixed order
    ``duty_a, duty_b, duty_c`` (the ``FeaturePairJoinBlock`` / ``TMRVoterBlock``
    two-burst egress shape, at three). The packet convention this block pins:

      * exactly 3 words per input sample, always a then b then c;
      * each word is the Q15 duty (a VALUE stream — a hosted chain must set
        ``output_words="q15"`` per INV-42, and a GR consumer splits the packet
        with ``blocks.deinterleave`` at 3);
      * the block declares exactly ONE output register — with more the build
        would classify the emit cell as a COMPLEX rail source and collapse the
        packet (the FeaturePairJoin condition (a)) — and the emit cell carries
        no internal handoff and no WRITE.CFG, so the build's full-cell patch
        covers all three bursts (condition (b)).

    ------------------------------------------------------------------ the fold

    SEVEN cells in a COLINEAR CHAIN, every handoff a single abutment:

        (0,0) rendezvous -> (1,0) scale -> (2,0) clarke -> (3,0) maxsel
            ^                   |
            +--- relock JUMP ---+          (the serialize-LOCK release)
        -> (4,0) minsel -> (5,0) inject -> (6,0) emit

    The rendezvous is a LEAF of the fold (INV-46 Rule 2 / layout_rules §4b):
    at N=2 the face budget is exactly 2 (arms) + 1 (forward) + 1 (release) = 4,
    so with the release riding the abutting ``scale`` cell (the proven
    TMRVoter shape) the rendezvous keeps two free faces for the two arms. The
    7x1 strip is the longitudinal shape layout_rules normally warns against —
    affordable here (7 <= 8 across) because the inputs land on the leaf cell
    from two different sides rather than tapping a bus edge, and the natural
    flow is port-to-port left to right.

    CORRIDOR-SHARING HAZARD (measured, a property of any face-locking join):
    the two arms are INDEPENDENT streams, and the arbiter HOLDS an
    early-arriving word until its turn — so that word's in-flight
    WRITE/DATA/JUMP words occupy the tail of its corridor. If the two arm
    corridors SHARE cells (e.g. a compact auto-pack herding both into the
    port corner), the held early word head-of-line blocks the OTHER arm: the
    pair can never complete and the chain emits nothing (measured: 10/12
    compact-packed layouts wedge on a beta-first sample; 12/12 spread
    layouts are clean in both orders). Diagnose per INV-67: a held word's
    run reporting ``Deadlock`` MID-group is the healthy hold signature — the
    wedge is the POST-group state, the completing word blocked in transit.
    Keep the two arms' delivery corridors disjoint in any design that drives
    them at independent times.

    Like the TMRVoter, the block sustains ONE SAMPLE PAIR in flight: the
    release rides ``scale`` (the only cell abutting the rendezvous), so the
    next pair is admitted once ``scale`` has dispatched, while the previous
    sample may still be in the deeper chain. Pairs may be driven back-to-back
    one pair at a time without limit; the whole-burst depth boundary is
    measured and guarded in the block's suite, not waived.

    Parameters:
      * ``face_alpha`` / ``face_beta``: the faces the two producers arrive on
        ('south'|'east'|'west'|'north'). MUST be distinct — the face IS the
        stream identity. Placement/routing internals the placer reserves and
        the build reconciles to the routed geometry, so NOT exposed in GRC
        (the DualFloatToComplex convention; ``GRC_UNSUPPORTED_PARAMS``).
    """
    CATEGORY = "motor_control"
    TAGS = ["svpwm", "space_vector", "pwm", "motor", "foc", "inverter",
            "three_phase", "rendezvous", "lock"]

    # The DEFINING property (shared with DualFloatToComplexBlock /
    # FeaturePairJoinBlock / TMRVoterBlock): the two inputs are independent
    # async streams distinguishable ONLY by arrival face, so the placer MUST
    # land them on DISTINCT faces and the build DRC rejects a same-face landing.
    NEEDS_DISTINCT_INPUT_FACES = True

    # (input port, face DataWord) pairs in FIRST-ACCEPTED order for the build's
    # face-reconciliation pass. Without this the pass falls back to the
    # DualFloatToComplex ``i``/``q`` names and becomes a SILENT NO-OP: the LOCK
    # gates the authored placeholder faces, the real arms are barred, and the
    # chain builds + routes perfectly while emitting ZERO output.
    RENDEZVOUS_FACE_PORTS = (("v_alpha", "face_alpha"), ("v_beta", "face_beta"))

    # face_alpha/face_beta are router-reconciled placement internals, not DSP
    # params — exposing them in GRC would invite a user to set a face the
    # router then overrides.
    GRC_UNSUPPORTED_PARAMS = ("face_alpha", "face_beta")

    _FACE = {"south": 0, "east": 1, "west": 2, "north": 3}

    # The Q15 constants, pinned as class attributes so the block, the integer
    # golden and the suite cannot drift apart.
    SQRT3_2_Q15 = 28378        # round(sqrt(3)/2 * 32768) = round(0.8660254*2^15)
    HALF_Q15 = 16384           # 0.5 in Q15 (MULQ by it == arithmetic >> 1)

    def __init__(self, name: str, face_alpha: str = "west",
                 face_beta: str = "south"):
        super().__init__(name, face_alpha=face_alpha, face_beta=face_beta)
        if face_alpha == face_beta:
            raise ValueError(
                f"HARDWARE LIMIT: SVPWMBlock distinguishes its two input "
                f"streams by ARRIVAL FACE, so face_alpha and face_beta must "
                f"differ; got both = {face_alpha!r}. (A same-face pair cannot "
                f"be told apart by the arbiter and would mis-pair permanently "
                f"— the face IS the stream identity.)")
        self._face_alpha, self._face_beta = face_alpha, face_beta

    @property
    def cell_count(self) -> int:
        return 7

    @property
    def interface(self) -> BlockInterface:
        # ONE output register — load-bearing: with >1 the build classifies the
        # emit cell as a COMPLEX rail source and steers the packet's words to
        # consecutive registers under one trigger, collapsing the 3-word packet
        # (the FeaturePairJoin condition (a), asserted by the suite).
        return BlockInterface(entry_address=1, input_registers=[0],
                              output_registers=[0])

    # ------------------------------------------------------------------ cells
    def build_cell_programs(self):
        fa = self._FACE.get(self._face_alpha, 2)   # west
        fb = self._FACE.get(self._face_beta, 0)    # south
        cells = {}

        # (1) rendezvous — the N=2 LOCK rotation with the N+1th stop.
        # Both arms land in R0, each on its OWN face-gated trigger, latched
        # immediately. got_beta forwards the pair and locks to the INTERNAL
        # forward face (which no arm arrives on, barring both producers) until
        # `scale` releases the lock back to face_alpha (INV-46 Rule 3 — the
        # straight re-lock is the measured saturated deadlock).
        cells["rendezvous"] = CellProgram(
            # Each input port declares ITS OWN entry: the alpha producer must
            # JUMP got_alpha, the beta producer got_beta — the entries run
            # DIFFERENT code. Without the declaration both producers resolve
            # the single default entry and the rendezvous deadlocks (0 egress).
            inputs=[Port("v_alpha", register=0, entry="got_alpha"),
                    Port("v_beta", register=0, entry="got_beta")],
            outputs=[Port("fa"), Port("fb"), Port("ftrig")],
            # got_alpha / got_beta (NO arm entry — boots pre-locked) plus
            # `relock`, the serialize-LOCK release target: it re-points the
            # lock from THIS cell's face_alpha word, the copy the build's
            # face-reconciliation pass patches to the ROUTED arm geometry (an
            # authored copy in the release cell would not be reconciled — the
            # measured stale-alpha failure, see the class docstring).
            entries=[EntryPoint("got_alpha"), EntryPoint("got_beta"),
                     EntryPoint("relock")],
            data=[DataWord("face_alpha", fa, address=1, is_face=True),
                  DataWord("face_beta", fb, address=2, is_face=True),
                  # The INTERNAL forward face (toward `scale`). Locking to it
                  # bars both external arms — nothing arrives on it — which is
                  # how the block holds the next pair until `scale` releases.
                  # is_face so it D4-transforms with the block (INV-23).
                  DataWord("face_fwd", 1, address=3, is_face=True)],
            # INV-33: pin every state register explicitly.
            state=[StateVar("xa", register=4), StateVar("xb", register=5)],
            assembly_template=(
                "got_alpha:\n"
                "    MOVE R{state:xa}, R{in:v_alpha}\n"
                "    MOVE [LOCK_FACE], R{data:face_beta}\n"
                "    HALT\n"
                "got_beta:\n"
                "    MOVE R{state:xb}, R{in:v_beta}\n"
                "    MOVE R0, R{state:xa}\n"
                "    {write:fa}\n"
                "    MOVE R0, R{state:xb}\n"
                "    {write:fb}\n"
                "    {jump:ftrig}\n"
                # Bar BOTH arms (lock to the internal face nothing drives)
                # until `scale` has dispatched and jumps `relock`. This also
                # ADMITS the release: the relock jump arrives on the internal
                # forward face, so the arbiter holds it until this very
                # instruction has run — the release cannot outrace the bar.
                "    MOVE [LOCK_FACE], R{data:face_fwd}\n"
                "    HALT\n"
                "relock:\n"
                # Re-admit the next pair, from the RECONCILED face word.
                "    MOVE [LOCK_FACE], R{data:face_alpha}\n"
                "    HALT\n"
            ),
            # Cold start: boot already LOCKED to face_alpha (LOCK=1 +
            # LOCK_FACE=face_alpha) so the FIRST word is accepted ONLY on the
            # alpha face — no arm JUMP, no race.
            initial_lock_face=fa,
        )

        # (2) scale — the Q15 pre-products AND the serialize-LOCK release.
        #   nh = -(v_alpha/2)  (MULQ by 16384 = floor halving, then 0 - x)
        #   t  = v_beta * sqrt(3)/2
        # forwards (nh, t, va) to `clarke`, then releases the rendezvous.
        #
        # THE RELEASE (INV-19/20) rides THIS cell because it is the ONE cell
        # abutting the rendezvous — the TMRVoter geometry — but it is a
        # BACKWARD JUMP into the rendezvous's `relock` entry, NOT the
        # TMRVoter's value-carrying WRITE.CFG: the LOCK_FACE value must be arm
        # alpha's ROUTED face, and only the rendezvous's own reconciled
        # face_alpha word knows it (an authored copy here mis-aims the lock on
        # ~70% of routed layouts — measured; see the class docstring).
        # `face_back` is the direction back into the rendezvous (WEST at
        # identity, D4-transformed); `face_tap` restores the EAST forward so
        # later handoffs are not diverted. The jump is authored LAST so it is
        # the cell's HIGHEST-addressed JUMP (INV-53: that is the one a
        # declared backward edge patch targets). No trailing HALT: R31 is
        # always HALT (the resolver's invariant).
        cells["scale"] = CellProgram(
            inputs=[Port("valpha", register=1), Port("vbeta", register=2)],
            outputs=[Port("fnh"), Port("ft"), Port("fva"), Port("ftrig"),
                     Port("unlock")],
            entries=[EntryPoint("scale")],
            data=[DataWord("half", self.HALF_Q15, address=3),
                  DataWord("k", self.SQRT3_2_Q15, address=4),
                  DataWord("zero", 0, address=5),
                  DataWord("face_back", 2, address=6, is_face=True),
                  DataWord("face_tap", 1, address=7, is_face=True)],
            assembly_template=(
                "scale:\n"
                "    MULQ R{in:valpha}, R{data:half}\n"   # R0 = va/2 (floor)
                "    SUB R{data:zero}, R0\n"              # R0 = -va/2
                "    {write:fnh}\n"
                "    MULQ R{in:vbeta}, R{data:k}\n"       # R0 = t
                "    {write:ft}\n"
                "    MOVE R0, R{in:valpha}\n"             # va rides unchanged
                "    {write:fva}\n"
                "    {jump:ftrig}\n"
                # THE SERIALIZE-LOCK RELEASE: flip WEST, jump the rendezvous's
                # relock entry (it re-points its own lock from its reconciled
                # face_alpha word), restore the EAST forward.
                "    MOVE [FACE], R{data:face_back}\n"
                "    {jump:unlock}\n"
                "    MOVE [FACE], R{data:face_tap}\n"
            ),
        )

        # (3) clarke — the inverse-Clarke b/c phases, SATURATING.
        #   vb = sat(nh + t), vc = sat(nh - t); va rides through.
        # The V-flag clamp: on signed overflow the wrapped result's sign is the
        # INVERSE of the true one, so N=1 -> clamp 0x7FFF, N=0 -> clamp 0x8000.
        # MOVE does not touch flags, so the BR.N after the first clamp MOVE
        # still tests the ADD/SUB's flags — load-bearing, not a coincidence.
        cells["clarke"] = CellProgram(
            inputs=[Port("va", register=1), Port("nh", register=2),
                    Port("t", register=3)],
            outputs=[Port("fva"), Port("fvb"), Port("fvc"), Port("ftrig")],
            entries=[EntryPoint("clarke")],
            data=[DataWord("qmax", 0x7FFF, address=4),
                  DataWord("qmin", 0x8000, address=5)],
            assembly_template=(
                "clarke:\n"
                "    ADD R{in:nh}, R{in:t}\n"      # vb = nh + t
                "    BR.NV vb_ok\n"
                "    MOVE R0, R{data:qmax}\n"
                "    BR.N vb_ok\n"
                "    MOVE R0, R{data:qmin}\n"
                "vb_ok:\n"
                "    {write:fvb}\n"
                "    SUB R{in:nh}, R{in:t}\n"      # vc = nh - t
                "    BR.NV vc_ok\n"
                "    MOVE R0, R{data:qmax}\n"
                "    BR.N vc_ok\n"
                "    MOVE R0, R{data:qmin}\n"
                "vc_ok:\n"
                "    {write:fvc}\n"
                "    MOVE R0, R{in:va}\n"
                "    {write:fva}\n"
                "    {jump:ftrig}\n"
            ),
        )

        # (4) maxsel — max(va, vb, vc) by SIGNED compare (CMP + BR.GE on the
        # SLT flag). This IS the "which sector" upper half: which phase is the
        # max changes every 60 degrees.
        cells["maxsel"] = CellProgram(
            inputs=[Port("va", register=1), Port("vb", register=2),
                    Port("vc", register=3)],
            outputs=[Port("fva"), Port("fvb"), Port("fvc"), Port("fmx"),
                     Port("ftrig")],
            entries=[EntryPoint("maxsel")],
            data=[],
            # INV-33: pinned, and placed ABOVE the inputs (a cell with no data
            # words auto-scans state from R0 — pinning is mandatory here).
            state=[StateVar("mx", register=4)],
            assembly_template=(
                "maxsel:\n"
                "    MOVE R{state:mx}, R{in:va}\n"
                "    CMP R{state:mx}, R{in:vb}\n"   # mx - vb: GE -> keep mx
                "    BR.GE mx_b\n"
                "    MOVE R{state:mx}, R{in:vb}\n"
                "mx_b:\n"
                "    CMP R{state:mx}, R{in:vc}\n"
                "    BR.GE mx_c\n"
                "    MOVE R{state:mx}, R{in:vc}\n"
                "mx_c:\n"
                "    MOVE R0, R{state:mx}\n"
                "    {write:fmx}\n"
                "    MOVE R0, R{in:va}\n"
                "    {write:fva}\n"
                "    MOVE R0, R{in:vb}\n"
                "    {write:fvb}\n"
                "    MOVE R0, R{in:vc}\n"
                "    {write:fvc}\n"
                "    {jump:ftrig}\n"
            ),
        )

        # (5) minsel — min(va, vb, vc) (the sector lower half), then the
        # midpoint m = floor(mx/2) + floor(mn/2). Each half is within +-16384,
        # so the plain ADD cannot overflow — which is exactly why the midpoint
        # is computed as two halvings and not as (mx + mn) >> 1 (a 16-bit
        # mx + mn CAN overflow).
        cells["minsel"] = CellProgram(
            inputs=[Port("va", register=1), Port("vb", register=2),
                    Port("vc", register=3), Port("mx", register=4)],
            outputs=[Port("fva"), Port("fvb"), Port("fvc"), Port("fm"),
                     Port("ftrig")],
            entries=[EntryPoint("minsel")],
            data=[DataWord("half", self.HALF_Q15, address=5)],
            state=[StateVar("mn", register=6)],
            assembly_template=(
                "minsel:\n"
                "    MOVE R{state:mn}, R{in:va}\n"
                "    CMP R{in:vb}, R{state:mn}\n"   # vb - mn: GE -> keep mn
                "    BR.GE mn_b\n"
                "    MOVE R{state:mn}, R{in:vb}\n"
                "mn_b:\n"
                "    CMP R{in:vc}, R{state:mn}\n"
                "    BR.GE mn_c\n"
                "    MOVE R{state:mn}, R{in:vc}\n"
                "mn_c:\n"
                "    MULQ R{state:mn}, R{data:half}\n"   # R0 = floor(mn/2)
                "    MOVE R{state:mn}, R0\n"             # reuse mn as the half
                "    MULQ R{in:mx}, R{data:half}\n"      # R0 = floor(mx/2)
                "    ADD R0, R{state:mn}\n"              # R0 = m (no overflow)
                "    {write:fm}\n"
                "    MOVE R0, R{in:va}\n"
                "    {write:fva}\n"
                "    MOVE R0, R{in:vb}\n"
                "    {write:fvb}\n"
                "    MOVE R0, R{in:vc}\n"
                "    {write:fvc}\n"
                "    {jump:ftrig}\n"
            ),
        )

        # (6) inject — duty_i = sat(v_i - m), the common-mode subtraction,
        # same V-flag clamp as `clarke`.
        cells["inject"] = CellProgram(
            inputs=[Port("va", register=1), Port("vb", register=2),
                    Port("vc", register=3), Port("m", register=4)],
            outputs=[Port("fda"), Port("fdb"), Port("fdc"), Port("ftrig")],
            entries=[EntryPoint("inject")],
            data=[DataWord("qmax", 0x7FFF, address=5),
                  DataWord("qmin", 0x8000, address=6)],
            assembly_template=(
                "inject:\n"
                "    SUB R{in:va}, R{in:m}\n"
                "    BR.NV da_ok\n"
                "    MOVE R0, R{data:qmax}\n"
                "    BR.N da_ok\n"
                "    MOVE R0, R{data:qmin}\n"
                "da_ok:\n"
                "    {write:fda}\n"
                "    SUB R{in:vb}, R{in:m}\n"
                "    BR.NV db_ok\n"
                "    MOVE R0, R{data:qmax}\n"
                "    BR.N db_ok\n"
                "    MOVE R0, R{data:qmin}\n"
                "db_ok:\n"
                "    {write:fdb}\n"
                "    SUB R{in:vc}, R{in:m}\n"
                "    BR.NV dc_ok\n"
                "    MOVE R0, R{data:qmax}\n"
                "    BR.N dc_ok\n"
                "    MOVE R0, R{data:qmin}\n"
                "dc_ok:\n"
                "    {write:fdc}\n"
                "    {jump:ftrig}\n"
            ),
        )

        # (7) emit — the THREE-BURST egress that makes the [a, b, c] packet.
        # Three independent WRITE+JUMP deliveries into the SAME downstream
        # register and entry (the FeaturePairJoin/TMRVoter output shape). The
        # cell deliberately carries NO internal handoff and NO WRITE.CFG so the
        # build's full-cell patch covers ALL THREE bursts identically.
        cells["emit"] = CellProgram(
            inputs=[Port("da", register=1), Port("db", register=2),
                    Port("dc", register=3)],
            outputs=[Port("out"), Port("trig"), Port("out2"), Port("trig2"),
                     Port("out3"), Port("trig3")],
            entries=[EntryPoint("emit")],
            assembly_template=(
                "emit:\n"
                "    MOVE R0, R{in:da}\n"
                "    {write:out}\n"
                "    {jump:trig}\n"
                "    MOVE R0, R{in:db}\n"
                "    {write:out2}\n"
                "    {jump:trig2}\n"
                "    MOVE R0, R{in:dc}\n"
                "    {write:out3}\n"
                "    {jump:trig3}\n"
            ),
        )
        return cells

    def internal_connections(self):
        return [
            ("rendezvous", "fa", "scale", "valpha"),
            ("rendezvous", "fb", "scale", "vbeta"),
            ("scale", "fnh", "clarke", "nh"),
            ("scale", "ft", "clarke", "t"),
            ("scale", "fva", "clarke", "va"),
            ("clarke", "fvb", "maxsel", "vb"),
            ("clarke", "fvc", "maxsel", "vc"),
            ("clarke", "fva", "maxsel", "va"),
            ("maxsel", "fmx", "minsel", "mx"),
            ("maxsel", "fva", "minsel", "va"),
            ("maxsel", "fvb", "minsel", "vb"),
            ("maxsel", "fvc", "minsel", "vc"),
            ("minsel", "fm", "inject", "m"),
            ("minsel", "fva", "inject", "va"),
            ("minsel", "fvb", "inject", "vb"),
            ("minsel", "fvc", "inject", "vc"),
            ("inject", "fda", "emit", "da"),
            ("inject", "fdb", "emit", "db"),
            ("inject", "fdc", "emit", "dc"),
        ]

    def internal_jumps(self):
        # EVERY declared EntryPoint is the target of at least one edge (INV-39,
        # with the rendezvous got_* entries targeted by the EXTERNAL
        # producers). The LAST edge is the BACKWARD serialize-LOCK release,
        # scale -> rendezvous.relock (see the scale cell's note); it carries
        # no data, so it lives here and not in internal_connections — a data
        # edge aimed at a real input port would make portmap classify it as a
        # feedback RETURN and DROP that arm from the block's external inputs
        # (the TMRVoter's measured trap).
        return [
            ("rendezvous", "ftrig", "scale", "scale"),
            ("scale", "ftrig", "clarke", "clarke"),
            ("clarke", "ftrig", "maxsel", "maxsel"),
            ("maxsel", "ftrig", "minsel", "minsel"),
            ("minsel", "ftrig", "inject", "inject"),
            ("inject", "ftrig", "emit", "emit"),
            ("scale", "unlock", "rendezvous", "relock"),
        ]

    def output_cell_ids(self):
        return ["emit"]

    def default_layout(self):
        # A STRICT COLINEAR CHAIN with `rendezvous` a LEAF (exactly ONE
        # in-block neighbour, `scale`), leaving free faces for the two arms.
        # 7x1 <= 8 across (INV-9); the release is a single backward abutment
        # from `scale`. See the class docstring for why the longitudinal shape
        # is the right one here.
        return {"rendezvous": (0, 0, "east"),
                "scale": (1, 0, "east"),
                "clarke": (2, 0, "east"),
                "maxsel": (3, 0, "east"),
                "minsel": (4, 0, "east"),
                "inject": (5, 0, "east"),
                "emit": (6, 0, "east")}

    # -------------------------------------------------------------- reference
    @staticmethod
    def _s16(w: int) -> int:
        w = int(w) & 0xFFFF
        return w - 0x10000 if w >= 0x8000 else w

    @staticmethod
    def _sat16(v: int) -> int:
        return max(-32768, min(32767, int(v)))

    @classmethod
    def duties(cls, v_alpha: int, v_beta: int) -> tuple:
        """The EXACT integer model of the shipped arithmetic — the golden.

        Takes the two Q15 input words (as raw uint16 or signed int16), returns
        the three SIGNED duty words ``(duty_a, duty_b, duty_c)`` exactly as the
        chip computes them. Every step mirrors one instruction sequence of the
        block (see the class docstring, "the exact chip arithmetic"); the
        on-chip gate compares word-for-word against this.

        There is no stock GNU Radio counterpart, so this model is written
        directly from the specification and cross-checked against the float
        reference (:meth:`duties_float`) within a derived quantization bound.
        """
        va = cls._s16(v_alpha)
        vbeta = cls._s16(v_beta)
        # scale cell: MULQ = floor((a*b) >> 15) (measured chip semantics).
        nh = -((va * cls.HALF_Q15) >> 15)
        t = (vbeta * cls.SQRT3_2_Q15) >> 15
        # clarke cell: saturating adds.
        pa = va
        pb = cls._sat16(nh + t)
        pc = cls._sat16(nh - t)
        # maxsel/minsel: the sector compare tree + the midpoint.
        mx = max(pa, pb, pc)
        mn = min(pa, pb, pc)
        m = ((mx * cls.HALF_Q15) >> 15) + ((mn * cls.HALF_Q15) >> 15)
        # inject: the saturating common-mode subtraction.
        return (cls._sat16(pa - m), cls._sat16(pb - m), cls._sat16(pc - m))

    @classmethod
    def phases(cls, v_alpha: int, v_beta: int) -> tuple:
        """The PRE-injection integer three-phase set ``(pa, pb, pc)`` — exposed
        so the suite can state the centering invariant exactly (its bound is
        the parity sum of the pre-injection extremes) and derive the sector."""
        va = cls._s16(v_alpha)
        vbeta = cls._s16(v_beta)
        nh = -((va * cls.HALF_Q15) >> 15)
        t = (vbeta * cls.SQRT3_2_Q15) >> 15
        return (va, cls._sat16(nh + t), cls._sat16(nh - t))

    @staticmethod
    def duties_float(v_alpha: float, v_beta: float) -> tuple:
        """The textbook float reference: inverse Clarke + min-max injection.
        The chip result must stay within a stated Q15 quantization bound of
        this (the exact gate is :meth:`duties`; this bounds the model itself)."""
        s = np.sqrt(3.0) / 2.0
        pa = v_alpha
        pb = -v_alpha / 2.0 + s * v_beta
        pc = -v_alpha / 2.0 - s * v_beta
        m = (max(pa, pb, pc) + min(pa, pb, pc)) / 2.0
        clip = lambda x: max(-1.0, min(32767.0 / 32768.0, x))  # noqa: E731
        return (clip(pa - m), clip(pb - m), clip(pc - m))

    @classmethod
    def sector(cls, v_alpha: int, v_beta: int) -> tuple:
        """The sector as the block actually selects it: (argmax, argmin) over
        the pre-injection phases, ties resolved EXACTLY as the compare tree
        does (first strictly-greater / strictly-smaller wins, scan order
        a, b, c — a tie keeps the earlier phase). Six (argmax, argmin) values
        occur over a rotation; the sweep gate asserts all six are seen."""
        pa, pb, pc = cls.phases(v_alpha, v_beta)
        mx, imx = pa, 0
        if pb > mx:
            mx, imx = pb, 1
        if pc > mx:
            mx, imx = pc, 2
        mn, imn = pa, 0
        if pb < mn:
            mn, imn = pb, 1
        if pc < mn:
            mn, imn = pc, 2
        return (imx, imn)

    @classmethod
    def process_reference_words(cls, alpha_words, beta_words) -> list:
        """N complete (v_alpha, v_beta) pairs in, the FLAT 3-word-per-sample
        packet stream ``[a0, b0, c0, a1, b1, c1, ...]`` out (uint16).

        A packet is emitted ONLY when both arms have supplied their word, so an
        arm starved after ``k`` words yields exactly ``k`` packets — the
        reference truncates to the shortest arm. The ARRIVAL ORDER is
        deliberately absent from this signature: the point of the LOCK
        rendezvous is that the output does not depend on it."""
        n = min(len(alpha_words), len(beta_words))
        out: list = []
        for i in range(n):
            da, db, dc = cls.duties(alpha_words[i], beta_words[i])
            out.extend((da & 0xFFFF, db & 0xFFFF, dc & 0xFFFF))
        return out

    def process_reference(self, input_samples) -> np.ndarray:
        """Carrier semantics only — the real contract is
        :meth:`process_reference_words` (two independent input arms, a 3-word
        packet stream out; a shared single-stream harness cannot express it).
        The block's own suite drives the real two-arm chain on chip."""
        return np.asarray(input_samples)

    def reset(self):
        pass
