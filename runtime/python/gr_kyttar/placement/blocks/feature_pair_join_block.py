# SPDX-License-Identifier: GPL-3.0-or-later
"""FeaturePairJoinBlock — see :class:`FeaturePairJoinBlock`."""
import numpy as np

from ..block import CellProgram, DataWord, EntryPoint, Port, StateVar
from ._base import BlockInterface, KyttarBlock


class FeaturePairJoinBlock(KyttarBlock):
    """ORDERED two-word rendezvous (1 cell) — accepts ONE word on input ``a``
    and ONE word on input ``b`` (arriving in ANY relative order, from
    independently rate-reduced upstreams) and emits them as **TWO SEQUENTIAL
    WRITE+JUMP bursts** into a SINGLE downstream entry, ALWAYS ``a`` first then
    ``b``.

    WHY IT EXISTS (the problem it solves, measured on the real fabric).
    A downstream cell that consumes a FIXED-ORDER pair of words on ONE input
    port + ONE entry — the toggle-cell contract (first trigger = word0, second
    trigger = word1, e.g. ``GRUCellBlock``'s ``fin`` cell) — cannot be fed by
    simply wiring TWO nets into that entry:

      * Two nets driving one target entry **build and route with ok=True and
        silently produce garbage**: the target's toggle cell reads the two
        streams as word0/word1 of *ALTERNATING* timesteps, HALVING the rate
        (12 windows in -> 6 class words out, all wrong).
      * The importer's join election declines to arbitrate when the target
        declares only an EntryPoint (no ``join``/``sink`` entry).
      * The COUNTING JOIN is explicitly ORDER-FREE — the wrong primitive for a
        FIXED word order.
      * Broker coalescing collapses two nets from one source into N WRITEs +
        ONE JUMP all targeting the same ``in_reg`` — the second value
        overwrites the first and the target fires ONCE.
      * ``DualFloatToComplexBlock`` has the right LOCK/face rendezvous but
        emits a 2-rail packet with ONE trigger — the wrong OUTPUT SHAPE.

    This block is the missing primitive: the Dual's rendezvous INPUT half with
    a two-burst OUTPUT half.

    ---------------------------------------------------------------- mechanism

    INPUT — LOCK-BY-FACE (the DualFloatToComplex mechanism, verbatim). The two
    producers fire at INDEPENDENT, ASYNCHRONOUS times, so a same-face counter
    cannot tell an ``a`` word from a ``b`` word and desyncs PERMANENTLY on the
    first re-ordering (a,a,b,...). The ONLY stream identity available is the
    physical channel — the arrival FACE. The cell uses the ISA arbiter lock
    (``LOCK`` / ``LOCK_FACE``, CONFIG 4/3) to accept an input from exactly ONE
    face at a time, so a fast/bursty producer on the other face is simply
    IGNORED by the arbiter until it is that face's turn. Hence
    ``NEEDS_DISTINCT_INPUT_FACES`` — the face IS the stream identity, and the
    build DRC rejects a same-face landing.

    The cell is LOCKED to ``face_a`` from the GET-GO via the cold-start
    ``initial_lock_face`` (LOCK=1 AND LOCK_FACE=face_a baked into the boot
    CONFIG). There is deliberately NO arm step: arming via a JUMP would be a
    RACE — a word arriving before the arm-JUMP would be accepted on an unlocked
    face and mis-paired, the exact failure the LOCK exists to prevent.

        (cold start): LOCK=1, LOCK_FACE=face_a  (accept ONLY a)
        got_a (face_a): latch A ; LOCK_FACE = face_b ; HALT   (accept ONLY b)
        got_b (face_b): latch B ; EMIT (A then B) ;
                        LOCK_FACE = face_a ; HALT             (re-lock for a)

    SEMANTICS this pins, and the gate proves:
      * ORDER — ``a`` is ALWAYS the first word emitted, whatever the arrival
        order, because the emit runs only from ``got_b`` and reads the LATCHED
        A first.
      * STARTUP — no PARTIAL pair is ever emitted: nothing is written until
        ``got_b`` runs, and ``got_b`` can only run after a ``got_a`` handed the
        lock over.
      * BACK-TO-BACK TIMESTEPS — no cross-timestep mixing: the re-lock to
        ``face_a`` is the LAST thing ``got_b`` does, so the next ``b`` word is
        barred until the next ``a`` has been latched.
      * STARVED ARM — the block STALLS (emits nothing) and NEVER emits a stale
        or duplicated pair: the arbiter holds the other producer's word, and
        the latched A is not re-emitted.

    OUTPUT — TWO SEQUENTIAL WRITE+JUMP BURSTS (the part that is NOT the Dual).
    The emit is authored as

        MOVE R0, as ; {write:out} ; {jump:trig}      <- burst 1 (word0 = A)
        MOVE R0, bs ; {write:out2} ; {jump:trig2}    <- burst 2 (word1 = B)

    i.e. FOUR external instructions from a SINGLE-RAIL output cell. The extra
    port names (``out2``/``trig2``) exist only to give the second burst its own
    placeholders in the template — the block still declares ONE output register
    and the design is wired on the single logical ``out`` stream; the build
    patches all four instructions IDENTICALLY (see below), which is what makes
    the two bursts land on the same target register and entry.

    NO ``RAW_OUTPUT_HOPS``, NO HAND-ROUTING (this was investigated and is not
    needed — recorded because the alternative was expected). The ordinary
    single-net source patchers already produce exactly the right shape:
    ``_patch_cell_handoff`` (the abutted/direct path) and
    ``_patch_cell_handoff`` via the broker path both set EVERY WRITE and EVERY
    JUMP in the cell to the SAME (hop, dest_reg, entry). Applied to this
    program that yields

        WRITE @h -> in_reg ; JUMP @h -> entry ; WRITE @h -> in_reg ; JUMP @h -> entry

    — two independent deliveries into ONE downstream entry, in program order.
    That is the required shape EXACTLY, with no new build machinery. The two
    conditions that keep this path live are ASSERTED by the block's own suite:
      (a) the block must declare exactly ONE output register, so the build never
          classifies it as a COMPLEX 2-rail source (which would steer the two
          WRITEs to CONSECUTIVE registers and fire one trigger), and
      (b) its output cell must carry NO internal handoff and no ``WRITE.CFG``,
          so ``_output_cell_carries_handoffs`` stays False and the patch covers
          BOTH bursts rather than only the last WRITE/JUMP.

    A MULTI-REGISTER CONSUMER ALSO WORKS (measured, not assumed). Reading the
    build alone suggests a >1-input-register target would divert the emit to
    ``_patch_complex_abutment_handoff`` (the complex-packet path, which steers
    rails to DIFFERENT registers with one trigger) and break the contract. It
    does not: that patcher sets the hop on EVERY WRITE/JUMP and the dest only on
    WRITE index ``rail_idx``, and for a net targeting the consumer's FIRST input
    ``rail_idx`` is 0 while the second burst's WRITE already carries that same
    dest from the template — so the outcome is identical to
    ``_patch_cell_handoff``: two independent deliveries into one register and
    one entry. Verified on chip against a 2-input-register consumer
    (``test_target_with_multiple_input_registers_still_gets_two_bursts``). The
    natural consumer is still the single-register toggle cell; this note exists
    so nobody re-derives a limit from the code that the hardware does not have.

    LAYOUT: 1 cell. Feed-forward, no internal handoffs, no reconvergent fan-in;
    the arbiter LOCK it does carry is the INPUT rendezvous, not an INV-19/20
    serialize-lock, and it is CONFIG state set at boot + by the program (never a
    backward ``WRITE.CFG``), so there is no unlock corridor to place.

    Parameters:
      * ``face_a`` / ``face_b``: the faces the A and B producers arrive on
        ('south'|'east'|'west'|'north'). MUST be distinct — the placer reserves
        two distinct free faces, the router lands one net on each, and the build
        reconciles these params + ``initial_lock_face`` with the faces the
        router actually chose. They are PLACEMENT/ROUTING internals, not
        user-facing DSP params, so they are NOT exposed in GRC (see
        ``GRC_UNSUPPORTED_PARAMS`` — the same convention
        ``DualFloatToComplexBlock`` uses).
    """
    CATEGORY = "signal_conditioning"
    TAGS = ["join", "rendezvous", "lock", "pair", "ordered", "feature"]

    # The DEFINING property (shared with DualFloatToComplexBlock): the two inputs
    # are independent async streams distinguishable ONLY by arrival face, so the
    # placer MUST land them on two DISTINCT faces and the build DRC MUST reject a
    # same-face landing.
    NEEDS_DISTINCT_INPUT_FACES = True

    # The build's face-reconciliation pass (``_apply_rendezvous_input_faces``) patches
    # the two authored placeholder faces to the ones the ROUTER actually chose. It
    # needs to know which input PORT pairs with which face DataWord, in FIRST-ACCEPTED
    # order (the cell boots locked to entry [0]'s face). Without this declaration the
    # pass falls back to the DualFloatToComplex's ``i``/``q`` names and becomes a
    # SILENT NO-OP for this block: the LOCK then gates whatever placeholder faces were
    # authored, the real arms are barred, and the chain builds + routes perfectly while
    # producing ZERO output. (Measured — this exact no-op cost a debug cycle.)
    RENDEZVOUS_FACE_PORTS = (("a", "face_a"), ("b", "face_b"))

    # face_a/face_b are the two distinct arrival faces the placer+router choose
    # and the build reconciles — pure placement/routing internals, exactly like
    # DualFloatToComplexBlock's face_i/face_q. Exposing them in GRC would invite a
    # user to set a face the router then overrides.
    GRC_UNSUPPORTED_PARAMS = ("face_a", "face_b")

    _FACE = {"south": 0, "east": 1, "west": 2, "north": 3}

    def __init__(self, name: str, face_a: str = "west", face_b: str = "south"):
        super().__init__(name, face_a=face_a, face_b=face_b)
        if face_a == face_b:
            raise ValueError(
                f"HARDWARE LIMIT: FeaturePairJoinBlock distinguishes its two "
                f"streams by ARRIVAL FACE, so face_a and face_b must differ; "
                f"got both = {face_a!r}. (A same-face pair cannot be told "
                f"apart by the arbiter and would mis-pair permanently.)")
        self._face_a, self._face_b = face_a, face_b

    @property
    def cell_count(self) -> int:
        return 1

    @property
    def interface(self) -> BlockInterface:
        # ONE output register — load-bearing: >1 makes the build classify this
        # as a COMPLEX 2-rail source and collapse the two bursts into a
        # consecutive-register packet with ONE trigger (asserted in the suite).
        return BlockInterface(entry_address=1, input_registers=[0],
                              output_registers=[0])

    def build_cell_programs(self):
        fa = self._FACE.get(self._face_a, 2)   # west
        fb = self._FACE.get(self._face_b, 0)   # south
        tmpl = (
            "got_a:\n"
            "    MOVE R{state:as}, R{in:a}\n"          # latch A (accepted on face_a)
            "    MOVE [LOCK_FACE], R{data:face_b}\n"   # now accept ONLY the b face
            "    HALT\n"
            "got_b:\n"
            "    MOVE R{state:bs}, R{in:b}\n"          # latch B (pairs with the A)
            # BURST 1 — word0 = A. WRITE lands the value in the target's input
            # register; JUMP fires the target's entry ONCE.
            "    MOVE R0, R{state:as}\n"
            "    {write:out}\n"
            "    {jump:trig}\n"
            # BURST 2 — word1 = B, into the SAME register + entry. The build
            # patches every WRITE/JUMP in this cell identically, so this is a
            # second, INDEPENDENT delivery of the same downstream entry.
            "    MOVE R0, R{state:bs}\n"
            "    {write:out2}\n"
            "    {jump:trig2}\n"
            # Re-lock LAST, so the next timestep's b word cannot barge in before
            # its a word has been latched (no cross-timestep mixing).
            "    MOVE [LOCK_FACE], R{data:face_a}\n"
            "    HALT\n"
        )
        return {0: CellProgram(
            # Each input port declares ITS OWN entry: a producer into ``a`` must
            # JUMP got_a, a producer into ``b`` must JUMP got_b. The two entries
            # run DIFFERENT code (latch-and-handover vs latch-and-emit); without
            # the declaration every producer resolves the block's single default
            # entry, got_b never runs, and the rendezvous deadlocks (0 egress).
            inputs=[Port("a", register=0, entry="got_a"),
                    Port("b", register=0, entry="got_b")],
            outputs=[Port("out"), Port("trig"), Port("out2"), Port("trig2")],
            # ONLY got_a / got_b — NO arm entry (the cell boots pre-locked).
            entries=[EntryPoint("got_a"), EntryPoint("got_b")],
            data=[DataWord("face_a", fa, address=1, is_face=True),
                  DataWord("face_b", fb, address=2, is_face=True)],
            state=[StateVar("as", register=3), StateVar("bs", register=4)],
            assembly_template=tmpl,
            # Cold start: boot already LOCKED to face_a (LOCK=1 + LOCK_FACE=face_a)
            # so the FIRST word is accepted ONLY on the a face — no arm, no race.
            initial_lock_face=fa,
        )}

    # -------------------------------------------------------------- reference
    @staticmethod
    def process_reference_pairs(a_words, b_words) -> list:
        """The block's contract as a pure function: N complete (a, b) pairs in,
        the flat ORDERED word stream ``[a0, b0, a1, b1, ...]`` out.

        A pair is emitted ONLY when BOTH arms have supplied their word, so an
        arm starved after ``k`` words yields exactly ``k`` complete pairs (the
        stall semantics) — the reference truncates to ``min(len(a), len(b))``.
        The ARRIVAL ORDER is deliberately absent from this signature: the whole
        point is that the output does not depend on it.
        """
        n = min(len(a_words), len(b_words))
        out = []
        for i in range(n):
            out.append(int(a_words[i]) & 0xFFFF)
            out.append(int(b_words[i]) & 0xFFFF)
        return out

    def process_reference(self, input_samples):
        """Pass-through carrier semantics: this block re-orders and re-triggers
        words, it does not transform them. The substrate proof is that only
        MATCHED, ORDERED pairs are emitted (see ``process_reference_pairs`` and
        the block's verification suite, which drives adversarial async
        interleavings)."""
        return np.asarray(input_samples)

    def reset(self):
        pass
