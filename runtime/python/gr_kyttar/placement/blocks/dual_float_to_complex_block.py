"""DualFloatToComplexBlock — see :class:`DualFloatToComplexBlock`."""
import numpy as np
from typing import Dict

from ..block import CellProgram, Port, EntryPoint, DataWord, StateVar
from ._base import KyttarBlock, BlockInterface


class DualFloatToComplexBlock(KyttarBlock):
    """Dual float -> complex rendezvous (1 cell) — pairs TWO independent real
    streams into ONE complex packet using the arbiter LOCK.

    GNU Radio equivalent: ``blocks.float_to_complex`` fed by two DISTINCT real
    streams (I on input 0, Q on input 1) -> a single complex stream. On this
    clockless array the two producers fire at INDEPENDENT times, so the cell must
    consume them as strictly MATCHED PAIRS. It does so with the ISA arbiter lock
    (``LOCK`` / ``LOCK_FACE`` — CONFIG 4 / 3), which forces the cell to accept an
    input from only ONE face at a time (PROGRAMMING_GUIDE §Configuration registers,
    "multi-input synchronization … regardless of timing"). The lock is CONFIG state
    that persists across HALT, so the cell can wait locked to one face.

    Rendezvous (the cell is ALWAYS locked to exactly one face, advancing I->Q->I):

        arm   (default): LOCK_FACE = face_i ; LOCK = 1 ; HALT   (accept only I)
        got_i (face_i):  latch I ; LOCK_FACE = face_q ; HALT    (accept only Q)
        got_q (face_q):  latch Q ; emit (xi=I, xq=Q) packet downstream ;
                         LOCK_FACE = face_i ; HALT               (re-arm for next I)

    Because the cell only ever fires the downstream complex block from ``got_q``,
    with a fresh I latched moments before, it CANNOT emit an unpaired or duplicated
    sample no matter how the two producers interleave.

    NOTE: this block is ONLY needed for TWO independent real producers. A single
    real stream feeding a complex block (real audio -> mixer, Q=0) is a LOGICAL-ONLY
    ``float_to_complex`` in GRC — the importer wires the float straight to the
    complex block's xi (xq=0), no cell. See
    dev_docs/LOGICAL_CONVERTERS_AND_DUAL_F2C_RENDEZVOUS.md.

    Parameters:
      * ``face_i`` / ``face_q``: the faces the I and Q producers arrive on
        ('south'|'east'|'west'|'north'). MUST be distinct — the router lands the
        I route on one face and the Q route on the other.
      * ``hop`` / ``dest_i`` / ``dest_q`` / ``entry``: the downstream complex block's
        hop, xi/xq registers, and entry (authored by the build's handoff patch).

    Interface: entries ``arm`` (default), ``got_i``, ``got_q``. I lands in R0, Q in
    R0 (each on its own trigger); latched to state xi/xq before emit.
    """
    CATEGORY = "type_conversion"
    TAGS = ["float_to_complex", "rendezvous", "lock", "type_conversion", "complex"]
    # This block authors its own output WRITE/JUMP hops (the downstream complex
    # packet) — the build must NOT default them to @1 abutment.
    RAW_OUTPUT_HOPS = True

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
        return BlockInterface(entry_address=1, input_registers=[0],
                              output_registers=[0])

    def build_cell_programs(self) -> Dict[int, CellProgram]:
        fi = self._FACE.get(self._face_i, 2)   # west
        fq = self._FACE.get(self._face_q, 0)   # south
        one = 1
        # The rendezvous. `arm` is fired ONCE at startup to lock to face_i; from then
        # on the cell self-re-arms at the end of got_q. LOCK/LOCK_FACE persist across
        # HALT (CONFIG state), so each HALT waits on exactly the locked face.
        tmpl = (
            "arm:\n"
            "    MOVE [LOCK_FACE], R{data:face_i}\n"
            "    MOVE [LOCK], R{data:lock_on}\n"
            "    HALT\n"
            "got_i:\n"
            "    MOVE R{state:xi}, R{in:i}\n"       # latch I
            "    MOVE [LOCK_FACE], R{data:face_q}\n"  # now accept only Q
            "    HALT\n"
            "got_q:\n"
            "    MOVE R{state:xq}, R{in:q}\n"       # latch Q (paired with the latched I)
            "    MOVE R0, R{state:xi}\n"
            f"    WRITE @{self._hop}, {self._dest_i}\n"   # xi -> downstream R0
            "    MOVE R0, R{state:xq}\n"
            f"    WRITE @{self._hop}, {self._dest_q}\n"   # xq -> downstream R1
            f"    JUMP @{self._hop}, {self._entry}\n"     # fire the complex block ONCE
            "    MOVE [LOCK_FACE], R{data:face_i}\n"      # re-arm: accept only I again
            "    HALT\n"
        )
        return {0: CellProgram(
            inputs=[Port("i", register=0), Port("q", register=0)],
            outputs=[Port("out")],
            entries=[EntryPoint("arm"), EntryPoint("got_i"), EntryPoint("got_q")],
            data=[DataWord("face_i", fi, address=1, is_face=True),
                  DataWord("face_q", fq, address=2, is_face=True),
                  DataWord("lock_on", one, address=3)],
            state=[StateVar("xi"), StateVar("xq")],
            assembly_template=tmpl,
        )}

    def process_reference(self, input_samples):
        # Pairs (I, Q) -> complex. The reference just re-interleaves matched pairs;
        # the substrate proof is that only MATCHED pairs are emitted (the block
        # verification drives adversarial interleavings and checks the pairing).
        arr = np.asarray(input_samples)
        return arr

    def reset(self):
        pass
