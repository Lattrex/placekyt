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
    Kyttar DC Blocker - drop-in for GNU Radio ``filter.dc_blocker_ff``.

    An LTI symmetric-FIR DC notch (delayed input minus a cascade of length-D
    moving averagers) on the chip. GR marker; the real DSP runs on the
    placeKYT-hosted chip.

    Parameters (mirroring GR's dc_blocker_xx verbatim):
        device_id: ID of the kyttar.device to use
        length: the moving-averager delay-line length D (default 32)
        long_form: long form (flatter passband, group delay 2D-2) vs short
            form (group delay D-1)
    """

    def __init__(
        self,
        device_id: str = "kyttar_0",
        length: int = 32,
        long_form: bool = True,
    ):
        super().__init__(name="Kyttar DC Blocker", n_in=1, n_out=1)
        self._device_id = device_id
        self._length = int(length)
        self._long_form = bool(long_form)
        # Advertise params for GRC↔placeKYT sync detection (see dsp_markers).
        self._advertise_grc_params(
            device_id, "DCBlockerBlock",
            {"length": int(length), "long_form": bool(long_form)})

    def set_length(self, length: int):
        """Set delay-line length."""
        self._length = int(length)

    def get_length(self) -> int:
        """Get current delay-line length."""
        return self._length
