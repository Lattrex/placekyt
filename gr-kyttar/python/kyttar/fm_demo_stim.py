"""Stimulus generators for the FM transceiver demo flowgraph
(examples/fm_transceiver/fm_transceiver.grc). Imported as a plain Python module
(like am_demo_stim / modem_demo_stim) so the .grc has no fragile inline epy source.

TWO independent chains share ONE chip, demuxed by stream_id (the BPSK-modem /
AM-transceiver pattern):

  * TX (modulator, stream_id 'tx'): audio -> tx_src -> frequency_modulator ->
    tx_sink. The on-chip VCO integrates the (scaled) audio into the instantaneous
    phase and emits the complex FM passband ``exp(j*phi)``, ``phi += sensitivity*
    audio``. Being a complex-output block, it egresses the I and Q rails INTERLEAVED
    on x16_out ([I0,Q0,I1,Q1,...]); the I rail is ``cos(phi)`` and Q is ``sin(phi)``.

  * RX (demodulator, stream_id 'rx'): fm_rf -> rx_src -> quadrature_demod ->
    rx_sink. The input is the COMPLEX FM passband (I/Q, streamed the PROVEN
    coherent-RX way: ``complex_in='complex'`` interleaves xi/xq per sample). The
    on-chip quadrature discriminator computes ``gain*arg(x[n]*conj(x[n-1]))`` and
    recovers the audio on x16_out (real).

The RX input burst (``fm_iq``) is the SAME complex FM the TX chain produces
(generated here from the identical audio + sensitivity), so the RX demodulator
independently recovers the transmitted audio — a true end-to-end transceiver loop
across the shared chip. Both GR sources target x16_in; the placeKYT server resolves
each stream to its own block's entry/hop/data-registers and demuxes the two output
streams by tag (see engine.port_config.stream_targets).
"""

import math


FS = 32000.0        # sample rate (Hz)
# Phase advance per unit input, radians (GR frequency_modulator_fc(sensitivity)).
# = 2*pi*f_dev/fs; with f_dev ~ 1500 Hz this is ~0.29 rad/sample — a healthy FM
# swing that stays well inside the block's sensitivity<=pi range.
SENSITIVITY = 2.0 * math.pi * 1500.0 / FS


def _tones(n, fs=FS):
    """The transmitted baseband audio: two sine tones (300 + 800 Hz). Kept LOW so
    the integrated FM phase swing per sample is modest (clean discrimination)."""
    return [0.6 * math.cos(2 * math.pi * 300.0 * k / fs)
            + 0.3 * math.cos(2 * math.pi * 800.0 * k / fs)
            for k in range(int(n))]


def tx_audio(n_samp, fs=FS):
    """The TX baseband audio vector (fed to the TX VCO, stream 'tx')."""
    return _tones(int(n_samp), fs)


def _fm_phase(n, fs=FS, sens=SENSITIVITY):
    """The integrated FM phase ``phi[k] = sum_{i<=k} sensitivity*audio[i]``. The VCO
    PRE-INCREMENTS: the phase advances by ``sensitivity*x[k]`` BEFORE emitting sample
    k (matching the on-chip phase cell, which accumulates then emits), so
    ``phi[k] = sum_{i=0..k} sensitivity*audio[i]``."""
    aud = _tones(int(n), fs)
    phi = []
    acc = 0.0
    for a in aud:
        acc += sens * a
        phi.append(acc)
    return phi


def fm_iq(n_samp, fs=FS):
    """The COMPLEX FM passband ``exp(j*phi)`` as an interleaved I/Q vector — the RX
    chain's input burst (stream 'rx'), streamed via ``complex_in='complex'``. This is
    exactly what the TX VCO emits, regenerated here so the RX discriminator recovers
    the transmitted audio. GNU Radio's complex vector source takes a list of complex
    numbers; return those (the source item type is complex)."""
    phi = _fm_phase(int(n_samp), fs)
    return [complex(math.cos(p), math.sin(p)) for p in phi]


def fm_real(n_samp, fs=FS):
    """The REAL part of the FM passband ``cos(phi)`` — what the TX chain emits on
    x16_out (the on-chip VCO's I rail). A finite real vector for the TX plot."""
    phi = _fm_phase(int(n_samp), fs)
    return [math.cos(p) for p in phi]


# A qtgui time_sink in FREE-trigger mode only flushes a completed frame once a
# sample arrives PAST the frame boundary. On a FINITE burst a `size` EQUAL to the
# delivered count leaves the last frame un-flushed => a flat plot. So the time-sink
# Number of Points must be a bit BELOW the delivered count (same guard the AM /
# modem demos use).
_PLOT_GUARD = 16


def points(n_samp):
    """Number of Points for the time-sinks: a guard below the burst length so the
    FREE-trigger frame completes and flushes on the finite batch."""
    return max(1, int(n_samp) - _PLOT_GUARD)
