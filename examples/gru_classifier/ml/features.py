"""Windowed feature front end for the GRU modulation classifier.

The on-chip front end computes per-window statistics over non-overlapping
N-sample windows of the complex baseband stream (N = WINDOW_N, feature rate
= sample_rate / N).  This module is the bit-level reference for that
windowing.  All features land in [0, 1) so they quantise directly to Q15.

Candidate features (per window of N complex samples x[k]):

  mag : mean |x[k]|                 (envelope mean; MagnitudeBlock + averager)
  rms : sqrt(mean |x[k]|^2)         (windowed RMS)
  zcr : zero-crossing count of Re(x) / N   (crude frequency feature)

The final 2-feature choice is made by check_features.py and recorded in the
weights file; keep FEATURE_ORDER in sync with that file.
"""

from __future__ import annotations

import numpy as np

WINDOW_N = 32  # samples per feature window -> feature rate = FS/32 = 1 kHz

ALL_FEATURES = ["mag", "rms", "zcr"]


def compute_features(x: np.ndarray, window_n: int = WINDOW_N,
                     quantize: bool = True) -> dict[str, np.ndarray]:
    """All candidate features over non-overlapping windows.

    Returns dict of float arrays of length len(x)//window_n.  With
    quantize=True each feature is snapped to the Q15 grid (round to
    1/32768), matching what the on-chip front end hands the GRU.
    """
    n = (len(x) // window_n) * window_n
    xw = x[:n].reshape(-1, window_n)
    mag = np.mean(np.abs(xw), axis=1)
    rms = np.sqrt(np.mean(np.abs(xw) ** 2, axis=1))
    re = np.real(xw)
    # zero crossing = sign change between consecutive samples (sign(0) treated
    # as +1 so a stuck-at-zero stream counts no crossings)
    s = np.where(re >= 0.0, 1.0, -1.0)
    zc = np.sum(s[:, 1:] != s[:, :-1], axis=1).astype(np.float64)
    zcr = zc / window_n
    feats = {"mag": mag, "rms": rms, "zcr": zcr}
    if quantize:
        for k in feats:
            q = np.round(np.clip(feats[k], 0.0, 32767.0 / 32768.0) * 32768.0)
            feats[k] = q / 32768.0
    return feats


def feature_matrix(x: np.ndarray, names: list[str],
                   window_n: int = WINDOW_N,
                   quantize: bool = True) -> np.ndarray:
    """(T, len(names)) feature time series for one clip."""
    f = compute_features(x, window_n, quantize)
    return np.stack([f[n] for n in names], axis=1)
