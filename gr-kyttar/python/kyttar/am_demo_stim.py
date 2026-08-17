# SPDX-License-Identifier: GPL-3.0-or-later
"""Stimulus generators for the DSB-AM transceiver demo flowgraph
(examples/am_transceiver/am_transceiver.grc). Imported as a plain Python module
(like modem_demo_stim) so the .grc has no fragile inline epy source.

TWO independent chains share ONE chip, demuxed by stream_id (exactly the BPSK
modem pattern):

  * TX (modulator, stream_id 'tx'): audio -> tx_src -> oscMix(fc) -> tx_sink.
    The on-chip oscillator-mixer forms the suppressed-carrier DSB-AM passband
    ``s = audio*cos(2*pi*fc*t)`` and streams it back on x16_out (tag 'tx').

  * RX (demodulator, stream_id 'rx'): am_rf -> rx_src -> oscMix(fc) -> LowPass
    -> Gain x2 -> rx_sink. The coherent product detector
    ``y = s*cos = audio*(1+cos 2fc)/2`` low-passed to ``audio/2`` then x2 gives
    back the audio on x16_out (tag 'rx').

The RX input burst is the SAME DSB-AM passband the TX chain produces (generated
here from the identical audio + carrier), so the RX chain independently recovers
the transmitted audio — a true end-to-end transceiver loop across the shared
chip, not a single serial pass. Both GR sources target x16_in; the placeKYT
server resolves each stream to its own block's entry/hop/data-register and demuxes
the two output streams by tag (see engine.port_config.stream_targets).
"""

import math


FS = 32000.0     # sample rate (Hz)
FC = 6000.0      # AM carrier (Hz)


def _tones(n, fs=FS):
    """The transmitted baseband audio: two sine tones (800 + 1500 Hz)."""
    return [0.5 * math.cos(2 * math.pi * 800.0 * k / fs)
            + 0.3 * math.cos(2 * math.pi * 1500.0 * k / fs)
            for k in range(int(n))]


def tx_audio(n_samp, fs=FS):
    """The TX baseband audio vector (fed to the TX oscillator-mixer, stream 'tx')."""
    return _tones(int(n_samp), fs)


def am_passband(n_samp, fs=FS, fc=FC):
    """The DSB-AM (suppressed-carrier) passband ``audio*cos(2*pi*fc*t)`` — the RX
    chain's input burst (stream 'rx'). This is exactly what the TX oscillator-mixer
    emits, regenerated here so the RX demodulator recovers the transmitted audio."""
    aud = _tones(int(n_samp), fs)
    return [a * math.cos(2 * math.pi * fc * k / fs) for k, a in enumerate(aud)]


# A qtgui time_sink in FREE-trigger mode only flushes a completed frame once a
# sample arrives PAST the frame boundary. On a FINITE burst a `size` EQUAL to the
# delivered count leaves the last frame un-flushed => a flat plot. So the time-sink
# Number of Points must be a bit BELOW the delivered count (same guard the modem
# demo uses).
_PLOT_GUARD = 16


def points(n_samp):
    """Number of Points for the audio time-sinks: a guard below the burst length so
    the FREE-trigger frame completes and flushes on the finite batch."""
    return max(1, int(n_samp) - _PLOT_GUARD)
