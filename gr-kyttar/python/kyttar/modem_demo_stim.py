# SPDX-License-Identifier: GPL-3.0-or-later
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


def tx_pb_len(n_bits):
    """Passband-word count the TX chain (mapper -> upsampler -> RRC -> I/Q
    upconvert) emits on x16_out for ``n_bits`` input bits.

    The chip TX chain emits a FIXED 4 words per input bit (empirically stable:
    32->128, 64->256, 100->400, 120->480)."""
    return 4 * int(n_bits)


# A qtgui time_sink in FREE-trigger mode only FLUSHES a completed frame once a
# sample arrives PAST the frame boundary, i.e. it needs strictly MORE than `size`
# samples to paint. On a FINITE batch burst (no trailing stream) a `size` EQUAL to
# the delivered count leaves the last frame un-flushed => a FLAT plot. And the RX
# recovered count can be one short of nominal on a warm chip (120 vs 119). So the
# time-sink Number of Points must be a bit BELOW the guaranteed delivered count:
# a full frame then always completes AND a trailing sample flushes it. This guard
# is the fix for the "recovered bits / TX passband plots are flat" bug.
_PLOT_GUARD = 16


def rx_bits_points(n_syms):
    """Number of Points for the RECOVERED-BITS time-sink. The RX chain recovers
    ~``n_syms`` bits (may be 1 short on a warm chip); a frame a guard below that
    always completes and flushes so the bit waveform PAINTS (see _PLOT_GUARD)."""
    return max(1, int(n_syms) - _PLOT_GUARD)


def tx_pb_points(n_bits):
    """Number of Points for the TX-PASSBAND time-sink: a guard below the emitted
    passband-word count (``tx_pb_len``) so its FREE-trigger frame completes and
    flushes on the finite burst (see _PLOT_GUARD)."""
    return max(1, tx_pb_len(n_bits) - _PLOT_GUARD)
