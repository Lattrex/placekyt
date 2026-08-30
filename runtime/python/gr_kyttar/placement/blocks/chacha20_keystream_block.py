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
#:
#: ``drn`` sits here, before the state line, even though it is not on the
#: datapath: PROGRAM ORDER IS WHAT DECIDES WHETHER A JUMP IS "BACKWARD", and
#: only ONE backward JUMP per cell survives the build (INV-48 rule 2 --
#: ``build._apply_internal_feedback`` restores the HIGHEST-ADDRESS jump per cell
#: and silently loses any other). ``drn`` fires FIVE triggers at the rows, so
#: listed after them it would keep exactly one and drop four, in silence. Listed
#: before them all five are ordinary forward handoffs the resolver sizes itself.
#: See :meth:`_drn`.
RING = (("seq", "wb", "wbk", "drn") + STATE_LINE + QR_CELLS
        + ("relay", "relay2"))

#: The REORDER band: each adder's four words held as a PAIR of depth-2 stages,
#: ``bufA_k`` then ``bufB_k``, and released stage by stage along one eastward
#: conveyor into the egress. This is what turns the drain's lap-major emission
#: into RFC 8439 §2.3.2 order -- see :meth:`_buf` and INV-55.
#:
#: The RELEASE order, which is also the west-to-east order the cells sit in
#: along the buffer row: ``bufB0 bufA0 bufB1 bufA1 ...``. A pair's B stage holds
#: the OLDER two words (it is the far end of the FIFO), so it releases first.
BUFFER_CHAIN = tuple(c for k in range(4) for c in (f"bufB{k}", f"bufA{k}"))

#: PROGRAM order for the same eight cells — deliberately NOT the chain order.
#:
#: Program order decides which internal edges count as BACKWARD, and the build
#: resolves a backward jump by rewriting the source cell's HIGHEST-ADDRESSED
#: jump (INV-53). Listing each pair's A stage first makes the spill
#: ``bufA_k -> bufB_k`` a FORWARD edge, which frees the A stage from having to
#: end on that jump — worth exactly the one instruction that brings it inside
#: its word budget. The single backward edge left is the chain's
#: ``bufB_k -> bufA_k`` baton, and it lands on the B stage, whose ``nxt`` IS its
#: highest jump and which has nine spare words either way.
#:
#: The COORDINATES are unaffected: ``default_layout`` reindexes ``_geometry``
#: by this order, so the two dicts still pair by position (INV-33).
BUFFER_PROGRAM_ORDER = tuple(c for k in range(4)
                             for c in (f"bufA{k}", f"bufB{k}"))

#: Cells that are NOT on the datapath cycle: the four finish adders, the eight
#: reorder buffers, the egress, and the one-word pass-throughs that PAVE shared
#: walks (a gap inside the block's own footprint is a dead end for a
#: block-internal WRITE -- INV-51).
INTERIOR_CELLS = (("out", "add_pad", "ctl_pad", "ra_pad",
                   "bpad0", "bpad1", "spad0", "spad1", "spad2")
                  + tuple(f"add{k}" for k in range(4))
                  + BUFFER_PROGRAM_ORDER)


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

    Datapath — 51 cells in a 10x7 fold
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
    * ``seq`` counts the 80 laps and the half boundaries, and issues the
      TWENTIETH realignment itself before arming the drain; ``wbk``'s ``bnd``
      entry issues the boundary spin schedule as unrolled literal ``JUMP``s.
    * ``drn`` sequences the four drain laps, spinning every row between them.
    * ``add0..add3`` finish: each drains one row and adds that row's four
      addends, held in a rotating register that steps in lockstep so the
      add-back tap is **also** always slot 0.
    * ``bufA0..bufA3`` / ``bufB0..bufB3`` are the REORDER BAND: each adder's
      four words held as a 4-deep FIFO built from two depth-2 stages, released
      stage by stage along one eastward conveyor. This is what turns the drain's
      lap-major emission into RFC 8439 §2.3.2 order (INV-55).
    * ``bpad0``/``bpad1`` and ``ctl_pad``/``ra_pad`` are one-word pass-throughs
      that PAVE shared walks — a gap inside the block's own footprint is a dead
      end for an internal ``WRITE`` (INV-51). ``add_pad`` paves the same column
      and additionally relays the drain baton from ``tap3`` to ``drn``.

    The initial state is a **build-time constant** — the four RFC constants are
    fixed and key/nonce/counter are block parameters — so the add-back needs no
    shadow copy of the state, and each row BOOTS holding its four words of it.

    Status
    ======

    **DONE. All sixteen RFC 8439 §2.3.2 state words, bit-exact AND IN §2.3.2
    ORDER, on the real placed + routed + built chip** — the on-chip gate in
    ``verification/tests/test_chacha20_fixed_tap_ring.py`` asserts every word
    and every schedule count, and is mutation-proven to fail (INV-4).

    What is proven ON THE CHIP, from the execution trace and the port words:

    * the ring runs the **whole of RFC 8439's schedule**: exactly 80
      quarter-round invocations through all sixteen stages, **20** half-boundary
      realignments, and 40 realignment spins of each of rows 1/2/3;
    * the finish arms all four taps, the drain runs its four laps, each adder
      fires exactly four times, **every one of the eight buffer stages stores
      four times and releases once** (the INV-56 store-count signature, clean),
      and the egress bursts eight stage payloads = 32 words;
    * ``stop_reason == "QueueEmpty"`` with ``completed`` — no deadlock —
      and the 32 words parse to the sixteen §2.3.2 state words IN ORDER.

    The REORDER BAND — how the transpose is fixed, and what it cost
    ---------------------------------------------------------------

    The drain is lap-major by construction: one lap empties one SLOT of every
    row, so output position ``4L + k`` naturally carries ``state[4k + L]`` —
    the 4x4 transpose of §2.3.2. The row side has NO freedom (both facts are
    gated with INV-4 mutants): the boot-time load map is FORCED by the
    quarter-round schedule, and all ``4^4 x 4^4 x 4!`` drain-side knob
    combinations were searched — none gives §2.3.2 order. The fix is at the
    COLLECTOR (INV-55): output group ``k`` is exactly ``add_k``'s four words
    in lap order, so each adder's words are held in a 4-deep FIFO built from
    two depth-2 stages (``bufA_k`` -> ``bufB_k``) and released stage by stage
    along one eastward conveyor into the egress. FIFO order IS lap order, so
    the reorder needs no schedule at all — only the stages' order along the
    conveyor (``bufB0 bufA0 bufB1 bufA1 …``, which is §2.3.2 order).

    THE TWO-WAVE DEADLOCKS, and the shape of the fix (INV-56)
    ---------------------------------------------------------

    Pass 7's band deadlocked: the store wave (each A stage spilling WEST into
    its B stage) and the release wave (words riding EAST to the egress) shared
    the single-file buffer row, overlapped on the fourth drain lap, and two
    abutting cells each held the word the other had to accept
    (``stop_reason == "Deadlock"``, bufB3's store count 3 against everyone
    else's 4). The final fold applies BOTH of INV-56's fix shapes, each
    measured necessary:

    * **SPACE** — every spill leaves the buffer row: ``bufA_k`` (k<3) flips
      SOUTH and its words ride a dedicated corridor — ``spad_k`` (a
      west-resting relay pad below it), then its own adder's northward column
      — into ``bufB_k`` at hop 3. Pair 3's spill is DELIVERED into the
      egress's ``sp`` relay, which re-emits it one hop west into ``bufB3``.
      Row 0 carries eastward traffic only.
    * **TIME** — space alone still deadlocked at the east corner: with the
      lap-close fired by ``tap3`` in parallel with the store chain, ``drn``'s
      fourth entry released ~83 ns behind a store wave still in flight, and
      the four cells ``add3 -> bufB3 -> bufA3 -> out -> add3`` wedged in a
      circular wait. The lap-close baton now leaves from ``bufA3`` — the END
      of each lap's store wave, right after its spill hand-off — so the
      release is causally later than the last spill by construction.

    THE EAST CORNER — why the egress sits ON the port cell
    ------------------------------------------------------

    ``out`` occupies the chip's ``x16_out`` cell (9,0) itself, resting EAST
    (the edge face the port hardware sits on), with authored literal port
    hops (``RAW_OUTPUT_HOPS`` — see :meth:`_out` for the two build passes
    that otherwise rewrite its instructions). Measured dead ends, in order:
    an egress at (9,1) resting north is a head-on resting pair with the cell
    above it; ALL FOUR resting faces at (9,1) deadlock; and bursting the port
    words through ANY other block cell wedges the moment that cell holds a
    word of its own, because a port word is not consumed independently of the
    cell it transits. ``bufA3`` therefore lives at (9,1) resting north — its
    stores arrive from an EAST-resting ``add3`` at hop 1, and its spill, its
    released words and its release trigger all ride the one northward resting
    face into the egress.

    The algebra is separately verified exact against RFC 8439 §2.3.2 and
    §2.4.2 (with eight INV-4 mutants) in the same test file.

    Interface:
        - Entry: ``seq``'s default entry; one trigger runs the whole block.
        - Output: 32 raw 16-bit words (16 state words, hi then lo).
    """

    CATEGORY = "fec"
    TAGS = ["chacha20", "crypto", "cipher", "rfc8439", "keystream",
            "block-function", "multi-word", "32-bit"]

    # 51 cells, a large fraction of a 120-cell array, so this is a CHIP_SCALE
    # block: the sole occupant of its die. The <=8-across convention exists only
    # to keep several blocks co-resident; a sole occupant has none to pass, and
    # a wider fold leaves whole free ROWS rather than fragmented perimeter (see
    # layout_rules.md §3 and INV-40 -- FFT64Block ships 9x12). The fold is the
    # full 10 wide and 7 tall and starts at array row 0; the chip's I/O corridor
    # taps `seq` and `out` from the WEST/EAST edges of the finish row. Measured:
    # origin y=0 routes both nets, y=1..4 all fail `in_blk` with "no free
    # corridor between the ports".
    # The class's one placement contract -- input and output reachable from the
    # chip's x16 ports -- is met by putting BOTH in the fold's top row, and it is
    # gated end to end on a real built chip, never by inspection.
    CHIP_SCALE = True
    CHIP_SCALE_ORIENTATIONS = ((),)

    #: The egress authors its own port WRITE/JUMP hops (it sits ON the output
    #: port cell and also carries the pair-3 spill relay, whose literal hops
    #: the build's exit-cell sink fixup would otherwise rewrite to the port
    #: hand-off -- measured: every WRITE/JUMP in `out` became `@1 dest 0`,
    #: which sent the relay's words into `bufB3`'s R0 with a dead entry).
    #: Same opt-out the panel-backed egress cells use.
    RAW_OUTPUT_HOPS = True

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
        # A GRC parameter arrives as a HEX STRING (the binding's dtype is
        # string — GRC has no bytes dtype), so coerce str -> bytes here rather
        # than requiring every caller to pre-parse. Length is still enforced
        # by initial_state below.
        self.key = (bytes.fromhex(key.strip()) if isinstance(key, str)
                    else bytes(key))
        self.nonce = (bytes.fromhex(nonce.strip()) if isinstance(nonce, str)
                      else bytes(nonce))
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
            entries=[EntryPoint("pub"), EntryPoint("spin"), EntryPoint("wb")],
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
    def _tap(last: bool, is_relay: bool = False) -> CellProgram:
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
        # At the END of the tap line a drain lap CLOSES. Each row holds four
        # 32-bit words and one lap emits only the head of each, so the drain
        # runs four times and something has to send it round again.
        #
        # The lap-close baton does NOT start here any more. It used to --
        # `tap3` flipped SOUTH and fired `nlap` down the idle quarter-round
        # chain -- but that put the baton in PARALLEL with the store chain
        # still running through `add3` and the pair-3 spill relay, and `drn`'s
        # fourth entry then released into a store wave still in flight: the
        # measured circular wait `add3 -> bufB3 -> bufA3 -> out -> add3`
        # (INV-56). The baton now leaves from `out.sp` -- the END of each
        # lap's store wave -- so the release is causally after the last spill
        # hand-off by construction. See :meth:`_out`.
        add_tail = qr_tail
        # The chain: arm the next tap. The last tap ends it.
        arm_tail = "" if last else "    {jump:narm}\n"
        # `tap0` ALSO carries the RELEASE trigger into the reorder band, and it
        # is the only cell that can.
        #
        # MEASURED: the state line is a uniform EAST conveyor -- every one of
        # its cells rests east, because `wb`, `wbk` and `drn` all need that one
        # walk -- so no word can climb out of the control corner through it.
        # The FOUR TAPS are the sole exception: each already owns a NORTH flip
        # for its adder, and the adders rest north too, so a tap's inward walk
        # passes THROUGH its adder and lands on the reorder row at hop 2. That
        # is exactly where the chain's head sits (`bufB_k` is directly above
        # `add_k`), which is why the reorder row is columned the way it is.
        #
        # It costs ZERO new data words: `f_in` (NORTH) fires it and `f_ring`
        # (EAST) restores, and both already exist for the drain path. `drn`
        # reaches `tap0` at hop 2 on its own resting face -- the same northward
        # walk its four drain spins ride -- so the whole trigger path from the
        # drain counter to the chain head adds not one face constant to the
        # block.
        rel_body = "" if not is_relay else (
            "    HALT\n"
            "rel:\n"
            "    MOVE [FACE], R{data:f_in}\n"
            "    {jump:rel}\n"
            "    MOVE [FACE], R{data:f_ring}\n")
        return CellProgram(
            inputs=[Port("h", register=1), Port("l", register=2)],
            outputs=([Port("q"), Port("ah"), Port("al")]
                     + ([] if last
                        else [Port("nq"), Port("narm")])
                     + ([Port("rel")] if is_relay else [])),
            entries=([EntryPoint("default"), EntryPoint("arm")]
                     + ([EntryPoint("rel")] if is_relay else [])),
            # The tap serves TWO directions: along the ring (its resting face)
            # to the collector and the next row, and INWARD to its adder. A cell
            # has exactly one outgoing walk, so the second direction costs an
            # in-program FACE flip -- 2 instructions and 1 data word per extra
            # direction (INV-48). The tap has the spare words for it; the row
            # cell did not, which is the other half of why the steering lives
            # here.
            #
            # `f_ring` DOUBLES AS THE CONSTANT 1: it is the EAST face code,
            # which is numerically 1, and the mode test only needs some value to
            # compare against. That is `wbk`'s own idiom (its `tog` toggles
            # against `f_in` rather than a dedicated constant), and it is what
            # pays for `tap0`'s release relay -- with a separate `one` the cell
            # is 25 instructions against a `base_addr` of 6 with its highest
            # pinned register AT 6, which is INV-33's silent overlap.
            data=[DataWord("f_ring", 1, address=3, is_face=True),    # EAST
                  DataWord("f_in", 3, address=4, is_face=True)],     # NORTH
            # Pinned just above the data words (INV-33: inputs < data < state).
            state=[StateVar("mode", register=5,
                            initial_value=0,
                            reset_per_batch=True, reset_value=0)],
            assembly_template="""\
default:
    CMP R{state:mode}, R{data:f_ring}
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
    MOVE R{state:mode}, R{data:f_ring}
""" + arm_tail + rel_body,
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
        # EVERY path restores the resting face before it ends. That is not
        # bookkeeping: `wb` sits directly on the walk from `seq` down to the
        # state line, so every `pub` trigger `seq` issues TRANSITS this cell and
        # rides its LIVE face (INV-48 root cause C). Leaving it north bounces
        # the next `pub` straight back into `seq`, which re-enters `step` and
        # ping-pongs. Measured twice now: once when the flip was first added,
        # and again in this pass when an experiment replaced this restore with a
        # `HALT` -- the ring ran 21 laps instead of 80 and emitted nothing, with
        # no error anywhere.
        #
        body += ("    MOVE [FACE], R{data:f_ctl}\n"
                 "    {jump:go}\n")
        # This cell does NOT carry the release trigger, though it is the
        # obvious candidate -- it is the control corner's one turn north.
        # MEASURED: with a second relay entry it assembles to 22 instructions
        # against a `base_addr` of 9, and its two face constants are pinned at
        # R8/R9 because the eight frame words fill R0..R7. That is INV-33's
        # silent overlap, and nothing can move: the frame width is the
        # quarter-round's, and a cell needs both faces. Every way of sharing the
        # flip or the restore between `default` and a `rel` was tried and each
        # either leaves the resting face wrong (the ping-pong bug below) or
        # fires the release on every lap. The trigger goes `row0` -> `wbk`
        # instead; see :meth:`_row` and :meth:`_wbk`.
        body += ("    MOVE [FACE], R{data:f_line}\n")
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
        #
        # **`{jump:step}` MUST BE THIS CELL'S HIGHEST-ADDRESSED JUMP.** It is the
        # cell's one BACKWARD internal jump (`seq` precedes `wbk` in program
        # order), and `build._apply_internal_feedback` restores a backward jump
        # by rewriting the HIGHEST-ADDRESS JUMP instruction of the source cell,
        # whichever one that is (INV-48 rule 2). So the `default` entry is
        # emitted LAST, after `bnd`, purely so that `{jump:step}` outranks
        # `{jump:back}`.
        #
        # With `bnd` last -- the obvious order -- the build silently rewrote
        # `{jump:back}` to `seq.step`, and the ONLY reason the block still ran
        # was that `seq.step` and `row0.pub` happened to resolve to the SAME
        # numeric address (15), so the corrupted jump landed on the right entry
        # by pure coincidence. Shortening `seq` by three words moved `seq.step`
        # to 18, decoupled the two, and the realignment's hand-back went to the
        # lap counter instead of to `row0.pub`: 80 laps still ran, `wbk.bnd`
        # still fired 20 times, and every drained word was wrong.
        entry_default = ""
        for k in range(4):
            entry_default += f"    {{jump:k{k}}}\n"
        # No trailing `HALT`: `default` is emitted LAST, so its final restore is
        # the program's final instruction and there is nothing to fall through
        # into. That freed word is what pays for the release relay below.
        entry_default += ("    MOVE [FACE], R{data:f_in}\n"
                          "    {jump:step}\n"
                          "    MOVE [FACE], R{data:f_ring}\n")
        body = ""
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
        # Both are just "spin row1 p times, row2 twice, row3 q times" with
        # ``(p, q) = (1, 3)`` on one boundary and ``(3, 1)`` on the next, so the
        # COMMON part -- row1 once, row2 twice, row3 once -- is issued
        # UNCONDITIONALLY and only the two remaining spins of whichever row
        # differs are branched over. Six jumps and one branch instead of twelve
        # jumps and two schedules; rotations of DIFFERENT rows commute, so
        # hoisting cannot reorder anything observable.
        #
        # ONE `back` jump serves both halves: each arm FALLS THROUGH to a shared
        # tail rather than ending in its own remote JUMP (INV-43 rule 2 -- a
        # remote JUMP does not stop local execution, so a second copy would need
        # its own HALT and cost two more words).
        #
        # `tog` toggles against `f_in` (numerically 2) rather than a dedicated
        # `1` constant. A two-state toggle only needs SOME single bit to flip and
        # a `BR.Z` to test it, so ANY non-zero constant serves. The same word
        # doubles as the `CMP`'s operand, which sets Z unconditionally (`MOVE`
        # does not touch the flags, so the compare must be explicit).
        common = ("    {jump:c1}\n"
                  "    {jump:c2_0}\n"
                  "    {jump:c2_1}\n"
                  "    {jump:c3}\n")
        body += ("bnd:\n"
                 + "    XOR R{state:tog}, R{data:f_in}\n"
                   "    MOVE R{state:tog}, R0\n"
                 + common
                 + "    BR.Z second\n"
                   "    {jump:a3_0}\n"
                   "    {jump:a3_1}\n"
                   "    BR.NZ back\n"
                   "second:\n"
                   "    {jump:b1_0}\n"
                   "    {jump:b1_1}\n"
                   "back:\n"
                   "    {jump:back}\n"
                   "    HALT\n")
        # `rel` -- the RELEASE relay, and the reason this cell gained a third
        # face. It is the ONLY cell that both reaches `bufB0` (north, hop 1 --
        # the head of the reorder chain) and is reachable from the control
        # corner where `drn` closes the drain. `row0`, directly below, is what
        # carries the trigger up to it.
        #
        # It fits because `default`'s trailing `HALT` was dropped -- `default`
        # is the last entry, so its final restore is the program's last
        # instruction and there is nothing to fall through into. That freed word
        # plus the four already spare pay for a NORTH constant and a flip pair.
        # `default` before `rel` -- see the note above `entry_default`: its
        # `{jump:step}` has to be the highest-addressed JUMP in the cell or the
        # build's backward-jump restore rewrites `{jump:back}` instead.
        body += "default:\n" + entry_default
        # `drn` -- the DRAIN rotate. The finish emits one 32-bit word per row per
        # lap and each row holds four, so between drain laps every row must
        # advance by ONE plain rotate. That is `row.spin`, a different entry from
        # the compute laps' `row.wb`: in drain there is no returned word and
        # `nh`/`nl` still hold the last compute lap's, so `wb` would install
        # garbage. Same cell, same southward walk, same hops 1/3/5/7 -- only the
        # target entry differs. It then restarts the lap at `row0.pub`, hop 1 on
        # that same walk. The words for it come from the `bnd` hoist above.
        outs = ([Port(f"k{k}") for k in range(4)] + [Port("step")]
                # the hoisted common spins: row1 once, row2 twice, row3 once
                + [Port("c1"), Port("c2_0"), Port("c2_1"), Port("c3")]
                # ...then the two that differ between the halves
                + [Port("a3_0"), Port("a3_1"),
                   Port("b1_0"), Port("b1_1")]
                + [Port("back")])
        return CellProgram(
            inputs=[Port("go", register=1)],
            outputs=outs,
            entries=[EntryPoint("default"), EntryPoint("bnd")],
            data=[DataWord("f_ring", 0, address=2, is_face=True),   # SOUTH
                  DataWord("f_in", 2, address=3, is_face=True)],    # WEST
            state=[StateVar("tog", register=4, initial_value=0,
                            reset_per_batch=True, reset_value=0)],
            assembly_template=body,
        )

    @staticmethod
    def _drn() -> CellProgram:
        """The DRAIN sequencer — run the finish four times, one slot per lap.

        Each row holds four 32-bit words and one drain lap publishes only the
        head of each, so the finish must run four laps with a plain ``row.spin``
        between them. Nothing on the original fold could do it: ``wbk``, the one
        cell whose walk reached all four rows, had ZERO spare words, and so did
        ``seq``, the only cell that could reach ``wbk``.

        The answer is not to hunt for those words but to MOVE THE JOB ONTO CELLS
        OF ITS OWN (INV-46, "prefer more cells doing less" -- the move that
        landed ``LZ4DecoderBlock``). The array has 80 free cells and this block
        is already ``CHIP_SCALE``, so a cell is the cheap resource and a word is
        not.

        Placed at ``(1, 2)`` resting NORTH, its walk enters the state line at
        ``row0`` and runs east along it, hitting the four rows at hops 1, 3, 5,
        7 -- the same clean pattern ``wbk`` has, and ``row0.pub`` is that same
        hop 1, so every one of its five jumps rides the RESTING face and none
        needs a flip (nor, therefore, a restore -- INV-52). Measured by
        exhaustive search over every free slot x every face: NINETEEN slots reach
        all four rows in order, so the earlier claim that ``(1, 0)`` was the only
        one was derived, not measured, and was wrong. ``(1, 2)`` is the one that
        is ALSO reachable from a cell with words to spare.

        The lap is closed by ``bufA3`` -- the END of each lap's store wave
        (INV-56's TIME half) -- whose southward flip crosses the idle
        quarter-round serpentine and climbs the control column to ``add_pad``
        at hop 21; an OCCUPIED cell is transparent to a hop-counted word, so
        those transits cost nothing. ``add_pad`` is a paving cell with spare
        words, so it takes the relay for free and hands the baton one hop
        east to here.

        ``lap`` counts DOWN from four: three times it spins every row and
        re-publishes; the fourth time it falls out and the block is done.
        Without the counter the drain recirculates forever.

        **The emission is lap-major HERE, and that is fine.** One lap of this
        sequencer drains one SLOT of every row, so the adders receive words in
        *lap-major* order — the 4x4 transpose of §2.3.2. That is structural:
        every knob this sequencer owns was searched exhaustively (all
        ``4^4 x 4^4 x 4!`` combinations of pre-drain rotation, inter-lap spin
        count and row publish order) and none gives §2.3.2 order, because one
        drain lap visits each row exactly once, making the row index the
        fast-varying half of the output position. The reorder therefore lives
        at the COLLECTOR — the per-adder buffer pairs of the reorder band —
        and both facts are gated with INV-4 mutants (INV-55).

        **On the fourth lap this cell fires the RELEASE** (``done`` is
        ``{jump:rel}``): by then every buffer pair is full, and — the INV-56
        TIME half — the lap-close baton that triggered this fourth entry was
        itself fired from the END of the store wave (``bufA3``, after its
        spill hand-off), so the release is causally after every store.
        """
        return CellProgram(
            inputs=[Port("go", register=1)],
            outputs=[Port("s0"), Port("s1"), Port("s2"), Port("s3"),
                     Port("pub"), Port("rel")],
            entries=[EntryPoint("default")],
            data=[DataWord("one", 1, address=2)],
            state=[StateVar("lap", register=3, initial_value=4,
                            reset_per_batch=True, reset_value=4)],
            # `done` is no longer a bare HALT: the fourth lap is when every
            # buffer pair is full, so it is exactly the moment to start the
            # release. `row0` is hop 1 on this cell's own RESTING face -- the
            # same walk its four drain spins ride -- so the hand-off needs no
            # face constant and no flip at all.
            assembly_template="""\
default:
    SUB R{state:lap}, R{data:one}
    MOVE R{state:lap}, R0
    BR.Z done
    {jump:s0}
    {jump:s1}
    {jump:s2}
    {jump:s3}
    {jump:pub}
    HALT
done:
    {jump:rel}
""",
        )

    @staticmethod
    def _drn_relay() -> CellProgram:
        """``add_pad``, promoted from bare paving to the drain baton's relay.

        It already had to exist -- a gap inside the block's own footprint is a
        dead end for a block-internal WRITE, so the control column is paved --
        and as a pad it carried no declared edge at all and 26 spare words. It
        sits at ``(0, 2)``, one hop WEST of ``drn``, and is the landing point of
        ``bufA3``'s southward baton walk (hop 21), so it is exactly the cell
        that joins the end of each drain lap's store wave to the drain
        controller (INV-56's TIME half).

        Taking the baton costs it ONE face flip: its RESTING face is NORTH,
        which is what paves the control column for every word that merely
        transits it, and the hand-off to ``drn`` goes EAST. The flip is UNDONE
        before the entry ends -- the face register is CELL state that persists
        across entries and steers transiting words too, so a path that leaves it
        pointing east would deflect the control column's traffic (INV-52).
        """
        return CellProgram(
            inputs=[Port("v", register=1)],
            outputs=[Port("o"), Port("go")],
            entries=[EntryPoint("default"), EntryPoint("baton")],
            data=[DataWord("f_col", 3, address=2, is_face=True),   # NORTH
                  DataWord("f_drn", 1, address=3, is_face=True)],  # EAST
            state=[],
            assembly_template="""\
default:
    MOVE R0, R{in:v}
    {write:o}
    {jump:o}
    HALT
baton:
    MOVE [FACE], R{data:f_drn}
    {jump:go}
    MOVE [FACE], R{data:f_col}
""",
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
    def _out(b3_entry: int, b3_vh: int, b3_vl: int) -> CellProgram:
        """The block's egress, ON the chip's output-port cell (9, 0).

        **Why on the port cell.** An egress one cell away sends every port
        word THROUGH another block cell, and a port word is NOT consumed
        independently of that cell's queues: measured, `out` at (9,1) bursting
        north held its port word at `bufA3`'s cell while `bufA3` held a
        release word toward `out` -- INV-56 rule 3's two-cell circular wait,
        live, zero words on the wire. From the port cell the write leaves on
        the chip edge and touches nothing. This is the shape the 41-cell fold
        proved for all 32 words. A single-waypoint egress route also never
        re-faces this cell, so the authored NORTH resting face survives the
        build (off-array: no head-on pair is possible).

        **`sp` -- the pair-3 spill relay (INV-56 fix shape (b)).** `bufA3`
        sits directly below and cannot spill west along the buffer row (the
        two-wave collision) nor south (no free cell); it DELIVERS its spill
        here, and `sp` re-emits the pair one hop WEST into `bufB3`, then
        restores. The relay's WRITE/JUMP are AUTHORED literal `@1` hops (the
        LZ4 egress idiom), not declared edges, for a measured reason: as a
        declared edge `out -> bufB3` is BACKWARD in program order, and a
        backward jump must be its cell's highest-addressed JUMP (INV-53) --
        but the build's port patch ALSO rewrites the highest WRITE/JUMP to
        the port hand-off (`_patch_last_write_handoff`), so one cell cannot
        carry both a declared backward jump and the port pair. Authored
        literals sidestep both passes; `b3_entry`/`b3_vh`/`b3_vl` are
        resolved from `bufB3`'s own program at build time, never hand-typed.

        **`sp` FIRST, `default` LAST.** The port patch takes the
        highest-addressed WRITE and JUMP; with `sp` last it landed on the
        relay instead -- measured as `bufB3` storing ZERO while `sp` ran four
        times (its second WRITE got the port tag as dest, its JUMP entry 0).

        The ``WRITE``+``JUMP`` pair of ``default`` IS the external port
        handshake. It must NOT also be declared an internal jump: a
        ``__terminate__`` edge on an external output port marks it
        internally-consumed and the portmap then DELETES the block's only
        output port.
        """
        return CellProgram(
            # FOUR input registers take a whole buffer stage's payload -- two
            # 32-bit words -- as ONE trigger (a WRITE's target register is an
            # instruction field, so a stage fills all four and jumps once);
            # `default` bursts them onto the port in order. Eight stages x
            # four words == the 32 output words. `sph`/`spl` take `bufA3`'s
            # spill pair for the `sp` relay.
            inputs=[Port("v0h", register=1), Port("v0l", register=2),
                    Port("v1h", register=3), Port("v1l", register=4),
                    Port("sph", register=5), Port("spl", register=6)],
            outputs=[Port("out")],
            entries=[EntryPoint("sp"), EntryPoint("default")],
            data=[DataWord("f_w", 2, address=7, is_face=True),   # WEST
                  DataWord("f_e", 1, address=8, is_face=True)],  # EAST
            state=[],
            # The cell RESTS EAST -- the chip-edge face the port hardware sits
            # on -- so the port bursts need no flip at all; `sp` flips WEST for
            # the relay and restores. The port protocol from the port cell is
            # `WRITE @1, 0` / `JUMP @1, 0` (hop 1 off the east edge, dest = the
            # net's out-tag 0, entry 0), read off what the build's own patch
            # resolved before RAW_OUTPUT_HOPS opted this cell out of it.
            assembly_template=f"""\
sp:
    MOVE [FACE], R{{data:f_w}}
    MOVE R0, R{{in:sph}}
    WRITE @1, {b3_vh}
    MOVE R0, R{{in:spl}}
    WRITE @1, {b3_vl}
    JUMP @1, {b3_entry}
    MOVE [FACE], R{{data:f_e}}
    HALT
default:
    MOVE R0, R{{in:v0h}}
    WRITE @1, 0
    JUMP @1, 0
    MOVE R0, R{{in:v0l}}
    WRITE @1, 0
    JUMP @1, 0
    MOVE R0, R{{in:v1h}}
    WRITE @1, 0
    JUMP @1, 0
    MOVE R0, R{{in:v1l}}
    WRITE @1, 0
    JUMP @1, 0
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

        **There are TWENTY realignments, not nineteen.** Each diagonal half is
        BRACKETED — ``k`` spins of row ``k`` before it and ``4 - k`` after — so
        ten double rounds need ten opening and ten closing brackets. Nineteen
        of those fall between laps and were always issued; the twentieth is the
        CLOSING bracket of the last diagonal half, and it has no lap after it to
        hang off. So ``finish`` issues it explicitly, and only then is the state
        aligned for the drain.

        Leaving it out is the subtlest bug this block had, and it survived a
        whole pass because it hides behind row 0. Row 0's bracket is ``0`` spins
        either way, so row 0 was always aligned and its head -- the RFC's first
        output word -- came out BIT-EXACT while rows 1, 2 and 3 were left
        rotated by ``4 - k`` too little and drained slot ``k`` instead of slot 0.
        Measured on chip as spin counts of 37/38/39 where the schedule requires
        40/40/40, i.e. exactly ``10a + 9b`` against ``10a + 10b``. A gate that
        checked only word 0 passed it.

        The closing bracket costs NO new schedule logic: ``wbk``'s ``bnd``
        alternates halves on a toggle, and on the twentieth entry that toggle is
        even, so ``bnd`` already issues the ``4 - k`` side. ``finish`` simply
        fires it once more, and ``bnd``'s existing hand-back to ``row0.pub``
        starts the first drain lap -- the taps having just been armed.
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
                  DataWord("f_line", 0, address=5, is_face=True),   # SOUTH
                  DataWord("f_ctl", 1, address=6, is_face=True),    # EAST
                  ],
            state=[StateVar("half", register=7, initial_value=4,
                            reset_per_batch=True, reset_value=4),
                   StateVar("laps", register=8, initial_value=LAPS_INIT,
                            reset_per_batch=True, reset_value=LAPS_INIT)],
            # `default` FALLS THROUGH into `more`. Both start a compute lap with
            # the identical three-instruction publish tail (flip south, jump,
            # restore east), and a fall-through costs nothing where a second copy
            # costs three words. That is what pays for `finish`'s closing
            # bracket. `default` only has to set `laps` first -- `half` already
            # resets to four per batch, so the explicit `MOVE half, four` there
            # was redundant.
            #
            # NOTE the entry-address hazard this deliberately steers around
            # (INV-6/11): entry addresses are `31 - instruction_count` plus the
            # label offset, so ANY change to this cell's length moves `step`, and
            # `wbk.back` resolves against `row0.pub` at the same numeric address.
            # Both entries are re-checked by
            # `test_entry_addresses_stay_distinct_where_edges_resolve`.
            assembly_template="""\
default:
    MOVE R{state:laps}, R{data:eighty}
more:
    MOVE [FACE], R{data:f_line}
    {jump:pub}
    MOVE [FACE], R{data:f_ctl}
    HALT
step:
    SUB R{state:laps}, R{data:f_ctl}
    MOVE R{state:laps}, R0
    BR.Z finish
    SUB R{state:half}, R{data:f_ctl}
    MOVE R{state:half}, R0
    BR.NZ more
    MOVE R{state:half}, R{data:four}
    {jump:bnd}
    HALT
finish:
    MOVE [FACE], R{data:f_line}
    {jump:fin}
    MOVE [FACE], R{data:f_ctl}
    {jump:bnd}
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

        **The result goes NORTH into this row's reorder buffer, not east to the
        egress.** The adder emits the four words of output group ``k`` in lap
        order, which is exactly the order group ``k`` must LEAVE in — but the
        four groups interleave, so they are held in ``bufA_k``/``bufB_k`` and
        released group by group (INV-55). The adder rests NORTH and ``bufA_k``
        sits directly above it, so the write rides the resting face and the cell
        needs no face constant at all — one fewer than the eastward form.

        **The ``oh``/``ol`` SPLIT saves an instruction.** A ``WRITE``'s target
        register is an instruction field, so hi and lo go to two DIFFERENT
        registers of the same buffer cell and ONE trailing ``JUMP`` triggers it,
        replacing the old write/jump/write/jump pair. That is what buys back the
        word this cell needed; it was measured at 1 spare before and is 2 after.
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
            outputs=[Port("oh"), Port("ol")],
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
    {write:oh}
    MOVE R0, R{in:vl}
    {write:ol}
    {jump:ol}
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

    @staticmethod
    def _buf(last: bool, west: bool, b3: bool = False) -> CellProgram:
        """One DEPTH-2 stage of a reorder buffer — the fix for the 4x4 transpose.

        **What it is for.** The drain emits lap-major: at output position
        ``4L + k`` it produces ``state[4k + L]``. Read the other way round, the
        word wanted at position ``4k + L`` is the one ``add_k`` produces on drain
        lap ``L``, so **output group ``k`` is exactly ``add_k``'s four words in
        lap order** and the whole transpose is "hold each adder's four words,
        release adder by adder" (INV-55 rule 1).

        **Why a PAIR of depth-2 stages and not one depth-4 cell.** A depth-4
        buffer needs ten live registers (eight for four 32-bit words plus the
        arriving pair), an 8-instruction shift, and a release that re-enters the
        cell three times behind a counter. Measured on the previous fold: 20
        instructions against a ``base_addr`` of 11 with 13 live words — an
        INV-33 overlap of exactly three, and the counter's ``SUB``/``MOVE``/
        ``BR`` triple was the whole shortfall. Halving the depth halves the
        state (four registers, not eight) AND removes the counter outright,
        because a depth-2 stage can release BOTH its words from one
        straight-line entry. Measured here: 20 instructions, ``base_addr`` 11,
        highest live register 6 — **five words spare**. That is INV-46's "prefer
        more cells doing less" paying for itself twice over.

        **The two entries.**

        * ``default`` (the STORE, one per drain lap) — spill this stage's oldest
          word onward, then shift and take the arriving pair. Running the four
          drain laps therefore leaves ``bufB_k`` holding ``state[4k+0]`` and
          ``state[4k+1]`` and ``bufA_k`` holding ``state[4k+2]`` and
          ``state[4k+3]``: the pair is a 4-deep FIFO, and FIFO order IS lap
          order.
        * ``rel`` (the RELEASE, once) — emit slot 0 then slot 1 to the egress and
          hand the baton to the next stage in the chain. No counter, no
          re-entry, no face flip: the buffer row is one eastward conveyor, so
          both the data words and the baton ride the cell's RESTING face.

        Because the B stage holds the older half, the chain releases
        ``bufB0 bufA0 bufB1 bufA1 ...`` — which is precisely §2.3.2 order.

        **The multi-register WRITE is what makes both entries fit.** A
        ``WRITE``'s target register is an instruction field, so several words
        can be written into several registers of the SAME downstream cell and
        triggered ONCE. Every edge here uses it: the adder fills ``bufA_k``'s
        ``vh``/``vl`` with one trigger, the store fills the next stage's the
        same way, and the release fills all FOUR of the egress's registers and
        triggers once for the whole stage. Each trigger avoided is an
        instruction saved in the cell that issues it, and this cell needed
        exactly one of them: at a trigger per 32-bit word it is 23 instructions
        against a ``base_addr`` of 8 with eight live registers, which is INV-33's
        silent overlap; at a trigger per stage it is 22 against 9.

        The registers to pay with come from the egress, which had 27 words spare
        and one instruction of work — the INV-46 trade, made in the small.
        """
        # `last` is `bufA3`, the end of the release chain: it has no successor
        # to hand the baton to, so its `rel` entry simply ends. Giving it a
        # `nxt` port would leave a jump nothing targets (INV-39: a dispatch
        # entry no jump targets is dead code, and the converse -- a jump with no
        # declared target -- is resolved by the build's fallback to whatever it
        # happens to yield).
        # `bufB3`'s baton to `bufA3` cannot ride the row (east of it is the
        # egress on the port cell): it flips SOUTH and its `nxt` reaches
        # `bufA3` at hop 2 through the east-resting `add3`, then restores.
        # `nxt` stays this cell's LAST jump: the edge is BACKWARD in program
        # order (`bufA3` precedes `bufB3`), so INV-53 requires it to be the
        # highest-addressed JUMP or the build's feedback pass rewrites the
        # release trigger instead.
        if last:
            rel_tail = ""
        elif b3:
            rel_tail = ("    MOVE [FACE], R{data:f_dn}\n"
                        "    {jump:nxt}\n"
                        "    MOVE [FACE], R{data:f_row}\n")
        else:
            rel_tail = "    {jump:nxt}\n"
        # The SPILL is the cell's one off-axis edge, and it does NOT travel the
        # buffer row. Pass 8 measured why: the row is one eastward single-file
        # conveyor, and a westward spill on it runs OPPOSITE to the release
        # wave -- on the fourth drain lap the two waves overlapped and the chip
        # returned ``stop_reason == "Deadlock"`` (INV-56: `bufA3` held its spill
        # WEST at `bufB3` while `bufB3` held a release word EAST at `bufA3`).
        # So the spill was given its OWN corridor -- INV-56's fix shape (b),
        # separate the waves in SPACE: `bufA_k` flips SOUTH and writes at hop 3,
        # transiting the west-resting relay pad below it (`spad_k`) and its own
        # adder (which rests north), and landing in `bufB_k` from the SOUTH.
        # Row 0 then carries eastward traffic ONLY, and the two-wave collision
        # cannot form on it. A B stage spills to nothing (it is the far end of
        # the FIFO), so it needs no constant at all.
        #
        # A B stage does not even WRITE on the store path: it has no next stage,
        # so the two `MOVE`+`WRITE` pairs and the trigger are simply omitted and
        # the shift is all that remains. That is three instructions cheaper and,
        # more to the point, it is what guarantees the FIFO's two boot zeros can
        # never be pushed onto the output port -- an omitted WRITE cannot be
        # mis-triggered, whereas an untriggered one relies on the build never
        # inventing a jump for it (INV-39's converse).
        # The spill MUST come before the shift. Moving it after (so the restore
        # could be the program's last instruction, saving a word) was tried and
        # MEASURED WRONG: shifting first means the word spilled is the one that
        # just arrived rather than the oldest, and the pair then emits
        # `1,2,2,3,5,6,6,7,...` instead of `0..15`. The FIFO's order is the
        # whole mechanism here, so the word has to be found elsewhere.
        if west:
            spill = ""
        elif last:
            # `bufA3` rests NORTH and BOTH its spill and its release ride that
            # one face at hop 1: the spill is DELIVERED into the egress's `sp`
            # relay (out at (9,0) sits directly above), which re-emits it one
            # hop west into `bufB3`. No flip for any data edge.
            spill = ("    MOVE R0, R{state:s0h}\n"
                     "    {write:sh}\n"
                     "    MOVE R0, R{state:s0l}\n"
                     "    {write:sl}\n"
                     "    {jump:sl}\n")
        else:
            spill = ("    MOVE [FACE], R{data:f_spill}\n"
                     "    MOVE R0, R{state:s0h}\n"
                     "    {write:sh}\n"
                     "    MOVE R0, R{state:s0l}\n"
                     "    {write:sl}\n"
                     "    {jump:sl}\n"
                     "    MOVE [FACE], R{data:f_row}\n")
        # `bufA3` closes each drain lap (INV-56 fix shape (a)): AFTER its
        # spill hand-off it flips SOUTH and fires the lap baton down the idle
        # quarter-round serpentine to `add_pad` (hop 21), which relays it to
        # `drn`. Sourcing the baton here -- the END of the store wave -- is
        # what makes the release causally later than every spill: `drn`'s
        # fourth entry (the one that releases) cannot run until the last
        # spill has left this cell. The old source, `tap3`, fired in PARALLEL
        # with the store chain and lost the race by construction (~83 ns,
        # INV-56).
        lap_tail = ("    MOVE [FACE], R{data:f_lap}\n"
                    "    {jump:lap}\n"
                    "    MOVE [FACE], R{data:f_rest}\n") if last else ""
        return CellProgram(
            inputs=[Port("vh", register=1), Port("vl", register=2)],
            outputs=([Port("o0h"), Port("o0l"), Port("o1h"), Port("o1l")]
                     + ([] if west else [Port("sh"), Port("sl")])
                     + ([Port("lap")] if last else [])
                     + ([] if last else [Port("nxt")])),
            entries=[EntryPoint("default"), EntryPoint("rel")],
            # `f_spill` is SOUTH -- the spill's own corridor, see above -- and
            # `f_row` restores the cell's OWN resting face, EAST, because the
            # buffer row carries every other stage's released words to the
            # egress and the FACE register steers TRANSITING words too
            # (INV-52). `bufA3` instead carries the lap baton's SOUTH flip and
            # its NORTH restore; `bufB3` carries the baton hand-off's SOUTH
            # flip (its `nxt` reaches `bufA3` at hop 2 through the adder) and
            # its EAST restore.
            data=([DataWord("f_lap", 0, address=7, is_face=True),   # SOUTH
                   DataWord("f_rest", 3, address=8, is_face=True)]  # NORTH
                  if last else
                  ([DataWord("f_dn", 0, address=7, is_face=True),   # SOUTH
                    DataWord("f_row", 1, address=8, is_face=True)]  # EAST
                   if b3 else
                   ([] if west else
                    [DataWord("f_spill", 0, address=7, is_face=True),  # SOUTH
                     DataWord("f_row", 1, address=8, is_face=True)]))),  # EAST
            # Four registers hold the two 32-bit words. They are PINNED (INV-33:
            # a cell with no data words has `max_data_address = -1`, so the auto
            # scan would start at R0 and land state on top of the inputs).
            #
            # They boot to ZERO and are `reset_per_batch`, so a second trigger
            # starts from a clean FIFO rather than releasing the previous
            # block's residue. Nothing reads a slot before four stores have
            # filled it, so the initial value is never observed -- but a
            # non-resetting buffer WOULD be observed on the second batch.
            state=[StateVar("s0h", register=3, initial_value=0,
                            reset_per_batch=True, reset_value=0),
                   StateVar("s0l", register=4, initial_value=0,
                            reset_per_batch=True, reset_value=0),
                   StateVar("s1h", register=5, initial_value=0,
                            reset_per_batch=True, reset_value=0),
                   StateVar("s1l", register=6, initial_value=0,
                            reset_per_batch=True, reset_value=0)],
            # `default` FIRST, `rel` LAST -- so `rel` needs no trailing `HALT`
            # (nothing follows it) while `default` gets one it would have needed
            # anyway. That is the single instruction that brings the A stage
            # inside its budget: 23 words against a `base_addr` of 8 with eight
            # live registers becomes 22 against 9.
            #
            # The order is only AVAILABLE because `BUFFER_PROGRAM_ORDER` lists
            # each A stage before its B stage, which makes the spill a FORWARD
            # edge. Were the spill backward, INV-53 would force `{jump:sl}` to
            # be this cell's highest-addressed jump -- i.e. force `rel` first --
            # and the build would otherwise rewrite the release chain's
            # `{jump:nxt}` into a store trigger, silently halving the output.
            assembly_template="""\
default:
""" + spill + """\
    MOVE R{state:s0h}, R{state:s1h}
    MOVE R{state:s0l}, R{state:s1l}
    MOVE R{state:s1h}, R{in:vh}
    MOVE R{state:s1l}, R{in:vl}
""" + lap_tail + """\
    HALT
rel:
    MOVE R0, R{state:s0h}
    {write:o0h}
    MOVE R0, R{state:s0l}
    {write:o0l}
    MOVE R0, R{state:s1h}
    {write:o1h}
    MOVE R0, R{state:s1l}
    {write:o1l}
    {jump:o1l}
""" + rel_tail,
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
                # `tap0` doubles as the release trigger's hop into the
                # reorder band -- see :meth:`_tap`.
                progs[cid] = self._tap(last=(cid == "tap3"),
                                       is_relay=(cid == "tap0"))
            elif cid == "wbk":
                progs[cid] = self._wbk()
            elif cid == "drn":
                progs[cid] = self._drn()
            elif cid == "seq":
                progs[cid] = self._seq()
            else:
                progs[cid] = qr[cid]
        for k in range(4):
            progs[f"add{k}"] = self._adder(k, last=(k == 3))
        # The reorder band, in PROGRAM order (A stage before its B stage) --
        # see `BUFFER_PROGRAM_ORDER` for why that differs from the release
        # order and what the difference buys.
        for cid in BUFFER_PROGRAM_ORDER:
            progs[cid] = self._buf(last=(cid == BUFFER_CHAIN[-1]),
                                   west=cid.startswith("bufB"),
                                   b3=(cid == "bufB3"))
        # `bpad0`/`bpad1` pave the west end of the reorder row. A gap inside the
        # block's own footprint is a dead end for a block-internal WRITE
        # (INV-51), and the row has to be continuous for the released words to
        # ride it -- even though nothing is emitted from these two columns.
        progs["bpad0"] = self._passthru()
        progs["bpad1"] = self._passthru()
        # `spad0..spad2` pave the SPILL corridor (INV-56 fix shape (b)): each
        # sits directly below its `bufA_k` and rests WEST, so the pair's spill
        # -- flipped SOUTH out of the A stage -- is forwarded west into the
        # adder's northward column and lands in `bufB_k` at hop 3, never
        # touching the buffer row's eastward conveyor. Pair 3 needs none: `out`
        # itself rests WEST below `bufA3` and serves the same turn.
        progs["spad0"] = self._passthru()
        progs["spad1"] = self._passthru()
        progs["spad2"] = self._passthru()
        # `add_pad` is still the control column's paving cell -- that is what its
        # `default` entry does -- but it ALSO carries the drain baton east to
        # `drn` on its `baton` entry. It had 26 spare words and no declared edge.
        progs["add_pad"] = self._drn_relay()
        progs["ctl_pad"] = self._passthru()
        progs["ra_pad"] = self._passthru()
        # The egress's spill relay authors LITERAL hops at `bufB3` (see
        # :meth:`_out`), so its dest registers and entry address are RESOLVED
        # from `bufB3`'s real program here -- never hand-typed numbers, which
        # entry addresses being params-dependent (INV-6/11) would rot.
        from ..resolver import CellProgramResolver
        b3p = progs["bufB3"]
        b3_ent = CellProgramResolver().compute_entry_addresses(b3p)["default"]
        b3_regs = {p.name: p.register for p in b3p.inputs}
        progs["out"] = self._out(b3_entry=int(b3_ent),
                                 b3_vh=int(b3_regs["vh"]),
                                 b3_vl=int(b3_regs["vl"]))

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
            # The finish result goes NORTH into this row's reorder buffer, hi
            # and lo into two registers of the SAME cell so one trigger serves
            # both (the `oh`/`ol` split -- a WRITE's target is an instruction
            # field, so this costs nothing and saves an instruction).
            conns.append((f"add{k}", "oh", f"bufA{k}", "vh"))
            conns.append((f"add{k}", "ol", f"bufA{k}", "vl"))
        # THE REORDER BAND. Each pair is a 4-deep FIFO built as two depth-2
        # stages: a store into `bufA_k` spills its oldest word into `bufB_k`,
        # so after the four drain laps `bufB_k` holds the older two words of
        # output group `k` and `bufA_k` the newer two.
        #
        # The spill does NOT travel the buffer row (INV-56: a westward spill on
        # the eastward release conveyor is the two-wave deadlock). It flips
        # SOUTH and rides its own corridor -- `spad_k` (resting west) into
        # `add_k`'s northward column -- landing in `bufB_k` from below. Pair 3
        # has no free cell below `bufA3`; the egress occupies that slot, so IT
        # relays the spill instead (`out.sp`, same corridor shape through
        # `add3`).
        for k in range(3):
            conns.append((f"bufA{k}", "sh", f"bufB{k}", "vh"))
            conns.append((f"bufA{k}", "sl", f"bufB{k}", "vl"))
        conns.append(("bufA3", "sh", "out", "sph"))
        conns.append(("bufA3", "sl", "out", "spl"))
        # `out -> bufB3` (the relay's second leg) is deliberately UNDECLARED:
        # its WRITE/JUMP are authored literal `@1` hops inside `out.sp` (see
        # :meth:`_out` for the measured INV-53/port-patch conflict a declared
        # backward edge creates).
        # A B stage is the FAR END of its FIFO and declares no spill ports at
        # all -- its `default` entry is the bare shift. That is what guarantees
        # the two boot zeros a 4-deep FIFO pushes through during the fill can
        # never reach the output: there is no WRITE to mis-trigger.
        # The RELEASE. Every stage writes its two words straight to the egress:
        # the buffer row is one eastward conveyor, so `out` sits at the end of
        # every stage's resting-face walk and no stage needs a face flip.
        for cid in BUFFER_CHAIN:
            conns.append((cid, "o0h", "out", "v0h"))
            conns.append((cid, "o0l", "out", "v0l"))
            conns.append((cid, "o1h", "out", "v1h"))
            conns.append((cid, "o1l", "out", "v1l"))
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
            # The finish result is STORED, not emitted: one trigger per lap into
            # this row's first buffer stage.
            jumps.append((f"add{k}", "ol", f"bufA{k}", "default"))
            # ...and the store spills the stage's oldest word into the second,
            # via the SOUTH corridor (pair 3 through the egress's `sp` relay).
            if k < 3:
                jumps.append((f"bufA{k}", "sl", f"bufB{k}", "default"))
        jumps.append(("bufA3", "sl", "out", "sp"))
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
        # The spins common to BOTH halves -- row1 once, row2 twice, row3 once --
        # are issued unconditionally; only the two that differ are branched over
        # (half A adds two more of row3, half B two more of row1).
        jumps.append(("wbk", "c1", "row1", "spin"))
        jumps.append(("wbk", "c2_0", "row2", "spin"))
        jumps.append(("wbk", "c2_1", "row2", "spin"))
        jumps.append(("wbk", "c3", "row3", "spin"))
        jumps.append(("wbk", "a3_0", "row3", "spin"))
        jumps.append(("wbk", "a3_1", "row3", "spin"))
        jumps.append(("wbk", "b1_0", "row1", "spin"))
        jumps.append(("wbk", "b1_1", "row1", "spin"))
        jumps.append(("wbk", "back", "row0", "pub"))
        # THE DRAIN LAP. The lap-close baton leaves from `out.sp` -- the END of
        # each lap's store wave (INV-56 fix shape (a); see :meth:`_out`) -- and
        # goes south down the idle quarter-round serpentine and up the control
        # column to `add_pad` at hop 21 (occupied cells are transparent to a
        # hop-counted word), which relays it one hop east to `drn`. `drn` spins
        # all four rows and re-publishes, four laps in all, so each row's four
        # 32-bit words all reach the adders.
        jumps.append(("bufA3", "lap", "add_pad", "baton"))
        jumps.append(("add_pad", "go", "drn", "default"))
        for k in range(4):
            jumps.append(("drn", f"s{k}", f"row{k}", "spin"))
        jumps.append(("drn", "pub", "row0", "pub"))
        # THE RELEASE. When the fourth drain lap closes, every buffer pair holds
        # its adder's four words in FIFO (== lap) order, and the reorder is just
        # "empty pair 0, then pair 1, ...". `drn` cannot reach the buffer row
        # itself -- measured over all four faces, its walks only ever enter the
        # state line -- so the trigger is relayed west to `wb`, north to `seq`,
        # and north again into the head of the chain. Each of those three hops
        # rides a walk the block already had (`wb` even reuses its existing
        # NORTH constant); only `drn` and `seq` gain a face.
        jumps.append(("drn", "rel", "tap0", "rel"))
        jumps.append(("tap0", "rel", "bufB0", "rel"))
        # ...and then the chain walks itself, stage by stage, eastward along the
        # buffer row. Every hand-off is hop 1 on the resting face.
        for src, dst in zip(BUFFER_CHAIN, BUFFER_CHAIN[1:]):
            jumps.append((src, "nxt", dst, "rel"))
        # Every released 32-bit value is ONE triggered delivery into the egress,
        # which bursts its two halves onto the port. Two per stage x 8 stages ==
        # the sixteen state words == the 32 output words.
        for cid in BUFFER_CHAIN:
            jumps.append((cid, "o1l", "out", "default"))
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
        # The drain lap's baton: `bufA3` turns SOUTH into the idle
        # quarter-round serpentine (hop 1 is the frame collector directly
        # below it), and `add_pad` turns EAST out of the control column.
        faces[("bufA3", "lap")] = "in0"
        faces[("add_pad", "go")] = "drn"
        # `bufB3` hands the release baton to `bufA3` through the east-resting
        # adder below it -- a SOUTH flip, hop 2.
        faces[("bufB3", "nxt")] = "add3"
        # THE RELEASE TRIGGER's two flipped hops into the reorder band. `drn`
        # fires it on its own RESTING face (`tap0` is hop 2 north, the same walk
        # its drain spins ride), then `tap0` turns INWARD -- past `add0` and on
        # to `bufA0` -- and `bufA0` turns WEST to the chain head. Both reuse a
        # face constant the cell already had.
        faces[("tap0", "rel")] = "add0"
        # Each A stage turns SOUTH to spill -- into the relay pad below it,
        # whose westward face carries the word into the adder's column and up
        # into the B stage (the spill's own corridor, INV-56 fix shape (b)).
        # `bufA3`'s spill rides its RESTING south face through `out`, so its
        # declaration names `out`; declared or not it resolves the same, but a
        # declared face is authoritative and survives a re-fold loudly.
        for k in range(3):
            faces[(f"bufA{k}", "sh")] = f"spad{k}"
            faces[(f"bufA{k}", "sl")] = f"spad{k}"
        faces[("bufA3", "sh")] = "out"
        faces[("bufA3", "sl")] = "out"
        # `bufA3` is the reorder row's EAST END: it rests SOUTH and drops its
        # released words onto the egress directly below, rather than along the
        # row like every other stage.
        for pt in ("o0h", "o0l", "o1h", "o1l"):
            faces[("bufA3", pt)] = "out"
        return faces

    def _geometry(self) -> Dict[Any, Tuple[int, int, str]]:
        """A 10x7 fold in five bands. Every edge is on a REAL forwarding walk.

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
           because ``wb`` (eight write-backs), ``wbk`` (four rotate triggers and
           the boundary spins) and ``drn`` (the drain's four spins and its
           re-publish) each have to reach several of them from ONE walk. This is
           the ``LMSEqualizerBlock`` broadcast idiom: consecutive targets along a
           single walk.
        2. **The REORDER band must be gap-free, its cell ORDER is the EMISSION
           order, and it carries EASTWARD traffic ONLY (INV-56).** The buffer
           stages and ``out`` share one eastward walk, and an unoccupied
           column is a DEAD END for a block-internal WRITE — hence the paving
           pads. Laying the stages out as ``bufB0 bufA0 bufB1 bufA1 …`` makes
           the release order a property of the GEOMETRY: each stage's baton is
           hop 1 to the next and each stage's words ride the same conveyor to
           the egress. The SPILLS leave the row entirely: each ``bufA_k``
           (k<3) flips SOUTH and its words ride ``spad_k`` (west-resting) and
           ``add_k``'s northward column into ``bufB_k`` at hop 3; pair 3's
           spill is DELIVERED into the egress's ``sp`` relay and re-emitted
           one hop west into ``bufB3``. A single-file row that carried both
           the westward spills and the eastward release was measured to wedge
           on the fourth drain lap (INV-56).
        3. **The control column is one northward walk.** ``relay2``, the three
           pads and ``drn`` all face north, so each one's walk climbs the
           column, passes through ``wb``, and continues east along the state
           line. The single backward edge, ``wb -> wbk``, is served by ``wb``'s
           one face flip. The drain lap's baton rides the same column: it
           leaves ``bufA3`` SOUTH (the END of the store wave — INV-56's TIME
           half), crosses the idle quarter-round serpentine, and lands on
           ``add_pad`` at hop 21, which turns it EAST into ``drn``. The
           RELEASE trigger then rides ``drn``'s own state-line walk to
           ``tap0`` (hop 2), whose existing north flip lifts it through
           ``add0`` onto the chain head ``bufB0`` (hop 2).

        The bands (``s0..s2`` are the spill pads)::

            y=0  reorder:  bpad0 bpad1 bufB0 bufA0 bufB1 bufA1 bufB2 bufA2 bufB3 out
            y=1  finish:   seq  wbk  add0 s0 add1 s1 add2 s2 add3 bufA3
            y=2  state:    wb | row0 tap0 row1 tap1 row2 tap2 row3 tap3 | in0
            y=3  ctl/QR:   add_pad drn . . . . .  l1_xor l1_add in1
            y=4  ctl/QR:   ctl_pad . . . . . .    l2_add l2_xor l2_rota
            y=5  QR leg 3: ra_pad l4_rotb … l3_rota l3_xor l3_add l2_rotb
            y=6  loop:     relay2 relay

        Each adder sits directly NORTH of its own tap (so the tap's inward
        face flip reaches it at hop 1). Adders 0..2 rest NORTH, feeding their
        stage-1 at hop 2 through the far stage directly above; ``add3`` rests
        EAST, feeding ``bufA3`` beside it at hop 1. ``out`` sits ON the
        chip's ``x16_out`` port cell, resting EAST toward the edge — an
        egress anywhere else bursts its port words through a busy block cell
        and wedges (measured; see :meth:`_out`). No two abutting cells rest
        facing each other anywhere in the fold, which the head-on gate checks
        statically (INV-56 rule 3).

        The fold is 10 wide — the full array width — which is why the block
        declares ``CHIP_SCALE``. The <=8-across convention exists only to leave
        a bus channel for OTHER blocks; a sole occupant has none to pass, and a
        wider fold leaves whole free rows rather than fragmented perimeter (see
        layout_rules.md §3 and INV-40). It is now 7 tall on a 12-tall array, so
        it still leaves five whole free rows.
        """
        lay: Dict[Any, Tuple[int, int, str]] = {}
        a, b = self._QR_LEG_A, self._QR_LEG_B

        # --- y=1, the FINISH row: the sequencer, the row-trigger cell and the
        #     four adders. The state line below is irreducibly 10 columns wide,
        #     so the block is full array width and there is no vertical
        #     corridor; the chip's I/O corridor runs along the array row above
        #     the block and taps `seq` (input) and `out` (output) from there.
        #     BOTH I/O cells are still on ONE edge, which `CHIP_SCALE` requires
        #     -- `out` simply moved up a band with the reorder buffers, and
        #     `seq` stayed on the finish row where `wb`'s northward hand-off
        #     needs it.
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
        lay["seq"] = (0, 1, "east")
        # `wbk` RESTS SOUTH: from (1,0) that walk enters the state line at
        # `row0` and runs east along it, hitting the four rows at hops 1, 3, 5,
        # 7. That single walk is what both of its schedules -- the per-lap
        # rotates and the half-boundary realignment -- need, and (1,0) is the
        # ONLY slot on this fold that provides it while also being the only slot
        # that can reach `seq` (west, hop 1). See :meth:`_wbk`.
        lay["wbk"] = (1, 1, "south")
        # Each adder RESTS toward its own stage-1 buffer -- NORTH for pairs
        # 0..2 (stage-1 is `bufA_k` at hop 2, through `bufB_k`), EAST for pair
        # 3 (`bufA3` is directly beside it at hop 1). Storing rides the
        # resting face either way, so no adder carries a face constant.
        for k in range(3):
            lay[f"add{k}"] = (2 + 2 * k, 1, "north")
        lay["add3"] = (8, 1, "east")

        # --- y=0, the REORDER band, and the block's egress. The release order
        #     is `bufB0 bufA0 bufB1 bufA1 ...`, and the cells sit along the row
        #     IN THAT ORDER, all resting EAST, with `out` at the end. So the row
        #     is ONE eastward conveyor that serves three jobs at once:
        #       * every stage's two released words ride it to `out`;
        #       * every stage's baton reaches the next stage at hop 1;
        #       * `seq`'s release trigger climbs into `bufB0` through `bpad0`.
        #     Nothing on this row needs a face flip, which is what makes the
        #     depth-2 stage fit with five words to spare.
        #
        #     `bufA_k` sits directly above `add_k` (columns 2,4,6,8) and
        #     `bufB_k` immediately WEST of it, so the fill edges are
        #     `add_k -> bufA_k` north hop 1 and `bufA_k -> bufB_k` west hop 1.
        #     The interleave is what makes the release order fall out of the
        #     geometry rather than out of a schedule.
        lay["bpad0"] = (0, 0, "east")
        lay["bpad1"] = (1, 0, "east")
        for k in range(4):
            lay[f"bufB{k}"] = (2 + 2 * k, 0, "east")
        for k in range(3):
            lay[f"bufA{k}"] = (3 + 2 * k, 0, "east")
        # THE EAST CORNER -- solved by measurement, three deadlocks deep.
        #
        # The egress sits ON the chip's output-port cell. An egress one cell
        # away sends every port word THROUGH a busy block cell, and the port
        # word is not consumed independently of that cell's queues: measured,
        # `out` at (9,1) bursting north held its port word at `bufA3` (9,0)
        # while `bufA3` held a release word south at `out` -- the two-cell
        # circular wait of INV-56 rule 3, live, and the run wedged with zero
        # words on the port. From the port cell itself the port write leaves
        # on the chip edge and touches no other cell (the shape the 41-cell
        # fold already proved for 32 words). Resting EAST -- the edge face the
        # port hardware sits on -- points off-array, so no head-on pair is
        # possible, and the port bursts ride the resting face with no flip.
        lay["out"] = (9, 0, "east")
        # Pair 3's stage-1 lives a row below the band, resting NORTH: every
        # word it emits -- release words and spill into `out`'s relay -- rides
        # that one face, so the cell needs no face constant for its data path
        # at all. Its adder rests EAST (unique among the four) and feeds it at
        # hop 1 on the resting face.
        lay["bufA3"] = (9, 1, "north")
        # The SPILL corridor's relay pads (INV-56 fix shape (b)): one below
        # each of `bufA0..bufA2`, resting WEST into the adder's column.
        for k in range(3):
            lay[f"spad{k}"] = (3 + 2 * k, 1, "west")

        # --- y=2, the STATE line: `wb` at its west end, then the four
        #     row/tap pairs, then the frame collector.
        lay["wb"] = (0, 2, "east")
        for i, cid in enumerate(STATE_LINE):
            lay[cid] = (1 + i, 2, "east")

        # --- the quarter round: down the east side, then three serpentine legs
        #     sized so the tail lands at column 1 and drops onto `relay`.
        qr = list(QR_CELLS)
        lay[qr[0]] = (9, 2, "south")
        c = len(qr) - 1 - a - b
        for i, cid in enumerate(qr[1:1 + a]):                  # leg 1, west
            lay[cid] = (9 - i, 3, "west" if i < a - 1 else "south")
        x3 = 9 - (a - 1)
        for i, cid in enumerate(qr[1 + a:1 + a + b]):          # leg 2, east
            lay[cid] = (x3 + i, 4, "east" if i < b - 1 else "south")
        x4 = x3 + (b - 1)
        for i, cid in enumerate(qr[1 + a + b:]):               # leg 3, west
            lay[cid] = (x4 - i, 5, "west" if i < c - 1 else "south")

        # --- the loop hand-off and the northward CONTROL column. `relay2`,
        #     `realign` and the two pads all face north, so each one's walk
        #     climbs the column, passes through `wb`, and continues east along
        #     the state line.
        lay["relay"] = (1, 6, "west")
        lay["relay2"] = (0, 6, "north")
        lay["ra_pad"] = (0, 5, "north")
        lay["ctl_pad"] = (0, 4, "north")
        lay["add_pad"] = (0, 3, "north")

        # --- the DRAIN sequencer. Resting NORTH from (1,3) its walk enters the
        #     state line at `row0` and runs east along it, so the four rows sit
        #     at hops 1, 3, 5, 7 and `row0.pub` is that same hop 1 -- every one
        #     of its jumps rides the resting face and none needs a flip.
        #     Exhaustive search over every free slot x every face finds NINETEEN
        #     slots that reach all four rows in order, so the earlier claim that
        #     `(1,0)` was the only such slot was derived, not measured, and was
        #     wrong. `(1,2)` -- now `(1,3)` after the shift -- is the one that is
        #     ALSO reachable from a cell with words to spare (`add_pad`, one hop
        #     west), which is what closes the drain lap. Its WESTWARD walk is
        #     what carries the release trigger back out (`add_pad` at hop 1,
        #     `wb` at hop 2).
        lay["drn"] = (1, 3, "north")

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

