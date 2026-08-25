"""Per-bin power -> dBFS, for a log spectrum display.

The spectrum block upstream computes |z|^2 from the chain tail's
interleaved I/Q at the q15/32768 scale, so this is just 10*log10 with a
floor. The transform's FFT/128 scale puts a full-scale coherent bin at
power 1.0 = 0 dBFS, so the plot reads directly in dBFS.
"""
import numpy as np
from gnuradio import gr


class blk(gr.sync_block):
    def __init__(self, n_fft=128, floor_db=-90.0):
        gr.sync_block.__init__(
            self, name='Power to dBFS',
            in_sig=[(np.float32, int(n_fft))],
            out_sig=[(np.float32, int(n_fft))])
        self.floor = float(floor_db)
        self.eps = 10.0 ** (self.floor / 10.0)

    def work(self, input_items, output_items):
        x = np.maximum(np.asarray(input_items[0]), self.eps)
        output_items[0][:] = (10.0 * np.log10(x)).astype(np.float32)
        return len(output_items[0])
