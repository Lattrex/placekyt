"""BPSKSlicerBlock — see :class:`BPSKSlicerBlock`."""
import numpy as np
from ..block import CellProgram, Port, EntryPoint, StateVar, DataWord
from typing import Dict
from ._base import KyttarBlock, BlockInterface, assemble_to_words, float_to_q15, q15_to_float


class BPSKSlicerBlock(KyttarBlock):
    """
    BPSK Hard-Decision Slicer Block.

    Turns a soft value (an LLR from :class:`SoftDemodulatorBlock`, or any signed
    sample) into a hard output bit by testing its sign:

        LLR >= 0  ->  bit 0   (the +1.0 / "0" BPSK symbol)
        LLR <  0  ->  bit 1   (the -1.0 / "1" BPSK symbol)

    This is the receiver's final decision stage: it closes the BPSK loop so the
    output stream is bits, not LLRs. Composed with the mapper+demod it is the
    identity for a clean channel:

        bit -> [mapper] -> +-1.0 -> [soft demod] -> +-LLR -> [slicer] -> bit

    Single cell. The decision uses the N (negative) flag from ``CMP R, 0`` —
    the same hard-decision pattern the DFE uses internally.

    Interface:
        - Entry: R1
        - Input: R31 (LLR / signed sample)
        - Output: bit (0x0000 or 0x0001)
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
        """Hard-decision slice on the sign of the input, with configurable output
        packing (``out_mode``: 'bit' / 'byte' / 'word').

        ``CMP R{in:llr}, R{data:zero}`` sets N when llr < 0; ``BR.NN`` then keeps
        bit 0, otherwise bit 1. In ``bit`` mode the bit is emitted immediately
        (one word per sample). In ``byte``/``word`` mode the bit is packed
        MSB-first (``word = (word << 1) | bit``) and emitted only when ``count``
        reaches 8 / 16, then ``word`` and ``count`` reset (a trailing partial
        group is dropped). Single cell, single output face."""
        if self._bits_per == 1:
            # 'bit' mode: slice and emit every sample (the original behaviour).
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
    MOVE R0, R{data:bit0}
    BR.NN emit
    MOVE R0, R{data:bit1}
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
    MOVE R{state:bit}, R{data:zero}
    CMP R{in:llr}, R{data:zero}
    BR.NN packed
    MOVE R{state:bit}, R{data:one}
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
        ``out_mode``. 'bit' returns one 0/1 word per sample; 'byte'/'word' pack
        8/16 bits MSB-first into each output word, dropping a trailing partial
        group (matching the on-chip emit-on-boundary behaviour)."""
        arr = np.asarray(input_samples, dtype=np.int32)
        bits = np.where(arr < 0, 1, 0).astype(np.int16)
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
