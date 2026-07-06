"""
Kyttar Complex Band Reject (notch) Filter Block for GNURadio

GR marker; the real DSP runs on the placeKYT-hosted chip. Complex (I/Q) in,
complex out — ONE shared real tap set on both rails = GNU Radio fir_filter_ccf fed
firdes.band_reject taps. Maps to the placeKYT ComplexBandRejectFilter block.

Copyright 2026 Kyttar Computer Project.
SPDX-License-Identifier: GPL-3.0-or-later
"""

import numpy as np

from .dsp_markers import _PassThrough


class complex_band_reject_filter(_PassThrough):
    """Kyttar Complex Band Reject Filter — drop-in for fir_filter_ccf + firdes.band_reject.
    Complex in, complex out. GR marker; real DSP runs on the placeKYT chip.

    NOTE: a MULTI-cell complex filter needs Sum|h|<=1; a band_reject at gain=1.0
    exceeds it (the DC passband dominates), so use a smaller gain (~0.4) for a wide
    filter (the placeKYT block raises a clear error otherwise). Correlation is
    gain-invariant.
    """

    def __init__(
        self,
        device_id: str = "kyttar_0",
        gain: float = 0.4,
        samp_rate: float = 32000.0,
        low_cutoff_freq: float = 4000.0,
        high_cutoff_freq: float = 8000.0,
        transition_width: float = 2000.0,
        window: str = "hamming",
        beta: float = 6.76,
    ):
        super().__init__(name="Kyttar Complex Band Reject Filter", n_in=1, n_out=1,
                         in_dtype=np.complex64, out_dtype=np.complex64)
        self._device_id = device_id
        self._gain = gain
        self._samp_rate = samp_rate
        self._low_cutoff_freq = low_cutoff_freq
        self._high_cutoff_freq = high_cutoff_freq
        self._transition_width = transition_width
        self._window = window
        self._beta = beta
        self._advertise_grc_params(device_id, "ComplexBandRejectFilter", {
            "gain": gain, "samp_rate": samp_rate,
            "low_cutoff_freq": low_cutoff_freq,
            "high_cutoff_freq": high_cutoff_freq,
            "transition_width": transition_width, "window": window, "beta": beta})

    def set_low_cutoff_freq(self, low_cutoff_freq: float):
        self._low_cutoff_freq = low_cutoff_freq

    def set_high_cutoff_freq(self, high_cutoff_freq: float):
        self._high_cutoff_freq = high_cutoff_freq
