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

    def __init__(self, device_id: str = "kyttar_0", gain: float = 0.5,
                 block_name: str = ""):
        super().__init__(name="Kyttar Gain", n_in=1, n_out=1)
        self._device_id = device_id
        self._gain = gain
        # Advertise params for GRC↔placeKYT sync detection + LIVE tuning (see
        # dsp_markers). block_name = the placeKYT block name, verbatim — set it
        # in the .grc whenever the design has SEVERAL gains, so the live slider
        # retunes ITS OWN cell (the construction-order fallback can swap
        # same-type instances — see register_params).
        self._advertise_grc_params(device_id, "GainBlock", {"gain": gain},
                                   block_name=block_name)

    def set_gain(self, gain: float):
        """Set gain value (GRC slider callback). Mid-run, the new value ships
        with the next burst's ``grc_params`` and placeKYT WRITEs it into the
        running fabric's gain cell — live retune, no reflash."""
        self._gain = float(gain)
        self._update_grc_param("gain", self._gain)

    def get_gain(self) -> float:
        """Get current gain value."""
        return self._gain
