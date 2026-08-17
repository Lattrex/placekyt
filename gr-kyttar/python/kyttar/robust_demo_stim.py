# SPDX-License-Identifier: GPL-3.0-or-later
"""Stimulus for the robust_rx demo flowgraph (robust_rx.grc).

One raised-cosine (full-Nyquist) 2-sps BPSK burst carrying a LARGE carrier
offset — 0.18 cycles/sample, far beyond a Costas loop's pull-in — feeds TWO
receiver chains placed on the same chip:

  * 'rx'  : FLLBandEdge (coarse frequency recovery) -> Costas(order 2)
            -> BPSK slicer — locks and recovers the bits (BER 0).
  * 'ctl' : Costas(order 2) -> BPSK slicer alone (the coherent-receiver
            carrier-recovery core WITHOUT the FLL) — cannot pull 0.18
            cyc/sample and the recovered bits are garbage (the demo's
            on-screen negative control).

The burst is the EXACT stimulus class the FLL block's end-to-end chain gate
proved (verification/tests/test_fll_band_edge.py::
test_end_to_end_chain_with_negative_control): full raised-cosine (not RRC)
pulse shaping, so the symbol instants are ISI-free WITHOUT a matched filter
and the chain isolates the carrier-recovery story; same seed, same shaping,
same operating point foff=0.18.
"""

import numpy as np

# The proven operating point (test_fll_band_edge tier 5).
N_SYMS = 600
SPS = 2
ROLLOFF = 0.35
FOFF = 0.18
SEED = 5
AMP = 0.9


def tx_bits(n_syms=N_SYMS, seed=SEED):
    """The transmitted 0/1 bits (the same RNG draw rx_burst modulates)."""
    rng = np.random.default_rng(seed)
    return [int(b) for b in rng.integers(0, 2, int(n_syms))]


def rx_burst(n_syms=N_SYMS, sps=SPS, rolloff=ROLLOFF, foff=FOFF, seed=SEED,
             amp=AMP):
    """Full raised-cosine (Nyquist) 2-sps BPSK with a carrier offset — zero
    ISI at the symbol centers, band edges present for the FLL. Returns the
    complex sample list (len = n_syms*sps)."""
    rng = np.random.default_rng(seed)
    bits = rng.integers(0, 2, int(n_syms))
    syms = 2.0 * bits - 1.0
    n_rc = 41
    t = (np.arange(n_rc) - (n_rc - 1) / 2) / sps
    beta = rolloff
    rc = np.zeros(n_rc)
    for i, tt in enumerate(t):
        if abs(1 - (2 * beta * tt) ** 2) < 1e-9:
            rc[i] = (np.pi / 4) * np.sinc(1 / (2 * beta))
        else:
            rc[i] = (np.sinc(tt) * np.cos(np.pi * beta * tt)
                     / (1 - (2 * beta * tt) ** 2))
    up = np.zeros(int(n_syms) * sps)
    up[::sps] = syms
    shaped = np.convolve(up, rc)[(n_rc - 1) // 2:
                                 (n_rc - 1) // 2 + int(n_syms) * sps]
    shaped = shaped / np.max(np.abs(shaped)) * amp
    n = np.arange(len(shaped))
    return [complex(v) for v in shaped * np.exp(2j * np.pi * foff * n)]


def n_rx(n_syms=N_SYMS, sps=SPS):
    """Complex-sample count of rx_burst (the Source burst length)."""
    return int(n_syms) * sps


def n_rx_bits(n_syms=N_SYMS, sps=SPS):
    """Word count each chain's slicer emits (one hard bit per SAMPLE at
    2 sps; the symbol decisions are every other word)."""
    return int(n_syms) * sps
