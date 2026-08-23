# SPDX-License-Identifier: GPL-3.0-or-later
"""BinArgmaxBlock — see :class:`BinArgmaxBlock`."""
import numpy as np
from typing import Dict

from ..block import CellProgram, Port, EntryPoint, StateVar, DataWord
from ._base import KyttarBlock, BlockInterface


class BinArgmaxBlock(KyttarBlock):
    """
    Framewise argmax — a placeKYT-native ([Kyttar]) peak-detection block, NO
    stock GNU Radio streaming counterpart (the golden reference is
    ``numpy.argmax`` over each frame).

    For each non-overlapping frame of ``n`` consecutive input words it emits ONE
    output word: the ZERO-BASED INDEX (``0 .. n-1``) of the frame's maximum
    value. Rate-reducing ``n``:1 (the Crc16Block / KeepOneInNBlock pattern); a
    trailing partial frame (< ``n`` words) is never emitted. The standard use is
    peak-picking over a magnitude/power bin vector (e.g. the output of a
    magnitude-squared stage): the winning bin index IS the detected symbol.

    Pinned conventions (each is gated by a dedicated test):

    * **Tie: FIRST occurrence wins.** The running maximum updates on a
      STRICTLY-greater comparison, so an earlier equal maximum keeps the index —
      exactly ``numpy.argmax``'s documented behavior ("indices corresponding to
      the first occurrence are returned"). An all-equal frame emits index 0.
    * **Comparison is SIGNED Q15.** Values compare as signed 16-bit words
      (on-chip via ``CMP`` + ``BR.GE`` on the SLT flag ``N ^ V``, so the compare
      is overflow-corrected and correct for ANY signed pair, including
      −32768 vs +32767). Callers feeding magnitudes/powers (all non-negative)
      get the natural behavior; negative inputs are legal and compare signed.
    * **State fully resets between frames.** The running max re-arms to the
      signed minimum (−32768, which any strictly-greater value displaces; an
      all-−32768 frame correctly emits index 0 because the argmax register
      re-arms to index 0) and the position/argmax counters reload, so adjacent
      frames are completely independent.

    Raw-word output convention (like Crc16Block / the slicer family): the output
    word IS the integer index ``0 .. n-1`` — an index, NOT a Q15 sample. A GRC
    float scope shows it as ``index / 32768``; put a ×32768 rescale in front of
    a value display. The GRC binding types the output ``short``.

    Supported range (loud raise outside it): ``n`` must be an integer in
    ``1 .. 32768`` (2^15). The frame length drives a 16-bit down-counter
    register, and the emitted index must stay a non-negative 16-bit word
    (0 .. 32767), which caps ``n`` at 32768. ``n = 1`` is the degenerate
    1:1 frame (every output is 0).

    Datapath (single cell): a running-max compare-and-update — the CMP +
    conditional-branch idiom of the slicer family — plus the Crc16Block frame
    down-counter. Per input word::

        if maxv < x (signed, strict):  maxv = x;  cm = cnt   # cnt = n - i here
        cnt -= 1
        if cnt == 0:  emit (n - cm)  == the frame argmax index
                      re-arm: cnt = n, cm = n, maxv = -32768

    Recording the down-counter value ``cm`` at the winning sample (instead of an
    up-counting index) makes the whole frame loop one counter; the index is
    recovered at emit time as ``n - cm`` (16-bit wraparound arithmetic keeps
    this exact for every ``n`` up to 32768).

    Interface:
        - Entry: R1
        - Input: R0 (one signed Q15 word per trigger)
        - Output: the raw index word, emitted once per ``n`` inputs.
    """
    CATEGORY = "demodulation"
    TAGS = ["argmax", "peak", "bin", "detector", "demodulation", "feature"]

    _interface = BlockInterface(
        entry_address=1, input_registers=[0], output_registers=[0])

    def __init__(self, name: str, n: int = 128):
        """Framewise argmax over non-overlapping frames of ``n`` words.

        Args:
            n: frame length; one index word (0 .. n-1) is emitted per ``n``
                input words (default 128). Integer, 1 .. 32768 (2^15) — the
                16-bit frame counter and the non-negative 16-bit index word cap
                it; anything else raises.
        """
        if int(n) != n:
            raise ValueError(
                f"n must be an integer frame length; got {n!r}.")
        if not (1 <= int(n) <= 32768):
            raise ValueError(
                "n must be 1..32768 (16-bit frame down-counter; the emitted "
                f"index 0..n-1 must fit a non-negative 16-bit word); got {n}.")
        super().__init__(name, n=int(n))
        self._n = int(n)

    @property
    def cell_count(self) -> int:
        return 1

    @property
    def n(self) -> int:
        return self._n

    @property
    def interface(self) -> BlockInterface:
        return self._interface

    def build_cell_programs(self) -> Dict[int, CellProgram]:
        """Running max + argmax + frame down-counter, one input word per trigger.

        The strictly-greater signed compare is ``CMP maxv, x`` + ``BR.GE skip``:
        SLT (``N ^ V``) is the overflow-corrected signed less-than, so the update
        fires iff ``maxv < x`` for ANY signed pair (the sentinel −32768 vs a
        +32767 input included) — a plain N-flag test would mis-order opposite-sign
        pairs whose difference overflows 16 bits. Ties (``maxv == x``) take the
        GE skip, so the FIRST occurrence keeps the index.

        ``SUB``'s Z flag survives the flag-preserving ``MOVE`` store (guide §4.8),
        so the frame boundary is the Crc16Block ``SUB / MOVE / BR.NZ`` tail. The
        emit path computes ``n - cm`` straight into R0, WRITEs it, and re-arms all
        three frame registers before falling through to ``done`` (the resolver's
        auto-HALT at R31). State registers are pinned explicitly (INV-33) well
        below the instruction region (14 instruction words at the top).
        """
        n_word = self._n & 0xFFFF          # 32768 encodes as 0x8000
        return {0: CellProgram(
            inputs=[Port("sample", register=0)],
            outputs=[Port("out")],
            entries=[EntryPoint("default")],
            data=[
                DataWord("one", 1, address=1),
                DataWord("nfrm", n_word, address=2),
                DataWord("minw", 0x8000, address=3),   # signed minimum sentinel
            ],
            state=[
                StateVar("xs", register=4),
                StateVar("maxv", register=5, initial_value=0x8000),
                StateVar("cm", register=6, initial_value=n_word),
                StateVar("cnt", register=7, initial_value=n_word),
            ],
            assembly_template="""\
start:
    MOVE R{state:xs}, R{in:sample}
    ; strictly-greater SIGNED update: fire iff maxv < x (SLT = N^V, overflow-
    ; corrected); ties take the GE skip -> FIRST occurrence keeps the index
    CMP R{state:maxv}, R{state:xs}
    BR.GE skip
    MOVE R{state:maxv}, R{state:xs}
    MOVE R{state:cm}, R{state:cnt}     ; cnt (pre-decrement) = n - i
skip:
    SUB R{state:cnt}, R{data:one}
    MOVE R{state:cnt}, R0
    BR.NZ done
    ; frame complete: emit the ZERO-BASED index n - cm, then re-arm
    SUB R{data:nfrm}, R{state:cm}
    {write:out}
    {jump:out}
    MOVE R{state:cnt}, R{data:nfrm}
    MOVE R{state:cm}, R{data:nfrm}
    MOVE R{state:maxv}, R{data:minw}
done:
""",
        )}

    # -------------------------------------------------------------- reference
    def process_reference_q15(self, x_q15) -> list:
        """Bit-exact reference on raw 16-bit words: interpret each word as
        SIGNED Q15, emit ``numpy.argmax`` (first-occurrence ties) per complete
        frame of ``n``; a trailing partial frame is dropped."""
        words = np.asarray([int(w) & 0xFFFF for w in x_q15], dtype=np.uint16)
        signed = words.view(np.int16)
        n_out = len(signed) // self._n
        return [int(np.argmax(signed[j * self._n:(j + 1) * self._n]))
                for j in range(n_out)]

    def process_reference(self, input_samples) -> np.ndarray:
        """Float reference: ``numpy.argmax`` per complete frame of ``n``
        (first-occurrence ties), trailing partial frame dropped. NOTE: float
        values whose difference is below one Q15 LSB can quantize to a tie
        on-chip; the bit-exact gate is :meth:`process_reference_q15` on the
        quantized words."""
        x = np.asarray(input_samples).reshape(-1)
        n_out = len(x) // self._n
        return np.asarray(
            [int(np.argmax(x[j * self._n:(j + 1) * self._n]))
             for j in range(n_out)], dtype=np.int16)

    def reset(self):
        """No cross-call state (each stream starts a fresh frame)."""
        pass
