# SPDX-License-Identifier: GPL-3.0-or-later
"""PackKBitsBlock — see :class:`PackKBitsBlock`."""
import numpy as np
from ..block import CellProgram, Port, EntryPoint, StateVar, DataWord
from typing import Dict
from ._base import KyttarBlock, BlockInterface


class PackKBitsBlock(KyttarBlock):
    """
    Pack K bits into a byte — GNU Radio ``blocks.pack_k_bits_bb``.

    Consumes ``k`` input bytes (each carrying ONE bit in its LSB, 0/1) and packs
    them **MSB-first** (GR's fixed convention) into a single output byte:

        byte = (b[0] << (k-1)) | (b[1] << (k-2)) | ... | (b[k-1] << 0)

    equivalently, per input bit, ``acc = (acc << 1) | (bit & 1)`` and the byte is
    emitted after every ``k`` bits. The very first input bit becomes the MOST
    significant bit of the output byte — verified BIT-EXACT against the live
    ``blocks.pack_k_bits_bb`` for k = 2..8.

    This is a RATE-REDUCING block: ``k`` inputs -> 1 output. Like GNU Radio, a
    trailing partial group of fewer than ``k`` bits at the end of the stream is
    NOT emitted (GR's ``pack_k_bits::work`` only produces ``floor(nin/k)`` bytes).

    GR reads only the LOW bit of each input item (``pack_k_bits`` masks
    ``d_bits[i] & 1``); this block does the same (``AND sample, 1``), so a stray
    high bit on an input word is ignored exactly as in GR.

    Architecture: single cell (1 cell). State is the 16-bit packing accumulator
    (``word``) and the bit counter (``count``); one input bit in per trigger, one
    packed byte out every ``k`` triggers.

    Interface:
        - Entry: R1
        - Input: R0 (one bit per sample, LSB used)
        - Output: the packed byte (MSB-first), emitted once per ``k`` inputs.
    """
    CATEGORY = "byte_operators"
    TAGS = ["pack", "bits", "byte", "framing", "conversion"]

    _interface = BlockInterface(
        entry_address=1, input_registers=[0], output_registers=[0])

    def __init__(self, name: str, k: int = 8):
        """Pack ``k`` MSB-first bits into one byte.

        Args (mirror ``blocks.pack_k_bits_bb`` VERBATIM):
            k: number of input bits packed into each output byte (MSB-first).
        """
        if not (1 <= int(k) <= 8):
            # GR pack_k_bits_bb requires 0 < k <= 8 (it packs into ONE uint8
            # output byte, so k > 8 has no valid byte to pack into). Mirror that
            # bound exactly (a genuine 8-bit-output limit, same as GR).
            raise ValueError(
                f"PackKBitsBlock requires 1 <= k <= 8 (packs into one byte); got k={k}.")
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
        """Bit-serial MSB-first packer, the exact ``blocks.pack_k_bits_bb`` work
        loop:

            bit  = sample & 1                 ; GR masks the input LSB
            word = (word << 1) | bit          ; MSB-first accumulate
            count = count + 1
            if count == k:  emit word;  word = 0;  count = 0

        Single cell, single output face. State (``word``, ``count``) persists
        across triggers; a trailing partial group (< k bits) is never emitted,
        matching GR's ``floor(nin/k)`` output count.
        """
        return {0: CellProgram(
            inputs=[Port("sample", register=0)],
            outputs=[Port("out")],
            entries=[EntryPoint("default")],
            data=[
                DataWord("one", 0x0001, address=1),
                DataWord("zero", 0x0000, address=2),
                DataWord("kbits", self._k, address=3),
            ],
            state=[StateVar("bit"), StateVar("word"), StateVar("count")],
            assembly_template="""\
start:
    ; bit = sample & 1  (GR masks the input LSB). AND writes its result to R0
    ; (guide §logic: R0 = SRC_A & SRC_B; the operand register is NOT modified),
    ; so copy R0 back into `bit` — otherwise the OR below would leak the input's
    ; high bits (a masked value must actually be stored, not just computed).
    MOVE R{state:bit}, R{in:sample}
    AND R{state:bit}, R{data:one}
    MOVE R{state:bit}, R0
    ; word = (word << 1) | bit   (MSB-first accumulate)
    SHL R{state:word}, #1
    OR R0, R{state:bit}
    MOVE R{state:word}, R0
    ; count = count + 1
    MOVE R0, R{state:count}
    ADD R0, R{data:one}
    MOVE R{state:count}, R0
    ; if count == k: emit the packed byte, then reset word + count
    CMP R{state:count}, R{data:kbits}
    BR.NZ done
    MOVE R0, R{state:word}
    {write:out}
    {jump:out}
    MOVE R{state:word}, R{data:zero}
    MOVE R{state:count}, R{data:zero}
done:
""",
        )}

    def process_reference(self, input_bits: np.ndarray) -> np.ndarray:
        """Bit-exact reference for ``blocks.pack_k_bits_bb``.

        Packs each group of ``k`` input bits MSB-first into one output byte
        (``byte = (((...(0<<1|b0)<<1|b1)...)<<1|b_{k-1})``), reading only the LOW
        bit of each input item (GR masks ``& 1``). A trailing partial group of
        fewer than ``k`` bits is dropped (GR emits ``floor(nin/k)`` bytes). State
        (accumulator + counter) is reset per call (each call is a fresh stream).
        """
        inp = np.asarray(input_bits).astype(np.int64)
        k = self._k
        n_out = len(inp) // k
        out = np.zeros(n_out, dtype=np.uint16)
        for j in range(n_out):
            byte = 0
            for i in range(k):
                byte = ((byte << 1) | (int(inp[j * k + i]) & 1)) & 0xFF
            out[j] = byte
        return out.view(np.int16) if n_out else np.zeros(0, dtype=np.int16)

    def reset(self):
        """No cross-call state to reset (each stream packs from a clean accumulator)."""
        pass
