# SPDX-License-Identifier: GPL-3.0-or-later
"""CSS DEMOD MAP — turn the chip's raw argmax BIN INDEX into the SYMBOL.

The on-chip FFT16 emits its 16 bins in bit-reversed (DIF) order, so the
winning bin index i is not the symbol: the symbol is ``s = brev4(i)`` — the
4-bit reversal of the index. This block applies exactly that map, and nothing
else, to the raw index words the kyttar sink delivers.

An embedded block on purpose: this is host-side DISPLAY glue (the same map
the example's gate applies), not chip DSP — stock converter ids are chip-side
splice markers to the placeKYT importer, so a plain converter cannot be used
here.

in0  = the kyttar sink output. A complex-input chain egresses RAW word floats,
       so each item is already the integer index 0..15 (no q15 rescale).
out0 = the decoded symbol s = brev4(index), as a float 0..15 — plot it
       against the transmitted symbol sequence.
"""
import numpy as np
from gnuradio import gr

# brev4: 4-bit reversal, the FFT16 DIF output-order map (index -> symbol).
_BREV4 = [((i & 1) << 3) | ((i & 2) << 1) | ((i & 4) >> 1) | ((i & 8) >> 3)
          for i in range(16)]


class blk(gr.sync_block):
    def __init__(self, n=16):
        gr.sync_block.__init__(self, name='CSS Bin -> Symbol (brev4)',
                               in_sig=[np.float32], out_sig=[np.float32])
        self.n = int(n)

    def work(self, input_items, output_items):
        x = input_items[0]
        idx = np.rint(x.astype(np.float64)).astype(np.int64) & 0xFFFF
        out = np.zeros(len(idx), dtype=np.float32)
        for k, i in enumerate(idx):
            out[k] = float(_BREV4[i]) if 0 <= i < 16 else -1.0
        output_items[0][:len(out)] = out
        return len(out)
