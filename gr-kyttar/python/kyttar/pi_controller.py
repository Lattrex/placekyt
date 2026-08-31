"""
Kyttar PI Controller Block for GNURadio

GR marker; the real DSP runs on the placeKYT-hosted chip. This block keeps the
exact GR interface (class name, params, ports) so it places/wires identically
in GRC, but does NO in-process placement and streams pure pass-through.

Copyright 2026 Kyttar Computer Project.
SPDX-License-Identifier: GPL-3.0-or-later
"""

from .dsp_markers import _PassThrough


class pi_controller(_PassThrough):
    """
    Kyttar PI Controller - discrete PI loop with a 32-bit anti-windup
    integrator (the FOC current-loop primitive).

    u = sat(kp*e + integral, +/-limit); integral += ki*e with conditional-skip
    anti-windup. GR marker; the real DSP runs on the placeKYT-hosted chip.

    Parameters (PIControllerBlock verbatim):
        device_id: ID of the kyttar.device to use
        kp: proportional gain, |kp| < 1
        ki: integral gain per sample, |ki| < 1
        limit: symmetric output saturation bound, 0 < limit <= 1
    """

    def __init__(
        self,
        device_id: str = "kyttar_0",
        kp: float = 0.25,
        ki: float = 0.01,
        limit: float = 1.0,
    ):
        super().__init__(name="Kyttar PI Controller", n_in=1, n_out=1)
        self._device_id = device_id
        self._kp = kp
        self._ki = ki
        self._limit = limit
        # Advertise params for GRC<->placeKYT sync detection (see dsp_markers).
        # Names are the PIControllerBlock class params VERBATIM.
        self._advertise_grc_params(
            device_id, "PIControllerBlock",
            {"kp": kp, "ki": ki, "limit": limit})

    def set_kp(self, kp: float):
        """Set the proportional gain."""
        self._kp = kp

    def get_kp(self) -> float:
        """Get the proportional gain."""
        return self._kp

    def set_ki(self, ki: float):
        """Set the integral gain."""
        self._ki = ki

    def get_ki(self) -> float:
        """Get the integral gain."""
        return self._ki

    def set_limit(self, limit: float):
        """Set the output saturation bound."""
        self._limit = limit

    def get_limit(self) -> float:
        """Get the output saturation bound."""
        return self._limit
