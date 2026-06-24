"""
Kyttar FIR Filter Block for GNURadio

GR marker; the real DSP runs on the placeKYT-hosted chip. This block keeps the
exact GR interface (class name, params, ports) so it places/wires identically in
GRC, but does NO in-process placement and streams pure pass-through.

Copyright 2026 Kyttar Computer Project.
SPDX-License-Identifier: GPL-3.0-or-later
"""

from typing import List

from .dsp_markers import _PassThrough


class fir_filter(_PassThrough):
    """
    Kyttar FIR Filter - Finite Impulse Response Filter

    Implements a FIR filter with configurable coefficients on the chip.
    GR marker; the real DSP runs on the placeKYT-hosted chip.

    Parameters:
        device_id: ID of the kyttar.device to use
        coefficients: List of filter coefficients (taps)
    """

    def __init__(
        self,
        device_id: str = "kyttar_0",
        coefficients: List[float] = None,
    ):
        super().__init__(name="Kyttar FIR Filter", n_in=1, n_out=1)

        self._device_id = device_id

        # Default to a simple lowpass filter if no coefficients provided
        if coefficients is None:
            coefficients = [0.25, 0.5, 0.25]  # Simple 3-tap lowpass

        self._coefficients = list(coefficients)
        self._num_taps = len(self._coefficients)
        # Advertise params for GRC↔placeKYT sync detection (see dsp_markers).
        self._advertise_grc_params(
            device_id, "FIRFilterBlock", {"coefficients": self._coefficients})

    def set_coefficients(self, coefficients: List[float]):
        """Set filter coefficients."""
        self._coefficients = list(coefficients)
        self._num_taps = len(self._coefficients)

    def get_coefficients(self) -> List[float]:
        """Get current filter coefficients."""
        return self._coefficients.copy()

    def get_num_taps(self) -> int:
        """Get number of filter taps."""
        return self._num_taps
