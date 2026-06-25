"""
Kyttar Band Pass Filter Block for GNURadio

GR marker; the real DSP runs on the placeKYT-hosted chip. This block keeps the
exact GR interface (class name, params, ports) so it places/wires identically in
GRC, but does NO in-process placement and streams pure pass-through.

It mirrors GNU Radio's GRC **Band Pass Filter** (a fir_filter_fff whose taps come
from firdes.band_pass): the user specifies the band in DSP units and the chip
block designs the firdes windowed-sinc taps internally.

Copyright 2026 Kyttar Computer Project.
SPDX-License-Identifier: GPL-3.0-or-later
"""

from .dsp_markers import _PassThrough


class band_pass_filter(_PassThrough):
    """
    Kyttar Band Pass Filter — drop-in for GNU Radio's Band Pass Filter
    (filter.fir_filter_fff + filter.firdes.band_pass).

    GR marker; the real DSP runs on the placeKYT-hosted chip.

    Parameters (mirroring GNU Radio's Band Pass Filter verbatim):
        device_id: ID of the kyttar.device to use
        gain: passband gain
        samp_rate: sample rate in Hz
        low_cutoff_freq: lower passband edge in Hz
        high_cutoff_freq: upper passband edge in Hz
        transition_width: transition-band width in Hz (sets the tap count)
        window: design window ("hamming" default, "hann", "blackman",
            "rectangular", "blackman_harris", "kaiser")
        beta: Kaiser window beta (used only for window="kaiser")
    """

    def __init__(
        self,
        device_id: str = "kyttar_0",
        gain: float = 1.0,
        samp_rate: float = 32000.0,
        low_cutoff_freq: float = 4000.0,
        high_cutoff_freq: float = 8000.0,
        transition_width: float = 2000.0,
        window: str = "hamming",
        beta: float = 6.76,
    ):
        super().__init__(name="Kyttar Band Pass Filter", n_in=1, n_out=1)
        self._device_id = device_id
        self._gain = gain
        self._samp_rate = samp_rate
        self._low_cutoff_freq = low_cutoff_freq
        self._high_cutoff_freq = high_cutoff_freq
        self._transition_width = transition_width
        self._window = window
        self._beta = beta
        self._advertise_grc_params(device_id, "BandPassFilter", {
            "gain": gain, "samp_rate": samp_rate,
            "low_cutoff_freq": low_cutoff_freq, "high_cutoff_freq": high_cutoff_freq,
            "transition_width": transition_width, "window": window, "beta": beta})

    def set_low_cutoff_freq(self, low_cutoff_freq: float):
        self._low_cutoff_freq = low_cutoff_freq

    def set_high_cutoff_freq(self, high_cutoff_freq: float):
        self._high_cutoff_freq = high_cutoff_freq

    def set_transition_width(self, transition_width: float):
        self._transition_width = transition_width
