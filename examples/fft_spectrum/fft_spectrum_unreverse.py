"""UN-REVERSE the chip's bin order and CENTRE it on 0 Hz — the
example's display contract.

Two maps, applied in this order.

1. UN-REVERSE. The placed FFT emits DIF order with deliberately NO
   reorder buffer: output slot k of each 64-sample frame carries
   frequency bin bit_reverse_6(k). Slot 52 is bin 11; slot 1 is bin
   32. A plot fed the raw slots is a SCRAMBLED spectrum that still
   looks plausible, which is exactly why this block exists and why
   the gate pins the mapping.

2. FFTSHIFT, so the x axis can read in Hz and be MONOTONIC. Natural
   bin k of an N-point transform at sample rate fs is the frequency

       f(k) = k*fs/N          for k <  N/2   (positive frequencies)
       f(k) = (k-N)*fs/N      for k >= N/2   (negative frequencies)

   i.e. the natural-order vector runs 0 -> +fs/2 and then jumps to
   -fs/2 -> 0, which no single linear axis can label. Rolling the
   vector by N/2 puts bin N/2 first, so the emitted vector runs
   monotonically from -fs/2 in steps of fs/N and the sink's
   set_x_axis(-samp_rate/2, bin_hz) labels every point correctly.
   At fs = 32000 and N = 64 the step is 500 Hz and the demo tone,
   natural bin 11, lands at index 11 + 32 = 43 -> -16000 + 43*500 =
   +5500 Hz = 11*500. That IS the mapping the README states.

It also strips the block's 63-sample pipeline LATENCY: the first 63
outputs of a burst are the deterministic startup values of the
zero-initialized pipeline, not a frame. Burst boundaries are found by
counting samples modulo burst_len, which is what the repeat-burst
source delivers.

Emits one 64-bin power VECTOR per whole frame, in ascending frequency
order from -fs/2.
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
            self, name='FFT bins to centred spectrum',
            in_sig=[np.float32], out_sig=[(np.float32, int(n_fft))])
        self.n = int(n_fft)
        self.latency = int(latency)
        self.burst = int(burst_len)
        # slot -> natural bin. The map is an involution, so this same
        # permutation both scrambles and unscrambles; applying it to the
        # emitted slots yields natural bin order.
        self.rev = _bitrev(self.n)
        # natural bin -> position on the CENTRED (-fs/2 .. +fs/2) axis.
        # This is exactly np.fft.fftshift's permutation, written out so
        # the mapping is readable in the flowgraph.
        self.shift = (np.arange(self.n) + self.n // 2) % self.n
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
            nat[self.rev] = frame          # slots -> natural bin order
            centred = np.zeros(self.n, dtype=np.float32)
            centred[self.shift] = nat      # natural bins -> -fs/2 .. +fs/2
            out[made][:] = centred
            made += 1
        return made
