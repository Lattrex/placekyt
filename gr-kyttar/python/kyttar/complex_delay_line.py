"""
Kyttar Complex Delay Line Block for GNURadio

A pure delay of ``depth`` complex samples: out[n] = in[n-depth], complex zeros
for n < depth. Both I/Q rails delayed identically (no skew), bit-exact.

GR marker; the real DSP runs on the placeKYT-hosted chip. This block keeps the
exact GR interface (class name, params, complex ports) so it places/wires
identically in GRC, but does NO in-process placement and streams pure
pass-through.

Copyright 2026 Kyttar Computer Project.
SPDX-License-Identifier: GPL-3.0-or-later
"""

import numpy as np

from .dsp_markers import _PassThrough


class complex_delay_line(_PassThrough):
    """
    Kyttar Complex Delay Line — integer complex-sample delay.

    out[n] = in[n-depth] (complex zeros for n < depth). Both rails are delayed
    by exactly the same amount, sample-for-sample. COMPLEX in / COMPLEX out.
    GR marker; the real DSP runs on the placeKYT-hosted chip.

    Parameters:
        device_id: ID of the kyttar.device to use
        depth: integer complex-sample delay (default 32; 0 = identity). Bounded
            on-chip by the verified multi-cell chain (max 64, 13 cells); a
            larger depth is rejected at placement.
    """

    def __init__(
        self,
        device_id: str = "kyttar_0",
        depth: int = 32,
    ):
        # COMPLEX -> COMPLEX. Explicit dtypes so a real GRC flowgraph wires
        # complex-to-complex.
        super().__init__(name="Kyttar Complex Delay Line", n_in=1, n_out=1,
                         in_dtype=np.complex64, out_dtype=np.complex64)
        self._device_id = device_id
        self._depth = int(depth)
        # Advertise params for GRC<->placeKYT sync detection (see dsp_markers).
        self._advertise_grc_params(device_id, "ComplexDelayLineBlock",
                                   {"depth": int(depth)})

    def set_depth(self, depth: int):
        """Set the integer complex-sample delay."""
        self._depth = int(depth)

    def get_depth(self) -> int:
        """Get the current integer complex-sample delay."""
        return self._depth
