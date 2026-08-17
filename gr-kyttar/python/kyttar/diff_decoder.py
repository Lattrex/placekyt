# SPDX-License-Identifier: GPL-3.0-or-later
"""
Kyttar Differential Decoder GRC Block.

Differential decoder ``y[n] = (x[n] - x[n-1]) mod M`` — the 1:1 drop-in for
GNU Radio ``digital.diff_decoder_bb`` (the inverse of ``diff_encoder_bb``).

GR marker; the real DSP runs on the placeKYT-hosted chip. This block keeps the
exact GR interface (class name, params, ports) so it places/wires identically in
GRC, but does NO in-process placement and streams pure pass-through.
"""

import numpy as np

from .dsp_markers import _PassThrough

# diff_coding_type enum (mirrors gnuradio.digital).
DIFF_DIFFERENTIAL = 0
DIFF_NRZI = 1


class diff_decoder(_PassThrough):
    """
    Differential Decoder Block (``digital.diff_decoder_bb``).

    ``y[n] = (x[n] - x[n-1]) mod modulus`` with ``x[-1] = 0`` (DIFF_DIFFERENTIAL,
    the default). ``coding = DIFF_NRZI`` (modulus 2 only) uses
    ``y[n] = (x[n] - x[n-1] + 1) mod 2``. Runs on the chip.

    Input: symbol stream (0..modulus-1 as bytes).
    Output: decoded symbol stream.

    Parameters:
        device_id: Device ID to register with.
        modulus: modulus of the code's alphabet (Kyttar: power of two).
        coding: differential coding type (0 = DIFF_DIFFERENTIAL, 1 = DIFF_NRZI).
    """

    def __init__(
        self,
        device_id: str = "kyttar_0",
        modulus: int = 2,
        coding: int = DIFF_DIFFERENTIAL,
    ):
        # digital.diff_decoder_bb is a BYTE block (uint8 in/out), not float — declare
        # the byte itemsize so GRC stream connections match (a _bb block, not _ff).
        super().__init__(name="Kyttar Differential Decoder", n_in=1, n_out=1,
                         in_dtype=np.uint8, out_dtype=np.uint8)
        self._device_id = device_id
        self._modulus = int(modulus)
        self._coding = int(coding)

    @property
    def modulus(self) -> int:
        """Modulus of the code's alphabet."""
        return self._modulus

    @property
    def coding(self) -> int:
        """Differential coding type (0 = DIFF_DIFFERENTIAL, 1 = DIFF_NRZI)."""
        return self._coding
