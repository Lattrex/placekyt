"""Stimulus generators for the SSB Weaver transceiver demo flowgraph
(examples/ssb_weaver/ssb_weaver.grc). Imported as a plain Python module (like
am_demo_stim / fm_demo_stim) so the .grc has no fragile inline epy source AND —
crucially — so the RX chain has its OWN batch stimulus instead of being daisy-
chained off the TX sink.

SELF-CONTAINED: imports ONLY math + numpy (like am/fm stim). It does NOT import
gr_kyttar / weaver_builder — those live in the placeKYT venv, NOT in the system
GNU Radio Python that runs GRC, so importing them here would break the flowgraph.
The SSB passband is a float64 Weaver (third-method) reference, faithful enough for
the RX to recover the audio.

TWO independent chains share ONE chip, demuxed by stream_id (the AM/FM/BPSK
transceiver pattern):

  * TX (modulator, stream_id 'tx'): audio -> tx_src -> ComplexMixer(-fa) ->
    ComplexLowPass -> IQUpconvert(fc) -> tx_sink  ==> SSB passband on x16_out.
  * RX (demodulator, stream_id 'rx'): ssb_rf -> rx_src -> ComplexMixer(-fc) ->
    ComplexLowPass -> IQUpconvert(fa) -> Gain x4 -> rx_sink  ==> recovered audio.

The RX input burst (``ssb_passband``) is the SAME SSB passband the TX chain
produces — regenerated HERE from the identical audio — so the RX chain
independently recovers the transmitted audio. A TRUE transceiver: two independent
chip chains sharing the port by tag, NOT a tx_sink -> rx_src daisy chain (that
imported as a bogus x16_out -> x16_in net that could never route).
"""

import math

import numpy as np

FS = 32000.0        # sample rate (Hz)
FA = 1500.0         # Weaver audio-band centre (Hz)
FC = 6000.0         # carrier (Hz)
FCUT = 1200.0       # baseband low-pass cutoff (Hz)


def _tones(n, fs=FS):
    """The transmitted baseband audio: two in-band tones (a simple 'voice')."""
    t = np.arange(int(n)) / fs
    return ((0.5 * np.sin(2 * np.pi * 800.0 * t)
             + 0.3 * np.sin(2 * np.pi * 1800.0 * t)) * 0.7).astype(np.float64)


def tx_audio(n_samp, fs=FS):
    """The TX baseband audio vector (fed to the TX chain, stream 'tx')."""
    return _tones(int(n_samp), fs).tolist()


def _lpf(x, fs=FS, fcut=FCUT):
    """A simple real/complex low-pass (moving-average-of-length matched to fcut) —
    a faithful Weaver baseband filter in pure numpy. Works on complex arrays too."""
    # FIR window ~ one period of the cutoff; odd length, Hamming-weighted sinc.
    ntaps = int(2 * round(fs / fcut)) | 1
    n = np.arange(ntaps) - (ntaps - 1) / 2
    h = np.sinc(2 * fcut / fs * n) * np.hamming(ntaps)
    h = h / h.sum()
    return np.convolve(x, h, mode="same")


def ssb_passband(n_samp, fs=FS, fa=FA, fc=FC):
    """The SSB (USB, third-method / Weaver) passband — the RX chain's input burst
    (stream 'rx'). Real vector. Faithful float64 Weaver TX:

        b   = audio * exp(-j*wa*t)          # down-mix to complex baseband
        bl  = LPF(b)                        # keep one sideband's image band
        ssb = Re{ bl * exp(+j*wc*t) }       # up-mix; real part = USB passband

    This is exactly what the on-chip Weaver TX emits, so the RX demodulator
    (ComplexMixer(-fc) -> LPF -> IQUpconvert(fa)) recovers the transmitted audio."""
    n = int(n_samp)
    t = np.arange(n) / fs
    m = _tones(n, fs)
    b = m * np.exp(-1j * 2 * np.pi * fa * t)          # complex baseband
    bl = _lpf(b, fs, FCUT)
    ssb = np.real(bl * np.exp(1j * 2 * np.pi * fc * t))
    # Normalise to a comfortable Q15 range (peak ~0.8).
    pk = float(np.max(np.abs(ssb))) or 1.0
    return (ssb * (0.8 / pk)).astype(np.float64).tolist()


# A qtgui time_sink in FREE-trigger mode only flushes a completed frame once a
# sample arrives PAST the frame boundary. On a FINITE burst a `size` EQUAL to the
# delivered count leaves the last frame un-flushed => a flat plot. So the time-sink
# Number of Points must be a bit BELOW the delivered count (same guard the AM/FM
# demos use).
_PLOT_GUARD = 16


def points(n_samp):
    """Number of Points for the time-sinks: a guard below the burst length so the
    FREE-trigger frame completes and flushes on the finite batch."""
    return max(1, int(n_samp) - _PLOT_GUARD)
