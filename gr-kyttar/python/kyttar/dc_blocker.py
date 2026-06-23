"""
Kyttar DC Blocker Block for GNURadio

GR marker; the real DSP runs on the placeKYT-hosted chip. This block keeps the
exact GR interface (class name, params, ports) so it places/wires identically in
GRC, but does NO in-process placement and streams pure pass-through.

Copyright 2026 Kyttar Computer Project.
SPDX-License-Identifier: GPL-3.0-or-later
"""

from .dsp_markers import _PassThrough


class dc_blocker(_PassThrough):
    """
    Kyttar DC Blocker - High-Pass Filter for DC Removal

    Removes DC offset using an adaptive IIR filter on the chip.
    GR marker; the real DSP runs on the placeKYT-hosted chip.

    Parameters:
        device_id: ID of the kyttar.device to use
        alpha: Adaptation rate (0.001 to 0.1, default 0.01)
    """

    def __init__(
        self,
        device_id: str = "kyttar_0",
        alpha: float = 0.01,
    ):
        super().__init__(name="Kyttar DC Blocker", n_in=1, n_out=1)
        self._device_id = device_id
        self._alpha = alpha

    def set_alpha(self, alpha: float):
        """Set alpha value."""
        self._alpha = alpha

    def get_alpha(self) -> float:
        """Get current alpha value."""
        return self._alpha
