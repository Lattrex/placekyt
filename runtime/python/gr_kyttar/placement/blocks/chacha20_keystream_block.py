# SPDX-License-Identifier: GPL-3.0-or-later
"""ChaCha20KeystreamBlock — see :class:`ChaCha20KeystreamBlock`."""
from typing import Any, Dict, List, Tuple

import numpy as np

from ..block import CellProgram, DataWord, EntryPoint, Port, StateVar
from ._base import BlockInterface, KyttarBlock
from .chacha20_qr_block import FRAME, ChaCha20QRBlock, _rotl32

MASK16 = 0xFFFF
MASK32 = 0xFFFFFFFF

#: Hardware face codes, as the cell's FACE register and ``CellProgram.fwd_face``
#: encode them. The block's ``is_face`` DataWords use the SAME numbering, and the
#: placer rewrites both through the orientation map (INV-23), so a face constant
#: and a resting face stay consistent under rotation.
FACE_CODE = {"south": 0, "east": 1, "west": 2, "north": 3}

#: RFC 8439 §2.3 — ASCII "expand 32-byte k" as four little-endian 32-bit words.
CHACHA20_CONSTANTS = (0x61707865, 0x3320646E, 0x79622D32, 0x6B206574)

#: Quarter-round invocations in RFC 8439's 20-round block function. ``seq``
#: counts DOWN from this to zero. Named rather than inlined because the counter
#: DIRECTION is a gated property: a wrong direction is a DIFFERENT CIPHER, not a
#: wrong count, so no count-based check would catch it.
LAPS_INIT = 80

#: The quarter-round stages reused from :class:`ChaCha20QRBlock`. Its ``emit``
#: cell is NOT reused: this block replaces it with ``wb``, which takes the frame
#: on the ordinary relay contract and peels it back into the rows.
QR_CELLS = ("in0", "in1", "l1_add", "l1_xor", "l2_add", "l2_xor", "l2_rota",
            "l2_rotb", "l3_add", "l3_xor", "l3_rota", "l3_rotb", "l4_add",
            "l4_xor", "l4_rota", "l4_rotb")

#: The state line: four rows, each followed by its TAP. They are laid out
#: COLLINEAR and CO-FACING because ``wb``, ``wbk`` and ``realign`` each have to
#: reach several of them from ONE forwarding walk.
STATE_LINE = tuple(c for k in range(4) for c in (f"row{k}", f"tap{k}"))

#: The datapath CYCLE, in dataflow order. One lap == one quarter-round
#: invocation, and 80 laps is the whole cipher. It is a cycle in DATAFLOW; the
#: LAYOUT that carries it is a serpentine, not a closed ring (INV-50).
RING = ("seq", "wb", "wbk") + STATE_LINE + QR_CELLS + ("relay", "relay2")

#: Cells that are NOT on the datapath cycle: the four finish adders, the egress,
#: and the six one-word pass-throughs that PAVE shared walks (a gap inside the
#: block's own footprint is a dead end for a block-internal WRITE -- INV-51).
INTERIOR_CELLS = (("out", "add_pad", "ctl_pad", "ra_pad")
                  + tuple(f"add{k}" for k in range(4))
                  + tuple(f"pass{k}" for k in range(3)))


def initial_state(key: bytes, nonce: bytes, counter: int) -> List[int]:
    """The 16-word ChaCha20 state (RFC 8439 §2.3), little-endian throughout."""
    if len(key) != 32:
        raise ValueError(f"a ChaCha20 key is 32 bytes; got {len(key)}")
    if len(nonce) != 12:
        raise ValueError(f"an RFC 8439 nonce is 12 bytes; got {len(nonce)}")
    words = list(CHACHA20_CONSTANTS)
    words += [int.from_bytes(key[4 * i:4 * i + 4], "little") for i in range(8)]
    words.append(counter & MASK32)
    words += [int.from_bytes(nonce[4 * i:4 * i + 4], "little")
              for i in range(3)]
    return words


def block_function(key: bytes, nonce: bytes, counter: int) -> List[int]:
    """RFC 8439 §2.3 block function — 20 rounds, then add the initial state."""
    init = initial_state(key, nonce, counter)
    s = list(init)
    for _ in range(10):
        for diag in (False, True):
            sh = 1 if diag else 0
            for j in range(4):
                ia, ib, ic, idd = (4 * k + ((j + k * sh) & 3) for k in range(4))
                a, b, c, d = s[ia], s[ib], s[ic], s[idd]
                a = (a + b) & MASK32
                d = _rotl32(d ^ a, 16)
                c = (c + d) & MASK32
                b = _rotl32(b ^ c, 12)
                a = (a + b) & MASK32
                d = _rotl32(d ^ a, 8)
                c = (c + d) & MASK32
                b = _rotl32(b ^ c, 7)
                s[ia], s[ib], s[ic], s[idd] = a, b, c, d
    return [(s[i] + init[i]) & MASK32 for i in range(16)]


class ChaCha20KeystreamBlock(KyttarBlock):
    """
    ChaCha20 **block function** (RFC 8439 §2.3) — a placeKYT-native ([Kyttar])
    cryptographic primitive with **no stock GNU Radio counterpart**. The golden
    reference is the published algorithm in
    ``verification/tests/chacha20_golden.py``, itself pinned by the RFC's own
    §2.3.2 (block function) and §2.4.2 (encryption) test vectors.

    Twenty rounds over a 16-word state, then the original state added back word
    by word mod 2**32. Output: the 16 state words as **32 raw 16-bit words**,
    hi then lo per value, bursting on one trigger. This is exact modular integer
    arithmetic, **not Q15 DSP** — every add wraps mod 2**32 and nothing
    saturates.

    Why the obvious shapes do not fit
    =================================

    A straight unroll is impossible: one quarter round measures **17 cells**
    (:class:`ChaCha20QRBlock`) and the cipher invokes it **80 times**, so an
    unrolled form is ``17 x 80 = 1360`` cells against a 120-cell array. The
    cipher therefore **reuses one datapath** across all 80 invocations —
    INV-49's recirculation, measured on chip at 1/2/4/8/10/20/80 passes.

    The 32-word state is *not* an obstacle: a **streaming** relay carries frames
    of 8..128 words through real cells bit-exact at a constant 3 instructions
    (INV-47, corrected). And the permutation needs no computed destination,
    because it is not data-dependent — all 80 invocations are ten identical
    repeats of a fixed 8-step cycle.

    The FIXED-TAP RING — why there is no selector and no demux
    ==========================================================

    Written as ``index(k) = 4k + ((j + k*shift) & 3)`` the schedule invites a
    per-row *selector*: a ``LOAD``-indirect read plus a 4-way ``CMP``/``BR``
    write-back (the ISA has no ``STORE [Rn]``), driven by a broadcast to every
    lane. That works, but costs a fan-out-8 broadcast and an 8-cell write-back
    demux whose targets must all lie on one forwarding walk (INV-48 root cause
    C).

    Reading the SAME schedule as a read offset per row collapses it::

        row 0 reads offsets  0 1 2 3 | 0 1 2 3
        row 1 reads offsets  0 1 2 3 | 1 2 3 0
        row 2 reads offsets  0 1 2 3 | 2 3 0 1
        row 3 reads offsets  0 1 2 3 | 3 0 1 2

    Every row reads **offset 0** provided it rotates left by one after each
    quarter round. The column half needs nothing else; the diagonal half is the
    same sequence started ``k`` positions later, so it is bracketed by ``k``
    extra rotations of row ``k`` and ``4 - k`` to restore alignment. The tap is
    therefore **always slot 0** — a constant — and the permutation is a shift
    register. No selector, no ``LOAD``-indirect, no write-back branch, no demux.

    Datapath — 40 cells in a 10x6 fold
    ==================================

    The DATAFLOW is a cycle — one lap per quarter-round invocation::

        wb -> row0 tap0 row1 tap1 row2 tap2 row3 tap3
                 -> [16 quarter-round stages] -> relay -> relay2 -> wb

    but the LAYOUT is a serpentine, **not** a closed geometric ring. A closed
    ring TRAPS ITS INTERIOR: every ring cell forwards along the ring, and a word
    is forwarded on each transit cell's own face, so a word emitted inside the
    ring in any direction joins it and follows it forever. A serpentine has free
    ends, so the egress and the control cells can reach the block's edge. See
    INV-50, and :meth:`default_layout` for the bands.

    * ``row0..row3`` hold four 32-bit state words each, as eight 16-bit
      registers. ``pub`` publishes slot 0; ``wb`` installs the returned word and
      rotates; ``spin`` rotates with no replacement (the realignment).
    * ``tap0..tap3`` steer each row's published pair into the quarter round or,
      during the finish laps, into that row's adder — a local ``BR`` on a mode
      flag, not a computed destination.
    * the 16 quarter-round stages are **reused verbatim** from
      :class:`ChaCha20QRBlock` — same programs, same proven arithmetic.
    * ``wb`` replaces that block's ``emit``: it takes the result frame on the
      ordinary relay contract and peels it back into the four rows, which lie
      consecutively along its own walk. ``wbk`` then fires the four rotates and
      advances the lap counter, in that order.
    * ``seq`` counts the 80 laps and the half boundaries; ``realign`` issues the
      fixed 12-spin boundary schedule as unrolled literal ``JUMP``s.
    * ``add0..add3`` finish: each drains one row and adds that row's four
      addends, held in a rotating register that steps in lockstep so the
      add-back tap is **also** always slot 0.
    * ``pass0..pass2``, ``add_pad`` and ``ctl_pad`` are one-word pass-throughs
      that PAVE shared walks — a gap inside the block's own footprint is a dead
      end for an internal ``WRITE`` (INV-50).

    The initial state is a **build-time constant** — the four RFC constants are
    fixed and key/nonce/counter are block parameters — so the add-back needs no
    shadow copy of the state, and each row BOOTS holding its four words of it.

    Status
    ======

    **This block is NOT `done`. The CIPHER now runs — the drain does not.**

    What is proven ON THE REAL PLACED + ROUTED + BUILT CHIP, from the execution
    trace and the emitted words:

    * the ring runs the **whole of RFC 8439's schedule**: exactly 80
      quarter-round invocations through all sixteen stages, 19 half-boundary
      realignments, and 37/38/39 realignment spins of rows 1/2/3 — the exact
      counts the reference schedule requires;
    * the finish arms all four taps and drains;
    * **state word 0 comes out BIT-EXACT** (``0xE4E7 0xF110``, RFC 8439 §2.3.2),
      and so does most of state word 15. A wrong 20-round permutation cannot
      produce those bytes, so the datapath, the write-back, the fixed tap and
      the realignment are all confirmed correct on silicon.

    What remains is **only the drain repeat**. Each row holds four 32-bit words
    and one drain lap emits the head of each, so the finish must run four laps
    with a plain rotate (``row.spin``) between them. The block emits 8 of its 32
    words: lap one, correct at its head, then stops.

    The rotate has to be issued by a cell that reaches all four rows on ONE walk,
    which on this fold is only ``wbk`` at ``(1,0)``, and the lap has to be closed
    by a cell that can reach ``wbk``, which is only ``wb`` or ``seq`` — the tap
    line and the finish row both run one-way AWAY from the control corner
    (measured over all four faces from every tap, adder and the egress). That
    costs a ``drn`` entry on ``wbk`` (5 words), a relay entry on ``wb`` (3) and a
    lap counter on ``tap3`` (5+1). **Measured shortfall: four words**, after
    compressing ``wbk``'s realignment schedule from twelve jumps to eight by
    hoisting the spins common to both halves. LAYER: block program / fold — a
    word-budget problem on this particular fold, not a substrate limit. It is
    not a routing problem and not an arithmetic one.

    The algebra is separately verified in
    ``verification/tests/test_chacha20_fixed_tap_ring.py`` (exact against RFC
    8439 §2.3.2 and §2.4.2, with eight INV-4 mutants). Do not use this block for
    keystream yet: it emits a quarter of its output.

    Interface:
        - Entry: ``seq``'s default entry; one trigger runs the whole block.
        - Output: 32 raw 16-bit words (16 state words, hi then lo).
    """

    CATEGORY = "fec"
    TAGS = ["chacha20", "crypto", "cipher", "rfc8439", "keystream",
            "block-function", "multi-word", "32-bit"]

    # A 34-cell RING that is a large fraction of a 120-cell array, so it is a
    # CHIP_SCALE block: the sole occupant of its die. The <=8-across convention
    # exists only to keep several blocks co-resident, and a closed ring can
    # never enclose a routing channel anyway (a cycle cannot jump a gap), so the
    # fold is 9 wide and leaves column 9 as one contiguous through-corridor.
    # The class's one placement contract -- input and output reachable from the
    # chip's x16 ports -- is met by putting BOTH on the ring's right edge, and
    # it is gated end to end on a real built chip, never by inspection.
    CHIP_SCALE = True
    CHIP_SCALE_ORIENTATIONS = ((),)

    _interface = BlockInterface(
        entry_address=16, input_registers=[1], output_registers=[0])

    GRC_UNSUPPORTED_PARAMS = ()

    #: 16 state words -> 32 sixteen-bit output words.
    OUT_WORDS = 32
    #: Quarter-round invocations in RFC 8439's 20-round block function.
    LAPS = 80

    #: The ring's rectangle and its ROTATION around that perimeter. All three
    #: are SOLVED constraints, not preferences — see :meth:`default_layout`.
    #: How the 16 quarter-round stages split across the three serpentine legs.
    #: Sized so the chain's tail lands at column 1 and drops onto ``relay`` --
    #: a solved constraint, not a preference.
    _QR_LEG_A, _QR_LEG_B = 3, 3

    def __init__(self, name: str,
                 key: bytes = bytes(range(32)),
                 nonce: bytes = bytes.fromhex("000000090000004a00000000"),
                 counter: int = 1):
        """RFC 8439 §2.3 block function.

        ``key`` is 32 bytes, ``nonce`` 12 bytes and ``counter`` a 32-bit block
        counter — the RFC's own parameters, parsed little-endian exactly as
        §2.3 specifies. A wrong length RAISES rather than being silently padded.

        They are build-time constants here: the initial state they define is
        baked into the finish stage as data words, which is what removes the
        need for a shadow copy of the state.
        """
        super().__init__(name, key=key, nonce=nonce, counter=counter)
        self.key = bytes(key)
        self.nonce = bytes(nonce)
        self.counter = int(counter) & MASK32
        self._initial = initial_state(self.key, self.nonce, self.counter)

    # ------------------------------------------------------------- structure
    @property
    def cell_count(self) -> int:
        return len(RING) + len(INTERIOR_CELLS)

    @property
    def interface(self) -> BlockInterface:
        return self._interface

    def output_cell_id(self):
        return "out"

    # --------------------------------------------------------- cell builders
    def _row(self, k: int) -> CellProgram:
        """One row of the state: four 32-bit slots as a rotating shift register.

        Three entries share ONE rotate body:

        * ``pub``  — publish slot 0 as two words to this row's TAP cell, then
          trigger the next row so the four rows emit in frame order a,b,c,d.
        * ``wb``   — the replacement word is already in ``nh``/``nl`` (the
          write-back cell put it there), so fall straight into the rotate.
        * ``spin`` — park the CURRENT head in ``nh``/``nl`` and rotate: a rotate
          with no replacement. Realigning row ``k`` is exactly ``k`` of these,
          which is the whole of the diagonal-half permutation.

        ``nh``/``nl`` double as the rotate temporaries — that is what keeps the
        cell inside its budget rather than needing two more registers.

        **Why the publish goes to a TAP and not straight to the collector.** The
        row's words must reach the quarter round during the 80 compute laps and
        the ADDER during the 4 drain laps — two destinations. A ``WRITE``'s
        ``HOP_CNT``/``DEST`` are instruction fields, so one ``WRITE`` cannot
        choose; two publish bodies would cost 5 more instructions, and with 10
        live registers the cell only has room for 20. Measured: the two-body
        form assembles to 23 instructions and lands ``base_addr`` at 8, i.e. two
        state words ON TOP of its own code — the silent overlap that assembles,
        loads, places and routes clean and returns a wrong answer (INV-33's
        overlap half). Splitting the choice into a tap cell is the INV-49 trade:
        cells are the surplus resource, words are the scarce one.
        """
        # The row BOOTS holding its four words of the RFC 8439 initial state,
        # and re-boots to them at every packet boundary (``reset_per_batch``),
        # so a second trigger recomputes the block from a cold start rather than
        # continuing to permute whatever the last run left behind.
        init = [self._initial[4 * k + i] for i in range(4)]
        st = []
        for i in range(4):
            st.append(StateVar(f"s{i}h", register=3 + 2 * i,
                               initial_value=(init[i] >> 16) & MASK16,
                               reset_per_batch=True,
                               reset_value=(init[i] >> 16) & MASK16))
            st.append(StateVar(f"s{i}l", register=4 + 2 * i,
                               initial_value=init[i] & MASK16,
                               reset_per_batch=True,
                               reset_value=init[i] & MASK16))
        return CellProgram(
            inputs=[Port("nh", register=1), Port("nl", register=2)],
            outputs=[Port("oh"), Port("ol"), Port("nxt")],
            entries=[EntryPoint("pub"), EntryPoint("spin"),
                     EntryPoint("wb")],
            data=[],
            state=st,
            assembly_template="""\
pub:
    MOVE R0, R{state:s0h}
    {write:oh}
    MOVE R0, R{state:s0l}
    {write:ol}
    {jump:nxt}
    HALT
spin:
    MOVE R{in:nh}, R{state:s0h}
    MOVE R{in:nl}, R{state:s0l}
wb:
    MOVE R{state:s0h}, R{state:s1h}
    MOVE R{state:s0l}, R{state:s1l}
    MOVE R{state:s1h}, R{state:s2h}
    MOVE R{state:s1l}, R{state:s2l}
    MOVE R{state:s2h}, R{state:s3h}
    MOVE R{state:s2l}, R{state:s3l}
    MOVE R{state:s3h}, R{in:nh}
    MOVE R{state:s3l}, R{in:nl}
""",
        )

    @staticmethod
    def _tap(last: bool) -> CellProgram:
        """Steers one row's published pair to the quarter round or to the adder.

        The row has ONE publish path — it cannot afford two, and a ``WRITE``'s
        ``HOP_CNT``/``DEST`` are instruction fields so one ``WRITE`` cannot pick
        a destination anyway. The choice therefore lives HERE, as a local
        ``BR`` on a mode flag: a branch selects between two ordinary
        compile-time constant destinations, which is not a computed destination
        at all.

        * compute mode (the 80 laps) — forward the pair into the frame collector
          as two ``WRITE``+``JUMP`` pairs (its serial contract), then trigger the
          next row's ``pub``.
        * drain mode (the 4 finish laps) — forward the pair to this row's adder
          and then pass the baton to the NEXT ROW'S PUBLISH, exactly as compute
          mode does. The words are UNTRANSFORMED here, which is the whole point:
          draining UPSTREAM of the quarter round is what lets the finish path
          reuse the publish path unchanged.

          The baton must go to ``row{k+1}.pub``, **not** to ``tap{k+1}``. A tap
          holds only whatever its row last published, so chaining tap-to-tap
          re-emits four STALE register pairs: measured, the drain produced eight
          words of which only the first pair -- row 0's, freshly published --
          was correct. Both modes therefore share the one ``nq`` port.

        ``arm`` is the entry ``seq`` uses to flip the taps into drain mode. It
        CHAINS along the tap line: each tap sets its own flag and then arms the
        next, so ``seq`` fires ONE trigger and all four flip. ``seq`` then starts
        the drain lap itself, with its ordinary ``pub`` jump -- because every tap
        is now in drain mode, that one publish walks the whole state out to the
        adders instead of into the quarter round.

        Arming without starting the lap leaves the block armed and IDLE, and
        arming only the first tap leaves three of them still in compute mode.
        Both were measured here: the ring ran all 80 laps correctly, ``tap0.arm``
        fired exactly once, and then nothing at all happened.
        """
        qr_tail = "" if last else "    {jump:nq}\n"
        # At the END of the tap line the drain closes its own lap: each row holds
        # FOUR 32-bit words and one lap emits only the head of each, so the drain
        # runs four times. `nlap` counts them -- without it the drain recirculates
        # forever. The baton goes SOUTH, the only direction from which anything
        # eventually reaches the control corner (see :meth:`_wb`'s `drn`).
        add_tail = qr_tail
        # The chain: arm the next tap. The last tap ends it.
        arm_tail = "" if last else "    {jump:narm}\n"
        return CellProgram(
            inputs=[Port("h", register=1), Port("l", register=2)],
            outputs=([Port("q"), Port("ah"), Port("al")]
                     + ([] if last else [Port("nq"), Port("narm")])),
            entries=[EntryPoint("default"), EntryPoint("arm")],
            data=[DataWord("one", 1, address=3),
                  # The tap serves TWO directions: along the ring (its resting
                  # face) to the collector and the next row, and INWARD to its
                  # adder. A cell has exactly one outgoing walk, so the second
                  # direction costs an in-program FACE flip -- 2 instructions
                  # and 1 data word per extra direction (INV-48). The tap has
                  # the spare words for it; the row cell did not, which is the
                  # other half of why the steering lives here.
                  DataWord("f_ring", 1, address=5, is_face=True),   # EAST
                  DataWord("f_in", 3, address=6, is_face=True)],    # NORTH
            state=[StateVar("mode", register=4, initial_value=0,
                            reset_per_batch=True, reset_value=0)],
            assembly_template="""\
default:
    CMP R{state:mode}, R{data:one}
    BR.Z drain
    MOVE R0, R{in:h}
    {write:q}
    {jump:q}
    MOVE R0, R{in:l}
    {write:q}
    {jump:q}
""" + qr_tail + """\
    HALT
drain:
    MOVE [FACE], R{data:f_in}
    MOVE R0, R{in:h}
    {write:ah}
    MOVE R0, R{in:l}
    {write:al}
    {jump:al}
    MOVE [FACE], R{data:f_ring}
""" + add_tail + """\
    HALT
arm:
    MOVE R{state:mode}, R{data:one}
""" + arm_tail,
        )

    @staticmethod
    def _wb() -> CellProgram:
        """Write-back: peel the quarter-round result frame back into the rows.

        This REPLACES :class:`ChaCha20QRBlock`'s ``emit``. ``emit`` bursts the
        eight words out of one port, which would force this cell to count eight
        separate arrivals; taking the frame the way every other relay stage
        takes it — eight plain ``WRITE``s plus one trigger — needs no counter at
        all, and that is the difference between fitting and not.

        Slot 0 arrives in R0 by ACCUMULATOR DELIVERY (the upstream stage writes
        it LAST), so its ``WRITE`` is this cell's first instruction with no
        ``MOVE`` — ``emit``'s word-saving idiom, kept.

        The four rows lie consecutively along this cell's own eastward walk, so
        all eight writes and the four triggers are constant hops, no face flip.

        **This cell RESTORES its resting face before it ends, and that restore
        is load-bearing for a word it never touches.** ``wb`` sits at ``(0,1)``,
        directly on the walk from ``seq`` down to the state line, so every
        ``pub`` trigger ``seq`` issues TRANSITS this cell — and a transiting word
        is forwarded on the transit cell's **live face register**, not on the
        face the layout gave it (INV-48 root cause C). Leaving the face pointing
        north at ``wbk`` therefore bounces ``seq``'s next ``pub`` straight back
        into ``seq``, which re-enters ``step``, decrements the lap counter again
        and ping-pongs until the counter runs out. Measured: the ring completed
        exactly ONE lap and then oscillated between ``seq`` and ``wb`` with no
        output and no error.
        """
        body = "    {write:w0h}\n"
        for slot, port in zip(FRAME[1:],
                              ("w0l", "w1h", "w1l", "w2h", "w2l",
                               "w3h", "w3l")):
            body += f"    MOVE R0, R{{in:{slot}}}\n    {{write:{port}}}\n"
        # The eight write-backs ride the state line's own eastward walk, so they
        # need no flip. Handing the baton to `wbk` goes north, so THAT costs one
        # in-program FACE flip (INV-48: 2 instructions + 1 data word per extra
        # direction) -- and the flip must be UNDONE before the path ends, both
        # because the next lap's writes need EAST and because `seq`'s `pub`
        # triggers transit this cell (see the docstring).
        # `drn` -- a bare RELAY of the drain lap's baton up to `wbk`, sharing the
        # write-back's own flip/restore pair as its TAIL.
        #
        # The drain lap closes at the LAST tap, and nothing on the tap line or
        # the finish row can reach `wbk`: both run one-way AWAY from the control
        # corner, measured over all four faces from each. The last tap's
        # southward walk does cross the idle quarter-round chain and climb the
        # control column to here, and this cell is one of only two that can reach
        # `wbk` at all. So the baton lands here and is passed on.
        #
        # It would cost THREE words, and `wb` has exactly TWO -- see the class
        # docstring's Status note for the four-word shortfall this is part of.
        body += ("    MOVE [FACE], R{data:f_ctl}\n"
                 "    {jump:go}\n"
                 "    MOVE [FACE], R{data:f_line}\n")
        return CellProgram(
            inputs=([Port(FRAME[0], register=0)]
                    + [Port(w, register=1 + i)
                       for i, w in enumerate(FRAME[1:])]),
            outputs=([Port(f"w{k}{h}") for k in range(4) for h in ("h", "l")]
                     + [Port("go")]),
            entries=[EntryPoint("default")],
            data=[DataWord("f_line", 1, address=8, is_face=True),   # EAST
                  DataWord("f_ctl", 3, address=9, is_face=True)],   # NORTH
            state=[],
            # No restore on the way IN: every path out of this cell restores on
            # the way OUT, so the resting face may be assumed on entry.
            assembly_template="default:\n" + body,
        )

    @staticmethod
    def _wbk() -> CellProgram:
        """The ROW-TRIGGER cell: both fixed jump schedules aimed at the rows.

        Two roles share this cell because they need the SAME WALK, and only one
        position on the fold provides it.

        * ``default`` — fire the four row rotates, then advance the lap counter.
          Split out of ``wb`` purely for BUDGET: with the eight frame words
          pinned at R0..R7 and two face constants, ``wb`` assembled to 22
          instructions against a ``base_addr`` of 9, i.e. its own data on top of
          its code. The ORDER is load-bearing — every row must be rotated BEFORE
          the counter advances, because advancing the counter is what starts the
          next lap's publish. Firing them from one instruction stream is what
          guarantees it; there is no lock available and none needed.
        * ``bnd`` — the half-boundary REALIGNMENT: a fixed 12-spin schedule.
          Row ``k`` reads offsets ``0,1,2,3`` through the column half and
          ``k,k+1,k+2,k+3`` through the diagonal half, so the diagonal half is
          the same sequence started ``k`` positions later. Realigning is
          therefore exactly ``k`` extra plain rotations of row ``k`` before the
          diagonal half and ``4 - k`` after it. That is the entire
          column/diagonal permutation, and the reason no selector exists.

        **Why one cell and not two.** Both schedules jump at ``row0..row3``, and
        on this fold the rows are only reachable from ONE slot: ``(1,0)``, whose
        southward walk enters the state line at ``row0`` and runs east through
        it, hitting the four rows at hops 1, 3, 5, 7. Every other slot either
        misses the rows or cannot itself be reached by ``seq`` — measured by
        exhaustive search over all free positions x all four faces, with the old
        two-cell split scoring ZERO feasible layouts. The lap counter also has
        to be reachable, and ``seq`` at ``(0,0)`` is reachable only from ``(1,0)``
        facing WEST, because the column below ``seq`` is a one-way NORTH conveyor
        that ``wb`` turns east. So ``(1,0)`` is forced twice over, and the two
        schedules must share the cell that occupies it.

        Merging is a BUDGET consolidation, not an architecture change (INV-49's
        "cells are the surplus resource, words are the scarce one", applied in
        reverse when the surplus runs out): the two programs are both pure
        fixed-destination jump schedules with the same input register, and the
        merged cell still has spare words.

        Both roles ride this cell's own southward ring walk; only the lap-advance
        trigger goes WEST to the sequencer, so the cell pays for ONE face flip
        (INV-48: 2 instructions + 1 data word per extra direction).
        """
        # `default` (the rotate + lap advance).
        #
        # **The FACE register PERSISTS across entries.** It is a cell register,
        # not a per-entry one, so an entry that does not set it inherits whatever
        # the last path left behind — and a face that misses its target gives NO
        # output and NO error (INV-48 root cause C). The discipline that makes
        # this cheap AND safe is: **every path RESTORES the resting face before
        # it ends**, so every path may assume the resting face on entry. Here
        # only the lap-advance leaves the ring walk, so `default` flips WEST for
        # `{jump:step}` and immediately restores; `bnd` never leaves it at all
        # and needs no flip. Two entries, ONE flip pair.
        body = ""
        for k in range(4):
            body += f"    {{jump:k{k}}}\n"
        body += ("    MOVE [FACE], R{data:f_in}\n"
                 "    {jump:step}\n"
                 "    MOVE [FACE], R{data:f_ring}\n"
                 "    HALT\n")
        # The realignment half. `tog` alternates so the SAME entry issues the
        # pre-diagonal `k` spins on one boundary and the post-diagonal `4 - k`
        # spins on the next. Every destination is a compile-time CONSTANT, so
        # the schedule is unrolled literal JUMPs -- nothing computes a
        # destination. Rows 1..3 sit at hops 3, 5, 7 along this cell's own
        # southward walk, so the realignment needs no face flip at all; only the
        # hand-back to `seq` does, and it reuses `f_in`.
        # The two halves spin row k by `k` and then by `4 - k`:
        #
        #     half A:  row1 x1  row2 x2  row3 x3
        #     half B:  row1 x3  row2 x2  row3 x1
        #
        # so the COMMON part -- row1 once, row2 twice, row3 once -- is the same
        # in both and is hoisted OUT of the branches. What is left is row3 twice
        # on one side and row1 twice on the other. Eight jumps instead of twelve,
        # and rotations commute across different rows so hoisting cannot reorder
        # anything that matters.
        a = "".join(f"    {{jump:a{k}_{i}}}\n"
                    for k in (1, 2, 3) for i in range(k))
        bb = "".join(f"    {{jump:b{k}_{i}}}\n"
                     for k in (1, 2, 3) for i in range((4 - k) & 3))
        common2 = ""
        # `bnd` (the realignment). ONE `back` jump serves both halves: the two
        # spin schedules are laid out so each FALLS THROUGH to a shared tail
        # rather than each ending in its own remote JUMP (INV-43 rule 2 -- a
        # remote JUMP does not stop local execution, so a second copy would need
        # its own HALT and cost two more words). The `BR` forwards over the
        # first half; the second half is the fall-through.
        #
        # `tog` toggles against `f_in` (numerically 2) rather than a dedicated
        # `1` constant. A two-state toggle only needs SOME single bit to flip and
        # a `BR.Z` to test it, so ANY non-zero constant serves; reusing the face
        # word buys back the one word this cell was over budget by. The same
        # word doubles as the `CMP`'s operand, which sets Z unconditionally
        # (`MOVE` does not touch the flags, so the compare must be explicit).
        body += ("bnd:\n"
                 + common2
                 + "    XOR R{state:tog}, R{data:f_in}\n"
                   "    MOVE R{state:tog}, R0\n"
                   "    BR.Z second\n"
                 + a
                 + "    CMP R{data:f_in}, R{data:f_in}\n"
                   "    BR.Z back\n"
                   "second:\n"
                 + bb
                 + "back:\n"
                   "    {jump:back}\n")
        # `drn` -- the DRAIN rotate. The finish emits one 32-bit word per row per
        # lap and each row holds four, so between drain laps every row must
        # advance by ONE plain rotate. That is `row.spin`, a different entry from
        # the compute laps' `row.wb`: in drain there is no returned word and
        # `nh`/`nl` still hold the last compute lap's, so `wb` would install
        # garbage. Same cell, same southward walk, same hops 1/3/5/7 -- only the
        # target entry differs. It then restarts the lap at `row0.pub`, hop 1 on
        # that same walk. The words for it come from the `bnd` hoist above.
        outs = ([Port(f"k{k}") for k in range(4)] + [Port("step")]
                + [Port(f"a{k}_{i}") for k in (1, 2, 3) for i in range(k)]
                + [Port(f"b{k}_{i}") for k in (1, 2, 3)
                   for i in range((4 - k) & 3)]
                + [Port("back")])
        return CellProgram(
            inputs=[Port("go", register=1)],
            outputs=outs,
            entries=[EntryPoint("default"), EntryPoint("bnd")],
            data=[DataWord("f_ring", 0, address=2, is_face=True),   # SOUTH
                  DataWord("f_in", 2, address=3, is_face=True)],    # WEST
            state=[StateVar("tog", register=4, initial_value=0,
                            reset_per_batch=True, reset_value=0)],
            assembly_template="default:\n" + body,
        )

    @staticmethod
    def _passthru() -> CellProgram:
        """A one-word pass-through that PAVES the finish row.

        Measured: a word does transit a cell that has a face and no program --
        but only if something SET that face, and the build sets faces on
        non-block cells only where a ROUTE claims them. A gap inside the block's
        own footprint is therefore a dead end for a block-internal WRITE, so the
        walk the adders share has to be paved with real cells.
        """
        return CellProgram(
            inputs=[Port("v", register=1)],
            outputs=[Port("o")],
            entries=[EntryPoint("default")],
            data=[], state=[],
            assembly_template="""\
default:
    MOVE R0, R{in:v}
    {write:o}
    {jump:o}
""",
        )

    @staticmethod
    def _out() -> CellProgram:
        """The block's egress: one word in, one word out of the block.

        It is the LAST cell of the serpentine, which is deliberate. The fold is
        a serpentine rather than a closed geometric ring because **a closed ring
        traps its interior**: every ring cell forwards along the ring, and a word
        is forwarded on each TRANSIT CELL'S OWN face, so a word emitted inside
        the ring in ANY direction joins the ring and follows it forever — there
        is no walk from the inside to the outside. Measured on the fold, not
        derived. A serpentine has a free end, so the egress simply sits there.

        The ``WRITE``+``JUMP`` pair IS the external port handshake. It must NOT
        also be declared an internal jump: a ``__terminate__`` edge on an
        external output port marks it internally-consumed and the portmap then
        DELETES the block's only output port, leaving a design that builds and
        routes cleanly with no output port at all.
        """
        return CellProgram(
            inputs=[Port("v", register=1)],
            outputs=[Port("out")],
            entries=[EntryPoint("default")],
            data=[], state=[],
            assembly_template="""\
default:
    MOVE R0, R{in:v}
    {write:out}
    {jump:out}
""",
        )

    @staticmethod
    def _relay() -> CellProgram:
        """One pure STREAMING relay — what lets the ring close as geometry.

        A rectangle's perimeter is always EVEN and the datapath cycle is 21
        cells, so exactly one filler is needed for the loop to close without a
        face flip. A streaming relay is the cheapest filler there is: INV-47's
        correction measured it at a CONSTANT cost for any frame width, with
        8/16/32/64/128-word frames crossing real cells bit-exact.
        """
        body = ""
        for slot in FRAME:
            body += f"    MOVE R0, R{{in:{slot}}}\n    {{write:o_{slot}}}\n"
        body += "    {jump:trig}\n"
        return CellProgram(
            inputs=[Port(w, register=1 + i) for i, w in enumerate(FRAME)],
            outputs=[Port(f"o_{w}") for w in FRAME] + [Port("trig")],
            entries=[EntryPoint("default")],
            data=[], state=[],
            assembly_template="default:\n" + body,
        )

    def _seq(self) -> CellProgram:
        """The lap counter: 80 quarter rounds as ten 8-lap double rounds.

        One lap of the ring IS one quarter-round invocation, so this cell only
        counts. ``half`` counts four laps and hands over to ``realign`` at each
        half boundary; ``laps`` counts all 80 and then starts the drain.

        **The half ORDER is load-bearing and is gated.** The column half must
        run first: starting on the diagonal, or walking the step index
        DOWNWARD, still performs exactly 80 invocations but computes a
        DIFFERENT cipher, so no count-based or structural check catches it —
        only a schedule-value gate does.
        """
        return CellProgram(
            inputs=[Port("go", register=1)],
            outputs=[Port("pub"), Port("bnd"), Port("fin")],
            entries=[EntryPoint("default"), EntryPoint("step")],
            data=[DataWord("one", 1, address=2),
                  DataWord("four", 4, address=3),
                  DataWord("eighty", self.LAPS, address=4),
                  # `seq` lives in the block's TOP row (so the chip's input port
                  # can reach it) and serves TWO directions. Its RESTING face is
                  # EAST, which reaches `wbk` at hop 1 -- the boundary hand-off.
                  # Everything else it drives is on the STATE line below, so
                  # those emits flip SOUTH and RESTORE before the path ends.
                  #
                  # The restore is not optional bookkeeping: the FACE register
                  # PERSISTS across entries, so a path that leaves it pointing
                  # south makes the NEXT lap's boundary jump fire south into
                  # `wb` instead of east into `wbk` -- no output, no error
                  # (INV-48 root cause C).
                  #
                  # EVERY path restores, `finish` included. It is tempting to
                  # skip it there -- `finish` is the terminal path of a batch --
                  # but `half` and `laps` are `reset_per_batch` while the FACE
                  # register is not, so a second trigger would enter `step` with
                  # the face still pointing south and fire the first boundary
                  # into `wb` instead of `wbk`. The word for it came from
                  # dropping `default`'s redundant `MOVE half, four`: `half`
                  # already resets to 4 per batch.
                  DataWord("f_line", 0, address=7, is_face=True),   # SOUTH
                  DataWord("f_ctl", 1, address=8, is_face=True)],   # EAST
            state=[StateVar("half", register=5, initial_value=4,
                            reset_per_batch=True, reset_value=4),
                   StateVar("laps", register=6, initial_value=LAPS_INIT,
                            reset_per_batch=True, reset_value=LAPS_INIT)],
            assembly_template="""\
default:
    MOVE R{state:half}, R{data:four}
    MOVE R{state:laps}, R{data:eighty}
    MOVE [FACE], R{data:f_line}
    {jump:pub}
    MOVE [FACE], R{data:f_ctl}
    HALT
step:
    SUB R{state:laps}, R{data:one}
    MOVE R{state:laps}, R0
    BR.Z finish
    SUB R{state:half}, R{data:one}
    MOVE R{state:half}, R0
    BR.NZ more
    MOVE R{state:half}, R{data:four}
    {jump:bnd}
    HALT
more:
    MOVE [FACE], R{data:f_line}
    {jump:pub}
    MOVE [FACE], R{data:f_ctl}
    HALT
finish:
    MOVE [FACE], R{data:f_line}
    {jump:fin}
    {jump:pub}
""",
        )

    def _adder(self, k: int, last: bool) -> CellProgram:
        """Finish stage for row ``k``: add the initial state back, emit 2 words.

        RFC 8439 §2.3 finishes by adding the ORIGINAL state to the permuted one
        word by word mod 2**32 — the step that makes the block function one-way.
        Dropping it leaves a trivially invertible permutation, which is an
        explicit mutation gate.

        The four addends this row needs live here as a ROTATING shift register
        that steps in lockstep with the row, so the tap is always slot 0 —
        exactly the fixed-tap idea that removed the selector from the main loop.
        No counter and no computed selection anywhere in the finish path either.

        **The 32-bit add must be in ONE cell**: ALU flags are per-cell, so a
        carry cannot cross a cell boundary. ``ADD``/park/``ADC``/park keeps the
        carry on the flag-preserving ``MOVE`` (INV-45's idiom) — it is never
        re-derived with a ``CMP``.
        """
        ks = [self._initial[4 * k + i] for i in range(4)]
        data = []
        for i, v in enumerate(ks):
            data.append(DataWord(f"k{i}h", (v >> 16) & MASK16,
                                 address=1 + 2 * i, reset_per_batch=True))
            data.append(DataWord(f"k{i}l", v & MASK16,
                                 address=2 + 2 * i, reset_per_batch=True))
        return CellProgram(
            inputs=[Port("vh", register=9), Port("vl", register=10)],
            outputs=[Port("out")],
            entries=[EntryPoint("default")],
            data=data,
            state=[],
            assembly_template="""\
default:
    ADD R{in:vl}, R{data:k0l}
    MOVE R{in:vl}, R0
    ADC R{in:vh}, R{data:k0h}
    MOVE R{in:vh}, R0
    MOVE R0, R{in:vh}
    {write:out}
    {jump:out}
    MOVE R0, R{in:vl}
    {write:out}
    {jump:out}
    MOVE R{in:vh}, R{data:k0h}
    MOVE R{in:vl}, R{data:k0l}
    MOVE R{data:k0h}, R{data:k1h}
    MOVE R{data:k0l}, R{data:k1l}
    MOVE R{data:k1h}, R{data:k2h}
    MOVE R{data:k1l}, R{data:k2l}
    MOVE R{data:k2h}, R{data:k3h}
    MOVE R{data:k2l}, R{data:k3l}
    MOVE R{data:k3h}, R{in:vh}
    MOVE R{data:k3l}, R{in:vl}
""",
        )

    def build_cell_programs(self) -> Dict[Any, CellProgram]:
        """Cells in LAYOUT order (positional pairing, INV-33).

        ``seq`` is the block's landing cell: ``engine/catalog.py``'s
        ``resolved_io`` picks the FIRST cell that declares inputs, so the
        external trigger must arrive at the sequencer. An iterative block that
        orders a mid-loop stage first starts the loop with its schedule
        registers uninitialised, and the design still builds and routes clean
        while emitting nothing (INV-47's wiring rule).
        """
        qr = ChaCha20QRBlock(f"{self.name}_qr").build_cell_programs()
        progs: Dict[Any, CellProgram] = {}
        for cid in RING:                       # ring order == layout order
            if cid == "wb":
                progs[cid] = self._wb()
            elif cid in ("relay", "relay2"):
                progs[cid] = self._relay()
            elif cid.startswith("row"):
                progs[cid] = self._row(int(cid[3:]))
            elif cid.startswith("tap"):
                progs[cid] = self._tap(last=(cid == "tap3"))
            elif cid == "wbk":
                progs[cid] = self._wbk()
            elif cid == "seq":
                progs[cid] = self._seq()
            else:
                progs[cid] = qr[cid]
        for k in range(4):
            progs[f"add{k}"] = self._adder(k, last=(k == 3))
        for k in range(3):
            progs[f"pass{k}"] = self._passthru()
        progs["add_pad"] = self._passthru()
        progs["ctl_pad"] = self._passthru()
        progs["ra_pad"] = self._passthru()
        progs["out"] = self._out()

        # PIN EVERY CELL'S RESTING FACE, from the one geometry that defines it.
        #
        # Without this the router GUESSES each block cell's ``fwd_face``: it
        # faces the cell at the other end of "an" internal connection, picking
        # whichever the dict happens to yield, and only then falls back to the
        # positional-next cell (``router.py`` ``_place_block_cells``). The guess
        # then feeds ``_get_routing_distance``, which walks those same faces to
        # size every WRITE and JUMP -- so one wrong guess silently mis-sizes
        # real hops.
        #
        # Measured here: ``tap3`` has connections to BOTH ``in0`` (east, 1 hop)
        # and ``add3`` (north, 1 hop). The router faced it NORTH, then walked
        # ``tap3 -> add3 -> out -> in0`` and sized ``tap3.q -> in0`` at THREE
        # hops. At run time the word leaves on the cell's real resting face,
        # EAST, so it overshot ``in0`` by two and landed in ``l1_add``. The
        # effect was that every frame arrived at the collector TWO WORDS SHORT:
        # ``in1``'s mod-8 counter never reached a frame boundary in step with
        # the ring, the compute head fired out of phase, and the whole cipher
        # ran five laps and stalled -- with no error anywhere.
        #
        # ``CellProgram.fwd_face`` is honoured ahead of the guess, so pinning it
        # makes the router's model equal the geometry the block actually gets.
        for cid, (_x, _y, f) in self._geometry().items():
            progs[cid].fwd_face = FACE_CODE[f]
        return progs

    # ---------------------------------------------------------------- wiring
    def internal_connections(self) -> List[Tuple[Any, str, Any, str]]:
        """DATA edges (each is a ``WRITE``)."""
        conns: List[Tuple[Any, str, Any, str]] = []
        # Each row publishes its head pair to its TAP, which steers it either
        # into the quarter round (the 80 compute laps) or into that row's adder
        # (the 4 drain laps) -- both compile-time constant destinations.
        for k in range(4):
            conns += [(f"row{k}", "oh", f"tap{k}", "h"),
                      (f"row{k}", "ol", f"tap{k}", "l")]
        # On the compute path the taps feed the collector, two words each, in
        # frame order a,b,c,d -- so in0 sees exactly the serial 8-word stream
        # ChaCha20QRBlock's collectors are already proven on.
        for k in range(4):
            conns.append((f"tap{k}", "q", "in0", "x"))
            conns.append((f"tap{k}", "ah", f"add{k}", "vh"))
            conns.append((f"tap{k}", "al", f"add{k}", "vl"))
            conns.append((f"add{k}", "out", "out", "v"))
        # in0 spills into in1, and the two collectors fill the compute head.
        conns.append(("in0", "spill", "in1", "sp"))
        head = QR_CELLS[2]
        conns += [("in1", f"h{i}", head, FRAME[i]) for i in range(4)]
        conns += [("in0", f"h{4+i}", head, FRAME[4 + i]) for i in range(4)]
        # Each compute stage relays the whole frame to the next.
        chain = QR_CELLS[2:]
        for src, dst in zip(chain, chain[1:]):
            conns += [(src, f"o_{w}", dst, w) for w in FRAME]
        # The last stage hands the frame to the ring's streaming relay, and the
        # relay to the write-back cell -- slot 0 into its ACCUMULATOR (R0) as
        # the final write, which is what keeps `wb` inside its budget.
        conns += [(chain[-1], f"o_{w}", "relay", w) for w in FRAME]
        conns += [("relay", f"o_{w}", "relay2", w) for w in FRAME]
        conns += [("relay2", f"o_{w}", "wb", w) for w in FRAME]
        # The write-back pushes each result word into its row's `nh`/`nl`.
        for k in range(4):
            conns += [("wb", f"w{k}h", f"row{k}", "nh"),
                      ("wb", f"w{k}l", f"row{k}", "nl")]
        return conns

    def internal_jumps(self) -> List[Tuple[Any, str, Any, str]]:
        """TRIGGER edges (each is a ``JUMP``).

        Note there is at most ONE backward ``JUMP`` per cell:
        ``build._apply_internal_feedback`` restores only the highest-address
        ``JUMP`` per cell and silently loses a second (INV-48 rule 2, INV-49).
        The ring's backward edge is ``relay -> wb``, which is not backward on
        the array at all — the fold makes it ordinary forward geometry.
        """
        jumps: List[Tuple[Any, str, Any, str]] = []
        # seq starts a lap at row0 and drives the boundary/finish paths. The
        # FINISH path enters the same rows through their taps' `to_add` entry,
        # which is how the drain reuses the publish path unchanged.
        jumps.append(("seq", "pub", "row0", "pub"))
        jumps.append(("seq", "bnd", "wbk", "bnd"))
        # The FINISH path arms the taps into drain mode; from then on the very
        # same publish path walks the state out to the adders instead.
        jumps.append(("seq", "fin", "tap0", "arm"))
        # Each row publishes to its tap; the tap forwards and passes the baton.
        for k in range(4):
            jumps.append((f"row{k}", "nxt", f"tap{k}", "default"))
            jumps.append((f"tap{k}", "q", "in0", "default"))
            if k < 3:
                # ONE baton port, used by BOTH modes -- see :meth:`_tap`.
                jumps.append((f"tap{k}", "nq", f"row{k+1}", "pub"))
                # ARM chains along the tap line so ONE trigger from `seq` flips
                # all four into drain mode.
                jumps.append((f"tap{k}", "narm", f"tap{k+1}", "arm"))
            jumps.append((f"tap{k}", "al", f"add{k}", "default"))
            jumps.append((f"add{k}", "out", "out", "default"))
        # The two collectors are a SHIFT PAIR: `in0` takes every word, spills its
        # oldest into `in1` and CLOCKS it; `in1` counts mod 8 and only then fires
        # the compute head. Both edges come straight from `ChaCha20QRBlock` and
        # both are required -- `in0 -> in1` is the clock, and omitting it leaves
        # `in1`'s `{jump:trig}` port undeclared while the assembly still emits
        # the word, so the build resolves it to whatever the fallback yields.
        jumps.append(("in0", "trig", "in1", "default"))
        jumps.append(("in1", "trig", QR_CELLS[2], "default"))
        chain = QR_CELLS[2:]
        for src, dst in zip(chain, chain[1:]):
            jumps.append((src, "trig", dst, "default"))
        jumps.append((chain[-1], "trig", "relay", "default"))
        jumps.append(("relay", "trig", "relay2", "default"))
        jumps.append(("relay2", "trig", "wb", "default"))
        # The write-back hands off to wbk, which rotates every row and only
        # THEN advances the lap counter -- the order matters, because advancing
        # the counter is what starts the next lap's publish.
        jumps.append(("wb", "go", "wbk", "default"))
        for k in range(4):
            jumps.append(("wbk", f"k{k}", f"row{k}", "wb"))
        jumps.append(("wbk", "step", "seq", "step"))
        # The realignment half of the SAME cell spins rows 1..3 the fixed number
        # of times, then starts the next lap directly at row0's publish. Row 2
        # gets two spins in EITHER half, so its pair is issued unconditionally
        # (`c2_*`) and only rows 1 and 3 differ between the halves.
        for k in (1, 2, 3):
            for i in range(k):
                jumps.append(("wbk", f"a{k}_{i}", f"row{k}", "spin"))
            for i in range((4 - k) & 3):
                jumps.append(("wbk", f"b{k}_{i}", f"row{k}", "spin"))
        jumps.append(("wbk", "back", "row0", "pub"))
        return jumps

    def emit_faces(self) -> Dict[Tuple[Any, str], Any]:
        """Ports this block emits while its cell is FLIPPED, and toward whom.

        A cell forwards on its RESTING face, and every word that merely transits
        it does too — but a cell may re-point itself mid-program with
        ``MOVE [FACE], R{data:…}`` and emit a ``WRITE``/``JUMP`` while flipped.
        The router sizes internal edges by walking resting faces
        (``router._get_routing_distance``), so for a flipped emit it walks a path
        the word never takes. Measured on this block: 211 of 211 resting-face
        edges resolved correctly and 6 of 22 FLIPPED ones did not, with the rest
        correct only because the walk happened to miss and the Manhattan
        fallback happened to equal the true distance.

        The value is a NEIGHBOUR CELL ID rather than a compass direction, so the
        declaration is orientation-free: the router derives the face from the two
        cells' placed coordinates, which the placer has already rotated (INV-23).
        """
        faces: Dict[Tuple[Any, str], Any] = {}
        for k in range(4):
            # The tap turns INWARD to its adder.
            faces[(f"tap{k}", "ah")] = f"add{k}"
            faces[(f"tap{k}", "al")] = f"add{k}"
        # `wb` hands the baton north, through `seq`, on to `wbk`.
        faces[("wb", "go")] = "seq"
        # `wbk` steps the lap counter west.
        faces[("wbk", "step")] = "seq"
        # `seq` drops south onto the state line for everything but the boundary.
        for p in ("pub", "fin"):
            faces[("seq", p)] = "wb"
        return faces

    def _geometry(self) -> Dict[Any, Tuple[int, int, str]]:
        """A 10x6 fold in four bands. Every edge is on a REAL forwarding walk.

        This layout is SOLVED, not chosen. A word is forwarded on each TRANSIT
        CELL'S OWN face, not the sender's, so from any cell there is exactly ONE
        outgoing walk and all of that cell's targets must lie along it, in the
        order the walk visits them. A layout that violates this places clean,
        builds clean, passes DRC and then HANGS — the failure mode is silence,
        not an error — so the fold was searched against a walk simulator and
        every one of the block's internal edges verified before any silicon.

        Three structural facts fix the shape; the rest was searched:

        1. **The state line must be collinear and co-facing.**
           ``row0 tap0 row1 tap1 row2 tap2 row3 tap3`` sit in one eastward row,
           because ``wb`` (eight write-backs), ``wbk`` (four rotate triggers)
           and ``realign`` (the boundary spins) each have to reach several of
           them from ONE walk. This is the ``LMSEqualizerBlock`` broadcast
           idiom: consecutive targets along a single walk.
        2. **The finish row must be gap-free.** The four adders share one
           eastward walk into ``out``, and an unoccupied column is a DEAD END
           for a block-internal WRITE — the build gives a bare array cell a face
           only where a ROUTE claims it. Hence ``pass0..pass2`` paving the
           columns between the adders. (A faced-but-programless cell DOES
           forward — measured at distances 2/3/4/6 — but nothing sets that face
           inside the block's own footprint.)
        3. **The control column is one northward walk.** ``relay2``, ``realign``,
           ``seq`` and ``wbk`` all face north, so each one's walk climbs the
           column, passes through ``wb``, and continues east along the state
           line. The single backward edge, ``wb -> wbk``, is served by ``wb``'s
           one face flip.

        The bands::

            y=0  finish:   add0 pass0 add1 pass1 add2 pass2 add3 out
            y=1  state:    wb | row0 tap0 row1 tap1 row2 tap2 row3 tap3 | in0
            y=2  QR leg 1: (west)  in1 l1_add l1_xor l2_add l2_xor l2_rota
            y=3  QR leg 2: (east)  l2_rotb l3_add l3_xor
            y=4  QR leg 3: (west)  l3_rota l3_rotb l4_add l4_xor l4_rota l4_rotb
            y=5  loop:     relay2 relay

        Each adder sits directly NORTH of its own tap, so the tap's inward face
        flip reaches it at hop 1.

        The fold is 10 wide — the full array width — which is why the block
        declares ``CHIP_SCALE``. The <=8-across convention exists only to leave
        a bus channel for OTHER blocks; a sole occupant has none to pass, and a
        wider fold leaves whole free rows rather than fragmented perimeter (see
        layout_rules.md §3 and INV-40).
        """
        lay: Dict[Any, Tuple[int, int, str]] = {}
        a, b = self._QR_LEG_A, self._QR_LEG_B

        # --- y=0, the TOP row. BOTH I/O cells live here. The state line below
        #     is irreducibly 10 columns wide, so the block is full array width
        #     and there is no vertical corridor; placed at array row 1 or below
        #     it leaves array ROW 0 free, and the corridor runs along that row
        #     and taps `seq` (input) and `out` (output) from above.
        #     `wbk` is here too: it emits only triggers, and from this row ONE
        #     face flip south drops it onto the state line, whose eastward walk
        #     then serves all four rows -- so a single cell reaches both `seq`
        #     and every row, which no position in the control column can do.
        # `seq` RESTS EAST, and that is FORCED by a word it never touches:
        # `wb`'s hand-off to `wbk` leaves `wb` going north INTO `seq`, and a
        # transiting word is forwarded on the transit cell's own face (INV-48
        # root cause C). Only `seq` facing east carries it on to `wbk`. So
        # `seq`'s own triggers, which all want the state line to the south, each
        # flip and restore instead.
        lay["seq"] = (0, 0, "east")
        # `wbk` RESTS SOUTH: from (1,0) that walk enters the state line at
        # `row0` and runs east along it, hitting the four rows at hops 1, 3, 5,
        # 7. That single walk is what both of its schedules -- the per-lap
        # rotates and the half-boundary realignment -- need, and (1,0) is the
        # ONLY slot on this fold that provides it while also being the only slot
        # that can reach `seq` (west, hop 1). See :meth:`_wbk`.
        lay["wbk"] = (1, 0, "south")
        for k in range(4):
            lay[f"add{k}"] = (2 + 2 * k, 0, "east")
        for k in range(3):
            lay[f"pass{k}"] = (3 + 2 * k, 0, "east")
        lay["out"] = (9, 0, "north")

        # --- y=1, the STATE line: `wb` at its west end, then the four
        #     row/tap pairs, then the frame collector.
        lay["wb"] = (0, 1, "east")
        for i, cid in enumerate(STATE_LINE):
            lay[cid] = (1 + i, 1, "east")

        # --- the quarter round: down the east side, then three serpentine legs
        #     sized so the tail lands at column 1 and drops onto `relay`.
        qr = list(QR_CELLS)
        lay[qr[0]] = (9, 1, "south")
        c = len(qr) - 1 - a - b
        for i, cid in enumerate(qr[1:1 + a]):                  # leg 1, west
            lay[cid] = (9 - i, 2, "west" if i < a - 1 else "south")
        x3 = 9 - (a - 1)
        for i, cid in enumerate(qr[1 + a:1 + a + b]):          # leg 2, east
            lay[cid] = (x3 + i, 3, "east" if i < b - 1 else "south")
        x4 = x3 + (b - 1)
        for i, cid in enumerate(qr[1 + a + b:]):               # leg 3, west
            lay[cid] = (x4 - i, 4, "west" if i < c - 1 else "south")

        # --- the loop hand-off and the northward CONTROL column. `relay2`,
        #     `realign` and the two pads all face north, so each one's walk
        #     climbs the column, passes through `wb`, and continues east along
        #     the state line.
        lay["relay"] = (1, 5, "west")
        lay["relay2"] = (0, 5, "north")
        lay["ra_pad"] = (0, 4, "north")
        lay["ctl_pad"] = (0, 3, "north")
        lay["add_pad"] = (0, 2, "north")

        assert len(lay) == self.cell_count, (
            f"layout has {len(lay)} cells, block declares {self.cell_count}")
        assert len({v[:2] for v in lay.values()}) == len(lay), \
            "two cells share a position"
        return lay

    def default_layout(self) -> Dict[Any, Tuple[int, int, str]]:
        """:meth:`_geometry`, reindexed into PROGRAM order.

        POSITIONAL PAIRING (INV-51 clause 2): the router and the build walk
        ``build_cell_programs`` and the placed cells in LOCKSTEP by position, so
        the two dicts must iterate in the SAME order. They are keyed by cell id,
        which hides the mismatch -- a layout in a different order silently pairs
        each program with the wrong cell, and the block builds and routes clean
        while whole cells come out EMPTY.
        """
        lay = self._geometry()
        order = list(self.build_cell_programs().keys())
        assert set(order) == set(lay), "layout and programs name different cells"
        return {cid: lay[cid] for cid in order}

    # ------------------------------------------------------------- reference
    def process_reference(self, input_words) -> np.ndarray:
        """The RFC 8439 §2.3 block function for this block's key/nonce/counter.

        The block takes no data input — one trigger emits one 64-byte keystream
        block — so ``input_words`` only sets HOW MANY blocks are produced, with
        the counter incrementing per block exactly as §2.3 specifies.
        """
        n = max(1, len(np.asarray(input_words).ravel()))
        out: List[int] = []
        for b in range(n):
            for v in block_function(self.key, self.nonce,
                                    (self.counter + b) & MASK32):
                out.append((v >> 16) & MASK16)
                out.append(v & MASK16)
        return np.array(out, dtype=np.uint16)

    def reset(self):
        """No cross-call state: every trigger recomputes a whole block."""
        pass

