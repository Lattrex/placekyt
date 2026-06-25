"""
Kyttar Signal Source Block for GNURadio

Numerically Controlled Oscillator -- a drop-in for GNU Radio's analog.sig_source_c
(complex cosine): emits amplitude*exp(j*theta_n).

GR marker; the real DSP runs on the placeKYT-hosted chip. This block keeps the
exact GR interface (class name, params, ports) so it places/wires identically in
GRC, but does NO in-process placement and streams pure pass-through.

Copyright 2026 Kyttar Computer Project.
SPDX-License-Identifier: GPL-3.0-or-later
"""

from .dsp_markers import _PassThrough


class nco(_PassThrough):
    """
    Kyttar Signal Source -- complex NCO (drop-in for analog.sig_source_c).

    Emits amplitude*(cos(theta_n) + j*sin(theta_n)), theta_n = 2*pi*frequency/
    sample_rate*n. GR marker; the real DSP runs on the placeKYT-hosted chip.

    Parameters (mirroring GNU Radio's Signal Source verbatim):
        device_id: ID of the kyttar.device to use
        sample_rate: sample rate in Hz
        frequency: tone frequency in Hz (the freq_word is derived internally)
        amplitude: output amplitude (0..1)
        waveform: "cos" (the complex cosine; GR_COS_WAVE)
    """

    def __init__(
        self,
        device_id: str = "kyttar_0",
        sample_rate: float = 32000.0,
        frequency: float = 2000.0,
        amplitude: float = 0.9,
        waveform: str = "cos",
    ):
        super().__init__(name="Kyttar Signal Source", n_in=1, n_out=1)
        self._device_id = device_id
        self._sample_rate = sample_rate
        self._frequency = frequency
        self._amplitude = amplitude
        self._waveform = waveform
        self._advertise_grc_params(device_id, "NCOBlock", {
            "sample_rate": sample_rate, "frequency": frequency,
            "amplitude": amplitude, "waveform": waveform})

    def set_frequency(self, frequency: float):
        self._frequency = frequency

    def get_frequency(self) -> float:
        return self._frequency
