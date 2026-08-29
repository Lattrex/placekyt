# SPDX-License-Identifier: GPL-3.0-or-later
"""TMRVoterBlock — see :class:`TMRVoterBlock`."""
import numpy as np

from ..block import CellProgram, DataWord, EntryPoint, Port, StateVar
from ._base import BlockInterface, KyttarBlock


class TMRVoterBlock(KyttarBlock):
    """Triple-modular-redundancy majority voter — THREE redundant chains
    converge on one block from THREE DISTINCT faces; the block votes and emits a
    2-word packet ``[value, status]`` per sample.

    This is the N=3 generalisation of the LOCK-by-face rendezvous that
    ``DualFloatToComplexBlock`` (N=2, verified) established. The three redundant
    arms fire at INDEPENDENT, ASYNCHRONOUS times, so a counter cannot tell an
    ``a`` word from a ``b`` word: the ONLY stream identity available on this
    clockless array is the physical channel — the arrival FACE. The rendezvous
    cell uses the ISA arbiter lock (``LOCK`` / ``LOCK_FACE``, CONFIG 4/3) to
    accept an input from exactly ONE face at a time and ROTATES that lock
    a -> b -> c -> a, so a fast or bursty arm is simply IGNORED by the arbiter
    until it is that arm's turn. Hence ``NEEDS_DISTINCT_INPUT_FACES``.

        (cold start): LOCK=1, LOCK_FACE=face_a   (accept ONLY a)
        got_a (face_a): latch A ; LOCK_FACE = face_b ; HALT
        got_b (face_b): latch B ; LOCK_FACE = face_c ; HALT
        got_c (face_c): latch C ; forward (A,B,C) ; LOCK_FACE = face_a ; HALT

    There is deliberately NO arm step: arming via a JUMP would be a RACE — a word
    arriving before the arm-JUMP is accepted on an UNLOCKED face and mis-pairs,
    the exact failure the LOCK exists to prevent. The cold start is BAKED into the
    boot CONFIG via ``initial_lock_face``.

    ------------------------------------------------------------------ the vote

    Status codes (the ``status`` word of the packet):

      ``0``  all three arms agree; ``value`` is the agreed value.
      ``1``  arm A disagreed; ``value`` is the (correct) B/C majority.
      ``2``  arm B disagreed; ``value`` is the (correct) A/C majority.
      ``3``  arm C disagreed; ``value`` is the (correct) A/B majority.
      ``7``  no two arms agree; ``value`` is the sentinel ``0xFFFF``.

    TMR CORRECTS a single fault: on status 1/2/3 the emitted value is still the
    majority, so a downstream consumer that ignores the status word still gets
    the right answer. ``0xFFFF`` is outside the 0-255 byte domain, so the
    no-majority sentinel can never collide with real data.

    THE ALGEBRAIC SIMPLIFICATION that makes the tree fit: if ``a != b`` then the
    majority, WHENEVER ONE EXISTS, is ALWAYS ``vc`` — ``b == c`` gives majority
    ``b == c``, and ``a == c`` gives majority ``a == c``; both equal ``vc``. So
    the disagree half needs NO value selection at all, and the agree half's value
    is always ``va``.

    ------------------------------------------------------------------- the fold

    FOUR cells in a COLINEAR CHAIN, every handoff a single abutment:

        (0,0) rendezvous -> (1,0) agree -> (2,0) disagree -> (3,0) emit
              ^                  |
              +---- WRITE.CFG ---+        (the serialize-LOCK release)

    THE FACE BUDGET IS THE WHOLE LAYOUT PROBLEM, and it is exactly tight. A cell
    has FOUR faces; an N-arm rendezvous needs

        N (one per arm — the face IS the path identity)
      + 1 (forward into the datapath)
      + 1 (a serialize-LOCK release corridor coming back)
      = N + 2

    At N=2 that is four and fits — and the shipped N=2 blocks are SINGLE-CELL, so
    they need neither a forward nor a release, which is why the budget never came
    up before. At N=3 it is FIVE, and the cell has four. Three consequences, all
    measured:

      * The rendezvous must be a LEAF of the fold — exactly ONE in-block
        neighbour, so three faces stay free for the arms. A compact 2x2 square
        (the obvious 4-cell fold, and the first tried) gives EVERY cell two
        in-block neighbours: the maze router reports "no free DISTINCT-face
        broker for a face-locking block's input" and the chain does not route.
      * The release CANNOT have a corridor of its own; it comes back through the
        one abutting cell, ``agree``. A dedicated ``transit_*`` unlock lane (the
        ComplexMixer pattern) needs a face to enter on and there is none.
      * The block is a longitudinal 4x1 strip, the shape ``layout_rules`` warns
        against — forced here, and affordable: 4 <= 8 across, and the three
        inputs land on one cell from three sides rather than tapping a bus edge,
        so the co-located-I/O convention does not apply.

    The four cells are a chain and NOT a dispatch tree: ``agree`` forwards to
    ``disagree`` in BOTH cases — either the three arm words (entry ``dis``,
    "resolve this") or the already-decided (value, status) pair (entry ``pass``,
    "just relay") — so ``disagree`` is the only cell that talks to ``emit`` and
    the fold stays a line. Exactly one entry runs per sample, so there is no
    CONCURRENT reconvergent fan-in. Both entries ARE the target of a declared
    ``internal_jumps`` edge, so neither is INV-39 dead code.

    THE BLOCK DOES need an INV-19/20 serialize-LOCK, and the saturated gate is
    what found that out — see the ``rendezvous`` cell's note. Because the release
    can only ride ``agree`` (the face budget again), the block sustains ONE
    TRIPLE IN FLIGHT: arms may arrive in any order WITHIN a sample, and any
    number of triples may be driven one at a time, but two complete triples
    queued before running deadlock. That limit is measured and guarded by
    ``test_known_limit_saturated_burst_depth_is_one``, not waived.

    ``emit`` performs the TWO-BURST egress (``WRITE``+``JUMP``, twice, into the
    same downstream register and entry) that makes the 2-word ``[value, status]``
    packet — the ``FeaturePairJoinBlock`` output shape, NOT the Dual's 2-rail
    complex packet. The block therefore declares exactly ONE output register: with
    more than one the build would classify it as a COMPLEX 2-rail source and steer
    the two WRITEs to CONSECUTIVE registers under a single trigger, collapsing the
    packet.

    Parameters:
      * ``face_a`` / ``face_b`` / ``face_c``: the faces the three redundant arms
        arrive on ('south'|'east'|'west'|'north'). MUST be pairwise distinct —
        the face IS the path identity. They are PLACEMENT/ROUTING internals that
        the placer reserves and the build reconciles to the geometry the router
        actually chose, so they are NOT exposed in GRC (the same convention
        ``DualFloatToComplexBlock`` and ``FeaturePairJoinBlock`` use).
      * ``fault_sentinel``: the value emitted when no two arms agree
        (default ``0xFFFF``).
    """
    CATEGORY = "signal_conditioning"
    TAGS = ["tmr", "voter", "redundancy", "majority", "rendezvous", "lock",
            "fault_tolerance"]

    # The DEFINING property (shared with DualFloatToComplexBlock and
    # FeaturePairJoinBlock, here at N=3): the inputs are independent async streams
    # distinguishable ONLY by arrival face, so the placer MUST land them on
    # DISTINCT faces and the build DRC MUST reject any same-face landing.
    NEEDS_DISTINCT_INPUT_FACES = True

    # The build's face-reconciliation pass (``_apply_rendezvous_input_faces``)
    # patches the authored placeholder faces to the ones the ROUTER actually
    # chose. It needs the (input port, face DataWord) pairs in FIRST-ACCEPTED
    # order — the cell boots locked to entry [0]'s face. Without this declaration
    # the pass falls back to the DualFloatToComplex ``i``/``q`` names and becomes
    # a SILENT NO-OP: the LOCK then gates the authored placeholder faces, the real
    # arms are barred, and the chain builds + routes perfectly while producing
    # ZERO output.
    RENDEZVOUS_FACE_PORTS = (("a", "face_a"), ("b", "face_b"), ("c", "face_c"))

    # The backward ``unlock`` edge of this block RE-POINTS a rotating face lock
    # (CONFIG 3 = LOCK_FACE), it does NOT clear the arbiter lock (CONFIG 4 =
    # LOCK) the way the ComplexMixer/Costas serialize-LOCK does. The build's
    # feedback pass defaults to CONFIG 4; declaring 3 here keeps it from
    # rewriting this block's authored ``WRITE.CFG @N, 3`` into a lock-CLEAR,
    # which would un-gate every face and let out-of-turn arms barge in (it
    # builds and routes cleanly, then desyncs after two samples).
    UNLOCK_CFG_ADDR = 3

    # face_a/b/c are placement/routing internals the placer+router choose and the
    # build reconciles — exposing them in GRC would invite a user to set a face
    # the router then overrides.
    GRC_UNSUPPORTED_PARAMS = ("face_a", "face_b", "face_c")

    _FACE = {"south": 0, "east": 1, "west": 2, "north": 3}

    # Status codes, pinned here so the block, the golden and the GRC binding
    # cannot drift apart.
    STATUS_AGREE = 0
    STATUS_FAULT_A = 1
    STATUS_FAULT_B = 2
    STATUS_FAULT_C = 3
    STATUS_NO_MAJORITY = 7

    def __init__(self, name: str, face_a: str = "west", face_b: str = "north",
                 face_c: str = "south", fault_sentinel: int = 0xFFFF):
        super().__init__(name, face_a=face_a, face_b=face_b, face_c=face_c,
                         fault_sentinel=fault_sentinel)
        faces = (face_a, face_b, face_c)
        if len(set(faces)) != 3:
            raise ValueError(
                f"HARDWARE LIMIT: TMRVoterBlock distinguishes its three "
                f"redundant paths by ARRIVAL FACE, so face_a, face_b and face_c "
                f"must be pairwise DISTINCT; got {faces}. (Two paths sharing a "
                f"face cannot be told apart by the arbiter and would mis-pair "
                f"permanently — the face IS the path identity.)")
        self._face_a, self._face_b, self._face_c = face_a, face_b, face_c
        self._sentinel = int(fault_sentinel) & 0xFFFF

    @property
    def cell_count(self) -> int:
        return 4

    @property
    def interface(self) -> BlockInterface:
        # ONE output register — load-bearing: with >1 the build classifies the
        # emit cell as a COMPLEX 2-rail source and collapses the [value, status]
        # packet into a consecutive-register pair under ONE trigger.
        return BlockInterface(entry_address=1, input_registers=[0],
                              output_registers=[0])

    # ------------------------------------------------------------------ cells
    def build_cell_programs(self):
        fa = self._FACE.get(self._face_a, 2)   # west
        fb = self._FACE.get(self._face_b, 3)   # north
        fc = self._FACE.get(self._face_c, 0)   # south
        cells = {}

        # (1) rendezvous — the LOCK ROTATION. All three arms land in R0, each on
        # its OWN face-gated trigger, and are latched immediately; the cell is
        # ALWAYS locked to exactly one face, advancing a -> b -> c -> (barred).
        #
        # THE ROTATION HAS FOUR STOPS, NOT THREE (INV-19/20, and this was
        # MEASURED, not assumed). The obvious construction — re-lock straight to
        # ``face_a`` at the end of ``got_c`` — is correct per-sample and
        # DEADLOCKS under saturation: it re-admits the NEXT sample's ``a`` word
        # the instant the current triple is dispatched, so sample N+1 enters the
        # agree/disagree/emit chain while sample N is still traversing it, and
        # the simulator reports an explicit ``Deadlock`` after exactly ONE
        # packet. (Per-sample drive hides it completely: each sample settles
        # before the next is injected. The three producer arms alone are
        # saturation-safe, so the hazard is this block's.)
        #
        # So ``got_c`` locks to ``face_fwd`` — the INTERNAL forward face, toward
        # ``agree``, which NO external arm ever arrives on. That bars all three
        # arms. The ``agree`` cell then re-points the lock to ``face_a`` with a
        # backward ``WRITE.CFG @N, 3`` (CONFIG 3 = LOCK_FACE) once it has
        # dispatched, which is what admits the next triple.
        #
        # WHY ``agree`` AND NOT ``emit`` (the deeper, more thorough point): the
        # FACE BUDGET. An N-arm rendezvous needs N (arms) + 1 (forward) + 1
        # (release corridor) = N+2 faces, and a cell has 4; at N=3 that is five,
        # so the release cannot have a corridor of its own and must come back
        # through the ONE cell that abuts the rendezvous. That bounds the block
        # to one triple in flight, which is the documented limit
        # (``test_known_limit_saturated_burst_depth_is_one``).
        cells["rendezvous"] = CellProgram(
            # Each input port declares ITS OWN entry: a producer into ``a`` must
            # JUMP got_a, into ``b`` got_b, into ``c`` got_c. The three entries
            # run DIFFERENT code; without the declaration every producer resolves
            # the block's single default entry, got_b/got_c never run, and the
            # rendezvous deadlocks with 0 egress.
            inputs=[Port("a", register=0, entry="got_a"),
                    Port("b", register=0, entry="got_b"),
                    Port("c", register=0, entry="got_c")],
            outputs=[Port("fa"), Port("fb"), Port("fc"), Port("ftrig")],
            # ONLY got_a/got_b/got_c — NO arm entry (the cell boots pre-locked).
            entries=[EntryPoint("got_a"), EntryPoint("got_b"),
                     EntryPoint("got_c")],
            data=[DataWord("face_a", fa, address=1, is_face=True),
                  DataWord("face_b", fb, address=2, is_face=True),
                  DataWord("face_c", fc, address=3, is_face=True),
                  # The INTERNAL forward face (toward `agree`). Locking to it
                  # bars every external arm — nothing arrives on it — which is
                  # how the block holds the next sample until `emit` releases it.
                  # is_face so it D4-transforms with the block (INV-23).
                  DataWord("face_fwd", 1, address=4, is_face=True)],
            # INV-33: pin every state register explicitly.
            state=[StateVar("va", register=5), StateVar("vb", register=6),
                   StateVar("vc", register=7)],
            assembly_template=(
                "got_a:\n"
                "    MOVE R{state:va}, R{in:a}\n"
                "    MOVE [LOCK_FACE], R{data:face_b}\n"
                "    HALT\n"
                "got_b:\n"
                "    MOVE R{state:vb}, R{in:b}\n"
                "    MOVE [LOCK_FACE], R{data:face_c}\n"
                "    HALT\n"
                "got_c:\n"
                "    MOVE R{state:vc}, R{in:c}\n"
                "    MOVE R0, R{state:va}\n"
                "    {write:fa}\n"
                "    MOVE R0, R{state:vb}\n"
                "    {write:fb}\n"
                "    MOVE R0, R{state:vc}\n"
                "    {write:fc}\n"
                "    {jump:ftrig}\n"
                # Bar ALL arms (lock to the internal face nothing drives) until
                # `emit` has egressed this packet and re-points us at face_a.
                "    MOVE [LOCK_FACE], R{data:face_fwd}\n"
                "    HALT\n"
            ),
            # Cold start: boot already LOCKED to face_a (LOCK=1 + LOCK_FACE=face_a)
            # so the FIRST word is accepted ONLY on the a face — no arm, no race.
            initial_lock_face=fa,
        )

        # (2) agree — the a == b half of the tree, the DISPATCH, and the
        # SERIALIZE-LOCK RELEASE.
        #   a == b : value = va (always), status = 0 if c agrees else 3;
        #            forward (value, status) to `disagree`'s PASS entry.
        #   a != b : forward the three arm words to `disagree`'s DIS entry.
        #
        # THE RELEASE (INV-19/20) rides THIS cell, not `emit`, and the reason is
        # GEOMETRIC. The release must reach the rendezvous cell, and the
        # rendezvous has ALL FOUR faces committed (three arms + the forward to
        # `agree`) — there is no fifth face for a return corridor. `agree` is the
        # ONE block cell it abuts, so `agree` is the only cell that can write back
        # to it without a transit relay the fold has no room for.
        #
        # Serialization depth is therefore ONE RENDEZVOUS, not one whole block:
        # the next triple may be latched while the previous one is still in
        # `disagree`/`emit`. That is sufficient — the measured deadlock was the
        # rendezvous re-admitting arm A *immediately*, which let an unbounded
        # number of triples pile into the chain. Holding the arms until `agree`
        # has dispatched bounds it, and the saturated gate confirms it.
        #
        # The authored `@1` hop is a placeholder; `_apply_internal_feedback`
        # re-patches it to the resolved agree->rendezvous corridor distance, so a
        # moved or ROTATED block still closes its own lock (a hardcoded hop
        # deadlocks a re-placed layout — the INV-19 trap).
        cells["agree"] = CellProgram(
            inputs=[Port("va", register=1), Port("vb", register=2),
                    Port("vc", register=3)],
            outputs=[Port("pv"), Port("ps"), Port("ptrig"),
                     Port("da"), Port("db"), Port("dc"), Port("dtrig"),
                     Port("unlock")],
            entries=[EntryPoint("vote")],
            data=[DataWord("zero", self.STATUS_AGREE, address=4),
                  DataWord("three", self.STATUS_FAULT_C, address=5),
                  # The final hop of the serialize-LOCK release. `unlock_face`
                  # serves DOUBLE DUTY — it is both the direction WEST back into
                  # the rendezvous AND the VALUE written into its LOCK_FACE (arm
                  # A's face). Both are WEST at identity and D4-transform
                  # identically, so they are ONE word (the INV-19
                  # merge-identical-words reclaim; it is what makes this cell
                  # fit). `face_tap` is the normal EAST forward, restored after
                  # the config write so the next sample's handoffs are not
                  # diverted. Both is_face for INV-23.
                  DataWord("unlock_face", fa, address=6, is_face=True),
                  DataWord("face_tap", 1, address=7, is_face=True)],
            assembly_template=(
                "vote:\n"
                "    CMP R{in:va}, R{in:vb}\n"
                "    BR.NZ dis\n"
                # a == b -> the majority value is va whatever c says. Stage the
                # STATUS first (R0 is free), then fall into the shared forward.
                "    MOVE R0, R{data:zero}\n"      # all three agree
                "    CMP R{in:va}, R{in:vc}\n"
                "    BR.Z pass_st\n"
                "    MOVE R0, R{data:three}\n"     # path C faulted
                "pass_st:\n"
                "    {write:ps}\n"
                "    MOVE R0, R{in:va}\n"
                "    {write:pv}\n"
                "    {jump:ptrig}\n"
                "    GOTO release\n"
                "dis:\n"
                "    MOVE R0, R{in:va}\n"
                "    {write:da}\n"
                "    MOVE R0, R{in:vb}\n"
                "    {write:db}\n"
                "    MOVE R0, R{in:vc}\n"
                "    {write:dc}\n"
                "    {jump:dtrig}\n"
                "release:\n"
                # THE SERIALIZE-LOCK RELEASE (INV-19/20). Both arms of the
                # dispatch fall through to here. Flip WEST back into the
                # rendezvous, re-point its LOCK_FACE at arm A (which ADMITS the
                # next triple), then restore the EAST forward face so the next
                # sample's handoffs are not diverted into the unlock corridor —
                # the ComplexMixer / iq_upconvert dual-face flip, verbatim.
                #
                # No trailing HALT: this is the LAST authored instruction and
                # R31 is always HALT (the resolver's invariant), so execution
                # stops here anyway — the saved word is what keeps this cell
                # inside its 32.
                "    MOVE [FACE], R{data:unlock_face}\n"
                "    MOVE R0, R{data:unlock_face}\n"
                "    WRITE.CFG @1, 3\n"
                "    MOVE [FACE], R{data:face_tap}\n"
            ),
        )

        # (3) disagree — TWO entries.
        #   ``pass`` : the agree half already decided; RELAY (value, status) on.
        #              The pass payload REUSES the vc/vb registers (value in vc,
        #              status in vb) rather than claiming two more input
        #              registers — the cell has exactly ONE word of slack, so a
        #              dedicated pval/pst pair does not fit (measured).
        #   ``dis``  : a != b, so the majority (if any) is ALWAYS vc.
        # Both entries are the target of a declared internal_jumps edge (INV-39:
        # an entry nothing jumps at is unreachable dead code that assembles, fits
        # its budget, and runs down the wrong path forever).
        cells["disagree"] = CellProgram(
            inputs=[Port("va", register=1), Port("vb", register=2),
                    Port("vc", register=3)],
            outputs=[Port("rv"), Port("rs"), Port("rtrig"),
                     Port("ev"), Port("es"), Port("etrig"),
                     Port("ev2"), Port("es2"), Port("etrig2"),
                     Port("ntrig")],
            entries=[EntryPoint("pass"), EntryPoint("dis")],
            data=[DataWord("one", self.STATUS_FAULT_A, address=4),
                  DataWord("two", self.STATUS_FAULT_B, address=5)],
            assembly_template=(
                "pass:\n"
                "    MOVE R0, R{in:vc}\n"         # agree wrote the value here
                "    {write:rv}\n"
                "    MOVE R0, R{in:vb}\n"         # ...and the status here
                "    {write:rs}\n"
                "    {jump:rtrig}\n"
                "    HALT\n"
                "dis:\n"
                "    CMP R{in:vb}, R{in:vc}\n"
                "    BR.NZ try_ac\n"
                "    MOVE R0, R{in:vc}\n"         # b == c -> majority vc
                "    {write:ev}\n"
                "    MOVE R0, R{data:one}\n"      # path A faulted
                "    {write:es}\n"
                "    {jump:etrig}\n"
                "    HALT\n"
                "try_ac:\n"
                "    CMP R{in:va}, R{in:vc}\n"
                "    BR.NZ nomaj\n"
                "    MOVE R0, R{in:vc}\n"         # a == c -> majority vc
                "    {write:ev2}\n"
                "    MOVE R0, R{data:two}\n"      # path B faulted
                "    {write:es2}\n"
                "    {jump:etrig2}\n"
                "    HALT\n"
                "nomaj:\n"
                # No majority: no DATA is forwarded at all — the sentinel and the
                # status-7 constant live in `emit`, which has slack to spare,
                # and a bare JUMP to its `nomaj` entry says everything. That is
                # what keeps this cell inside its 32 words.
                "    {jump:ntrig}\n"
                "    HALT\n"
            ),
        )

        # (4) emit — the TWO-BURST egress that makes the [value, status] packet.
        # Two independent WRITE+JUMP deliveries into the SAME downstream register
        # and entry (the FeaturePairJoin output shape), NOT a 2-rail packet.
        # A SECOND entry, ``nomaj``, emits the no-majority packet from its OWN
        # constants (the sentinel + status 7) with no forwarded data.
        cells["emit"] = CellProgram(
            inputs=[Port("val", register=1), Port("st", register=2)],
            outputs=[Port("out"), Port("trig"), Port("out2"), Port("trig2"),
                     Port("nout"), Port("ntrig"), Port("nout2"),
                     Port("ntrig2")],
            entries=[EntryPoint("emit"), EntryPoint("nomaj")],
            data=[DataWord("seven", self.STATUS_NO_MAJORITY, address=3),
                  DataWord("sentinel", self._sentinel, address=4)],
            assembly_template=(
                "emit:\n"
                "    MOVE R0, R{in:val}\n"
                "    {write:out}\n"
                "    {jump:trig}\n"
                "    MOVE R0, R{in:st}\n"
                "    {write:out2}\n"
                "    {jump:trig2}\n"
                "    HALT\n"
                "nomaj:\n"
                "    MOVE R0, R{data:sentinel}\n"
                "    {write:nout}\n"
                "    {jump:ntrig}\n"
                "    MOVE R0, R{data:seven}\n"
                "    {write:nout2}\n"
                "    {jump:ntrig2}\n"
                "    HALT\n"
            ),
        )
        return cells

    def internal_connections(self):
        return [
            ("rendezvous", "fa", "agree", "va"),
            ("rendezvous", "fb", "agree", "vb"),
            ("rendezvous", "fc", "agree", "vc"),
            # agree -> disagree, PASS arm (already decided). The payload REUSES
            # disagree's vc (value) / vb (status) registers — see the cell note.
            ("agree", "pv", "disagree", "vc"),
            ("agree", "ps", "disagree", "vb"),
            # agree -> disagree, DIS arm (resolve the a != b half)
            ("agree", "da", "disagree", "va"),
            ("agree", "db", "disagree", "vb"),
            ("agree", "dc", "disagree", "vc"),
            # disagree -> emit
            ("disagree", "rv", "emit", "val"),
            ("disagree", "rs", "emit", "st"),
            ("disagree", "ev", "emit", "val"),
            ("disagree", "es", "emit", "st"),
            ("disagree", "ev2", "emit", "val"),
            ("disagree", "es2", "emit", "st"),
            # BACKWARD edge: the serialize-LOCK release corridor, agree ->
            # rendezvous. It carries NO DATA — only the WRITE.CFG that re-points
            # the rendezvous's LOCK_FACE at arm A — but it must be declared so
            # `_apply_internal_feedback` resolves the authored `@1` placeholder
            # to the real corridor distance, and re-resolves it when the block is
            # moved or ROTATED. A hardcoded hop deadlocks a re-placed layout
            # (the INV-19 trap).
            #
            # The destination is named ``lock_cfg``, NOT the real input port
            # ``a``. The edge is CONFIG-ONLY — the build's ``unlock`` branch
            # patches the WRITE.CFG's hop and returns BEFORE resolving any
            # destination register, so no real port is needed. Naming ``a`` here
            # made ``portmap`` classify it as a feedback RETURN and DROP it from
            # the block's external inputs: the block then advertised only two of
            # its three arms, which a hand-wired chain survives (it wires by
            # name) but GRC import does not.
            ("agree", "unlock", "rendezvous", "lock_cfg"),
        ]

    def internal_jumps(self):
        # EVERY declared EntryPoint is the target of at least one edge (INV-39).
        return [
            ("rendezvous", "ftrig", "agree", "vote"),
            ("agree", "ptrig", "disagree", "pass"),
            ("agree", "dtrig", "disagree", "dis"),
            ("disagree", "rtrig", "emit", "emit"),
            ("disagree", "etrig", "emit", "emit"),
            ("disagree", "etrig2", "emit", "emit"),
            ("disagree", "ntrig", "emit", "nomaj"),
        ]

    def output_cell_ids(self):
        return ["emit"]

    def default_layout(self):
        # A STRICT CHAIN folded so `rendezvous` is a LEAF — it has exactly ONE
        # in-block neighbour (`agree`), leaving THREE free faces for the three
        # redundant arms. See the class docstring: at N=3 the rendezvous needs all
        # four of its faces (3 arms + 1 forward), so any second in-block
        # neighbour makes the block UNROUTABLE ("no free DISTINCT-face broker").
        # A 2x2 square gives EVERY cell two in-block neighbours and therefore
        # cannot host an N=3 rendezvous at all; the fold must be a chain.
        #
        # THE FACE BUDGET IS THE WHOLE DESIGN, and it is exactly tight:
        #
        #   a cell has 4 faces; an N-arm rendezvous needs N (arms) + 1 (forward)
        #   + 1 (a serialize-LOCK release corridor) = N + 2.
        #
        # At N=2 that is 4 and fits (and the shipped N=2 blocks are single-cell,
        # so they need neither a forward nor a release). At N=3 it is FIVE, and
        # the cell has four. So the N=3 rendezvous cannot have a release corridor
        # of its own: the release must come back THROUGH THE FORWARD FACE, from
        # the one cell that abuts it. That cell is `agree`, which is why the
        # release rides `agree` and not the (deeper, more thorough) `emit`.
        #
        # THE CHAIN IS COLINEAR (4x1): rendezvous -> agree -> disagree -> emit,
        # every forward handoff a single abutment, and the release a single
        # backward abutment from `agree`. A 4x1 row is normally the longitudinal
        # shape layout_rules warns against; here it is FORCED by the face budget
        # and it is affordable — 4 <= 8 across, and the block's three INPUTS land
        # on the one rendezvous cell from three different sides rather than
        # tapping a single bus edge, so the usual co-located-I/O tap convention
        # does not apply to it.
        return {"rendezvous": (0, 0, "east"),
                "agree": (1, 0, "east"),
                "disagree": (2, 0, "east"),
                "emit": (3, 0, "east")}

    # -------------------------------------------------------------- reference
    @classmethod
    def vote(cls, a, b, c, sentinel: int = 0xFFFF):
        """The block's contract as a pure function: three 16-bit arm words in,
        the ``(value, status)`` packet out.

        This is the GOLDEN. There is no stock GNU Radio counterpart, so it is
        written directly from the specification and cross-checked against the
        on-chip result word for word."""
        a = int(a) & 0xFFFF
        b = int(b) & 0xFFFF
        c = int(c) & 0xFFFF
        if a == b:
            return (a, cls.STATUS_AGREE if a == c else cls.STATUS_FAULT_C)
        if b == c:
            return (b, cls.STATUS_FAULT_A)
        if a == c:
            return (a, cls.STATUS_FAULT_B)
        return (int(sentinel) & 0xFFFF, cls.STATUS_NO_MAJORITY)

    @classmethod
    def process_reference_words(cls, a_words, b_words, c_words,
                                sentinel: int = 0xFFFF) -> list:
        """N complete (a, b, c) triples in, the FLAT 2-word-per-sample stream
        ``[v0, s0, v1, s1, ...]`` out.

        A sample is emitted ONLY when all THREE arms have supplied their word, so
        an arm starved after ``k`` words yields exactly ``k`` complete packets —
        the reference truncates to the shortest arm. The ARRIVAL ORDER is
        deliberately absent from this signature: the whole point of the LOCK
        rotation is that the output does not depend on it."""
        n = min(len(a_words), len(b_words), len(c_words))
        out: list = []
        for i in range(n):
            v, s = cls.vote(a_words[i], b_words[i], c_words[i], sentinel)
            out.append(v)
            out.append(s)
        return out

    def process_reference(self, input_samples) -> np.ndarray:
        """Carrier semantics: with three IDENTICAL (fault-free) arms the voter is
        the identity on the value rail. The substrate proof is that only MATCHED
        triples are voted and that each fault case reports the right path — see
        ``vote`` / ``process_reference_words`` and the block's verification
        suite, which drives adversarial async interleavings."""
        return np.asarray(input_samples)

    def reset(self):
        pass
