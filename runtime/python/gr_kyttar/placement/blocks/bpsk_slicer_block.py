# SPDX-License-Identifier: GPL-3.0-or-later
"""BPSKSlicerBlock — see :class:`BPSKSlicerBlock`."""
import numpy as np
from ..block import CellProgram, Port, EntryPoint, StateVar, DataWord
from typing import Dict
from ._base import KyttarBlock, BlockInterface, assemble_to_words, float_to_q15, q15_to_float


class BPSKSlicerBlock(KyttarBlock):
    """
    BPSK Hard-Decision Slicer Block — GNU Radio ``digital.binary_slicer_fb``.

    Turns a recovered real sample (an LLR / matched-filter output) into a hard
    output bit by testing its sign, EXACTLY as GNU Radio ``digital.binary_slicer_fb``
    does (binary_slicer_fb.h): ``input < 0 -> 0``, ``input >= 0 -> 1`` — the ``0``
    tie goes to bit ``1``:

        sample <  0  ->  bit 0
        sample >= 0  ->  bit 1   (INCLUDING sample == 0 -> 1)

    This is the receiver's final decision stage: the output stream is hard bits,
    not soft values. It is a MEMORYLESS feed-forward slicer — bit-exact with GR at
    the decision boundary (no RMS normalization, no group delay).

    Single cell. The decision uses the N (negative) flag from ``CMP R, 0``.

    Hardware deviations from digital.binary_slicer_fb:
        - ``out_mode`` (Kyttar-ONLY ergonomic packing extension; NOT a GR param).
          GR emits ONE byte (0/1) PER input sample. ``out_mode="bit"`` reproduces
          that exactly (one word per sample, value 0/1 — the GR-equivalent, verified
          mode). ``out_mode="byte"``/``"word"`` additionally PACK 8/16 sliced bits
          MSB-first into one output word to cut output-port pressure in a long
          receiver chain — a hardware/plumbing convenience with no GR counterpart.
          The GR-equivalence gate runs against ``out_mode="bit"``. See the manifest
          ``HW-DEVIATION:`` note and ``out_mode`` in ``__init__``.

    Interface:
        - Entry: R1
        - Input: R31 (recovered sample / LLR)
        - Output: bit (0x0000 or 0x0001), one per sample in ``bit`` mode.
    """
    CATEGORY = "demodulation"
    TAGS = ["slicer", "hard_decision", "bpsk", "demodulation"]

    _interface = BlockInterface(entry_address=1, input_registers=[31], output_registers=[31])

    # Output packing modes: how many sliced bits are accumulated (MSB-first) before
    # a word is emitted on the output port. 'bit' = emit every bit (one word per
    # sample — useful for watching a bit toggle, but maximal port pressure); 'byte'
    # = pack 8 then emit; 'word' = pack 16 then emit (least port pressure — the
    # production default). A trailing partial group (<N bits) is dropped, exactly
    # like the end-of-chain packing slicer in CoherentBPSKRxBlock.
    _BITS_PER = {"bit": 1, "byte": 8, "word": 16}

    def __init__(self, name: str, out_mode: str = "word"):
        # HARDWARE DEVIATION: out_mode is a Kyttar-only OUTPUT-PACKING extension and
        # has NO GNU Radio counterpart — digital.binary_slicer_fb emits one byte
        # (0/1) per sample and takes no parameters. 'bit' == the GR-exact 1:1
        # byte-per-sample behaviour (this is what the GR-equivalence gate verifies);
        # 'byte'/'word' pack 8/16 sliced bits MSB-first to reduce output-port
        # pressure in a long receiver chain. Documented loudly per INV-0.
        if out_mode not in self._BITS_PER:
            raise ValueError(
                f"BPSKSlicerBlock out_mode must be one of {sorted(self._BITS_PER)}, "
                f"got {out_mode!r}")
        super().__init__(name, out_mode=out_mode)
        self._out_mode = out_mode
        self._bits_per = self._BITS_PER[out_mode]

    @property
    def cell_count(self) -> int:
        return 1

    @property
    def out_mode(self) -> str:
        return self._out_mode

    @property
    def interface(self) -> BlockInterface:
        return self._interface

    def build_cell_programs(self) -> Dict[int, CellProgram]:
        """Hard-decision slice on the sign of the input (GNU Radio
        ``digital.binary_slicer_fb``: ``sample >= 0 -> 1``, ``sample < 0 -> 0``,
        tie at 0 -> 1), with configurable output packing (``out_mode``: 'bit' /
        'byte' / 'word').

        ``CMP R{in:llr}, R{data:zero}`` computes ``sample - 0`` and sets the N
        (negative) flag iff ``sample < 0``. The bit defaults to 1; ``BR.NN`` (branch
        if NOT negative, i.e. ``sample >= 0``, INCLUDING sample == 0) keeps it, and
        the fall-through (``sample < 0``) overwrites it with 0 — exactly GR's
        boundary. In ``bit`` mode the bit is emitted immediately (one word per
        sample). In ``byte``/``word`` mode the bit is packed MSB-first
        (``word = (word << 1) | bit``) and emitted only when ``count`` reaches
        8 / 16, then ``word`` and ``count`` reset (a trailing partial group is
        dropped). Single cell, single output face."""
        if self._bits_per == 1:
            # 'bit' mode: slice and emit every sample. GR binary_slicer_fb:
            # sample >= 0 -> 1 (tie at 0 -> 1); sample < 0 -> 0.
            return {0: CellProgram(
                inputs=[Port("llr", register=0)],
                outputs=[Port("out")],
                entries=[EntryPoint("default")],
                data=[
                    DataWord("zero", 0x0000, address=1),
                    DataWord("bit0", 0x0000, address=2),
                    DataWord("bit1", 0x0001, address=3),
                ],
                assembly_template="""\
start:
    CMP R{in:llr}, R{data:zero}
    MOVE R0, R{data:bit1}
    BR.NN emit
    MOVE R0, R{data:bit0}
emit:
    {write:out}
    {jump:out}
""",
            )}
        # 'byte'/'word' mode: pack `bits_per` sliced bits MSB-first, emit on the
        # boundary. State persists across calls (word accumulator + bit counter).
        return {0: CellProgram(
            inputs=[Port("llr", register=0)],
            outputs=[Port("out")],
            entries=[EntryPoint("default")],
            data=[
                DataWord("zero", 0x0000, address=2),
                DataWord("one", 0x0001, address=3),
                DataWord("nbits", self._bits_per, address=4),
            ],
            state=[StateVar("bit"), StateVar("word"), StateVar("count")],
            assembly_template="""\
start:
    MOVE R{state:bit}, R{data:one}
    CMP R{in:llr}, R{data:zero}
    BR.NN packed
    MOVE R{state:bit}, R{data:zero}
packed:
    SHL R{state:word}, #1
    OR R0, R{state:bit}
    MOVE R{state:word}, R0
    MOVE R0, R{state:count}
    ADD R0, R{data:one}
    MOVE R{state:count}, R0
    CMP R{state:count}, R{data:nbits}
    BR.NZ done
    MOVE R0, R{state:word}
    {write:out}
    {jump:out}
    MOVE R{state:word}, R{data:zero}
    MOVE R{state:count}, R{data:zero}
done:
""",
        )}

    def process_reference(self, input_samples: np.ndarray) -> np.ndarray:
        """Reference: hard-decision bit from the sign of each sample, packed per
        ``out_mode``. Matches GNU Radio ``digital.binary_slicer_fb``:
        ``sample >= 0 -> 1`` (tie at 0 -> 1), ``sample < 0 -> 0``. 'bit' returns
        one 0/1 word per sample; 'byte'/'word' pack 8/16 bits MSB-first into each
        output word, dropping a trailing partial group (matching the on-chip
        emit-on-boundary behaviour). Inputs are interpreted as SIGNED samples (a
        Q15 word like 0x8000 is negative)."""
        arr = np.asarray(input_samples, dtype=np.int32)
        bits = np.where(arr < 0, 0, 1).astype(np.int16)
        n = self._bits_per
        if n == 1:
            return bits
        full = (len(bits) // n) * n
        words = []
        for i in range(0, full, n):
            w = 0
            for b in bits[i:i + n]:
                w = ((w << 1) | int(b)) & 0xFFFF
            words.append(w)
        # A packed word can exceed +32767 (it's a bit pattern, not a signed value).
        # Carry it through uint16, then reinterpret the bits as int16 so the dtype
        # matches the per-sample 'bit' path without clipping.
        return np.asarray(words, dtype=np.uint16).view(np.int16)

    def reset(self):
        """No state to reset."""
        pass
