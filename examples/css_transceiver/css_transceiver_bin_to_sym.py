# SPDX-License-Identifier: GPL-3.0-or-later
"""CSS DEMOD MAP + SEGMENT SPLITTER — what the DECODED-vs-TRANSMITTED scope draws.

Two jobs, and the second one is why this block exists at all.

**1. The decode map.** The on-chip FFT16 emits its 16 bins in bit-reversed (DIF)
order, so the winning bin index ``i`` is not the symbol: the symbol is
``s = brev4(i)`` — the 4-bit reversal of the index.

**2. The reference rides the SAME stream as the decode.** This is the
load-bearing part. The shipped burst is TWO segments through one chain —
segment A at +10 dB (which decodes exactly) and segment B at -10 dB (the
on-chip NEGATIVE CONTROL, which must not). Displaying that correctly is not a
matter of taste; two earlier display defects made a perfect decode look broken:

  * **PHASE DRIFT.** The old flowgraph drew the transmitted reference from a
    SEPARATE ``vector_source`` on channel 1 of the scope. That source free-runs
    while the chip's stream is gated by the simulator's batch turnaround —
    measured at **+27.9 % more items over the same run**. A QT time_sink pulls
    the same count from both channels, so the red reference SLID against the
    blue decode. Once offset by as few as 3 items, **22 of segment A's 24
    perfectly-decoded symbols render as mismatches**. The plot smeared while the
    chip was bit-exact.
  * **A/B CONFLATION.** Even at zero offset, segment B's intentional garbage was
    drawn on the same axis as segment A's perfect lock, with nothing to tell a
    viewer which half was which. 17 of 50 plotted points disagreed *by design*
    and the plot did not say so.

The cure for both: derive the reference HERE, from the item index of the very
stream being decoded, and split the two segments onto their OWN channels. The
reference cannot drift from the decode because it is generated per item from the
same counter, and segment A's lock is never overplotted by segment B's collapse.

Frame layout (one batch = ``n_words`` items, ``seg_words`` per segment)::

    word 0            the framing-latency word — carries no data symbol
    word 1 .. n_data  symbol f arrives in word f+1

so a segment's reference trace is ``[BLANK] + preamble + message symbols``.

Outputs (all four are the SAME length and the SAME phase — plot them together):

  out0  segment A decoded  (chip)          blank outside segment A
  out1  segment A transmitted (reference)  blank outside segment A
  out2  segment B decoded  (chip)          blank outside segment B
  out3  segment B transmitted (reference)  blank outside segment B

``BLANK`` is NaN: GNU Radio's time sink leaves a gap rather than drawing a
misleading 0 or -1, so each pair occupies only its own half of the sweep.
"""
import numpy as np
from gnuradio import gr

# brev4: 4-bit reversal, the FFT16 DIF output-order map (index -> symbol).
_BREV4 = [((i & 1) << 3) | ((i & 2) << 1) | ((i & 4) >> 1) | ((i & 8) >> 3)
          for i in range(16)]

BLANK = float("nan")


class blk(gr.sync_block):
    """n        = samples per chirp symbol == FFT size == alphabet (16)
    tx_syms     = the transmitted symbol sequence for ONE segment, on the output
                  word grid (element 0 is the framing-latency word)
    seg_words   = index words per segment
    """

    def __init__(self, n=16, tx_syms=(), seg_words=25):
        gr.sync_block.__init__(
            self, name='CSS Bin -> Symbol + A/B split',
            in_sig=[np.float32],
            out_sig=[np.float32, np.float32, np.float32, np.float32])
        self.n = int(n)
        self.seg_words = int(seg_words)
        # The per-segment reference trace, on the output word grid. Element 0 is
        # the framing-latency word (no data symbol) and is drawn BLANK.
        ref = [BLANK] + [float(s) for s in tx_syms]
        ref = ref[:self.seg_words]
        ref += [BLANK] * (self.seg_words - len(ref))
        self._ref = np.asarray(ref, dtype=np.float32)
        # Item counter modulo the whole two-segment batch — this is what keeps
        # the reference locked to the decode (see the module docstring).
        self._k = 0

    def work(self, input_items, output_items):
        x = input_items[0]
        nout = len(x)
        w = self.seg_words
        period = 2 * w

        idx = np.rint(x.astype(np.float64)).astype(np.int64) & 0xFFFF
        sym = np.full(nout, BLANK, dtype=np.float32)
        good = (idx >= 0) & (idx < 16)
        sym[good] = [float(_BREV4[i]) for i in idx[good]]

        # Position of every item within the two-segment batch.
        pos = (np.arange(nout, dtype=np.int64) + self._k) % period
        in_a = pos < w
        in_b = ~in_a
        ref = self._ref[pos % w]

        # A word the reference does not cover carries no data symbol (word 0 is
        # the framing-latency word). Blank the DECODE there too — plotting it
        # would put a lone unmatched point on an otherwise exact overlay and
        # invite precisely the "one symbol is wrong" question it is not.
        drawn = ~np.isnan(ref)
        sym = np.where(drawn, sym, BLANK)

        a_dec, a_ref, b_dec, b_ref = (output_items[i] for i in range(4))
        a_dec[:nout] = np.where(in_a, sym, BLANK)
        a_ref[:nout] = np.where(in_a, ref, BLANK)
        b_dec[:nout] = np.where(in_b, sym, BLANK)
        b_ref[:nout] = np.where(in_b, ref, BLANK)

        self._k = (self._k + nout) % period
        return nout
