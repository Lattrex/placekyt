# SPDX-License-Identifier: GPL-3.0-or-later
"""Convergence phase splitter — encode TIME as COLOR.

The QT constellation sink repaints only when its whole size-buffer is
full, so a single-stream display can never show WHERE the new points
are. Instead, split each 600-symbol burst into three phase streams
(early / adapting / converged) drawn as separately-colored overlays:
one glance shows the early scatter (red), the pull-in (magenta) and
the converged corners (green). Off-phase samples are parked outside
the visible window (the sync sink needs equal-rate inputs).
"""
import numpy as np
from gnuradio import gr


class blk(gr.sync_block):
    def __init__(self, burst=600, early=100, mid=250):
        gr.sync_block.__init__(self, name='Convergence Phases',
                               in_sig=[np.complex64],
                               out_sig=[np.complex64] * 3)
        self.burst = int(burst)
        self.early = int(early)
        self.mid = int(mid)
        self.k = 0
        self.park = 10 + 10j     # far outside the +-1.2 window

    def work(self, input_items, output_items):
        x = input_items[0]
        n = len(x)
        idx = (self.k + np.arange(n)) % self.burst
        bands = ((0, self.early), (self.early, self.mid),
                 (self.mid, self.burst))
        for p, (lo, hi) in enumerate(bands):
            out = output_items[p]
            out[:n] = self.park
            m = (idx >= lo) & (idx < hi)
            out[:n][m] = x[m]
        self.k = (self.k + n) % self.burst
        return n
