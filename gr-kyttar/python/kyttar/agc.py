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

    Parameters:
        device_id: ID of the kyttar.device to use
        target: Target output level (0.0 to 1.0, default 0.7)
        rate: Attack/decay rate (0.001 to 0.1, default 0.01)
        initial_gain: Initial gain value (default 0.5)
    """

    def __init__(
        self,
        device_id: str = "kyttar_0",
        target: float = 0.7,
        rate: float = 0.01,
        initial_gain: float = 0.5,
    ):
        super().__init__(name="Kyttar AGC", n_in=1, n_out=1)
        self._device_id = device_id
        self._target = target
        self._rate = rate
        self._initial_gain = initial_gain

    def set_target(self, target: float):
        """Set target level."""
        self._target = target

    def get_target(self) -> float:
        """Get current target level."""
        return self._target

    def set_rate(self, rate: float):
        """Set attack/decay rate."""
        self._rate = rate

    def get_rate(self) -> float:
        """Get current attack/decay rate."""
        return self._rate

    def set_initial_gain(self, gain: float):
        """Set initial gain."""
        self._initial_gain = gain

    def get_initial_gain(self) -> float:
        """Get initial gain value."""
        return self._initial_gain
