"""
Kyttar Squelch Block for GNURadio

A signal level gate (squelch) block; gates the signal based on estimated power.

GR marker; the real DSP runs on the placeKYT-hosted chip. This block keeps the
exact GR interface (class name, params, ports) so it places/wires identically in
GRC, but does NO in-process placement and streams pure pass-through.

Copyright 2026 Kyttar Computer Project.
SPDX-License-Identifier: GPL-3.0-or-later
"""

from .dsp_markers import _PassThrough


class squelch(_PassThrough):
    """
    Kyttar Squelch - Signal Level Gate

    Gates the signal based on power level on the chip.
    GR marker; the real DSP runs on the placeKYT-hosted chip.

    Parameters:
        device_id: ID of the kyttar.device to use
        threshold: Power threshold for gating (0.0 to 1.0)
        hysteresis: Hysteresis amount to prevent rapid cycling
        attack_alpha: Attack smoothing factor (0-1, higher = faster)
        release_alpha: Release smoothing factor (0-1, higher = faster)
    """

    def __init__(
        self,
        device_id: str = "kyttar_0",
        threshold: float = 0.1,
        hysteresis: float = 0.02,
        attack_alpha: float = 0.25,
        release_alpha: float = 0.03,
    ):
        super().__init__(name="Kyttar Squelch", n_in=1, n_out=1)
        self._device_id = device_id
        self._threshold = threshold
        self._hysteresis = hysteresis
        self._attack_alpha = attack_alpha
        self._release_alpha = release_alpha
        # Advertise params for GRC↔placeKYT sync detection (see dsp_markers).
        self._advertise_grc_params(
            device_id, "SquelchBlock",
            {"threshold": threshold, "hysteresis": hysteresis,
             "attack_alpha": attack_alpha, "release_alpha": release_alpha})

    def set_threshold(self, threshold: float):
        """Set squelch threshold."""
        self._threshold = threshold

    def set_hysteresis(self, hysteresis: float):
        """Set hysteresis."""
        self._hysteresis = hysteresis

    def get_threshold(self) -> float:
        """Get current threshold."""
        return self._threshold

    def get_hysteresis(self) -> float:
        """Get current hysteresis."""
        return self._hysteresis
