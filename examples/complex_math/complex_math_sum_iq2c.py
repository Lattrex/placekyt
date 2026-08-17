# SPDX-License-Identifier: GPL-3.0-or-later
"""Interleaved I,Q floats -> complex pairs (2:1).

The kyttar sink emits a complex chain's recovered stream as INTERLEAVED
I,Q float32 (its output port is always float); this embedded block
reassembles gr_complex for the scope. An embedded block
(not blocks.float_to_complex) on purpose: stock converter ids are chip-side
splice markers to the placeKYT importer, and this display glue must stay
host-side."""
import numpy as np
from gnuradio import gr


class blk(gr.decim_block):
    def __init__(self):
        gr.decim_block.__init__(self, name='IQ Pairs to Complex',
                                in_sig=[np.float32],
                                out_sig=[np.complex64], decim=2)

    def work(self, input_items, output_items):
        n = len(output_items[0])
        x = input_items[0][:2 * n]
        output_items[0][:] = x[0::2] + 1j * x[1::2]
        return n
