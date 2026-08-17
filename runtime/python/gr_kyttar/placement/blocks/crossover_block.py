# SPDX-License-Identifier: GPL-3.0-or-later
"""CrossoverBlock — see :class:`CrossoverBlock`."""
import numpy as np
from ..block import CellProgram, Port, EntryPoint, DataWord
from typing import Dict
from ._base import KyttarBlock, BlockInterface, assemble_to_words, float_to_q15, q15_to_float


class CrossoverBlock(KyttarBlock):
    """
    Crossover Relay Block (1 cell) — two-track signal crossover.

    A single cell with TWO entry points, each its own routing "track". Each
    entry sets the cell's output FACE to a fixed direction, then relays the
    incoming value + a JUMP onward. Two crossing signals can therefore share one
    cell without colliding: track A enters on one entry and exits one face;
    track B enters on the other entry and exits a different face.

    This is the standard primitive for routing two signals across a shared
    channel (e.g. an SRAM panel's to-panel write path and from-panel read
    return). It is a PROGRAMMED relay (it runs a tiny program), NOT a transit
    cell.

    Parameters select each track's output face and where it relays to:
      * ``face_a`` / ``face_b``: output direction for each track
        ('south'|'east'|'west'|'north').
      * ``hop_a`` / ``hop_b``: hops to the track's destination cell (@N).
      * ``dest_a`` / ``dest_b``: destination register at that cell (WRITE addr).
      * ``entry_a`` / ``entry_b``: entry address to JUMP-trigger there.

    Interface:
      * Entry ``track_a`` (default), Entry ``track_b``.
      * The relayed value arrives in R{input} (default R20).
    """
    CATEGORY = "memory_interface"
    TAGS = ["crossover", "relay", "routing", "memory_interface"]
    # This block authors its own output WRITE/JUMP hops (the relay @N) — the
    # build must NOT default them to @1 abutment.
    RAW_OUTPUT_HOPS = True
    # Incoming nets must LAND ON this cell (the track entry runs here) — the
    # build must not strip the on-target waypoint to an abutting broker (a
    # broker relay resolves against the block's shifting register map and, in
    # the duplex-panel build, delivered into the wrong register — the TX
    # runaway loop).
    RELAY_LANDING = True

    _FACE = {"south": 0, "east": 1, "west": 2, "north": 3}

    def __init__(self, name: str,
                 face_a: str = "south", hop_a: int = 1,
                 dest_a: int = 20, entry_a: int = 1,
                 face_b: str = "east", hop_b: int = 1,
                 dest_b: int = 20, entry_b: int = 1,
                 face_c: str = "south", hop_c: int = 1, entry_c: int = 0,
                 dest_c=None, restore_face=None):
        super().__init__(name, face_a=face_a, hop_a=hop_a, dest_a=dest_a,
                         entry_a=entry_a, face_b=face_b, hop_b=hop_b,
                         dest_b=dest_b, entry_b=entry_b,
                         face_c=face_c, hop_c=hop_c, entry_c=entry_c,
                         dest_c=dest_c, restore_face=restore_face)
        self._face_a, self._hop_a = face_a, hop_a
        self._dest_a, self._entry_a = dest_a, entry_a
        self._face_b, self._hop_b = face_b, hop_b
        self._dest_b, self._entry_b = dest_b, entry_b
        # Track C: by default a CONTROL-ONLY relay (no data) — set face, JUMP
        # onward. Used e.g. to relay a read-trigger to a controller's read
        # entry; entry_c=0 leaves it a harmless local no-op JUMP when unused.
        # With ``dest_c`` set (an int), track C becomes a FULL DATA track like
        # a/b (the duplex-panel RX egress needs a third data path through the
        # shared crossover cell). Default None keeps the control-only program.
        self._face_c, self._hop_c, self._entry_c = face_c, hop_c, entry_c
        self._dest_c = None if dest_c is None else int(dest_c)
        # restore_face: when OTHER streams TRANSIT this cell (the duplex-panel
        # layout: the RX input rides THROUGH the TX crossover), a relay that
        # leaves the face flipped would misroute the next transit. With
        # ``restore_face`` set, every track restores the cell's transit face
        # after its relay (the §1.2 broker's self-restore, applied here).
        # Default None keeps the historical program (no transits → no restore).
        self._restore_face = restore_face

    @property
    def cell_count(self) -> int:
        return 1

    @property
    def interface(self) -> BlockInterface:
        return BlockInterface(entry_address=1, input_registers=[20],
                              output_registers=[20])

    def build_cell_programs(self) -> Dict[int, CellProgram]:
        fa = self._FACE.get(self._face_a, 0)
        fb = self._FACE.get(self._face_b, 1)
        fc = self._FACE.get(self._face_c, 0)
        restore = ("" if self._restore_face is None else
                   "    MOVE R0, R{data:face_r}\n"
                   "    MOVE [FACE], R0\n")
        # Each data track: set FACE, move relayed value to R0, WRITE it on,
        # JUMP, then (when transits share this cell) restore the transit face.
        # Track C is control-only unless dest_c is set (then a full data track).
        tmpl = (
            "track_a:\n"
            "    MOVE R0, R{data:face_a}\n"
            "    MOVE [FACE], R0\n"
            "    MOVE R0, R{in:relay}\n"
            f"    WRITE @{self._hop_a}, {self._dest_a}\n"
            f"    JUMP @{self._hop_a}, {self._entry_a}\n"
            + restore +
            "    HALT\n"
            "track_b:\n"
            "    MOVE R0, R{data:face_b}\n"
            "    MOVE [FACE], R0\n"
            "    MOVE R0, R{in:relay}\n"
            f"    WRITE @{self._hop_b}, {self._dest_b}\n"
            f"    JUMP @{self._hop_b}, {self._entry_b}\n"
            + restore +
            "    HALT\n"
            "track_c:\n"
            "    MOVE R0, R{data:face_c}\n"
            "    MOVE [FACE], R0\n"
            + (""
               if self._dest_c is None else
               "    MOVE R0, R{in:relay}\n"
               f"    WRITE @{self._hop_c}, {self._dest_c}\n")
            + f"    JUMP @{self._hop_c}, {self._entry_c}\n"
            + restore +
            "    HALT\n"
        )
        data = [DataWord("face_a", fa, address=1),
                DataWord("face_b", fb, address=2),
                DataWord("face_c", fc, address=3)]
        if self._restore_face is not None:
            data.append(DataWord(
                "face_r", self._FACE.get(self._restore_face, 0), address=4))
        return {0: CellProgram(
            inputs=[Port("relay")],
            outputs=[Port("out")],
            entries=[EntryPoint("track_a"), EntryPoint("track_b"),
                     EntryPoint("track_c")],
            data=data,
            state=[],
            assembly_template=tmpl,
        )}

    def process_reference(self, input_samples: np.ndarray) -> np.ndarray:
        # A relay passes its input through unchanged.
        return np.asarray(input_samples, dtype=np.uint16)

    def reset(self):
        pass
