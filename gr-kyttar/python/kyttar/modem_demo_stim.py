"""Stimulus generators for the full-duplex BPSK modem demo flowgraph
(bpsk_modem.grc). Imported as a plain Python module (like coherent_demo_stim) so
the .grc has no fragile inline epy source. Two streams share one chip:

  * TX: a bit vector fed to the PSK symbol mapper (stream_id 'tx'). The TX chain
    (mapper -> upsampler -> RRC -> I/Q upconvert) produces the real passband.
  * RX: an RRC-shaped BPSK I/Q burst with carrier + timing offset (stream_id
    'rx'), fed to the complex matched filter -> Costas -> Gardner -> slicer, which
    recovers the bits.

The two GR sources both target the shared input port x16_in; the placeKYT server
resolves each stream to its own block's entry/hop/data-registers and demuxes the
two output streams by tag (see engine.port_config.stream_targets). The RX burst
reuses coherent_demo_stim so the recovered-bit demo matches the proven RX path.
"""

import random

from . import coherent_demo_stim as _rx


def tx_bits(n_bits, seed=7):
    """A repeatable 0/1 bit vector for the TX chain (fed to the PSK mapper)."""
    random.seed(seed)
    return [random.randint(0, 1) for _ in range(int(n_bits))]


def rx_burst(n_syms, sps=2, beta=0.35, span=6, toff=0.45, foff=0.008, seed=5):
    """The RX I/Q burst (RRC-BPSK, carrier + timing offset) — delegates to the
    proven coherent_demo_stim.burst so the live RX recovery matches that demo."""
    return _rx.burst(n_syms, sps=sps, beta=beta, span=span,
                     toff=toff, foff=foff, seed=seed)


def rx_burst_len(n_syms, sps=2, span=6):
    """Complex-sample count rx_burst returns (for the RX Source's Burst length)."""
    return _rx.burst_len(n_syms, sps=sps, span=span)
