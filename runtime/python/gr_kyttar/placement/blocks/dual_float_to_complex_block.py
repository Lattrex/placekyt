"""DualFloatToComplexBlock — see :class:`DualFloatToComplexBlock`."""
import numpy as np
from typing import Dict

from ..block import CellProgram, Port, EntryPoint, DataWord, StateVar
from ._base import KyttarBlock, BlockInterface


class DualFloatToComplexBlock(KyttarBlock):
    """Dual float -> complex rendezvous (1 cell) — pairs TWO independent real
    streams into ONE complex packet using the arbiter LOCK, distinguishing the
    two streams by their ARRIVAL FACE.

    GNU Radio equivalent: ``blocks.float_to_complex`` fed by two DISTINCT real
    streams (I on input 0, Q on input 1) -> a single complex stream. On this
    clockless array the two producers fire at INDEPENDENT, ASYNCHRONOUS times, so the
    cell must consume them as strictly MATCHED PAIRS regardless of interleaving — if
    the Q path is slow you can get I,I,Q,I,Q,Q and a naive counter mis-pairs forever.

    The ONLY way to tell I from Q when they arrive independently is the physical
    channel they arrive on: the FACE. This cell uses the ISA arbiter lock (``LOCK`` /
    ``LOCK_FACE`` — CONFIG 4 / 3), which forces the cell to accept an input from only
    ONE face at a time (PROGRAMMING_GUIDE §Configuration registers, "multi-input
    synchronization … regardless of timing"). The lock is CONFIG state that persists
    across HALT, so the cell can wait locked to one face until that stream's word
    actually arrives — a slow/bursty producer on the other face is simply IGNORED
    until it's that face's turn. This is why the block REQUIRES its two inputs on TWO
    DISTINCT faces (``NEEDS_DISTINCT_INPUT_FACES``): the face IS the stream identity.

    Rendezvous (the cell is ALWAYS locked to exactly one face, advancing I->Q->I):

        arm   (default): LOCK_FACE = face_i ; LOCK = 1 ; HALT   (accept only I)
        got_i (face_i):  latch I ; LOCK_FACE = face_q ; HALT    (accept only Q)
        got_q (face_q):  latch Q ; emit (yi=I, yq=Q) complex packet ;
                         LOCK_FACE = face_i ; HALT               (re-arm for next I)

    Because the cell only ever fires the downstream from ``got_q``, with a fresh I
    latched moments before, it CANNOT emit an unpaired or duplicated sample no matter
    how the two producers interleave. Cold start is handled by ``initial_lock_face``:
    the build boots the cell already LOCKED to ``face_i`` (LOCK=1), so the very first
    word accepted is the I of the first pair — no external ``arm`` JUMP is needed.

    A NOTE on why NOT a phase toggle: a "counter that alternates I,Q,I,Q on ONE face"
    was tried and is BROKEN — it has no way to know which stream a same-face word came
    from, so any async re-ordering (I,I,Q,...) desyncs it permanently. Merging both
    rails onto one serialized face DESTROYS the stream identity; only distinct faces +
    LOCK preserve it. (Design doc §7 / dev_docs, and CM 2026-07-07.)

    OUTPUT: on the Q trigger it emits the paired complex sample as a 2-rail packet —
    ``yi`` (=xi) and ``yq`` (=xq) plus a ``trig`` — via declarative ``{write:yi}`` /
    ``{write:yq}`` / ``{jump:trig}`` (the ComplexMixer's exact output shape). The build
    BROKERS + hop-patches it like any complex source (no RAW_OUTPUT_HOPS). Because
    ``yi``/``yq`` are a same-cell I/Q pair, the importer's I/Q split auto-wires
    yi->consumer.xi AND yq->consumer.xq to a genuine 2-input complex downstream, so the
    imaginary rail is NOT lost. A REAL downstream (fed through a logical
    ``complex_to_real`` that drops Q) simply leaves ``yq`` unconsumed.

    NOTE: this block is ONLY needed for TWO independent real producers, and it is the
    ONLY block that needs two distinct input faces. Every OTHER complex block receives
    a coordinated ``yi``/``yq`` COMPLEX PACKET (already ordered + paired at its source,
    from one upstream complex block OR from this dual), so it needs no face distinction.
    A single real stream feeding a complex block (real audio -> mixer, Q=0) is a
    LOGICAL-ONLY ``float_to_complex`` in GRC — the importer wires the float straight to
    the complex block's xi (xq=0), no cell. See
    dev_docs/LOGICAL_CONVERTERS_AND_DUAL_F2C_RENDEZVOUS.md.

    Parameters:
      * ``face_i`` / ``face_q``: the faces the I and Q producers arrive on
        ('south'|'east'|'west'|'north'). MUST be distinct — the placer reserves two
        distinct free faces and the router lands the I route on one, the Q on the
        other; the build reconciles these params + ``initial_lock_face`` to the faces
        the router actually chose.
      * ``hop`` / ``dest_i`` / ``dest_q`` / ``entry``: retained for API compatibility;
        the output handoff is now declarative + brokered, not authored.

    Interface: entries ``arm`` (default, address 1), ``got_i``, ``got_q``. I and Q each
    land in R0 on their own trigger; latched to state xi/xq before the paired emit.
    """
    CATEGORY = "type_conversion"
    TAGS = ["float_to_complex", "rendezvous", "lock", "type_conversion", "complex"]
    # The DEFINING property of this block: its two inputs (i, q) are independent async
    # real streams that can ONLY be distinguished by arrival face, so the placer MUST
    # land them on two DISTINCT faces and the build DRC MUST reject same-face landing.
    # No other block sets this (they receive pre-paired complex packets).
    NEEDS_DISTINCT_INPUT_FACES = True

    _FACE = {"south": 0, "east": 1, "west": 2, "north": 3}

    def __init__(self, name: str,
                 face_i: str = "west", face_q: str = "south",
                 hop: int = 1, dest_i: int = 0, dest_q: int = 1, entry: int = 1):
        super().__init__(name, face_i=face_i, face_q=face_q, hop=hop,
                         dest_i=dest_i, dest_q=dest_q, entry=entry)
        self._face_i, self._face_q = face_i, face_q
        self._hop, self._dest_i, self._dest_q = hop, dest_i, dest_q
        self._entry = entry

    @property
    def cell_count(self) -> int:
        return 1

    @property
    def interface(self) -> BlockInterface:
        # Default entry is `arm` (address 1); the I/Q producers target got_i/got_q.
        # I lands in R0 (on the got_i trigger), Q in R0 (on got_q) — each on its own
        # face-gated trigger.
        return BlockInterface(entry_address=1, input_registers=[0],
                              output_registers=[0])

    def build_cell_programs(self) -> Dict[int, CellProgram]:
        fi = self._FACE.get(self._face_i, 2)   # west
        fq = self._FACE.get(self._face_q, 0)   # south
        # LOCK-by-face rendezvous. `arm` locks to face_i at startup (but cold-start
        # `initial_lock_face` below boots it pre-locked so `arm` is only a fallback);
        # from then on the cell self-re-arms at the end of got_q. LOCK/LOCK_FACE persist
        # across HALT (CONFIG state), so each HALT waits on exactly the locked face — a
        # slow producer on the OTHER face is ignored until it is that face's turn.
        #
        # On the Q trigger it emits the paired complex sample as TWO rails yi=xi, yq=xq
        # (+ one trigger) via declarative {write:yi}/{write:yq}/{jump:trig} (the
        # ComplexMixer's output shape) so the build BROKERS + hop-patches it — no
        # RAW_OUTPUT_HOPS. `yi`/`yq` are a same-cell I/Q pair, so the importer's I/Q
        # split auto-wires yi->consumer.xi AND yq->consumer.xq to a 2-input complex
        # downstream; a REAL downstream (via a logical complex_to_real) consumes only yi.
        tmpl = (
            "arm:\n"
            "    MOVE [LOCK_FACE], R{data:face_i}\n"
            "    MOVE [LOCK], R{data:lock_on}\n"
            "    HALT\n"
            "got_i:\n"
            "    MOVE R{state:xi}, R{in:i}\n"            # latch I (accepted on face_i)
            "    MOVE [LOCK_FACE], R{data:face_q}\n"     # now accept ONLY the Q face
            "    HALT\n"
            "got_q:\n"
            "    MOVE R{state:xq}, R{in:q}\n"            # latch Q (paired with the I)
            "    MOVE R0, R{state:xi}\n"
            "    {write:yi}\n"                           # recovered I rail -> consumer.xi
            "    MOVE R0, R{state:xq}\n"
            "    {write:yq}\n"                           # recovered Q rail -> consumer.xq
            "    {jump:trig}\n"                          # trigger the downstream ONCE
            "    MOVE [LOCK_FACE], R{data:face_i}\n"     # re-arm: accept ONLY the I face
            "    HALT\n"
        )
        return {0: CellProgram(
            inputs=[Port("i", register=0), Port("q", register=0)],
            outputs=[Port("yi"), Port("yq"), Port("trig")],
            entries=[EntryPoint("arm"), EntryPoint("got_i"), EntryPoint("got_q")],
            data=[DataWord("face_i", fi, address=1, is_face=True),
                  DataWord("face_q", fq, address=2, is_face=True),
                  DataWord("lock_on", 1, address=3)],
            state=[StateVar("xi"), StateVar("xq")],
            assembly_template=tmpl,
            # Cold start: boot the cell already LOCKED to face_i (LOCK=1), so the first
            # word accepted is the I of the first pair with NO external arm JUMP.
            initial_lock_face=fi,
        )}

    def process_reference(self, input_samples):
        # Pairs (I, Q) -> complex. The reference just re-interleaves matched pairs;
        # the substrate proof is that only MATCHED pairs are emitted (the block
        # verification drives adversarial ASYNC interleavings and checks the pairing).
        arr = np.asarray(input_samples)
        return arr

    def reset(self):
        pass
