# SPDX-License-Identifier: GPL-3.0-or-later
"""Stimulus for the complex_math demo flowgraph (complex_math.grc).

Two ANALYTIC complex tones (different frequencies, Q15-grid-snapped so the
chip and any float golden compute over exactly the same words) drive three
two-stream complex arithmetic blocks placed on ONE chip:

  * AddCC      : a + b — the two tones superpose (a beat envelope);
  * SubCC      : a - b — the same superposition with the second tone flipped;
  * MultiplyCC : a * b — THE MIXER: multiplying analytic tones ADDS their
                 frequencies, so the product is a single clean tone at
                 f_a + f_b (the classic up-conversion beat-note).

Each block gets its OWN ingress stream pair (six streams total — 'sum' /
'b_add', 'diff'/'b_sub', 'prod'/'b_mul' — the same duplicated-ingress
pattern the fec_link demo uses for its tx/txcrc pair): a complex ingress
stream cannot fan out on-chip (the importer's auto-spliced fan-out relay is
single-rail), so each landing cell receives its own copy of the tone and
pairs the two per-sample packets with its counting join, in any arrival
order — the two-external-complex-stream client contract. The block's
recovered stream rides its FIRST input's stream reply (the deterministic
out_tag-ownership rule in engine.port_config.stream_targets), so each sink
names the block's first-port stream: 'sum', 'diff', 'prod'.

Frequencies are exact DFT bins of the burst length so the mixer claim is
assertable bin-sharp: f_a = 10/256, f_b = 17/256, product at 27/256 cycles
per sample.
"""

import math

N = 256                    # samples per burst (a power of 2: exact DFT bins)
BIN_A = 10                 # tone A at 10/256 cyc/sample
BIN_B = 17                 # tone B at 17/256 cyc/sample
AMP = 0.45                 # per-tone amplitude (sums stay inside Q15)


def _snap(v):
    """Snap a float to the Q15 grid (the exact words the chip receives)."""
    return max(-32768, min(32767, round(v * 32768.0))) / 32768.0


def _tone(bin_k, n=N, amp=AMP):
    return [complex(_snap(amp * math.cos(2 * math.pi * bin_k * t / n)),
                    _snap(amp * math.sin(2 * math.pi * bin_k * t / n)))
            for t in range(int(n))]


def tone_a(n=N):
    """Analytic tone A (exp(+j*2*pi*BIN_A*t/N), Q15-snapped)."""
    return _tone(BIN_A, n)


def tone_b(n=N):
    """Analytic tone B (exp(+j*2*pi*BIN_B*t/N), Q15-snapped)."""
    return _tone(BIN_B, n)


def n_samples():
    """Complex-sample count per burst (the Source burst length)."""
    return N
