"""
Kyttar AGC Block for GNURadio

GR marker; the real DSP runs on the placeKYT-hosted chip. This block keeps the
exact GR interface (class name, params, ports) so it places/wires identically in
GRC, but does NO in-process placement and streams pure pass-through.

Copyright 2026 Kyttar Computer Project.
SPDX-License-Identifier: GPL-3.0-or-later
"""

from .dsp_markers import _PassThrough


class agc(_PassThrough):
    """
    Kyttar AGC - Automatic Gain Control

    Maintains a target output level by adaptively adjusting gain on the chip.
    GR marker; the real DSP runs on the placeKYT-hosted chip.

    Parameters (mirror GNU Radio analog.agc_ff VERBATIM):
        device_id: ID of the kyttar.device to use
        rate: update rate of the loop (GR default 1e-4)
        reference: reference value to adjust signal power to (GR default 1.0)
        gain: initial gain value (GR default 1.0)
        max_gain: maximum gain value; 0 = unlimited (GR default 0)
    """

    def __init__(
        self,
        device_id: str = "kyttar_0",
        rate: float = 1e-4,
        reference: float = 1.0,
        gain: float = 1.0,
        max_gain: float = 0.0,
    ):
        super().__init__(name="Kyttar AGC", n_in=1, n_out=1)
        self._device_id = device_id
        self._rate = rate
        self._reference = reference
        self._gain = gain
        self._max_gain = max_gain
        # Advertise params for GRC↔placeKYT sync detection (see dsp_markers).
        # Names are the AGCBlock/analog.agc_ff class params VERBATIM.
        self._advertise_grc_params(
            device_id, "AGCBlock",
            {"rate": rate, "reference": reference, "gain": gain,
             "max_gain": max_gain})

    def set_reference(self, reference: float):
        """Set reference level."""
        self._reference = reference

    def get_reference(self) -> float:
        """Get current reference level."""
        return self._reference

    def set_rate(self, rate: float):
        """Set update rate."""
        self._rate = rate

    def get_rate(self) -> float:
        """Get current update rate."""
        return self._rate

    def set_gain(self, gain: float):
        """Set gain."""
        self._gain = gain

    def get_gain(self) -> float:
        """Get gain value."""
        return self._gain

    def set_max_gain(self, max_gain: float):
        """Set maximum gain."""
        self._max_gain = max_gain

    def get_max_gain(self) -> float:
        """Get maximum gain value."""
        return self._max_gain
