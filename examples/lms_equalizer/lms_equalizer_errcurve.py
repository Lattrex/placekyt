# SPDX-License-Identifier: GPL-3.0-or-later
"""Convergence learning curve — |y - nearest decision point|, smoothed.

The quantitative "it is getting better": per equalized symbol, the
distance to the nearest QPSK decision point (+-0.7071 +-0.7071j),
lightly EMA-smoothed so the decay reads as a curve. Restarts high at
every burst boundary (the chip cold-starts per batch) and decays as
the on-chip taps adapt.
"""
import numpy as np
from gnuradio import gr


class blk(gr.sync_block):
    def __init__(self):
        gr.sync_block.__init__(self, name='Distance to Decision',
                               in_sig=[np.complex64],
                               out_sig=[np.float32])
        self.d = 0.70710678
        self.ema = 0.0

    def work(self, input_items, output_items):
        x = input_items[0]
        dec = (np.sign(x.real) * self.d
               + 1j * np.sign(x.imag) * self.d)
        err = np.abs(x - dec).astype(np.float32)
        out = output_items[0]
        a = 0.15
        e = self.ema
        for i in range(len(err)):
            e = (1.0 - a) * e + a * float(err[i])
            out[i] = e
        self.ema = e
        return len(err)
