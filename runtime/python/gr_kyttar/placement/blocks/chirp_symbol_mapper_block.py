# SPDX-License-Identifier: GPL-3.0-or-later
"""ChirpSymbolMapperBlock — see :class:`ChirpSymbolMapperBlock`."""
import math
from typing import Dict

import numpy as np

from ._base import KyttarBlock
from .pack_k_bits_block import PackKBitsBlock


class ChirpSymbolMapperBlock(PackKBitsBlock):
    """
    CSS symbol mapper — pack ``log2(m)`` bits into one RAW symbol word.

    Consumes ``k = log2(m)`` input bits (one 0/1 word each, LSB read — a stray
    high bit is masked exactly like GR ``pack_k_bits_bb``) and emits ONE raw
    symbol word in ``0..m-1``, **MSB-first** (the FIRST input bit becomes the
    MOST significant bit of the symbol — pinned and mutation-gated). The output
    word feeds :class:`ChirpGeneratorBlock` directly (its raw-symbol input).

    HONEST ANCESTRY: this is :class:`PackKBitsBlock` (GNU Radio
    ``blocks.pack_k_bits_bb``) re-parameterized — the identical single-cell
    MSB-first accumulate/count/emit program, with (a) the alphabet expressed as
    ``m`` (the CSS parameterization; ``k`` is derived), and (b) the GR 8-bit
    output-byte cap lifted: a symbol is a raw 16-bit word, so ``m`` up to
    ``2^15`` packs in-register (``pack_k_bits_bb`` stops at k=8 only because its
    output item is one uint8). For ``m <= 256`` the block IS bit-for-bit
    ``pack_k_bits_bb(log2 m)`` and is gated against the live GR block; larger
    ``m`` is gated against the same-recurrence numpy golden.

    Rate-REDUCING (``k`` in -> 1 out); a trailing partial group of fewer than
    ``k`` bits is never emitted (GR's ``floor(nin/k)`` convention). Single cell.

    Parameters:
      * ``m`` — symbol alphabet size (power of two, 4 ≤ m ≤ 32768; matches the
        chirp generator's ``m``). ``k = log2(m)`` bits are packed per symbol.
    """
    CATEGORY = "modulation"
    TAGS = ["chirp", "css", "symbol_mapper", "pack", "bits", "modulation"]

    def __init__(self, name: str, m: int = 128):
        m = int(m)
        # k = log2(m) must be 2..15: a 1-bit "alphabet" (m=2) is legal in
        # principle but degenerate (pass-through) — allow it anyway; the real
        # caps are m a power of two and k <= 15 (the 16-bit symbol word).
        if m < 2 or (m & (m - 1)) or m > 32768:
            raise ValueError(
                f"ChirpSymbolMapperBlock: m must be a power of two in "
                f"[2, 32768] (k = log2 m bits pack into one 16-bit symbol "
                f"word); got {m}")
        # Bypass PackKBitsBlock.__init__ (its 1<=k<=8 bound is GR's uint8
        # OUTPUT-item cap, which does not apply to a raw 16-bit symbol word).
        KyttarBlock.__init__(self, name, m=m)
        self._m = m
        self._k = int(round(math.log2(m)))

    @property
    def m(self) -> int:
        return self._m

    # build_cell_programs() is inherited VERBATIM from PackKBitsBlock — the
    # bit-serial MSB-first packer (mask LSB; word = word<<1 | bit; emit + reset
    # every k triggers). Only the derived k differs.

    def process_reference(self, input_bits: np.ndarray) -> np.ndarray:
        """MSB-first pack of each k-bit group into one raw symbol word (numpy
        golden). Unlike PackKBits' byte reference there is NO 0xFF mask — the
        symbol is a k-bit word (k up to 15); a trailing partial group is
        dropped."""
        inp = np.asarray(input_bits).astype(np.int64)
        k = self._k
        n_out = len(inp) // k
        out = np.zeros(n_out, dtype=np.uint16)
        for j in range(n_out):
            w = 0
            for i in range(k):
                w = ((w << 1) | (int(inp[j * k + i]) & 1)) & 0xFFFF
            out[j] = w
        return out.view(np.int16) if n_out else np.zeros(0, dtype=np.int16)
