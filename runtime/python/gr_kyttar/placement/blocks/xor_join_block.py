# SPDX-License-Identifier: GPL-3.0-or-later
"""XorJoinBlock — see :class:`XorJoinBlock`."""
import numpy as np

from ..block import CellProgram, DataWord, EntryPoint, Port, StateVar
from ._base import BlockInterface, KyttarBlock


class XorJoinBlock(KyttarBlock):
    """Bitwise XOR of TWO INDEPENDENT producers (1 cell) — ``out = a ^ b``,
    rendezvoused by the arbiter LOCK and told apart by ARRIVAL FACE.

        out[n] = a[n] ^ b[n]           (bitwise, per element)

    WHY THIS IS NOT ``XorBlock``. ``XorBlock`` computes the same function but
    can only be fed from ONE source cell: its two operands arrive via the
    complex-burst fan-in, which keys on ``(src_cell, in_cell)`` and therefore
    requires both words to come from a single producer. It also declares only a
    default entry — no per-port entries — so the importer declines to arbitrate
    two independent producers into it. That is fine for a 2-operand op inside
    one chain and useless for the case this block exists for: a stream cipher's
    ``plaintext XOR keystream``, where the plaintext and the keystream are
    produced by two SEPARATE on-chip chains that fire at INDEPENDENT,
    ASYNCHRONOUS times.

    THE MECHANISM (INV-46, at N=2). Two independent producers on this clockless
    array can be told apart ONLY by the physical channel they arrive on — the
    FACE. A naive counter that alternates on one face desyncs PERMANENTLY on the
    first re-ordering (a, a, b, ...), and XOR is the worst possible place for
    that: ``a[1] ^ a[0]`` is a perfectly plausible-looking byte, so a mis-pairing
    is SILENT. This cell therefore uses the ISA arbiter lock (``LOCK`` /
    ``LOCK_FACE``, CONFIG 4/3) to accept an input from exactly ONE face at a
    time and ROTATES it a -> b -> a, so a fast or bursty producer on the other
    face is simply HELD by the arbiter until it is that face's turn. Hence
    ``NEEDS_DISTINCT_INPUT_FACES``: the face IS the stream identity, and the
    build DRC rejects a same-face landing.

        (cold start): LOCK=1, LOCK_FACE=face_a   (accept ONLY a)
        got_a (face_a): latch A ; LOCK_FACE = face_b ; HALT
        got_b (face_b): R0 = B ^ A ; emit ; LOCK_FACE = face_a ; HALT

    The cold start is BAKED into the boot CONFIG via ``initial_lock_face``.
    There is deliberately NO arm step: arming via a JUMP would be a RACE — a
    word arriving before the arm-JUMP is accepted on an UNLOCKED face and
    mis-pairs, the exact failure the LOCK exists to prevent.

    The re-lock to ``face_a`` is the LAST thing ``got_b`` does, so the next
    sample's ``b`` word cannot barge in before its ``a`` word has been latched
    (no cross-sample mixing).

    THE XOR ITSELF. ``XOR R0, R{state:va}`` is one native LOGIC op on the cell
    ALU, and LOGIC writes R0 — so the emit reads straight out of R0 with no
    further move. It is a PURE BITWISE op, not Q15 arithmetic: no rounding, no
    saturation, no overflow corner. A byte 0..255 rides the low 8 bits of the
    16-bit data word and XOR is bit-parallel, so the result is BIT-EXACT over
    the whole word.

    XOR IS ITS OWN INVERSE, which is the property the cipher decrypts with:
    ``(x ^ k) ^ k == x``. Because the block is a pure, stateless-per-sample
    function of a matched pair, the same block serves as both the encrypt and
    the decrypt half of a stream cipher.

    WHY IT NEEDS NO SERIALIZE-LOCK (INV-19), and why that is not luck. The
    face-budget arithmetic of INV-46 is ``N`` (one face per arm) ``+ 1``
    (forward into the block's datapath) ``+ 1`` (a release corridor coming back)
    ``= N + 2``. At N=2 that is FOUR and a cell has four — which is exactly why
    the N=2 members of this family are SINGLE-CELL, and a single cell needs
    neither a forward nor a release: there is no internal datapath for a group
    to pile into. The LOCK the block already carries IS the serialization
    (it is INV-19's own prescribed fix), so the saturated drive equals the
    per-sample drive with no extra machinery. The N=3 voter, needing five faces
    and having four, is the block where all of this becomes expensive.

    OUTPUT: ONE word per matched pair, a single brokered ``{write:out}`` +
    ``{jump:trig}`` — the ordinary single-rail handoff, 1:1 in rate (unlike
    ``FeaturePairJoinBlock``'s two-burst emit or ``TMRVoterBlock``'s
    ``[value, status]`` packet, both of which are rate-EXPANDING).

    Parameters:
      * ``face_a`` / ``face_b``: the faces the two producers arrive on
        ('south'|'east'|'west'|'north'). MUST be distinct — the placer reserves
        two distinct free faces, the router lands one net on each, and the build
        reconciles these params + ``initial_lock_face`` with the faces the
        router actually chose. They are PLACEMENT/ROUTING internals, not
        user-facing DSP params, so they are NOT exposed in GRC (the same
        convention ``DualFloatToComplexBlock`` and ``FeaturePairJoinBlock``
        use). Like GR's ``xor_bb``, the block has no user-facing parameters.
    """
    CATEGORY = "signal_conditioning"
    TAGS = ["xor", "join", "rendezvous", "lock", "logic", "byte", "crypto"]

    # The DEFINING property (shared with DualFloatToComplexBlock and
    # FeaturePairJoinBlock at N=2, TMRVoterBlock at N=3): the inputs are
    # independent async streams distinguishable ONLY by arrival face, so the
    # placer MUST land them on DISTINCT faces and the build DRC MUST reject a
    # same-face landing.
    NEEDS_DISTINCT_INPUT_FACES = True

    # The build's face-reconciliation pass (``_apply_rendezvous_input_faces``)
    # patches the authored placeholder faces to the ones the ROUTER actually
    # chose. It needs the (input port, face DataWord) pairs in FIRST-ACCEPTED
    # order — the cell boots locked to entry [0]'s face. Without this
    # declaration the pass falls back to the DualFloatToComplex ``i``/``q``
    # names and becomes a SILENT NO-OP: the LOCK then gates the authored
    # placeholder faces, the real producers are barred, and the chain builds +
    # routes perfectly while producing ZERO output.
    RENDEZVOUS_FACE_PORTS = (("a", "face_a"), ("b", "face_b"))

    # face_a/face_b are the two distinct arrival faces the placer + router
    # choose and the build reconciles — pure placement/routing internals.
    # Exposing them in GRC would invite a user to set a face the router then
    # overrides.
    GRC_UNSUPPORTED_PARAMS = ("face_a", "face_b")

    _FACE = {"south": 0, "east": 1, "west": 2, "north": 3}

    def __init__(self, name: str, face_a: str = "west", face_b: str = "south"):
        super().__init__(name, face_a=face_a, face_b=face_b)
        if face_a == face_b:
            raise ValueError(
                f"HARDWARE LIMIT: XorJoinBlock distinguishes its two "
                f"independent producers by ARRIVAL FACE, so face_a and face_b "
                f"must differ; got both = {face_a!r}. (A same-face pair cannot "
                f"be told apart by the arbiter and would mis-pair permanently — "
                f"and an XOR of two mis-paired words is a plausible-looking "
                f"byte, so the corruption would be SILENT.)")
        self._face_a, self._face_b = face_a, face_b

    @property
    def cell_count(self) -> int:
        return 1

    @property
    def interface(self) -> BlockInterface:
        # ONE output register: a single word per matched pair. With >1 the build
        # would classify the cell as a COMPLEX 2-rail source and steer the emit
        # to consecutive registers.
        return BlockInterface(entry_address=1, input_registers=[0],
                              output_registers=[0])

    def build_cell_programs(self):
        fa = self._FACE.get(self._face_a, 2)   # west
        fb = self._FACE.get(self._face_b, 0)   # south
        tmpl = (
            "got_a:\n"
            "    MOVE R{state:va}, R{in:a}\n"          # latch A (on face_a)
            "    MOVE [LOCK_FACE], R{data:face_b}\n"   # now accept ONLY face_b
            "    HALT\n"
            "got_b:\n"
            # MEASURED REDUNDANT, KEPT DELIBERATELY. Both input ports are
            # declared at R0 (each arrives on its OWN face-gated trigger, the
            # shipped N=2 convention), so this MOVE assembles to `MOVE R0, R0`.
            # Building without it was measured correct under both arrival
            # orders and under the saturated burst. It is kept because it makes
            # the operand explicit rather than dependent on a register-
            # allocation coincidence: if `b` were ever re-pinned off R0, the
            # XOR below would silently read the wrong word. One word out of 32
            # is a cheap price for that. (Corollary for anyone writing a
            # mutation test: REORDERING this line with the XOR is a NO-OP and
            # proves nothing — mutate the XOR or the latch instead.)
            "    MOVE R0, R{in:b}\n"                   # B (paired with the A)
            # ONE native LOGIC op: R0 = B ^ A. LOGIC writes R0, so the emit
            # below reads the result straight out of R0.
            "    XOR R0, R{state:va}\n"
            "    {write:out}\n"
            "    {jump:trig}\n"
            # Re-lock LAST, so the next sample's b word cannot barge in before
            # its a word has been latched (no cross-sample mixing).
            "    MOVE [LOCK_FACE], R{data:face_a}\n"
            "    HALT\n"
        )
        return {0: CellProgram(
            # Each input port declares ITS OWN entry: a producer into ``a`` must
            # JUMP got_a, a producer into ``b`` must JUMP got_b. The two entries
            # run DIFFERENT code (latch-and-handover vs xor-and-emit); without
            # the declaration every producer resolves the block's single default
            # entry, got_b never runs, and the rendezvous deadlocks (0 egress).
            inputs=[Port("a", register=0, entry="got_a"),
                    Port("b", register=0, entry="got_b")],
            outputs=[Port("out"), Port("trig")],
            # ONLY got_a / got_b — NO arm entry (the cell boots pre-locked).
            entries=[EntryPoint("got_a"), EntryPoint("got_b")],
            data=[DataWord("face_a", fa, address=1, is_face=True),
                  DataWord("face_b", fb, address=2, is_face=True)],
            # INV-33: pin every state register explicitly. An unpinned StateVar
            # lands on top of R0 and the inputs.
            state=[StateVar("va", register=3)],
            assembly_template=tmpl,
            # Cold start: boot already LOCKED to face_a (LOCK=1 + LOCK_FACE=
            # face_a) so the FIRST word is accepted ONLY on the a face — no arm,
            # no race.
            initial_lock_face=fa,
        )}

    # -------------------------------------------------------------- reference
    @staticmethod
    def process_reference_words(a_words, b_words) -> list:
        """The block's contract as a pure function: N matched (a, b) pairs in,
        the word stream ``[a0 ^ b0, a1 ^ b1, ...]`` out.

        This is the GOLDEN. There is no stock GNU Radio counterpart for the
        TWO-INDEPENDENT-PRODUCER case, so it is written directly from the
        specification (``blocks.xor_bb``'s function, on rendezvoused pairs) and
        compared word for word against the real chip.

        A word is emitted ONLY when BOTH arms have supplied theirs, so an arm
        starved after ``k`` words yields exactly ``k`` outputs — the reference
        truncates to the shorter arm. The ARRIVAL ORDER is deliberately absent
        from this signature: the whole point of the LOCK rendezvous is that the
        output does not depend on it."""
        n = min(len(a_words), len(b_words))
        return [(int(a_words[i]) ^ int(b_words[i])) & 0xFFFF for i in range(n)]

    def process_reference(self, input_samples) -> np.ndarray:
        """Reference for the generic harness. The two streams are carried as one
        complex array (real = a, imag = b); returns their bitwise XOR as float
        (byte values 0..255). For direct word compares use
        :meth:`process_reference_words` — the substrate proof is that only
        MATCHED pairs are XORed, which the block's suite drives with adversarial
        async interleavings."""
        arr = np.asarray(input_samples)
        if np.iscomplexobj(arr):
            a = np.rint(arr.real).astype(np.int64)
            b = np.rint(arr.imag).astype(np.int64)
        else:
            a = np.rint(arr).astype(np.int64)
            b = np.zeros_like(a)
        return (np.bitwise_xor(a, b) & 0xFFFF).astype(np.float32)

    def reset(self):
        pass
