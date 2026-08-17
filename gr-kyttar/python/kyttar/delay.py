"""
Kyttar Delay Block for GNURadio

GR marker; the real DSP runs on the placeKYT-hosted chip. This block keeps the
exact GR interface (class name, params, ports) so it places/wires identically in
GRC, but does NO in-process placement and streams pure pass-through.

Copyright 2026 Kyttar Computer Project.
SPDX-License-Identifier: GPL-3.0-or-later
"""

from .dsp_markers import _PassThrough


class delay(_PassThrough):
    """
    Kyttar Delay — integer-sample delay line (drop-in for GNU Radio blocks.delay).

    Delays the stream by ``delay`` samples: y[n] = x[n-delay] (prepend ``delay``
    zeros). GR marker; the real DSP runs on the placeKYT-hosted chip.

    Parameters:
        device_id: ID of the kyttar.device to use
        delay: integer sample delay (default 1). Bounded on-chip by the cell's
            register/RAM depth (max 12); a larger delay is rejected at placement.
    """

    def __init__(
        self,
        device_id: str = "kyttar_0",
        delay: int = 1,
    ):
        super().__init__(name="Kyttar Delay", n_in=1, n_out=1)
        self._device_id = device_id
        self._delay = int(delay)
        # Advertise params for GRC↔placeKYT sync detection (see dsp_markers).
        self._advertise_grc_params(device_id, "DelayBlock", {"delay": int(delay)})

    def set_delay(self, delay: int):
        """Set the integer sample delay."""
        self._delay = int(delay)

    def get_delay(self) -> int:
        """Get the current integer sample delay."""
        return self._delay
