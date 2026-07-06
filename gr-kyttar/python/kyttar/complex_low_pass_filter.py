"""
Kyttar Complex Low Pass Filter Block for GNURadio

GR marker; the real DSP runs on the placeKYT-hosted chip. Complex (I/Q) in,
complex out — ONE shared real tap set applied to both rails, exactly GNU Radio's
filter.fir_filter_ccf fed taps from filter.firdes.low_pass. Keeps the GR interface
(class name, params, complex ports) so it places/wires identically in GRC and maps
to the placeKYT ComplexLowPassFilter block.

Copyright 2026 Kyttar Computer Project.
SPDX-License-Identifier: GPL-3.0-or-later
"""

import numpy as np

from .dsp_markers import _PassThrough


class complex_low_pass_filter(_PassThrough):
    """
    Kyttar Complex Low Pass Filter — drop-in for GNU Radio's fir_filter_ccf fed
    firdes.low_pass taps. Complex baseband in, complex baseband out (one gr_complex
    stream each side). GR marker; the real DSP runs on the placeKYT chip.

    Parameters (mirroring GNU Radio's Low Pass Filter verbatim):
        device_id: ID of the kyttar.device to use
        gain: passband (DC) gain. NOTE: a MULTI-cell complex filter needs Sum|h|<=1;
            firdes low_pass at gain=1.0 slightly exceeds it, so use gain<=~0.9 for a
            wide (multi-cell) filter (the placeKYT block raises a clear error
            otherwise). Correlation is gain-invariant.
        samp_rate: sample rate in Hz
        cutoff_freq: passband-edge cutoff in Hz
        transition_width: transition-band width in Hz (sets the tap count)
        window: design window ("hamming" default, "hann", "blackman",
            "rectangular", "blackman_harris", "kaiser")
        beta: Kaiser window beta (used only for window="kaiser")
    """

    def __init__(
        self,
        device_id: str = "kyttar_0",
        gain: float = 0.9,
        samp_rate: float = 32000.0,
        cutoff_freq: float = 4000.0,
        transition_width: float = 2000.0,
        window: str = "hamming",
        beta: float = 6.76,
    ):
        super().__init__(name="Kyttar Complex Low Pass Filter", n_in=1, n_out=1,
                         in_dtype=np.complex64, out_dtype=np.complex64)
        self._device_id = device_id
        self._gain = gain
        self._samp_rate = samp_rate
        self._cutoff_freq = cutoff_freq
        self._transition_width = transition_width
        self._window = window
        self._beta = beta
        self._advertise_grc_params(device_id, "ComplexLowPassFilter", {
            "gain": gain, "samp_rate": samp_rate, "cutoff_freq": cutoff_freq,
            "transition_width": transition_width, "window": window, "beta": beta})

    def set_cutoff_freq(self, cutoff_freq: float):
        self._cutoff_freq = cutoff_freq

    def set_transition_width(self, transition_width: float):
        self._transition_width = transition_width
