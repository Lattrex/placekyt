"""
Kyttar Complex FIR Filter Block for GNURadio

Complex (I/Q in, I/Q out) FIR sharing ONE set of real taps — a drop-in for GNU
Radio's filter.fir_filter_ccf. The same real coefficients filter the I and the Q
rail independently.

GR marker; the real DSP runs on the placeKYT-hosted chip. This block keeps the
exact GR interface (class name, params, complex ports) so it places/wires
identically in GRC, but does NO in-process placement and streams pure
pass-through.

Copyright 2026 Kyttar Computer Project.
SPDX-License-Identifier: GPL-3.0-or-later
"""

from typing import List

import numpy as np

from .dsp_markers import _PassThrough


class complex_fir_filter(_PassThrough):
    """
    Kyttar Complex FIR Filter — complex FIR with ONE shared real tap set.

    out_i = FIR(in_i, coefficients),  out_q = FIR(in_q, coefficients)

    COMPLEX in / COMPLEX out, matching GNU Radio's filter.fir_filter_ccf. GR
    marker; the real DSP runs on the placeKYT-hosted chip.

    Parameters (VERBATIM the GR block's names; ``coefficients`` = GR ``taps``):
        device_id:     ID of the kyttar.device to use
        coefficients:  the real FIR taps
        decimation:    output decimation factor (must be 1 — not supported yet)
        interpolation: output interpolation factor (must be 1 — not supported yet)
    """

    def __init__(
        self,
        device_id: str = "kyttar_0",
        coefficients: List[float] = None,
        decimation: int = 1,
        interpolation: int = 1,
    ):
        # COMPLEX -> COMPLEX (fir_filter_ccf). Explicit dtypes so a real GRC
        # flowgraph wires complex-to-complex.
        super().__init__(name="Kyttar Complex FIR Filter", n_in=1, n_out=1,
                         in_dtype=np.complex64, out_dtype=np.complex64)
        self._device_id = device_id
        if coefficients is None:
            coefficients = [0.25, 0.5, 0.25]  # simple 3-tap lowpass
        self._coefficients = list(coefficients)
        self._num_taps = len(self._coefficients)
        self._decimation = int(decimation)
        self._interpolation = int(interpolation)
        self._advertise_grc_params(device_id, "ComplexFIRFilterBlock", {
            "coefficients": list(coefficients),
            "decimation": int(decimation),
            "interpolation": int(interpolation)})

    def set_coefficients(self, coefficients: List[float]):
        """Set filter coefficients (shared across the I and Q rail)."""
        self._coefficients = list(coefficients)
        self._num_taps = len(self._coefficients)

    def get_coefficients(self) -> List[float]:
        """Get current filter coefficients."""
        return self._coefficients.copy()

    def get_num_taps(self) -> int:
        """Get number of filter taps."""
        return self._num_taps
