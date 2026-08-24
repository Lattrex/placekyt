# SPDX-License-Identifier: GPL-3.0-or-later
"""ChirpSyncBlock — see :class:`ChirpSyncBlock`."""
from typing import Dict

import numpy as np

from ..block import CellProgram, DataWord, EntryPoint, Port, StateVar
from ._base import BlockInterface, KyttarBlock


class ChirpSyncBlock(KyttarBlock):
    """
    CSS preamble sync — a K-consecutive-equal-argmax run detector.

    (placeKYT-native; no stock GNU Radio streaming counterpart — the golden is
    the independent Python state machine below.)

    Sits downstream of the CSS receive spine's peak picker
    (dechirp → FFT → magnitude → :class:`BinArgmaxBlock`): during a preamble (a
    run of consecutive ``s = 0`` up-chirps) the argmax index stream repeats the
    SAME peak bin frame after frame (bin 0-adjacent when symbol-aligned), so
    preamble detection is a run-length test on the index stream. For each input
    index word the block emits ONE output word (1:1)::

        x == previous index  ->  run += 1   (saturating at K)
        x != previous index  ->  previous = x, run = 1
        output: the LOCKED BIN (= the run's index value) if run >= K,
                else the NO-SYNC sentinel 0xFFFF (raw -1)

    PACKED-WORD OUTPUT CONVENTION (pinned, gated): sync flag and locked bin
    share one word — the SIGN BIT is the (inverted) sync flag and the value is
    the locked bin. ``out = 0xFFFF`` (-1, sign set) means NO SYNC;
    ``out >= 0`` means SYNC ASSERTED and ``out`` IS the locked bin (a legal
    argmax index is 0..32767, so the sentinel can never collide). The flag
    asserts on the K-th consecutive equal index and DE-ASSERTS (re-arms) the
    moment the run breaks — the block reports "preamble present NOW"; the
    demod latches the rising edge + locked bin as its symbol reference.

    Pinned conventions (each gated):
      * Equality is EXACT 16-bit word equality (raw index words).
      * The run counter SATURATES at K (a preamble longer than K stays locked
        with no counter overflow, for arbitrarily long preambles).
      * A run must be K consecutive EQUAL values: the first sample of any new
        value counts as run = 1 (so K = 1 locks on every sample — the
        degenerate pass-through).
      * On a mismatch the run FULLY re-arms to the NEW value (run = 1) — no
        credit is carried (the no-reset-on-mismatch mutant is gated).

    HONEST LIMITATION (documented, by design): this block does NOT do
    fractional-timing alignment — it is integer-symbol-boundary sync only. A
    timing offset within a symbol shifts the dechirped peak bin; the
    system-level handling is that the demod TRACKS THE LOCKED BIN reported
    here as the symbol reference (bin 0 exactly only when aligned), rather
    than this block deriving a timing correction.

    Datapath (single cell): previous-index register + saturating run counter —
    the ZCR previous-sample / BinArgmax counter idioms. Raw-word I/O (the
    crc16/BinArgmax convention): input is the argmax index word (dtype short),
    output is the packed sync word (dtype short; -1 = no sync).

    Parameters:
      * ``k`` — the run length: sync asserts after ``k`` consecutive equal
        indices (default 4, the classic short-preamble configuration).
        Integer 1..32767 (the run counter compares signed against k); raises
        outside.

    Interface: entry R1, input R0 (one raw index word per trigger), output one
    packed word per trigger (1:1, delay 0).
    """
    CATEGORY = "demodulation"
    TAGS = ["chirp", "css", "sync", "preamble", "detector", "demodulation"]

    NO_SYNC_WORD = 0xFFFF

    _interface = BlockInterface(
        entry_address=1, input_registers=[0], output_registers=[0])

    def __init__(self, name: str, k: int = 4):
        if int(k) != k:
            raise ValueError(f"k must be an integer run length; got {k!r}.")
        if not (1 <= int(k) <= 32767):
            raise ValueError(
                "k must be 1..32767 (the run counter saturates at k and "
                f"compares signed against it); got {k}.")
        super().__init__(name, k=int(k))
        self._k = int(k)

    @property
    def cell_count(self) -> int:
        return 1

    @property
    def k(self) -> int:
        return self._k

    @property
    def interface(self) -> BlockInterface:
        return self._interface

    def build_cell_programs(self) -> Dict[int, CellProgram]:
        """Previous-index register + saturating run counter, one word/trigger.

        Equality is ``SUB prev, xs`` + ``BR.Z`` (exact 16-bit equality — the
        difference is 0 mod 2^16 iff the words are equal). The run counter
        SATURATES at k: the pre-increment ``CMP run, kword; BR.GE locked``
        short-circuits an already-locked run, so ``run`` never exceeds k and
        can never overflow on a long preamble. Both compares are the
        overflow-corrected signed SLT branch (run and k are 0..32767, always
        in signed range). EXIT-CELL RULE: conditional branches only (a GOTO
        would be rewritten by the output-handoff pass) — the two emit paths
        each carry their own ``{write}/{jump}`` pair with a terminal HALT on
        the fall-through path (the INV-13 two-path structure). State registers
        pinned explicitly (INV-33).
        """
        return {0: CellProgram(
            inputs=[Port("idx", register=0)],
            outputs=[Port("out")],
            entries=[EntryPoint("default")],
            data=[
                DataWord("one", 1, address=1),
                DataWord("zero", 0, address=2),
                DataWord("kword", self._k, address=3),
                DataWord("sent", self.NO_SYNC_WORD, address=4),
            ],
            state=[
                StateVar("xs", register=5),
                StateVar("prev", register=6, initial_value=0),
                StateVar("run", register=7, initial_value=0),
            ],
            assembly_template="""\
start:
    MOVE R{state:xs}, R{in:idx}
    ; exact 16-bit equality: prev - x == 0 iff equal
    SUB R{state:prev}, R{state:xs}
    BR.Z same
    ; new value: re-arm the run on it (run counts from 0 -> 1 below)
    MOVE R{state:prev}, R{state:xs}
    MOVE R{state:run}, R{data:zero}
same:
    ; saturate-at-k: an already-locked run stays locked, run never overflows
    CMP R{state:run}, R{data:kword}
    BR.GE locked
    ADD R{state:run}, R{data:one}
    MOVE R{state:run}, R0
    CMP R{state:run}, R{data:kword}
    BR.GE locked
    ; below k: emit the NO-SYNC sentinel (0xFFFF = raw -1)
    MOVE R0, R{data:sent}
    {write:out}
    {jump:out}
    HALT
locked:
    ; sync asserted: emit the LOCKED BIN (the run's index value)
    MOVE R0, R{state:prev}
    {write:out}
    {jump:out}
""",
        )}

    # -------------------------------------------------------------- reference
    def process_reference_q15(self, idx_words) -> list:
        """Bit-exact reference on raw 16-bit index words: one packed word per
        input — the locked bin once ``k`` consecutive equal indices have been
        seen (run saturating at k), else 0xFFFF."""
        out = []
        prev, run = 0, 0
        for w in idx_words:
            w = int(w) & 0xFFFF
            if w == prev:
                run = min(run + 1, self._k)
            else:
                prev, run = w, 1
            out.append(prev if run >= self._k else self.NO_SYNC_WORD)
        return out

    def process_reference(self, input_samples) -> np.ndarray:
        """Float view of the packed-word stream (words as signed int16:
        -1 = no sync, >= 0 = the locked bin). Input floats are interpreted as
        index words via round(v*32768) — the harness quantization, exact for
        indices 0..32767."""
        words = [max(-32768, min(32767, int(round(float(v) * 32768.0))))
                 & 0xFFFF for v in np.asarray(input_samples).reshape(-1)]
        ref = self.process_reference_q15(words)
        return np.asarray(
            [(w - 0x10000 if w >= 0x8000 else w) for w in ref],
            dtype=np.int16)

    def reset(self):
        """No cross-call state (each stream starts un-synced)."""
        pass
