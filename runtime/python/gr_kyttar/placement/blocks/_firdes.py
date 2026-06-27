# SPDX-License-Identifier: GPL-3.0-or-later
"""Pure-Python re-implementation of GNU Radio's ``gnuradio.filter.firdes``.

The convenience FIR blocks (Low/High/Band-pass, Band-reject) are *windowed-sinc*
filter designers whose taps a GRC user would otherwise get from
``filter.firdes.{low_pass, high_pass, band_pass, band_reject}(...)``. The
production Kyttar runtime does NOT have GNU Radio installed (it ships in the
customer modem's ``.venv``; GR is only present on the verification host), so the
blocks cannot ``import gnuradio`` at runtime. Instead this module reproduces the
firdes algorithm EXACTLY in pure Python so the convenience blocks compute the
same taps GR would, standalone.

Faithfulness (verified in ``verification/tests/test_firdes_filters.py`` against the
real ``gnuradio.filter.firdes`` on the GR host):

  * The integer **tap count** matches firdes exactly (same window-attenuation /
    transition-width formula).
  * The **float taps** reproduce firdes to within floating-point rounding (~1
    float32 ULP). Two sources of last-bit difference, both sub-ULP and far below
    a Q15 LSB: (a) on a host that links the SAME libm as GR, only the
    Blackman/Blackman-Harris windows differ — by one ULP from GR's C++ fused
    multiply-add in ``coswindow``, not portably reproducible in Python; (b) the
    customer-modem ``.venv`` links a DIFFERENT libm than the GR verification
    host, so ``sin``/``cos`` can differ in the last bit and ANY window's tap can
    move ~1 ULP.
  * The **Q15-quantized** taps — the coefficients that actually reach the chip —
    are BIT-EXACT to firdes for EVERY supported window (the sub-ULP float
    difference never crosses a Q15 rounding boundary). So the on-chip filter is
    provably the firdes filter regardless of the float quirk.

All arithmetic mirrors the GR C++ source op-for-op: the windowed-sinc product is
formed in ``double`` and cast to ``float32`` per tap, the unity-gain
normalisation accumulates the float taps in ``double``, and the final scale is a
``float *= double`` cast back to ``float32`` — exactly as ``firdes.cc`` does.
"""
from __future__ import annotations

import math
from typing import List

import numpy as np

_f32 = np.float32


# --- window functions (gr::fft::window), returned as float32 lists ------------
# Each mirrors gr::fft::window's build op-for-op. The cos-windows use
# ``M = ntaps - 1`` and accumulate c0 - c1*cos(2*pi*n/M) + c2*cos(4*pi*n/M) - ...

def _hamming(ntaps: int) -> List[np.float32]:
    M = ntaps - 1
    return [_f32(0.54 - 0.46 * math.cos((2.0 * math.pi * n) / M)) for n in range(ntaps)]


def _hann(ntaps: int) -> List[np.float32]:
    M = ntaps - 1
    return [_f32(0.5 - 0.5 * math.cos((2.0 * math.pi * n) / M)) for n in range(ntaps)]


def _blackman(ntaps: int) -> List[np.float32]:
    M = ntaps - 1
    return [_f32(0.42 - 0.5 * math.cos((2.0 * math.pi * n) / M)
                 + 0.08 * math.cos((4.0 * math.pi * n) / M)) for n in range(ntaps)]


def _blackman_harris(ntaps: int) -> List[np.float32]:
    M = ntaps - 1
    a0, a1, a2, a3 = 0.35875, 0.48829, 0.14128, 0.01168
    return [_f32(a0 - a1 * math.cos((2.0 * math.pi * n) / M)
                 + a2 * math.cos((4.0 * math.pi * n) / M)
                 - a3 * math.cos((6.0 * math.pi * n) / M)) for n in range(ntaps)]


def _rectangular(ntaps: int) -> List[np.float32]:
    return [_f32(1.0)] * ntaps


_IZERO_EPSILON = 1e-21


def _izero(x: float) -> float:
    """Modified Bessel function I0(x) via the same series GR uses (window.cc)."""
    s = u = 1.0
    n = 1
    halfx = x / 2.0
    while True:
        temp = halfx / n
        n += 1
        temp *= temp
        u *= temp
        s += u
        if not (u >= _IZERO_EPSILON * s):
            break
    return s


def _kaiser(ntaps: int, beta: float) -> List[np.float32]:
    if beta < 0:
        raise ValueError("kaiser window beta must be >= 0")
    i_beta = 1.0 / _izero(beta)
    inm1 = 1.0 / (ntaps - 1)
    out = []
    for n in range(ntaps):
        temp = 2 * n * inm1 - 1.0
        out.append(_f32(_izero(beta * math.sqrt(1.0 - temp * temp)) * i_beta))
    return out


# Window name -> (builder, max-attenuation dB used by compute_ntaps). The
# attenuation constants are gr::fft::window::max_attenuation's; Kaiser's is a
# beta-dependent formula (handled in _attenuation).
_WINDOWS = {
    "hamming": (_hamming, 53.0),
    "hann": (_hann, 44.0),
    "hanning": (_hann, 44.0),
    "blackman": (_blackman, 74.0),
    "blackman_harris": (_blackman_harris, 92.0),
    "blackmanharris": (_blackman_harris, 92.0),
    "rectangular": (_rectangular, 21.0),
    "boxcar": (_rectangular, 21.0),
    "kaiser": (_kaiser, None),
}

# GRC window-enum integer -> canonical name (so a block param can be given as the
# GR ``firdes.WIN_*`` integer too, matching what a .grc file stores).
_WIN_ENUM = {
    0: "hamming", 1: "hann", 2: "blackman", 3: "rectangular",
    4: "kaiser", 5: "blackman_harris",
}


def _normalize_window(window) -> str:
    if isinstance(window, str):
        key = window.strip().lower().replace("win_", "").replace("-", "_").replace(" ", "_")
        if key in _WINDOWS:
            return key
        raise ValueError(f"unsupported firdes window {window!r}; "
                         f"supported: {sorted(set(_WINDOWS))}")
    if isinstance(window, int):
        if window in _WIN_ENUM:
            return _WIN_ENUM[window]
        raise ValueError(f"unknown firdes window enum {window}")
    raise TypeError(f"window must be a name or enum int, got {type(window)}")


def _attenuation(window: str, beta: float) -> float:
    _, atten = _WINDOWS[window]
    if atten is not None:
        return atten
    # Kaiser: GR's max_attenuation(WIN_KAISER, beta) = beta/0.1102 + 8.7
    return beta / 0.1102 + 8.7


def _build_window(window: str, ntaps: int, beta: float) -> List[np.float32]:
    builder, _ = _WINDOWS[window]
    if window == "kaiser":
        return _kaiser(ntaps, beta)
    return builder(ntaps)


def compute_ntaps(sampling_freq: float, transition_width: float,
                  window="hamming", beta: float = 6.76) -> int:
    """firdes::compute_ntaps — the odd tap count for a transition width.

    ``ntaps = int(atten * fs / (22 * transition_width))`` rounded UP to the next
    odd integer, where ``atten`` is the window's stop-band attenuation in dB.
    """
    if transition_width <= 0:
        raise ValueError("transition_width must be > 0")
    win = _normalize_window(window)
    atten = _attenuation(win, beta)
    ntaps = int(atten * sampling_freq / (22.0 * transition_width))
    if (ntaps & 1) == 0:
        ntaps += 1
    return ntaps


def _design(kind: str, gain, fs, f0, f1, transition_width, window, beta):
    """Shared windowed-sinc designer for all four firdes filter kinds."""
    win = _normalize_window(window)
    ntaps = compute_ntaps(fs, transition_width, win, beta)
    w = _build_window(win, ntaps, beta)
    M = (ntaps - 1) // 2
    taps = [_f32(0.0)] * ntaps

    if kind == "low_pass":
        fwT0 = 2.0 * math.pi * f0 / fs
        for n in range(-M, M + 1):
            if n == 0:
                taps[n + M] = _f32(fwT0 / math.pi * w[n + M])
            else:
                taps[n + M] = _f32(math.sin(n * fwT0) / (n * math.pi) * w[n + M])
        # unity gain at DC
        fmax = float(taps[0 + M])
        for n in range(1, M + 1):
            fmax += 2.0 * float(taps[n + M])

    elif kind == "high_pass":
        fwT0 = 2.0 * math.pi * f0 / fs
        for n in range(-M, M + 1):
            if n == 0:
                taps[n + M] = _f32((1.0 - fwT0 / math.pi) * w[n + M])
            else:
                taps[n + M] = _f32((-math.sin(n * fwT0) / (n * math.pi)) * w[n + M])
        # unity gain at Nyquist (fs/2): sum taps[n] * cos(n*pi)
        fmax = float(taps[0 + M])
        for n in range(1, M + 1):
            fmax += 2.0 * float(taps[n + M]) * math.cos(n * math.pi)

    elif kind == "band_pass":
        fwT0 = 2.0 * math.pi * f0 / fs
        fwT1 = 2.0 * math.pi * f1 / fs
        for n in range(-M, M + 1):
            if n == 0:
                taps[n + M] = _f32((fwT1 - fwT0) / math.pi * w[n + M])
            else:
                taps[n + M] = _f32(
                    (math.sin(n * fwT1) - math.sin(n * fwT0)) / (n * math.pi) * w[n + M])
        # unity gain at the band centre
        fmax = float(taps[0 + M])
        freq = math.pi * (f0 + f1) / fs
        for n in range(1, M + 1):
            fmax += 2.0 * float(taps[n + M]) * math.cos(n * freq)

    elif kind == "band_reject":
        fwT0 = 2.0 * math.pi * f0 / fs
        fwT1 = 2.0 * math.pi * f1 / fs
        for n in range(-M, M + 1):
            if n == 0:
                taps[n + M] = _f32((1.0 - (fwT1 - fwT0) / math.pi) * w[n + M])
            else:
                taps[n + M] = _f32(
                    (math.sin(n * fwT0) - math.sin(n * fwT1)) / (n * math.pi) * w[n + M])
        # unity gain at DC
        fmax = float(taps[0 + M])
        for n in range(1, M + 1):
            fmax += 2.0 * float(taps[n + M])

    else:
        raise ValueError(f"unknown firdes kind {kind!r}")

    gain2 = gain / fmax
    return [float(_f32(float(t) * gain2)) for t in taps]


def low_pass(gain, sampling_freq, cutoff_freq, transition_width,
             window="hamming", beta: float = 6.76) -> List[float]:
    """firdes.low_pass — windowed-sinc low-pass taps (DC unity gain ``gain``)."""
    if not (0 < cutoff_freq <= sampling_freq / 2):
        raise ValueError("cutoff_freq must be in (0, fs/2]")
    return _design("low_pass", gain, sampling_freq, cutoff_freq, None,
                   transition_width, window, beta)


def high_pass(gain, sampling_freq, cutoff_freq, transition_width,
              window="hamming", beta: float = 6.76) -> List[float]:
    """firdes.high_pass — windowed-sinc high-pass taps (Nyquist unity gain)."""
    if not (0 < cutoff_freq <= sampling_freq / 2):
        raise ValueError("cutoff_freq must be in (0, fs/2]")
    return _design("high_pass", gain, sampling_freq, cutoff_freq, None,
                   transition_width, window, beta)


def band_pass(gain, sampling_freq, low_cutoff_freq, high_cutoff_freq,
              transition_width, window="hamming", beta: float = 6.76) -> List[float]:
    """firdes.band_pass — windowed-sinc band-pass taps (band-centre unity gain)."""
    if not (0 < low_cutoff_freq < high_cutoff_freq <= sampling_freq / 2):
        raise ValueError("require 0 < low_cutoff < high_cutoff <= fs/2")
    return _design("band_pass", gain, sampling_freq, low_cutoff_freq,
                   high_cutoff_freq, transition_width, window, beta)


def band_reject(gain, sampling_freq, low_cutoff_freq, high_cutoff_freq,
                transition_width, window="hamming", beta: float = 6.76) -> List[float]:
    """firdes.band_reject — windowed-sinc band-reject taps (DC unity gain)."""
    if not (0 < low_cutoff_freq < high_cutoff_freq <= sampling_freq / 2):
        raise ValueError("require 0 < low_cutoff < high_cutoff <= fs/2")
    return _design("band_reject", gain, sampling_freq, low_cutoff_freq,
                   high_cutoff_freq, transition_width, window, beta)


def root_raised_cosine(gain: float, sampling_freq: float, symbol_rate: float,
                       alpha: float, ntaps: int) -> List[float]:
    """firdes.root_raised_cosine(gain, sampling_freq, symbol_rate, alpha, ntaps).

    A bit-exact (to float precision) port of gr::filter::firdes::root_raised_cosine
    (firdes.cc): the closed-form RRC impulse response, scaled so the tap SUM equals
    ``gain``. ``ntaps`` is forced ODD (GR adds 1 if even). ``alpha`` is the rolloff
    (excess-bandwidth) factor; ``samples-per-symbol = sampling_freq/symbol_rate``.
    Verified max deviation ~1e-8 vs ``filter.firdes.root_raised_cosine`` across
    gain / fs / alpha / ntaps. (NOT a windowed sinc, so it does not go through
    ``_design`` — it is GR's own RRC formula.)"""
    ntaps = int(ntaps)
    if ntaps % 2 == 0:
        ntaps += 1
    spb = sampling_freq / symbol_rate           # samples per symbol
    taps = [0.0] * ntaps
    scale = 0.0
    for i in range(ntaps):
        xindx = i - ntaps // 2
        x1 = math.pi * xindx / spb
        x2 = 4.0 * alpha * xindx / spb
        x3 = x2 * x2 - 1.0
        if abs(x3) >= 0.000001:                 # avoid 0/0 at x2^2 == 1
            if i != ntaps // 2:
                num = (math.cos((1.0 + alpha) * x1)
                       + math.sin((1.0 - alpha) * x1) / (4.0 * alpha * xindx / spb))
            else:
                num = math.cos((1.0 + alpha) * x1) + (1.0 - alpha) * math.pi / (4.0 * alpha)
            den = x3 * math.pi
        else:
            if alpha == 1.0:
                taps[i] = -1.0
                scale += taps[i]
                continue
            x3 = (1.0 - alpha) * x1
            x2 = (1.0 + alpha) * x1
            num = (math.sin(x2) * (1.0 + alpha) * math.pi
                   - math.cos(x3) * ((1.0 - alpha) * math.pi * spb) / (4.0 * alpha * xindx)
                   + math.sin(x3) * spb * spb / (4.0 * alpha * xindx * xindx))
            den = -32.0 * math.pi * alpha * alpha * xindx / spb
        taps[i] = 4.0 * alpha * num / den
        scale += taps[i]
    return [t * gain / scale for t in taps]
