"""Signal generators for the GRU modulation-classifier example.

Four classes of complex-baseband clips:

  0: SSB voice  -- upper-sideband analytic signal of a voice-proxy audio
                   (bandlimited noise with a slow syllabic envelope), the
                   complex-baseband view of the Weaver SSB chain in
                   examples/ssb_weaver (300..2700 Hz USB band).
  1: BPSK       -- RRC-shaped BPSK, sps=4, alpha=0.35 (the pulse-shaping
                   parameters of examples/bpsk_modem).
  2: 4-FSK      -- RRC-shaped 4-level PAM -> FM, sps=2, sensitivity pi/2
                   rad/sample (the exact TX math of examples/fsk4_modem and
                   verification/tests/test_fsk4_sync_timing_recovery.py:
                   tx = exp(j*cumsum((pi/2)*shaped))).
  3: noise      -- complex AWGN only.

All generators return unit-RMS clips; the channel model applies gain,
frequency offset, phase, and AWGN.  numpy only, deterministic given an
np.random.Generator.
"""

from __future__ import annotations

import math

import numpy as np

FS = 32000.0  # common sample rate (Hz), matches examples/bpsk_modem + ssb_weaver

CLASS_NAMES = ["ssb", "bpsk", "fsk4", "noise"]


# ----------------------------------------------------------------------------
# Root-raised-cosine taps (ported from verification/tests/
# test_fsk4_sync_timing_recovery.py::_rrc -- unit-energy normalisation)
# ----------------------------------------------------------------------------
def rrc_taps(beta: float, sps: int, span: int) -> np.ndarray:
    n = span * sps
    taps = []
    for i in range(n + 1):
        t = (i - n / 2) / sps
        if abs(t) < 1e-8:
            v = 1 - beta + 4 * beta / math.pi
        elif abs(abs(4 * beta * t) - 1.0) < 1e-8:
            v = (beta / math.sqrt(2)) * (
                (1 + 2 / math.pi) * math.sin(math.pi / (4 * beta))
                + (1 - 2 / math.pi) * math.cos(math.pi / (4 * beta)))
        else:
            num = (math.sin(math.pi * t * (1 - beta))
                   + 4 * beta * t * math.cos(math.pi * t * (1 + beta)))
            den = math.pi * t * (1 - (4 * beta * t) ** 2)
            v = num / den
        taps.append(v)
    taps = np.asarray(taps, dtype=np.float64)
    return taps / np.sqrt(np.sum(taps * taps))


def _unit_rms(x: np.ndarray) -> np.ndarray:
    r = np.sqrt(np.mean(np.abs(x) ** 2))
    return x / (r + 1e-12)


# ----------------------------------------------------------------------------
# Class generators (each returns exactly n complex samples, unit RMS)
# ----------------------------------------------------------------------------
def gen_bpsk(n: int, rng: np.random.Generator, sps: int = 4,
             alpha: float = 0.35, span: int = 8) -> np.ndarray:
    """RRC-shaped BPSK at sps samples/symbol (bpsk_modem pulse shaping)."""
    taps = rrc_taps(alpha, sps, span)
    nsym = n // sps + 2 * span
    bits = rng.integers(0, 2, nsym)
    up = np.zeros(nsym * sps)
    up[::sps] = 2.0 * bits - 1.0
    shaped = np.convolve(up, taps)
    # trim filter transients, take the steady-state middle
    start = span * sps
    x = shaped[start:start + n].astype(np.complex128)
    return _unit_rms(x)


# 4-FSK symbol levels: the +-1, +-1/3 PAM alphabet of the fsk4 modem chain.
FSK4_LEVELS = np.array([1.0, 1.0 / 3.0, -1.0 / 3.0, -1.0])


def gen_fsk4(n: int, rng: np.random.Generator, sps: int = 2,
             beta: float = 0.5, span: int = 8) -> np.ndarray:
    """RRC-shaped 4-PAM -> FM, sensitivity pi/2 rad/sample (fsk4_modem math)."""
    taps = rrc_taps(beta, sps, span)
    nsym = n // sps + 2 * span
    dibits = rng.integers(0, 4, nsym)
    up = np.zeros(nsym * sps)
    up[::sps] = FSK4_LEVELS[dibits]
    shaped = np.convolve(up, taps)
    shaped = shaped / (np.max(np.abs(shaped)) + 1e-12) * 0.9
    tx = np.exp(1j * np.cumsum((math.pi / 2) * shaped))
    start = span * sps
    x = tx[start:start + n]
    return _unit_rms(x)


def _bandlimit_real(x: np.ndarray, fs: float, flo: float,
                    fhi: float) -> np.ndarray:
    """Brick-wall bandpass of a real signal via FFT (deterministic)."""
    n = len(x)
    X = np.fft.rfft(x)
    f = np.fft.rfftfreq(n, d=1.0 / fs)
    X[(f < flo) | (f > fhi)] = 0.0
    return np.fft.irfft(X, n)


def _analytic(x: np.ndarray) -> np.ndarray:
    """Analytic signal via FFT (positive-frequency doubling)."""
    n = len(x)
    X = np.fft.fft(x)
    h = np.zeros(n)
    h[0] = 1.0
    if n % 2 == 0:
        h[n // 2] = 1.0
        h[1:n // 2] = 2.0
    else:
        h[1:(n + 1) // 2] = 2.0
    return np.fft.ifft(X * h)


def gen_ssb(n: int, rng: np.random.Generator, flo: float = 300.0,
            fhi: float = 2700.0, syllabic_hz: float = 4.0) -> np.ndarray:
    """USB SSB of a voice-proxy audio, at complex baseband.

    Voice proxy = Gaussian noise bandlimited to the 300..2700 Hz speech band
    (the ssb_weaver passband), amplitude-modulated by a slow non-negative
    "syllabic" envelope so the clip has voice-like level bursts.  The USB
    complex-baseband signal is simply the analytic signal of that audio.
    """
    pad = 256
    audio = rng.standard_normal(n + 2 * pad)
    audio = _bandlimit_real(audio, FS, flo, fhi)
    # slow syllabic envelope: lowpassed |noise|, floor keeps it non-silent
    env = rng.standard_normal(n + 2 * pad)
    env = _bandlimit_real(env, FS, 0.0, syllabic_hz)
    env = np.abs(env)
    env = env / (np.max(env) + 1e-12) + 0.15
    x = _analytic(audio * env)[pad:pad + n]
    return _unit_rms(x)


def gen_noise(n: int, rng: np.random.Generator) -> np.ndarray:
    x = (rng.standard_normal(n) + 1j * rng.standard_normal(n)) / math.sqrt(2)
    return _unit_rms(x)


GENERATORS = {
    "ssb": gen_ssb,
    "bpsk": gen_bpsk,
    "fsk4": gen_fsk4,
    "noise": gen_noise,
}


# ----------------------------------------------------------------------------
# Channel
# ----------------------------------------------------------------------------
def apply_channel(x: np.ndarray, rng: np.random.Generator,
                  snr_db: float | None, gain: float,
                  foff_hz: float, fs: float = FS) -> np.ndarray:
    """gain * unit-RMS signal, rotated by a frequency offset, plus AWGN.

    snr_db=None means clean (no added noise).  For the noise class pass the
    all-zero signal with snr_db=None and add the noise via gen_noise scaled
    by gain, so total clip power matches the signal classes.
    """
    n = len(x)
    ph0 = rng.uniform(0, 2 * math.pi)
    rot = np.exp(1j * (2 * math.pi * foff_hz / fs * np.arange(n) + ph0))
    y = gain * x * rot
    if snr_db is not None:
        nrms = gain * 10.0 ** (-snr_db / 20.0)
        y = y + nrms * (rng.standard_normal(n)
                        + 1j * rng.standard_normal(n)) / math.sqrt(2)
    return y


def make_clip(cls: str, n: int, rng: np.random.Generator,
              snr_db: float | None, gain: float,
              foff_hz: float) -> np.ndarray:
    """One labelled clip: class signal through the channel model.

    The noise class ignores snr_db (there is no signal to be "at an SNR");
    its total power is set to gain^2 like the signal classes' signal part.
    """
    if cls == "noise":
        return apply_channel(gen_noise(n, rng), rng, None, gain, foff_hz)
    x = GENERATORS[cls](n, rng)
    return apply_channel(x, rng, snr_db, gain, foff_hz)
