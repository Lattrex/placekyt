"""The chain tail's INTERLEAVED I/Q words -> a centred POWER SPECTRUM.

This is the example's display contract, and it exists because the raw
sink stream is NOT a spectrum. Four things separate them, and every one
of them is load-bearing:

1. DE-INTERLEAVE. Chain A's tail is a COMPLEX exit cell: it emits
   out_i then out_q from one cell, so the sink's float stream is
   I, Q, I, Q ... at the q15/32768 scale — two words per frequency
   bin. Plotting that stream directly is what the "spikes flow across
   the screen with time on the x axis" report was looking at: a time
   series of raw words, where a single bin's energy shows up as two
   adjacent samples and the bin INDEX is nowhere on the axis.

2. STRIP THE LATENCY. The transform's pipeline latency is 127 samples
   (64 from die 0, 63 from die 1), so the first 127 complex outputs of
   a burst are the deterministic startup values of the zero-initialised
   pipeline, not a frame. Burst boundaries are found by counting
   complex samples modulo burst_len.

3. UN-REVERSE. The placed FFT emits DIF order with deliberately NO
   reorder buffer: output slot k of each 128-sample frame carries
   frequency bin bit_reverse_7(k). Slot 72 is bin 9; slot 82 is bin
   37; slot 1 is bin 64. A plot fed the raw slots is a SCRAMBLED
   spectrum that still looks plausible, which is exactly why this
   block exists and why the gate pins the mapping.

4. FFTSHIFT, so the x axis can read in Hz and be MONOTONIC. Natural
   bin k of an N-point transform at sample rate fs is the frequency

       f(k) = k*fs/N          for k <  N/2   (positive frequencies)
       f(k) = (k-N)*fs/N      for k >= N/2   (negative frequencies)

   i.e. the natural-order vector runs 0 -> +fs/2 and then jumps to
   -fs/2 -> 0, which no single linear axis can label. Rolling the
   vector by N/2 puts bin N/2 first, so the emitted vector runs
   monotonically from -fs/2 in steps of fs/N and the sink's
   set_x_axis(-samp_rate/2, bin_hz) labels every point correctly.
   At fs = 32000 and N = 128 the step is 250 Hz, and the demo's two
   tones (natural bins 9 and 37) land at indices 9 + 64 = 73 ->
   -16000 + 73*250 = +2250 Hz = 9*250, and 37 + 64 = 101 ->
   -16000 + 101*250 = +9250 Hz = 37*250.

Emits one 128-bin POWER vector per whole frame, in ascending frequency
order from -fs/2. Power is |z|^2 at the FFT/128 scale, so a full-scale
coherent bin reads 1.0.
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
    def __init__(self, n_fft=128, latency=127, burst_len=384):
        gr.basic_block.__init__(
            self, name='FFT128 words to centred spectrum',
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
        self.pos = 0          # index of buf[0] in COMPLEX samples of the burst

    def forecast(self, noutput_items, ninputs):
        # two float words per complex bin
        return [2 * (noutput_items * self.n + self.n)] * ninputs

    def general_work(self, input_items, output_items):
        x = input_items[0]
        self.buf = np.concatenate([self.buf, np.asarray(x, np.float32)])
        self.consume(0, len(x))
        out = output_items[0]
        made = 0
        while made < len(out):
            # position of buf[0] inside its burst, in COMPLEX samples
            off = self.pos % self.burst
            if off < self.latency:            # inside the startup transient
                drop = min(self.latency - off, len(self.buf) // 2)
                if drop == 0:
                    break
                self.buf = self.buf[2 * drop:]
                self.pos += drop
                continue
            if off + self.n > self.burst:
                # THE BURST'S RAGGED TAIL. burst_len is not latency + k*n_fft
                # (384 vs 127 + 2*128 = 383), so a burst ends with fewer than
                # n_fft samples left over. Those are not a frame, and a frame
                # STRADDLING the boundary is 1 real sample plus 127 of the
                # NEXT burst's zero-fill transient — measured as an all-zero
                # "spectrum" painted as every third frame. Drop the remainder.
                drop = min(self.burst - off, len(self.buf) // 2)
                if drop == 0:
                    break
                self.buf = self.buf[2 * drop:]
                self.pos += drop
                continue
            if len(self.buf) < 2 * self.n:
                break
            frame = self.buf[:2 * self.n]
            self.buf = self.buf[2 * self.n:]
            self.pos += self.n
            # DE-INTERLEAVE: out_i, out_q per bin -> one complex slot
            bins = frame[0::2].astype(np.float64) \
                + 1j * frame[1::2].astype(np.float64)
            power = np.abs(bins) ** 2
            nat = np.zeros(self.n, dtype=np.float32)
            nat[self.rev] = power          # slots -> natural bin order
            centred = np.zeros(self.n, dtype=np.float32)
            centred[self.shift] = nat      # natural bins -> -fs/2 .. +fs/2
            out[made][:] = centred
            made += 1
        return made
