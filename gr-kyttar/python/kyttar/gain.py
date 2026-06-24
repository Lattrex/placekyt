"""
Kyttar Gain Block for GNURadio

GR marker; the real DSP runs on the placeKYT-hosted chip. This block keeps the
exact GR interface (class name, params, ports) so it places/wires identically in
GRC, but does NO in-process placement and streams pure pass-through.

Copyright 2026 Kyttar Computer Project.
SPDX-License-Identifier: GPL-3.0-or-later
"""

from .dsp_markers import _PassThrough


class gain(_PassThrough):
    """
    Kyttar Gain - Simple Multiplier

    Multiplies input by a gain coefficient (output = input * gain) on the chip.
    GR marker; the real DSP runs on the placeKYT-hosted chip.

    Parameters:
        device_id: ID of the kyttar.device to use
        gain: Multiplication factor (-1.0 to 1.0 for Q15)
    """

    def __init__(self, device_id: str = "kyttar_0", gain: float = 0.5):
        super().__init__(name="Kyttar Gain", n_in=1, n_out=1)
        self._device_id = device_id
        self._gain = gain
        # Advertise params for GRC↔placeKYT sync detection (see dsp_markers).
        self._advertise_grc_params(device_id, "GainBlock", {"gain": gain})

    def set_gain(self, gain: float):
        """Set gain value."""
        self._gain = gain

    def get_gain(self) -> float:
        """Get current gain value."""
        return self._gain
