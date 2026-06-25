"""
Kyttar Complex Mixer Block for GNURadio

Frequency shifter -- a drop-in for GNU Radio's blocks.multiply_cc(signal,
analog.sig_source_c(...)): multiplies a complex input by a complex exponential.

GR marker; the real DSP runs on the placeKYT-hosted chip. This block keeps the
exact GR interface (class name, params, ports) so it places/wires identically in
GRC, but does NO in-process placement and streams pure pass-through.

Copyright 2026 Kyttar Computer Project.
SPDX-License-Identifier: GPL-3.0-or-later
"""

from .dsp_markers import _PassThrough


class complex_mixer(_PassThrough):
    """
    Kyttar Complex Mixer -- frequency shifter (multiply_cc + sig_source_c).

    out[n] = in[n] * exp(j*2*pi*frequency/sample_rate*n) -- the full complex product
    (yi = xi*cos - xq*sin, yq = xi*sin + xq*cos). GR marker; the real DSP runs on the
    placeKYT-hosted chip.

    Parameters (mirroring GNU Radio's Signal Source -- the mixing oscillator):
        device_id: ID of the kyttar.device to use
        sample_rate: sample rate in Hz
        frequency: mixing/shift frequency in Hz (the freq_word is derived)
    """

    def __init__(
        self,
        device_id: str = "kyttar_0",
        sample_rate: float = 32000.0,
        frequency: float = 2000.0,
    ):
        super().__init__(name="Kyttar Complex Mixer", n_in=1, n_out=1)
        self._device_id = device_id
        self._sample_rate = sample_rate
        self._frequency = frequency
        self._advertise_grc_params(device_id, "ComplexMixerBlock", {
            "sample_rate": sample_rate, "frequency": frequency})

    def set_frequency(self, frequency: float):
        self._frequency = frequency

    def get_frequency(self) -> float:
        return self._frequency
