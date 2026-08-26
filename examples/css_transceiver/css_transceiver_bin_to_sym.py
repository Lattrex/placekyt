# SPDX-License-Identifier: GPL-3.0-or-later
"""CSS DEMOD MAP + PER-SEGMENT SPLITTER + LIVE SER — what the two verdict
scopes and the SER readout draw.

Three jobs, and the last two are why this block exists at all.

**1. The decode map.** The on-chip FFT16 emits its 16 bins in bit-reversed (DIF)
order, so the winning bin index ``i`` is not the symbol: the symbol is
``s = brev4(i)`` — the 4-bit reversal of the index.

**2. The reference rides the SAME stream as the decode.** This is the
load-bearing part. The shipped burst is TWO segments through one chain —
segment A at +10 dB (which decodes exactly) and segment B at -10 dB (the
on-chip NEGATIVE CONTROL, which must not). Displaying that correctly is not a
matter of taste; four display defects in a row made a perfect run look broken:

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
  * **THE CONTROL READ AS A DEFECT.** Splitting A and B onto four traces of ONE
    scope fixed the arithmetic but not the meaning: a viewer looking at one axis
    where half the points lock and half scatter reads "half of it is broken",
    because nothing on screen says the scatter is the intended result. Reported
    verbatim as "the +10 dB works flawlessly but the -10 dB doesn't work at
    all" — a description of the demo working exactly as designed.

The cure for the first two: derive the reference HERE, from the item index of
the very stream being decoded, so it cannot drift, and blank each segment
outside its own words.

  * **HALF THE TRACES WERE NEVER DRAWN.** A qtgui ``time_sink`` channel set to
    line style 0 (NoPen, "markers only") paints NOTHING on any channel above
    channel 0. Reproduced standalone with two vector sources of *different*
    amplitude — so it is not occlusion — on a real X display as well as
    offscreen. The old scope used NoPen on all four of its traces, so this
    block's decoded outputs were among the ones the window never rendered.

The cure for the first two: derive the reference HERE, from the item index of
the very stream being decoded, so it cannot drift, and blank each segment
outside its own words.

The cure for the third: give each segment its **own scope**, so each carries
its own verdict in its own title, and publish the **measured SER of each
segment as a live number** next to them. A mismatch inside a panel that says
"NEGATIVE CONTROL — this is the expected result" cannot be misread as breakage.

The cure for the fourth lives in the ``.grc``, not here, but it constrains how
these channels are wired: both traces of a panel use a solid pen (never style
0), the REFERENCE goes to scope input 0 as a wide circle, and the DECODED
output goes to input 1 — the last-painted channel — as a narrower X, so an
exact overlay shows the X inside the ring instead of hiding the decode under
its own reference.

Frame layout (one batch = ``n_words`` items, ``seg_words`` per segment)::

    word 0            the framing-latency word — carries no data symbol
    word 1 .. n_data  symbol f arrives in word f+1

so a segment's reference trace is ``[BLANK] + preamble + message symbols``.

Outputs (all six are the SAME length and the SAME phase — one stream in, one
item out per item, so nothing can slide against anything else):

  out0  segment A decoded  (chip)          blank outside segment A
  out1  segment A transmitted (reference)  blank outside segment A
  out2  segment B decoded  (chip)          blank outside segment B
  out3  segment B transmitted (reference)  blank outside segment B
  out4  segment A measured SER  (0.0..1.0, held between passes)
  out5  segment B measured SER  (0.0..1.0, held between passes)

``BLANK`` is NaN: GNU Radio's time sink leaves a gap rather than drawing a
misleading 0 or -1, so each segment's trace occupies only its own half of the
sweep. Both scopes span the WHOLE batch and share one x axis, so the two
half-filled panels stack into the burst's timeline: A fills the left half, B
fills the right, and which is which is never in doubt.

The SER channels (out4/out5) are the honest measurement, not a constant: each
counts the plotted mismatches of the segment it belongs to over that segment's
own symbols, publishes the value once the pass is COMPLETE, and holds it steady
while the next pass accumulates. They exist so the two numbers the README quotes
are ON SCREEN, where nobody has to take the README's word for them.
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
            self, name='CSS Bin -> Symbol + A/B split + SER',
            in_sig=[np.float32],
            out_sig=[np.float32] * 6)
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
        # Live SER accumulators, one pair per segment: [errors, symbols scored]
        # for the segment CURRENTLY being received, and the last COMPLETE value
        # that the number sink holds while the other segment is in flight.
        self._acc = [[0, 0], [0, 0]]
        self._ser = [BLANK, BLANK]
        self._complete = [False, False]

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

        a_dec, a_ref, b_dec, b_ref, a_ser, b_ser = (
            output_items[i] for i in range(6))
        a_dec[:nout] = np.where(in_a, sym, BLANK)
        a_ref[:nout] = np.where(in_a, ref, BLANK)
        b_dec[:nout] = np.where(in_b, sym, BLANK)
        b_ref[:nout] = np.where(in_b, ref, BLANK)

        # --- the live SER readout, scored on exactly the plotted points -------
        # Walk item by item: cheap (one batch is tens of items) and it keeps the
        # segment-boundary bookkeeping obvious rather than clever.
        #
        # What is PUBLISHED is the last COMPLETE pass over the segment, held
        # steady while the next pass accumulates — so the number a viewer reads
        # is a whole segment's SER, not a partial ratio that swings between 0
        # and 1 mid-segment as symbols arrive. The one exception is the very
        # first pass, before any complete result exists: there the running ratio
        # is shown so the readout is never blank on screen for a whole batch.
        for j in range(nout):
            seg = 0 if in_a[j] else 1
            if pos[j] % w == 0:
                # First word of a segment: the previous pass over this segment
                # is complete, so publish it and start the next one.
                e, tot = self._acc[seg]
                if tot:
                    self._ser[seg] = e / tot
                    self._complete[seg] = True
                self._acc[seg] = [0, 0]
            if drawn[j]:
                self._acc[seg][1] += 1
                d, r = sym[j], ref[j]
                if np.isnan(d) or int(d) != int(r):
                    self._acc[seg][0] += 1
                if not self._complete[seg]:
                    self._ser[seg] = self._acc[seg][0] / self._acc[seg][1]
            a_ser[j] = self._ser[0]
            b_ser[j] = self._ser[1]

        self._k = (self._k + nout) % period
        return nout
