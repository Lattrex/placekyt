"""
Kyttar Decimator Block for GNURadio

A decimating FIR filter block; combines lowpass filtering with sample rate
reduction.

GR marker; the real DSP runs on the placeKYT-hosted chip. This block keeps the
exact GR interface (class name, params, ports) so it places/wires identically in
GRC, but does NO in-process placement and streams pure pass-through (the actual
decimation runs on the chip).

Copyright 2026 Kyttar Computer Project.
SPDX-License-Identifier: GPL-3.0-or-later
"""

from typing import List

from .dsp_markers import _PassThrough


class decimator(_PassThrough):
    """
    Kyttar Decimator - FIR Filter with Downsampling

    Implements a decimating FIR filter on the chip. GR marker; the real DSP
    runs on the placeKYT-hosted chip (this GR block passes samples through).

    Parameters:
        device_id: ID of the kyttar.device to use
        coefficients: FIR filter coefficients (anti-aliasing filter)
        decimation: Decimation factor (output one sample per N inputs)
    """

    def __init__(
        self,
        device_id: str = "kyttar_0",
        coefficients: List[float] = None,
        decimation: int = 4,
    ):
        super().__init__(name="Kyttar Decimator", n_in=1, n_out=1)

        self._device_id = device_id
        self._decimation = decimation

        # Default to a simple averaging filter
        if coefficients is None:
            coefficients = [1.0 / decimation] * decimation

        self._coefficients = list(coefficients)
        self._num_taps = len(self._coefficients)
        # Advertise params for GRC↔placeKYT sync detection (see dsp_markers).
        # placeKYT names the rate `decimation` (matching GR's GRC `decim`).
        self._advertise_grc_params(
            device_id, "DecimatorBlock",
            {"coefficients": self._coefficients, "decimation": decimation})

    def set_coefficients(self, coefficients: List[float]):
        """Set filter coefficients."""
        self._coefficients = list(coefficients)
        self._num_taps = len(self._coefficients)

    def set_decimation(self, decimation: int):
        """Set decimation factor."""
        self._decimation = decimation

    def get_coefficients(self) -> List[float]:
        """Get current filter coefficients."""
        return self._coefficients.copy()

    def get_decimation(self) -> int:
        """Get decimation factor."""
        return self._decimation
