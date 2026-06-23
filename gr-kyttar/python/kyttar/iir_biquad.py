"""
Kyttar IIR Biquad Filter Block for GNURadio

A second-order IIR (Infinite Impulse Response) biquad filter block.

GR marker; the real DSP runs on the placeKYT-hosted chip. This block keeps the
exact GR interface (class name, params, ports) so it places/wires identically in
GRC, but does NO in-process placement and streams pure pass-through.

Copyright 2026 Kyttar Computer Project.
SPDX-License-Identifier: GPL-3.0-or-later
"""

from typing import List

from .dsp_markers import _PassThrough


class iir_biquad(_PassThrough):
    """
    Kyttar IIR Biquad Filter - Second-Order IIR Filter

    Implements a direct form I biquad section on the chip.
    GR marker; the real DSP runs on the placeKYT-hosted chip.

    Parameters:
        device_id: ID of the kyttar.device to use
        b_coeffs: [b0, b1, b2] - feedforward coefficients
        a_coeffs: [a1, a2] - feedback coefficients (normalized, a0=1)
    """

    def __init__(
        self,
        device_id: str = "kyttar_0",
        b_coeffs: List[float] = None,
        a_coeffs: List[float] = None,
    ):
        super().__init__(name="Kyttar IIR Biquad", n_in=1, n_out=1)

        self._device_id = device_id

        # Default coefficients for a simple lowpass at ~0.1 * fs
        if b_coeffs is None:
            b_coeffs = [0.0976, 0.1953, 0.0976]
        if a_coeffs is None:
            a_coeffs = [-0.9428, 0.3333]

        self._b_coeffs = list(b_coeffs)
        self._a_coeffs = list(a_coeffs)

    def set_coefficients(self, b_coeffs: List[float], a_coeffs: List[float]):
        """Set filter coefficients."""
        self._b_coeffs = list(b_coeffs)
        self._a_coeffs = list(a_coeffs)

    def get_b_coeffs(self) -> List[float]:
        """Get feedforward coefficients."""
        return self._b_coeffs.copy()

    def get_a_coeffs(self) -> List[float]:
        """Get feedback coefficients."""
        return self._a_coeffs.copy()
