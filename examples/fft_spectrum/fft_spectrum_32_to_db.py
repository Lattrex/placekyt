"""Per-bin power -> dBFS, for a log spectrum display.

The chip already emits POWER (re^2+im^2 in Q15), so this is just
10*log10 with a floor. The FFT/32 scale puts a full-scale coherent
bin at power 1.0 = 0 dBFS, so the plot reads directly in dBFS.
"""
import numpy as np
from gnuradio import gr


class blk(gr.sync_block):
    def __init__(self, n_fft=32, floor_db=-90.0):
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
