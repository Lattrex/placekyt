# SPDX-License-Identifier: GPL-3.0-or-later
"""ChaCha20QRBlock — see :class:`ChaCha20QRBlock`."""
from typing import Any, Dict, List, Tuple

import numpy as np

from ..block import CellProgram, DataWord, EntryPoint, Port, StateVar
from ._base import BlockInterface, KyttarBlock

MASK16 = 0xFFFF
MASK32 = 0xFFFFFFFF

#: The eight 16-bit words of a quarter-round frame, in wire order.
FRAME = ("a_hi", "a_lo", "b_hi", "b_lo", "c_hi", "c_lo", "d_hi", "d_lo")


def _rotl32(x: int, n: int) -> int:
    """Rotate a 32-bit word left by ``n`` — RFC 8439 §2.1's ``<<<``."""
    x &= MASK32
    n &= 31
    return x if n == 0 else ((x << n) | (x >> (32 - n))) & MASK32


class ChaCha20QRBlock(KyttarBlock):
    """
    ChaCha20 **quarter round** (RFC 8439 §2.1) — a placeKYT-native ([Kyttar])
    cryptographic primitive with **no stock GNU Radio counterpart**. The golden
    reference is the published algorithm in
    ``verification/tests/chacha20_golden.py``, itself pinned by the RFC's own
    §2.1.1 and §2.2.1 test vectors.

    One quarter round on four 32-bit words ``a, b, c, d``::

        a += b;  d ^= a;  d <<<= 16
        c += d;  b ^= c;  b <<<= 12
        a += b;  d ^= a;  d <<<= 8
        c += d;  b ^= c;  b <<<= 7

    This is **exact modular integer arithmetic, not Q15 DSP**. Every add wraps
    mod 2**32; nothing saturates. A 32-bit value is carried as a **hi/lo pair
    of 16-bit registers**, and the block streams frames of eight 16-bit words:

        in / out order:  ``a_hi a_lo b_hi b_lo c_hi c_lo d_hi d_lo``

    Rate: **8 words in, 8 words out** — one input word per trigger; the frame
    egresses as an 8-word burst on the eighth trigger. Words are RAW 16-bit
    values, never Q15-scaled.

    Datapath — 17 cells in an 8x3 out-and-back serpentine
    =====================================================

    Two collector cells turn the serial word stream into a resident 8-word
    frame, fourteen feed-forward stages carry that frame (each rewriting the
    one 32-bit value it owns and relaying the other three), and one egress cell
    bursts the result out::

        row 0 (east):  in0     in1     l1_add l1_xor l2_add  l2_xor  l2_rota l2_rotb
        row 1 (west):  l4_rotb l4_rota l4_xor l4_add l3_rotb l3_rota l3_xor  l3_add
        row 2:         emit

    ``in0`` (the landing cell, at (0,0)) and ``emit`` (the egress cell, at
    (0,2)) both sit on the WEST edge within ``COLOCATION_SPAN``, so a single bus
    taps both (layout convention 1).

    How each ISA primitive is built (the measured costs, which scope the rest
    of the multi-word-arithmetic family — Poly1305 and the ChaCha20 keystream)
    ================================================================

    * **32-bit ADD = 4 instructions.** ``ADD lo,lo / MOVE lo,R0 / ADC hi,hi /
      MOVE hi,R0``. ``ADC`` (guide §4.2) carries bit 16 across the halves; the
      intervening ``MOVE`` is flag-preserving (only ALU ops touch the flags),
      so the carry survives the park. The carry is NEVER synthesised.
    * **32-bit XOR = 4 instructions.** Two 16-bit ``XOR``s plus their parks.
    * **``ROTL32(x, 16)`` = 0 instructions.** It is exactly the hi/lo swap, and
      the swap is folded into *which register each relay ``MOVE`` reads* — the
      cell that computes ``d ^= a`` relays ``d_lo`` into the ``d_hi`` slot and
      vice versa. Not two ``MOVE``s: none.
    * **``ROTL32(x, n)`` for n < 16 = 7 instructions over 2 cells.** The naive
      cross-half form (``hi' = hi<<n | lo>>(16-n)``, and symmetrically for lo)
      needs both original halves alive while writing both results, which does
      not fit a relay cell's budget. Instead use the **rotate-then-merge**
      identity, with ``u = ROL16(hi, n)``, ``v = ROL16(lo, n)`` and
      ``M = (1 << n) - 1``::

          hi' = u ^ ((u ^ v) & M)        lo' = v ^ ((u ^ v) & M)

      (``ROL16(hi, n)`` already holds ``hi << n`` in its high bits and
      ``hi >> (16-n)`` in its low ``n`` bits — precisely the two pieces the
      cross-half form needs, so the only work left is to trade the low ``n``
      bits between the halves.) That splits cleanly into a 4-instruction
      ``rota`` cell (the two ``ROL``s) and a 3-instruction ``rotb`` cell (the
      masked merge — 5 instructions, 2 of which ARE the relay writes it
      replaces), each of which fits alongside the 8-word relay.

    Per-cell word budget (the binding constraint, INV-33). A relay stage holds
    the whole 8-word frame in its input registers and forwards it with
    ``MOVE R0, Rw`` + ``WRITE`` per word, so::

        8 inputs + 16 relay + 1 jump = 25 of the 31 usable words

    leaving **6** for the stage's own data + state + body. That ceiling is what
    forces the rotate to split across two cells, and it is why the block is 17
    cells for **53 instructions of actual arithmetic** — on this substrate
    **carrying a wide value costs more than computing on it** (INV-45).

    ``rotb`` recovers the two words it would otherwise be over budget by
    writing its two results **straight out of R0** (the value is already there
    when the merge finishes), skipping both the park ``MOVE`` and the relay's
    reload ``MOVE``. ``emit`` recovers its one word the same way, via
    accumulator delivery — see :meth:`_emit`, and note that an over-budget cell
    is SILENT: it assembles, loads, places and routes, and returns a wrong
    answer that looks like a routing fault.

    Interface:
        - Entry: ``in0``'s default entry.
        - Input: one 16-bit word per trigger (the frame arrives over 8).
        - Output: the 8-word result frame, burst on the eighth trigger.
    """

    CATEGORY = "fec"
    TAGS = ["chacha20", "crypto", "cipher", "rfc8439", "quarter-round",
            "multi-word", "32-bit"]

    # Resolved dynamically by resolved_io (INV-6); these are in0's real layout.
    _interface = BlockInterface(
        entry_address=16, input_registers=[5], output_registers=[0])

    GRC_UNSUPPORTED_PARAMS = ()

    #: Words per frame — four 32-bit values as hi/lo pairs.
    FRAME_WORDS = 8

    def __init__(self, name: str):
        """One ChaCha20 quarter round (RFC 8439 §2.1). No parameters: the
        quarter round is a fixed permutation, and its four rotate constants
        (16/12/8/7) are part of the specification, not user settings."""
        super().__init__(name)

    # ------------------------------------------------------------- structure
    @property
    def cell_count(self) -> int:
        return 17          # in0, in1 + 14 compute stages + emit

    @property
    def interface(self) -> BlockInterface:
        return self._interface

    def output_cell_id(self):
        return "emit"

    # --------------------------------------------------------- cell builders
    @staticmethod
    def _relay(order: Tuple[str, ...], skip: Tuple[str, ...] = (),
               last: str | None = None) -> str:
        """The frame-relay tail: forward all eight words, then trigger.

        ``order`` is the register each output slot reads from, in slot order —
        swapping two entries IS the free ``ROTL32(x, 16)``. ``skip`` names
        slots already written from R0 inside the body. ``last`` forces one slot
        to be written LAST, which is what makes accumulator delivery safe: the
        downstream cell's R0 must not be disturbed by any later write.
        """
        pairs = [(s, r) for s, r in zip(FRAME, order) if s not in skip]
        if last is not None:
            pairs = ([p for p in pairs if p[0] != last]
                     + [p for p in pairs if p[0] == last])
        tail = ""
        for slot, src in pairs:
            tail += f"    MOVE R0, R{{in:{src}}}\n    {{write:o_{slot}}}\n"
        return tail + "    {jump:trig}\n"

    @classmethod
    def _relay_cell(cls, body: str, *, order: Tuple[str, ...] = FRAME,
                    skip: Tuple[str, ...] = (),
                    last: str | None = None,
                    data: Tuple[Tuple[str, int], ...] = (),
                    state: Tuple[str, ...] = ()) -> CellProgram:
        """A frame-relay stage: 8 words in, ``body``, 8 words out.

        Registers are pinned explicitly (INV-33), and allocation starts at R1,
        never R0: R0 is the ACCUMULATOR that every ALU op overwrites, so a
        constant or a frame word parked there is destroyed by the stage's first
        instruction. Layout is data, then state, then the eight frame words.
        """
        n_data = len(data)
        n_state = len(state)
        return CellProgram(
            inputs=[Port(w, register=1 + n_data + n_state + k)
                    for k, w in enumerate(FRAME)],
            outputs=[Port(f"o_{w}") for w in FRAME] + [Port("trig")],
            entries=[EntryPoint("default")],
            data=[DataWord(n, v, address=1 + i)
                  for i, (n, v) in enumerate(data)],
            state=[StateVar(n, register=1 + n_data + i)
                   for i, n in enumerate(state)],
            assembly_template="default:\n" + body + cls._relay(order, skip,
                                                               last),
        )

    @classmethod
    def _add32(cls, dst: str, src: str) -> CellProgram:
        """``dst += src`` (mod 2**32). ADD/park/ADC/park — the carry rides the
        flag-preserving MOVE, never re-derived."""
        return cls._relay_cell(f"""\
    ADD R{{in:{dst}_lo}}, R{{in:{src}_lo}}
    MOVE R{{in:{dst}_lo}}, R0
    ADC R{{in:{dst}_hi}}, R{{in:{src}_hi}}
    MOVE R{{in:{dst}_hi}}, R0
""")

    @classmethod
    def _xor32(cls, dst: str, src: str, *, rot16: bool = False) -> CellProgram:
        """``dst ^= src`` (mod 2**32), optionally followed by the FREE
        ``ROTL32(dst, 16)`` — a hi/lo swap expressed purely as which register
        each relay slot reads, costing zero instructions."""
        order = list(FRAME)
        if rot16:
            i, j = order.index(f"{dst}_hi"), order.index(f"{dst}_lo")
            order[i], order[j] = order[j], order[i]
        return cls._relay_cell(f"""\
    XOR R{{in:{dst}_lo}}, R{{in:{src}_lo}}
    MOVE R{{in:{dst}_lo}}, R0
    XOR R{{in:{dst}_hi}}, R{{in:{src}_hi}}
    MOVE R{{in:{dst}_hi}}, R0
""", order=tuple(order))

    @classmethod
    def _rot_a(cls, dst: str, n: int) -> CellProgram:
        """Rotate stage A: ``u = ROL16(hi, n)``, ``v = ROL16(lo, n)`` in place."""
        return cls._relay_cell(f"""\
    ROL R{{in:{dst}_hi}}, #{n}
    MOVE R{{in:{dst}_hi}}, R0
    ROL R{{in:{dst}_lo}}, #{n}
    MOVE R{{in:{dst}_lo}}, R0
""")

    @classmethod
    def _rot_b(cls, dst: str, n: int,
               last: str | None = None) -> CellProgram:
        """Rotate stage B: trade the low ``n`` bits between the two rotated
        halves — ``hi' = u ^ k``, ``lo' = v ^ k`` with ``k = (u ^ v) & M``.

        Both results are written straight out of R0, which is what keeps the
        cell inside its 6-word body budget. ``last`` forces one relayed slot to
        the end of the tail (the final stage delivers slot 0 into the egress
        cell's accumulator, so nothing may be written after it).
        """
        hi, lo = f"{dst}_hi", f"{dst}_lo"
        body = f"""\
    XOR R{{in:{hi}}}, R{{in:{lo}}}
    AND R0, R{{data:msk}}
    MOVE R{{state:k}}, R0
    XOR R{{in:{hi}}}, R{{state:k}}
    {{write:o_{hi}}}
    XOR R{{in:{lo}}}, R{{state:k}}
    {{write:o_{lo}}}
"""
        return cls._relay_cell(body, skip=(hi, lo), last=last,
                               data=(("msk", (1 << n) - 1),), state=("k",))

    @staticmethod
    def _emit() -> CellProgram:
        """Egress cell: push the finished frame out of the block on ONE port.

        Each word leaves as a ``WRITE``+``JUMP`` pair on the single ``out``
        port — the rate-expanding burst idiom (``UpsamplerBlock``), where the
        port's single-outstanding handshake paces the words and keeps them in
        slot order. One port, not eight: a burst on one net is what a bus taps
        and what a consumer drains.

        **The ACCUMULATOR-DELIVERY idiom is load-bearing here** (INV-33). The
        naive shape — hold all 8 words in R1..R8 and emit each with
        ``MOVE R0, Rw`` / ``WRITE`` / ``JUMP`` — is exactly ONE word over
        budget: 24 instructions put ``base_addr`` at 31-24 = 7, so the frame's
        own R7/R8 land ON TOP of the cell's first instruction words. The
        resolver does NOT catch that: its space guard compares only DATA
        against ``base_addr``, never state or pinned inputs. The cell
        assembles, the bitstream loads, and the burst silently drops its
        LEADING word while the other seven come out bit-exact — a wrong answer
        that looks like a routing fault.

        The fix is to carry slot 0 in R0: the upstream stage writes ``a_hi``
        into this cell's accumulator as its LAST write before the trigger, and
        this cell's FIRST instruction is that word's ``WRITE`` — no ``MOVE``
        needed, and nothing has run yet to clobber R0. That saves the one
        instruction AND the one register the cell was over by (23 instructions,
        ``base_addr`` 8, frame at R1..R7).

        The remaining seven words land at R1..R7, never R0: every later
        ``MOVE R0, Rw`` overwrites the accumulator, so a word parked there
        would be destroyed before its own ``WRITE``.
        """
        # Slot 0 arrives in R0 (accumulator delivery) and goes out FIRST.
        body = "    {write:out}\n    {jump:out}\n"
        for slot in FRAME[1:]:
            body += (f"    MOVE R0, R{{in:{slot}}}\n"
                     "    {write:out}\n    {jump:out}\n")
        return CellProgram(
            inputs=([Port(FRAME[0], register=0)]
                    + [Port(w, register=1 + i)
                       for i, w in enumerate(FRAME[1:])]),
            outputs=[Port("out")],
            entries=[EntryPoint("default")],
            data=[],
            state=[],
            assembly_template="default:\n" + body,
        )

    @staticmethod
    def _in0() -> CellProgram:
        """Landing cell: a 4-deep shift of the arriving word stream.

        Every trigger it spills its oldest word into ``in1`` and re-publishes
        its four held words into the compute head's ``a_hi..b_lo`` slots. The
        head is only ever TRIGGERED by ``in1`` on the eighth word, so the
        intermediate publications are simply overwritten — which is what lets
        this cell stay unconditional (no branch, no second jump, no lock).
        """
        return CellProgram(
            inputs=[Port("x", register=5)],
            outputs=[Port("spill"), Port("h4"), Port("h5"), Port("h6"),
                     Port("h7"), Port("trig")],
            entries=[EntryPoint("default")],
            data=[],
            state=[StateVar("s0", register=1), StateVar("s1", register=2),
                   StateVar("s2", register=3), StateVar("s3", register=4)],
            assembly_template="""\
default:
    MOVE R0, R{state:s3}
    {write:spill}
    MOVE R{state:s3}, R{state:s2}
    MOVE R{state:s2}, R{state:s1}
    MOVE R{state:s1}, R{state:s0}
    MOVE R{state:s0}, R{in:x}
    MOVE R0, R{state:s3}
    {write:h4}
    MOVE R0, R{state:s2}
    {write:h5}
    MOVE R0, R{state:s1}
    {write:h6}
    MOVE R0, R{state:s0}
    {write:h7}
    {jump:trig}
""",
        )

    @staticmethod
    def _in1() -> CellProgram:
        """Second collector: a 4-deep shift of ``in0``'s spill, plus the frame
        counter. On the eighth word it publishes its four (the OLDEST four,
        i.e. frame slots 0-3) and fires the compute head exactly once."""
        return CellProgram(
            inputs=[Port("sp", register=8)],
            outputs=[Port("h0"), Port("h1"), Port("h2"), Port("h3"),
                     Port("trig")],
            entries=[EntryPoint("default")],
            # Data words start at R1, NEVER R0: R0 is the ACCUMULATOR and every
            # ALU op overwrites it, so a constant parked there survives exactly
            # one instruction (INV-33). The R0 slot is a deliberate hole.
            data=[DataWord("one", 1, address=1),
                  DataWord("eight", 8, address=2)],
            state=[StateVar("t0", register=3), StateVar("t1", register=4),
                   StateVar("t2", register=5), StateVar("t3", register=6),
                   StateVar("n", register=7, initial_value=8,
                            reset_per_batch=True, reset_value=8)],
            assembly_template="""\
default:
    MOVE R{state:t3}, R{state:t2}
    MOVE R{state:t2}, R{state:t1}
    MOVE R{state:t1}, R{state:t0}
    MOVE R{state:t0}, R{in:sp}
    SUB R{state:n}, R{data:one}
    MOVE R{state:n}, R0
    BR.NZ done
    MOVE R{state:n}, R{data:eight}
    MOVE R0, R{state:t3}
    {write:h0}
    MOVE R0, R{state:t2}
    {write:h1}
    MOVE R0, R{state:t1}
    {write:h2}
    MOVE R0, R{state:t0}
    {write:h3}
    {jump:trig}
done:
""",
        )

    def build_cell_programs(self) -> Dict[Any, CellProgram]:
        """The 17 cells, in LAYOUT order (positional pairing, INV-33)."""
        return {
            "in0": self._in0(),
            "in1": self._in1(),
            # a += b ; d ^= a ; d <<<= 16   (the rot16 is free — see _xor32)
            "l1_add": self._add32("a", "b"),
            "l1_xor": self._xor32("d", "a", rot16=True),
            # c += d ; b ^= c ; b <<<= 12
            "l2_add": self._add32("c", "d"),
            "l2_xor": self._xor32("b", "c"),
            "l2_rota": self._rot_a("b", 12),
            "l2_rotb": self._rot_b("b", 12),
            # a += b ; d ^= a ; d <<<= 8
            "l3_add": self._add32("a", "b"),
            "l3_xor": self._xor32("d", "a"),
            "l3_rota": self._rot_a("d", 8),
            "l3_rotb": self._rot_b("d", 8),
            # c += d ; b ^= c ; b <<<= 7
            "l4_add": self._add32("c", "d"),
            "l4_xor": self._xor32("b", "c"),
            "l4_rota": self._rot_a("b", 7),
            # The last stage writes frame slot 0 LAST, straight into the egress
            # cell's R0 — the accumulator delivery that keeps `emit` in budget.
            "l4_rotb": self._rot_b("b", 7, last=FRAME[0]),
            "emit": self._emit(),
        }

    # ---------------------------------------------------------------- wiring
    #: The 14 frame-relay compute stages, in dataflow order.
    _CHAIN = ("l1_add", "l1_xor", "l2_add", "l2_xor", "l2_rota", "l2_rotb",
              "l3_add", "l3_xor", "l3_rota", "l3_rotb",
              "l4_add", "l4_xor", "l4_rota", "l4_rotb")

    def internal_connections(self) -> List[Tuple[Any, str, Any, str]]:
        head = self._CHAIN[0]
        conns: List[Tuple[Any, str, Any, str]] = [
            # in0 spills its oldest word into in1's shift.
            ("in0", "spill", "in1", "sp"),
            # The frame lands in the head: in1 supplies slots 0-3, in0 slots 4-7.
            ("in1", "h0", head, FRAME[0]), ("in1", "h1", head, FRAME[1]),
            ("in1", "h2", head, FRAME[2]), ("in1", "h3", head, FRAME[3]),
            ("in0", "h4", head, FRAME[4]), ("in0", "h5", head, FRAME[5]),
            ("in0", "h6", head, FRAME[6]), ("in0", "h7", head, FRAME[7]),
        ]
        # Each compute stage relays the whole frame to the next.
        for src, dst in zip(self._CHAIN, self._CHAIN[1:]):
            conns += [(src, f"o_{w}", dst, w) for w in FRAME]
        # The last stage hands seven slots to `emit`'s registers and slot 0 to
        # its ACCUMULATOR (register 0) as its final write — see _emit.
        conns += [(self._CHAIN[-1], f"o_{w}", "emit", w) for w in FRAME]
        return conns

    def internal_jumps(self) -> List[Tuple[Any, str, Any, str]]:
        jumps: List[Tuple[Any, str, Any, str]] = [
            ("in0", "trig", "in1", "default"),
            ("in1", "trig", self._CHAIN[0], "default"),
        ]
        for src, dst in zip(self._CHAIN, self._CHAIN[1:]):
            jumps.append((src, "trig", dst, "default"))
        # NOTE: no ``__terminate__`` edge for ``emit``'s ``out``. The egress
        # WRITE/JUMP pairs ARE the block's external port handshake
        # (UpsamplerBlock's shape); declaring an internal jump from that port
        # would mark it internally-consumed and the portmap would drop the
        # block's ONLY output — a block that builds and routes with no output
        # port at all.
        jumps.append((self._CHAIN[-1], "trig", "emit", "default"))
        return jumps

    def default_layout(self) -> Dict[Any, Tuple[int, int, str]]:
        """8x3 out-and-back serpentine, I/O co-located on the WEST edge::

            col:   0        1        2       3        4        5        6        7
            row0:  in0(E)   in1(E)   l1_add  l1_xor   l2_add   l2_xor   l2_rota  l2_rotb(S)
            row1:  l4_rotb  l4_rota  l4_xor  l4_add   l3_rotb  l3_rota  l3_xor   l3_add(W)
            row2:  emit(W)

        The chain runs east along row 0, doubles back west along row 1, and
        finishes with the egress cell in row 2, so the LAST cell returns to the
        west column instead of stranding the output on the far edge.
        ``in0`` (the input) at (0,0) and ``emit`` (the output) at (0,2) are
        both on the west edge and within ``COLOCATION_SPAN``, so ONE bus taps
        both — layout convention 1. The fold is 8 wide (the <=8-across
        convention, INV-9) with an even column count (INV-14).

        Program-dict order == layout order (positional pairing, INV-33).
        """
        row0 = ["in0", "in1"] + list(self._CHAIN[:6])
        row1 = list(self._CHAIN[6:14])
        lay: Dict[Any, Tuple[int, int, str]] = {}
        for i, cid in enumerate(row0):
            lay[cid] = (i, 0, "east" if i < len(row0) - 1 else "south")
        # Row 1 runs WESTWARD: the chain doubles back from column 7 to 0.
        for i, cid in enumerate(row1):
            lay[cid] = (7 - i, 1, "west" if i < len(row1) - 1 else "south")
        lay["emit"] = (0, 2, "west")
        return lay

    # ------------------------------------------------------------- reference
    def process_reference(self, input_words) -> np.ndarray:
        """Bit-exact reference: RFC 8439 §2.1's quarter round over 8-word
        hi/lo frames.

        ``input_words`` is the flat 16-bit word stream (raw, NOT Q15). Each
        complete group of 8 words is one frame ``a_hi a_lo b_hi b_lo c_hi c_lo
        d_hi d_lo``; a trailing partial frame produces no output. Every add
        wraps mod 2**32 — this never saturates.
        """
        w = [int(v) & MASK16 for v in np.asarray(input_words).ravel()]
        out: List[int] = []
        for f in range(len(w) // self.FRAME_WORDS):
            g = w[f * self.FRAME_WORDS:(f + 1) * self.FRAME_WORDS]
            a, b, c, d = (((g[2 * k] << 16) | g[2 * k + 1]) for k in range(4))
            a = (a + b) & MASK32
            d = _rotl32(d ^ a, 16)
            c = (c + d) & MASK32
            b = _rotl32(b ^ c, 12)
            a = (a + b) & MASK32
            d = _rotl32(d ^ a, 8)
            c = (c + d) & MASK32
            b = _rotl32(b ^ c, 7)
            for v in (a, b, c, d):
                out.append((v >> 16) & MASK16)
                out.append(v & MASK16)
        return np.array(out, dtype=np.uint16)

    def reset(self):
        """No cross-call state: the frame counter re-arms every 8 words and is
        reset per batch (see ``in1``'s ``n``)."""
        pass
