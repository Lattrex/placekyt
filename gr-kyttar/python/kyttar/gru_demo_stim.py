# SPDX-License-Identifier: GPL-3.0-or-later
"""Stimulus for the GRU modulation-classifier demo flowgraph
(examples/gru_classifier/gru_classifier.grc).

Imported by the flowgraph as a plain Python module (the fec_demo_stim /
qam16_demo_stim pattern), so the ``.grc`` carries no fragile inline source.
Pure Python + numpy — this module runs under the GNU RADIO interpreter and must
NOT import ``gr_kyttar``.

WHAT IT PRODUCES. One concatenated complex-baseband clip that walks the four
trained classes in order — **SSB -> BPSK -> 4-FSK -> noise** — so the chip's
class output visibly tracks the input, plus the matching per-window ground
truth for the comparison scope.

It is a byte-for-byte port of ``examples/gru_classifier/gru_stimulus.py`` (and
the generators it calls in ``examples/gru_classifier/ml/signals.py``), inlined
here because the installed GR package cannot reach the repo's example tree. The
example's gate asserts the two produce the IDENTICAL clip, so this copy can
never drift.

TWO PROPERTIES OF THE CLIP ARE LOAD-BEARING, not cosmetic:

* **Channel distribution.** Every segment is generated at a gain inside the
  trained ``gain_range`` (0.25..0.7) with a frequency offset from the trained
  range. Feeding the model out-of-distribution clips degrades it — measured:
  peak-normalised 4-FSK clips are voted BPSK by the OFFLINE reference too, so
  this is a property of the model and not of the chip.

* **Q15 headroom.** The on-chip RMS arm begins with ComplexToMagSquared, which
  computes ``re^2 + im^2`` in Q15 and SATURATES at full scale, so any sample
  with ``|z| >= 1`` clips and biases that window's mean power DOWNWARD.

  These pull against each other and the tension is real: ``gain`` sets a
  segment's RMS while saturation is driven by its PEAK, and the classes' crest
  factors differ sharply (median over 12 clips each: 4-FSK 1.27, BPSK 1.71,
  noise 3.10, SSB 3.59). At the top of the trained gain range the high-crest
  classes peak well above 1.0 — over the trained set ``peak|z| > 1`` for 100%
  of SSB and 79% of noise clips. Float features never notice; a Q15 power stage
  clips hard. Peak-normalising is NOT a free fix (it pushes the clip
  out-of-distribution, above). So the per-segment gains are pinned at the LOW
  end of the trained range and the shipped clip measures ``peak|z| = 0.862``.
"""

import math

import numpy as np

# --- the model's configuration (examples/gru_classifier/ml/config.json) ------
FS = 32000.0
WINDOW_N = 32
CLASSES = ["ssb", "bpsk", "fsk4", "noise"]
GAIN_RANGE = (0.25, 0.7)
FREQ_OFFSET_HZ = (-100.0, 100.0)

# --- the shipped clip's parameters (examples/gru_classifier/gru_stimulus.py) -
SEGMENT_STEPS = 120          # feature windows per class segment
SEGMENT_SNR_DB = 20.0
SEED = 20260824
#: pinned at the LOW end of the trained range for Q15 headroom — see the module
#: docstring. Selected by sweeping the range in 0.01 steps at this SEED and
#: keeping, per class, the gain that maximised the offline chip model's
#: per-step accuracy among those with peak|z| < 0.95.
SEGMENT_GAIN = {"ssb": 0.27, "bpsk": 0.28, "fsk4": 0.40, "noise": 0.25}

FSK4_LEVELS = np.array([1.0, 1.0 / 3.0, -1.0 / 3.0, -1.0])


# ---------------------------------------------------------------- primitives
def rrc_taps(beta, sps, span):
    """Unit-energy root-raised-cosine taps."""
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


def _unit_rms(x):
    r = np.sqrt(np.mean(np.abs(x) ** 2))
    return x / (r + 1e-12)


def _bandlimit_real(x, fs, flo, fhi):
    """Brick-wall bandpass of a real signal via FFT (deterministic)."""
    n = len(x)
    X = np.fft.rfft(x)
    f = np.fft.rfftfreq(n, d=1.0 / fs)
    X[(f < flo) | (f > fhi)] = 0.0
    return np.fft.irfft(X, n)


def _analytic(x):
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


# ------------------------------------------------------- class generators
def gen_bpsk(n, rng, sps=4, alpha=0.35, span=8):
    """RRC-shaped BPSK at sps samples/symbol (the bpsk_modem pulse shaping)."""
    taps = rrc_taps(alpha, sps, span)
    nsym = n // sps + 2 * span
    bits = rng.integers(0, 2, nsym)
    up = np.zeros(nsym * sps)
    up[::sps] = 2.0 * bits - 1.0
    shaped = np.convolve(up, taps)
    start = span * sps
    return _unit_rms(shaped[start:start + n].astype(np.complex128))


def gen_fsk4(n, rng, sps=2, beta=0.5, span=8):
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
    return _unit_rms(tx[start:start + n])


def gen_ssb(n, rng, flo=300.0, fhi=2700.0, syllabic_hz=4.0):
    """USB SSB of a voice proxy, at complex baseband (the ssb_weaver band)."""
    pad = 256
    audio = rng.standard_normal(n + 2 * pad)
    audio = _bandlimit_real(audio, FS, flo, fhi)
    env = rng.standard_normal(n + 2 * pad)
    env = _bandlimit_real(env, FS, 0.0, syllabic_hz)
    env = np.abs(env)
    env = env / (np.max(env) + 1e-12) + 0.15
    return _unit_rms(_analytic(audio * env)[pad:pad + n])


def gen_noise(n, rng):
    x = (rng.standard_normal(n) + 1j * rng.standard_normal(n)) / math.sqrt(2)
    return _unit_rms(x)


GENERATORS = {"ssb": gen_ssb, "bpsk": gen_bpsk, "fsk4": gen_fsk4,
              "noise": gen_noise}


# ------------------------------------------------------------------- channel
def apply_channel(x, rng, snr_db, gain, foff_hz, fs=FS):
    """gain * unit-RMS signal, rotated by a frequency offset, plus AWGN."""
    n = len(x)
    ph0 = rng.uniform(0, 2 * math.pi)
    rot = np.exp(1j * (2 * math.pi * foff_hz / fs * np.arange(n) + ph0))
    y = gain * x * rot
    if snr_db is not None:
        nrms = gain * 10.0 ** (-snr_db / 20.0)
        y = y + nrms * (rng.standard_normal(n)
                        + 1j * rng.standard_normal(n)) / math.sqrt(2)
    return y


def make_clip(cls, n, rng, snr_db, gain, foff_hz):
    """One labelled clip: class signal through the channel model."""
    if cls == "noise":
        return apply_channel(gen_noise(n, rng), rng, None, gain, foff_hz)
    return apply_channel(GENERATORS[cls](n, rng), rng, snr_db, gain, foff_hz)


# ------------------------------------------------------- the flowgraph's API
def segment_samples():
    """Complex samples in one class segment."""
    return SEGMENT_STEPS * WINDOW_N


def make_stimulus():
    """``(iq, truth)`` — the shipped clip and its per-window class labels."""
    n = segment_samples()
    g0, g1 = GAIN_RANGE
    f0, f1 = FREQ_OFFSET_HZ
    segs, truth = [], []
    for ci, cls in enumerate(CLASSES):
        gain = float(SEGMENT_GAIN[cls])
        if not (g0 <= gain <= g1):
            raise ValueError(
                f"SEGMENT_GAIN[{cls!r}] = {gain} is outside the model's "
                f"trained gain_range [{g0}, {g1}] — the stimulus must stay in "
                f"distribution")
        rng = np.random.default_rng([SEED, ci])
        foff = float(rng.uniform(f0, f1))
        segs.append(make_clip(cls, n, rng, SEGMENT_SNR_DB, gain, foff))
        truth.append(np.full(SEGMENT_STEPS, ci, dtype=np.int64))
    return np.concatenate(segs), np.concatenate(truth)


def iq():
    """The complex baseband clip, as the list a GRC vector source takes."""
    z, _t = make_stimulus()
    return [complex(v) for v in z]


def truth():
    """The TRUE class index per feature window, as floats for the scope."""
    _z, t = make_stimulus()
    return [float(v) for v in t]


def n_samples():
    """Complex samples in the clip (the source burst length)."""
    return len(CLASSES) * segment_samples()


def n_windows():
    """Class words the chip returns — one per WINDOW_N input samples."""
    return len(CLASSES) * SEGMENT_STEPS


def peak_magnitude():
    """``max |z|`` of the shipped clip — must stay < 1 (Q15 headroom)."""
    z, _t = make_stimulus()
    return float(np.max(np.abs(z)))


if __name__ == "__main__":
    z, t = make_stimulus()
    print(f"{len(z)} complex samples, {len(t)} feature windows, "
          f"{len(CLASSES)} segments of {SEGMENT_STEPS}")
    print(f"peak |z| = {peak_magnitude():.4f} (must be < 1.0)")
