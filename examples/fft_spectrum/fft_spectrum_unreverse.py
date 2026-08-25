"""UN-REVERSE the chip's bin order — the example's display contract.

The placed FFT emits DIF order with deliberately NO reorder buffer:
output slot k of each 64-sample frame carries frequency bin
bit_reverse_6(k). Slot 52 is bin 11; slot 1 is bin 32. A plot fed the
raw slots is a SCRAMBLED spectrum that still looks plausible, which is
exactly why this block exists and why the gate pins the mapping.

It also strips the block's 63-sample pipeline LATENCY: the first 63
outputs of a burst are the deterministic startup values of the
zero-initialized pipeline, not a frame. Burst boundaries are found by
counting samples modulo burst_len, which is what the repeat-burst
source delivers.

Emits one natural-order 64-bin power VECTOR per whole frame.
"""
import numpy as np
from gnuradio import gr


def _bitrev(n):
    bits = int(n).bit_length() - 1
    out = []
    for k in range(n):
        r, v = 0, k
        for _ in range(bits):
            r = (r << 1) | (v & 1)
            v >>= 1
        out.append(r)
    return np.array(out, dtype=np.intp)


class blk(gr.basic_block):
    def __init__(self, n_fft=64, latency=63, burst_len=255):
        gr.basic_block.__init__(
            self, name='FFT bin un-reverse',
            in_sig=[np.float32], out_sig=[(np.float32, int(n_fft))])
        self.n = int(n_fft)
        self.latency = int(latency)
        self.burst = int(burst_len)
        # slot -> natural bin. The map is an involution, so this same
        # permutation both scrambles and unscrambles; applying it to the
        # emitted slots yields natural bin order.
        self.rev = _bitrev(self.n)
        self.buf = np.zeros(0, dtype=np.float32)
        self.pos = 0          # index of buf[0] within the current burst

    def forecast(self, noutput_items, ninputs):
        return [noutput_items * self.n + self.n] * ninputs

    def general_work(self, input_items, output_items):
        x = input_items[0]
        self.buf = np.concatenate([self.buf, np.asarray(x, np.float32)])
        self.consume(0, len(x))
        out = output_items[0]
        made = 0
        while made < len(out):
            # position of buf[0] inside its burst
            off = self.pos % self.burst
            if off < self.latency:            # inside the startup transient
                drop = min(self.latency - off, len(self.buf))
                if drop == 0:
                    break
                self.buf = self.buf[drop:]
                self.pos += drop
                continue
            if len(self.buf) < self.n:
                break
            frame = self.buf[:self.n]
            self.buf = self.buf[self.n:]
            self.pos += self.n
            nat = np.zeros(self.n, dtype=np.float32)
            nat[self.rev] = frame
            out[made][:] = nat
            made += 1
        return made
