"""
Kyttar Complex Mixer Block for GNURadio

Frequency translation block - multiplies input by a cosine oscillator.

GR marker; the real DSP runs on the placeKYT-hosted chip. This block keeps the
exact GR interface (class name, params, ports) so it places/wires identically in
GRC, but does NO in-process placement and streams pure pass-through.

Copyright 2026 Kyttar Computer Project.
SPDX-License-Identifier: GPL-3.0-or-later
"""

from .dsp_markers import _PassThrough


class complex_mixer(_PassThrough):
    """
    Kyttar Complex Mixer - Frequency Translation

    Multiplies the input signal by a cosine oscillator on the chip.
    GR marker; the real DSP runs on the placeKYT-hosted chip.

    Parameters:
        device_id: ID of the kyttar.device to use
        freq_word: Phase increment per sample (0-65535)
        sample_rate: Sample rate in Hz
    """

    def __init__(
        self,
        device_id: str = "kyttar_0",
        freq_word: int = 655,
        sample_rate: float = 32000.0,
    ):
        super().__init__(name="Kyttar Complex Mixer", n_in=1, n_out=1)
        self._device_id = device_id
        self._freq_word = freq_word
        self._sample_rate = sample_rate
        # Advertise params for GRC↔placeKYT sync detection (see dsp_markers).
        self._advertise_grc_params(
            device_id, "ComplexMixerBlock",
            {"freq_word": freq_word, "sample_rate": sample_rate})

    def set_freq_word(self, freq_word: int):
        self._freq_word = freq_word

    def get_freq_word(self) -> int:
        return self._freq_word
