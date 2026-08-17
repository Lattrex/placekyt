"""
Kyttar Nlog10 Block for GNURadio

GR marker; the real DSP runs on the placeKYT-hosted chip. This block keeps the
exact GR interface (class name, params, ports) of blocks.nlog10_ff so it
places/wires identically in GRC, but does NO in-process placement and streams
pure pass-through.

Copyright 2026 Kyttar Computer Project.
SPDX-License-Identifier: GPL-3.0-or-later
"""

from .dsp_markers import _PassThrough


class nlog10(_PassThrough):
    """
    Kyttar Nlog10 - power/level to decibels.

    Drop-in for GNU Radio blocks.nlog10_ff: ``out = n*log10(in) + k``. GR marker;
    the real DSP runs on the placeKYT-hosted chip.

    HARDWARE DEVIATION (Q15 fabric): the chip emits a SCALED dB word
    ``out/32768 == (n*log10(in)+k)/db_scale`` with ``db_scale`` an auto-derived
    power of two (64 for the default n=10, k=0), because a dB value is ~45x
    outside the Q15 [-1, 1) range. Recover true dB by multiplying by db_scale.

    Parameters:
        device_id: ID of the kyttar.device to use
        n: output scale (GR default 10.0)
        k: output offset in dB (GR default 0.0)
    """

    def __init__(
        self,
        device_id: str = "kyttar_0",
        n: float = 10.0,
        k: float = 0.0,
    ):
        super().__init__(name="Kyttar Nlog10", n_in=1, n_out=1)
        self._device_id = device_id
        self._n = n
        self._k = k
        # Advertise params for GRC↔placeKYT sync detection (see dsp_markers). n and
        # k map 1:1 onto the placeKYT Nlog10Block fields of the same name.
        self._advertise_grc_params(
            device_id, "Nlog10Block", {"n": n, "k": k})

    def set_n(self, n: float):
        """Set the output scale n."""
        self._n = n

    def get_n(self) -> float:
        """Get the output scale n."""
        return self._n

    def set_k(self, k: float):
        """Set the dB offset k."""
        self._k = k

    def get_k(self) -> float:
        """Get the dB offset k."""
        return self._k
