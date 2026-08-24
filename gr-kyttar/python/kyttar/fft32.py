"""
Kyttar FFT32 Block for GNURadio

32-point STREAMING radix-2 FFT (single-path delay feedback, DIF): complex in,
complex out, 1:1 rate. Output frames are in BIT-REVERSED bin order (slot k of
a 32-sample frame carries bin bit_reverse_5(k)), scaled FFT/32, with a
31-sample pipeline latency — see the block documentation.

GR marker; the real DSP runs on the placeKYT-hosted chip. This block keeps the
exact GR interface (class name, params, complex ports) so it places/wires
identically in GRC, but does NO in-process computation and streams pure
pass-through.

Copyright 2026 Kyttar Computer Project.
SPDX-License-Identifier: GPL-3.0-or-later
"""

import numpy as np

from .dsp_markers import _PassThrough


class fft32(_PassThrough):
    """
    Kyttar FFT32 — 32-point streaming radix-2 FFT (R2SDF, DIF).

    COMPLEX in / COMPLEX out, one output per input. Output frames are in
    BIT-REVERSED bin order (no reorder buffer), scaled FFT/32, latency 31
    samples. GR marker; the real DSP runs on the placeKYT-hosted chip.

    Parameters:
        device_id: ID of the kyttar.device to use
    """

    def __init__(self, device_id: str = "kyttar_0"):
        super().__init__(name="Kyttar FFT32", n_in=1, n_out=1,
                         in_dtype=np.complex64, out_dtype=np.complex64)
        self._device_id = device_id
        # Advertise params for GRC<->placeKYT sync detection (see dsp_markers).
        self._advertise_grc_params(device_id, "FFT32Block", {})
