"""Stimulus generators for the SSB Weaver transceiver demo flowgraph
(examples/ssb_weaver/ssb_weaver.grc). Imported as a plain Python module (like
am_demo_stim / fm_demo_stim) so the .grc has no fragile inline epy source AND —
crucially — so the RX chain has its OWN batch stimulus instead of being daisy-
chained off the TX sink.

TWO independent chains share ONE chip, demuxed by stream_id (the AM/FM/BPSK
transceiver pattern):

  * TX (modulator, stream_id 'tx'): audio -> tx_src -> ComplexMixer(-fa) ->
    ComplexLowPass -> IQUpconvert(fc) -> tx_sink. Forms the SSB (Weaver / third-
    method) passband and streams it back on x16_out (tag 'tx').

  * RX (demodulator, stream_id 'rx'): ssb_rf -> rx_src -> ComplexMixer(-fc) ->
    ComplexLowPass -> IQUpconvert(fa) -> Gain x4 -> rx_sink. Recovers the audio
    on x16_out (tag 'rx').

The RX input burst (``ssb_passband``) is the SAME SSB passband the TX chain
produces — regenerated HERE from the identical audio through the verified Weaver
TX block references — so the RX chain independently recovers the transmitted audio.
This is a TRUE end-to-end transceiver: two independent chip chains sharing the port
by tag, NOT a TX-sink -> RX-source daisy chain (which imported as a bogus
x16_out -> x16_in net that could never route).
"""

import math
import os
import sys
from pathlib import Path

import numpy as np

# The Weaver TX physics + verified block references live in the example's builder.
_SSB_DIR = Path(__file__).resolve()
# gr-kyttar/python/kyttar/ssb_demo_stim.py -> repo root -> examples/ssb_weaver
for _up in _SSB_DIR.parents:
    _cand = _up / "examples" / "ssb_weaver"
    if _cand.is_dir():
        if str(_cand) not in sys.path:
            sys.path.insert(0, str(_cand))
        break

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

FS = 32000.0        # sample rate (Hz)
FA = 1500.0         # Weaver audio-band centre (Hz)
FC = 6000.0         # carrier (Hz)


def _tones(n, fs=FS):
    """The transmitted baseband audio: two in-band tones (a simple 'voice')."""
    t = np.arange(int(n)) / fs
    return ((0.5 * np.sin(2 * np.pi * 800.0 * t)
             + 0.3 * np.sin(2 * np.pi * 1800.0 * t)) * 0.7).astype(np.float64)


def tx_audio(n_samp, fs=FS):
    """The TX baseband audio vector (fed to the TX chain, stream 'tx')."""
    return _tones(int(n_samp), fs).tolist()


def _plan():
    from weaver_builder import WeaverPlan
    return WeaverPlan()


def _ssb_passband_real(n):
    """The REAL SSB (USB) passband the Weaver TX emits for the audio: run the audio
    through the VERIFIED Weaver TX block references (ComplexMixer(-fa) ->
    ComplexLowPass -> IQUpconvert(fc)). This is the exact signal the on-chip TX
    chain produces, so the RX recovers the transmitted audio."""
    import math as _m

    from gr_kyttar.placement.blocks.complex_mixer_block import ComplexMixerBlock
    from gr_kyttar.placement.blocks.iq_upconvert_block import IQUpconvertBlock
    from weaver_builder_cfir import _clpf, calibrate_phase_steps_cfir

    def _q2f(w):
        return float(w) / 32768.0

    plan = _plan()
    m = _tones(int(n))
    # Phase-compensation step counts (calibrated once against the Q15 reference).
    kfa, _kfc, _c, _s = calibrate_phase_steps_cfir(plan, m)
    fs, fa, fc = plan.fs, plan.fa, plan.fc
    ph_fa = 2 * _m.pi * (-fa) / fs * (1 + kfa)
    # TX down-mix (real audio -> complex I/Q), complex LPF, up-mix to SSB (real) —
    # EXACTLY the TX half of weaver_reference_cfir (the (a,b) Q15 pairs are rebuilt
    # into a complex array before the complex LPF, as that reference does).
    bpair = ComplexMixerBlock("txmix", sample_rate=fs, frequency=-fa,
                              phase=ph_fa).process_reference_q15(
        [complex(float(x), 0.0) for x in m])
    biq = np.array([complex(_q2f(a), _q2f(b)) for a, b in bpair])
    txl = _clpf(plan).process_reference(biq)
    ssb = IQUpconvertBlock("txup", sample_rate=fs,
                           frequency=fc).process_reference(txl)
    return np.array([_q2f(w) for w in ssb], dtype=np.float64)


def ssb_passband(n_samp, fs=FS):
    """The SSB passband — the RX chain's input burst (stream 'rx'). A REAL vector
    (the RX source is complex_in='float'; a float_to_complex + null Q form the
    complex baseband the RX ComplexMixer needs). This is exactly what the TX chain
    emits, regenerated so the RX demodulator recovers the transmitted audio."""
    return _ssb_passband_real(int(n_samp)).tolist()


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
