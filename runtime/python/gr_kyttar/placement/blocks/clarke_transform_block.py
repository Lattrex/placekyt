# SPDX-License-Identifier: GPL-3.0-or-later
"""ClarkeTransformBlock — see :class:`ClarkeTransformBlock`."""
import numpy as np

from ..block import CellProgram, DataWord, EntryPoint, Port, StateVar
from ._base import BlockInterface, KyttarBlock


class ClarkeTransformBlock(KyttarBlock):
    """Two-input Clarke (abc -> alpha-beta) transform for 3-phase FOC (1 cell) —
    joins TWO INDEPENDENT phase-current streams by the arbiter LOCK and emits
    the stationary-frame pair as one complex packet:

        i_alpha[n] = ia[n]
        i_beta[n]  = (ia[n] + 2*ib[n]) / sqrt(3)
                   = ia[n]*(1/sqrt(3)) + ib[n]*(2/sqrt(3))

    (The amplitude-invariant two-current form: with ia + ib + ic = 0 the third
    phase current is redundant, so only ia and ib are sensed — the standard
    two-shunt FOC front end.)

    WHY A RENDEZVOUS (INV-46, at N=2). The two phase currents come from two
    independent on-chip chains (two ADC/source paths) firing at ASYNCHRONOUS
    times, and on this clockless array the ONLY stream identity available is
    the physical channel — the arrival FACE. A same-face counter desyncs
    PERMANENTLY on the first re-ordering (ia, ia, ib, ...), and a mis-paired
    Clarke output is a plausible-looking current vector, so the corruption
    would be SILENT — exactly the XorJoin argument. The cell therefore uses the
    ISA arbiter lock (``LOCK`` / ``LOCK_FACE``, CONFIG 4/3) to accept an input
    from exactly ONE face at a time and ROTATES it ia -> ib -> ia; a fast or
    bursty producer on the other face is simply HELD by the arbiter until it is
    that face's turn. Hence ``NEEDS_DISTINCT_INPUT_FACES``: the face IS the
    stream identity, and the build DRC rejects a same-face landing.

        (cold start): LOCK=1, LOCK_FACE=face_ia    (accept ONLY ia)
        got_ia (face_ia): latch ia ; LOCK_FACE = face_ib ; HALT
        got_ib (face_ib): compute + emit (yi=i_alpha, yq=i_beta) ;
                          LOCK_FACE = face_ia ; HALT

    The cold start is BAKED into the boot CONFIG via ``initial_lock_face`` —
    arming via a JUMP is a RACE (a word arriving before the arm-JUMP is
    accepted on an unlocked face and mis-pairs). The re-lock to ``face_ia`` is
    the LAST thing ``got_ib`` does, so the next sample's ib word cannot barge
    in before its ia word has been latched (no cross-sample mixing).

    THE ARITHMETIC (Q15, all constants derived — INV-15 for the >1 gain).
    ``1/sqrt(3)`` = 0.57735 is Q15-representable: C = round(32768/sqrt(3)) =
    18919. ``2/sqrt(3)`` = 1.1547 is NOT (Q15 tops out just below 1.0), so the
    coefficient is stored HALVED and applied TWICE (INV-15): the ib term is
    computed once as ``t_b = MULQ(ib, C)`` and ADDED TWICE. Every add
    SATURATES to the Q15 rails (INV-13 / the AddBlock idiom): |i_beta| reaches
    1.73 at full-scale same-sign inputs, so a bare ADD would WRAP — a sign
    flip on a current vector, the classic Q15 footgun. The exact chip
    arithmetic, in evaluation order (the golden models THIS, bit for bit):

        t_b    = (ib * C) >> 15            (signed, truncating MULQ)
        t_a    = (ia * C) >> 15
        s1     = sat16(t_a + t_b)          (saturating ADD #1)
        i_beta = sat16(s1 + t_b)           (saturating ADD #2)

    The saturating ADD is the proven flag-restore form: ``ADD`` sets V on
    signed overflow; on overflow the rail is rebuilt from the OPERAND's sign
    (``SHR #15`` is a LOGICAL shift -> 0 or 1; ``+ 0x7FFF`` -> 0x7FFF or
    0x8000). Restoring from ``t_b`` (not a saved accumulator) is valid because
    a signed add can only overflow when both addends share a sign — this saves
    the AddBlock's accumulator-save MOVE and its state register, which is what
    lets the whole block fit ONE cell (29/32 words).

    i_alpha is ``ia`` UNCHANGED (exact, no arithmetic).

    OUTPUT — one COMPLEX packet per matched pair: ``yi`` = i_alpha, ``yq`` =
    i_beta, one ``trig`` (the ComplexMixer 2-rail shape, ``output_registers=
    [0, 1]`` so the build steers the rails to consecutive registers/tags). In
    GRC the block is 2 float in -> 1 complex out (alpha + j*beta), the natural
    input shape for the Park rotation stage that follows it in an FOC loop.
    ``yi`` is emitted EARLY (right after the operands are secured, before the
    beta arithmetic) — write ORDER is what steers the rails, not adjacency,
    and emitting alpha while it is still in R0 saves a state register + two
    MOVEs.

    WHY IT NEEDS NO SERIALIZE-LOCK (INV-19). At N=2 the face budget (INV-46:
    N + 2 = 4) lets the whole rendezvous + datapath be ONE cell, and a single
    cell has no internal datapath for queued samples to pile into: the arbiter
    LOCK the block already carries IS the serialization INV-19 prescribes, so
    the saturated drive equals the per-sample drive with no extra machinery
    (the XorJoinBlock argument, verbatim — proven by this block's own
    saturated gate).

    Parameters:
      * ``face_ia`` / ``face_ib``: the faces the two producers arrive on
        ('south'|'east'|'west'|'north'). MUST be distinct — the placer
        reserves two distinct free faces, the router lands one net on each,
        and the build reconciles these params + ``initial_lock_face`` with the
        faces the router actually chose. PLACEMENT/ROUTING internals, not
        user-facing DSP params, so NOT exposed in GRC (the
        DualFloatToComplex/XorJoin convention). Like the transform itself, the
        block has no user-facing parameters.
    """
    CATEGORY = "signal_conditioning"
    TAGS = ["clarke", "transform", "foc", "motor", "rendezvous", "lock",
            "alpha_beta", "three_phase"]

    # The DEFINING property of the LOCK-rotation family (INV-46): the inputs
    # are independent async streams distinguishable ONLY by arrival face, so
    # the placer MUST land them on DISTINCT faces and the build DRC MUST
    # reject a same-face landing.
    NEEDS_DISTINCT_INPUT_FACES = True

    # (input port, face DataWord) pairs in FIRST-ACCEPTED order for the
    # build's face-reconciliation pass (the cell boots locked to entry [0]'s
    # face). Without this declaration the pass falls back to the
    # DualFloatToComplex ``i``/``q`` names and becomes a SILENT NO-OP: the
    # LOCK then gates the authored placeholder faces, the real producers are
    # barred, and the chain builds + routes perfectly while emitting ZERO
    # output (measured on FeaturePairJoin — INV-46 Rule 1).
    RENDEZVOUS_FACE_PORTS = (("ia", "face_ia"), ("ib", "face_ib"))

    # face_ia/face_ib are the two distinct arrival faces the placer + router
    # choose and the build reconciles — pure placement/routing internals.
    GRC_UNSUPPORTED_PARAMS = ("face_ia", "face_ib")

    _FACE = {"south": 0, "east": 1, "west": 2, "north": 3}

    # 1/sqrt(3) in Q15 — round(32768 / sqrt(3)) = 18919. Also the HALVED store
    # of the 2/sqrt(3) coefficient (INV-15): 2/sqrt(3) in Q14 quantizes to the
    # SAME word, so one data word serves both terms and the ib term is simply
    # added twice.
    INV_SQRT3_Q15 = 18919
    SAT_POS_Q15 = 0x7FFF

    def __init__(self, name: str, face_ia: str = "west", face_ib: str = "south"):
        super().__init__(name, face_ia=face_ia, face_ib=face_ib)
        if face_ia == face_ib:
            raise ValueError(
                f"HARDWARE LIMIT: ClarkeTransformBlock distinguishes its two "
                f"independent phase-current producers by ARRIVAL FACE, so "
                f"face_ia and face_ib must differ; got both = {face_ia!r}. "
                f"(A same-face pair cannot be told apart by the arbiter and "
                f"would mis-pair permanently — and a mis-paired Clarke output "
                f"is a plausible-looking current vector, so the corruption "
                f"would be SILENT.)")
        self._face_ia, self._face_ib = face_ia, face_ib

    @property
    def cell_count(self) -> int:
        return 1

    @property
    def interface(self) -> BlockInterface:
        # TWO output registers: the block is a genuine COMPLEX 2-rail source
        # (yi = i_alpha, yq = i_beta emitted per trigger). The build keys its
        # complex-packet patchers (abutted, brokered, AND output-port) on
        # ``len(output_registers) > 1`` — declaring one register would collapse
        # both rails onto one downstream register (INV-23's orientation bug
        # class).
        return BlockInterface(entry_address=1, input_registers=[0],
                              output_registers=[0, 1])

    def build_cell_programs(self):
        fa = self._FACE.get(self._face_ia, 2)   # west
        fb = self._FACE.get(self._face_ib, 0)   # south
        tmpl = (
            "got_ia:\n"
            "    MOVE R{state:xa}, R{in:ia}\n"          # latch ia (on face_ia)
            "    MOVE [LOCK_FACE], R{data:face_ib}\n"   # now accept ONLY face_ib
            "    HALT\n"
            "got_ib:\n"
            # R0 = ib (delivered to the shared input register on the ib
            # face-gated trigger; the MOVE-to-self a `MOVE R0, R{in:ib}` would
            # assemble to is skipped — INV-46 Rule 5).
            "    MULQ R0, R{data:inv_sqrt3}\n"          # t_b = (ib*C) >> 15
            "    MOVE R{state:tb}, R0\n"
            "    MOVE R0, R{state:xa}\n"
            "    {write:yi}\n"                          # i_alpha = ia (rail 0, exact)
            "    MULQ R0, R{data:inv_sqrt3}\n"          # t_a = (ia*C) >> 15
            # Saturating ADD #1: s1 = sat16(t_a + t_b). ADD sets V on signed
            # overflow; overflow implies sign(t_a) == sign(t_b), so the rail is
            # rebuilt from t_b's sign: SHR #15 is LOGICAL -> 0 or 1; + 0x7FFF
            # -> 0x7FFF or 0x8000.
            "    ADD R0, R{state:tb}\n"
            "    BR.NV +3\n"
            "    MOVE R0, R{state:tb}\n"
            "    SHR R0, #15\n"
            "    ADD R0, R{data:satpos}\n"
            # Saturating ADD #2: i_beta = sat16(s1 + t_b) — the second half of
            # the INV-15 halved 2/sqrt(3) coefficient.
            "    ADD R0, R{state:tb}\n"
            "    BR.NV +3\n"
            "    MOVE R0, R{state:tb}\n"
            "    SHR R0, #15\n"
            "    ADD R0, R{data:satpos}\n"
            "    {write:yq}\n"                          # i_beta (rail 1)
            "    {jump:trig}\n"                         # ONE trigger per pair
            # Re-lock LAST, so the next sample's ib word cannot barge in
            # before its ia word has been latched (no cross-sample mixing).
            "    MOVE [LOCK_FACE], R{data:face_ia}\n"
            "    HALT\n"
        )
        return {0: CellProgram(
            # Each input port declares ITS OWN entry: a producer into ``ia``
            # must JUMP got_ia, one into ``ib`` must JUMP got_ib — the two
            # entries run DIFFERENT code (latch-and-handover vs
            # compute-and-emit). Without the declaration every producer
            # resolves the single default entry, got_ib never runs, and the
            # rendezvous deadlocks (0 egress).
            inputs=[Port("ia", register=0, entry="got_ia"),
                    Port("ib", register=0, entry="got_ib")],
            outputs=[Port("yi"), Port("yq"), Port("trig")],
            # ONLY got_ia / got_ib — NO arm entry (the cell boots pre-locked).
            entries=[EntryPoint("got_ia"), EntryPoint("got_ib")],
            data=[DataWord("face_ia", fa, address=1, is_face=True),
                  DataWord("face_ib", fb, address=2, is_face=True),
                  DataWord("inv_sqrt3", self.INV_SQRT3_Q15, address=3),
                  DataWord("satpos", self.SAT_POS_Q15, address=4)],
            # INV-33: pin every state register explicitly (an unpinned StateVar
            # lands on top of R0 and the inputs).
            state=[StateVar("xa", register=5), StateVar("tb", register=6)],
            assembly_template=tmpl,
            # Cold start: boot already LOCKED to face_ia (LOCK=1 + LOCK_FACE=
            # face_ia) so the FIRST word is accepted ONLY on the ia face — no
            # arm, no race.
            initial_lock_face=fa,
        )}

    # -------------------------------------------------------------- reference
    @staticmethod
    def _s16(w) -> int:
        w = int(w) & 0xFFFF
        return w - 0x10000 if w >= 0x8000 else w

    @classmethod
    def process_reference_words(cls, ia_words, ib_words) -> list:
        """The block's contract as a pure function — the GOLDEN.

        There is no stock GNU Radio Clarke block, so this is the pinned exact
        integer model of the SHIPPED chip arithmetic (constants, evaluation
        order, truncating MULQ, per-step saturation — the poly1305_golden
        pattern), compared word for word against the real chip. N matched
        (ia, ib) pairs in, the interleaved word stream
        ``[alpha0, beta0, alpha1, beta1, ...]`` out.

        A pair is emitted ONLY when BOTH arms have supplied their word, so an
        arm starved after ``k`` words yields exactly ``k`` pairs — the
        reference truncates to the shorter arm. The ARRIVAL ORDER is
        deliberately absent from this signature: the whole point of the LOCK
        rendezvous is that the output does not depend on it."""
        C = cls.INV_SQRT3_Q15
        n = min(len(ia_words), len(ib_words))
        out: list[int] = []
        for i in range(n):
            ia = cls._s16(ia_words[i])
            ib = cls._s16(ib_words[i])
            t_b = (ib * C) >> 15            # truncating signed MULQ
            t_a = (ia * C) >> 15
            s1 = t_a + t_b
            if not (-32768 <= s1 <= 32767):     # ADD #1 overflow -> V set
                s1 = 32767 if t_b >= 0 else -32768   # rail from t_b's sign
            beta = s1 + t_b
            if not (-32768 <= beta <= 32767):   # ADD #2 overflow -> V set
                beta = 32767 if t_b >= 0 else -32768
            out.append(ia & 0xFFFF)             # i_alpha = ia, exact
            out.append(beta & 0xFFFF)
        return out

    def process_reference(self, input_samples) -> np.ndarray:
        """Float reference for the generic harness: the two streams are
        carried as one complex array (real = ia, imag = ib); returns
        i_alpha + j*i_beta with the ideal float arithmetic, clamped to the Q15
        range. The bit-exact contract is :meth:`process_reference_words` — the
        substrate proof is that only MATCHED pairs are transformed, which the
        block's suite drives with adversarial async interleavings."""
        arr = np.asarray(input_samples)
        if np.iscomplexobj(arr):
            ia = arr.real.astype(np.float64)
            ib = arr.imag.astype(np.float64)
        else:
            ia = arr.astype(np.float64)
            ib = np.zeros_like(ia)
        beta = (ia + 2.0 * ib) / np.sqrt(3.0)
        beta = np.clip(beta, -1.0, 32767.0 / 32768.0)
        return (ia + 1j * beta).astype(np.complex64)

    def reset(self):
        pass
