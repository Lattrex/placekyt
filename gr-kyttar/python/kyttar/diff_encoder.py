"""
Kyttar Differential Encoder Block for GNU Radio.

Drop-in for ``digital.diff_encoder_bb(modulus, coding)`` — the DBPSK/DQPSK
precoder ``y[n] = (x[n] + y[n-1] + bias) mod M`` (bias=0 DIFF_DIFFERENTIAL,
bias=1 DIFF_NRZI). GR marker; the real DSP runs on the placeKYT-hosted chip.
This block keeps the exact GR interface (class name, params, ports) so it
places/wires identically in GRC, but does NO in-process placement and streams
pure pass-through.

Copyright 2026 Kyttar Computer Project.
SPDX-License-Identifier: GPL-3.0-or-later
"""

import numpy as np

from .dsp_markers import _PassThrough


class diff_encoder(_PassThrough):
    """
    Kyttar Differential Encoder.

    y[n] = (x[n] + y[n-1]) mod M (DIFF_DIFFERENTIAL) — a differential PSK precoder
    (PSK31, DBPSK, DQPSK). DIFF_NRZI adds a +1 bias (modulus 2 only, matching
    GNU Radio). The exact inverse of ``digital.diff_decoder_bb``.

    Parameters:
        device_id: ID of the kyttar.device to use
        modulus: Modulus M of the code's alphabet (GR default 2)
        coding: "DIFF_DIFFERENTIAL" (GR default) or "DIFF_NRZI"
    """

    def __init__(self, device_id: str = "kyttar_0", modulus: int = 2,
                 coding: str = "DIFF_DIFFERENTIAL"):
        # digital.diff_encoder_bb is a BYTE block (uint8 in/out), not float — declare
        # the byte itemsize so GRC stream connections match (a _bb block, not _ff).
        super().__init__(name="Kyttar Differential Encoder", n_in=1, n_out=1,
                         in_dtype=np.uint8, out_dtype=np.uint8)
        self._device_id = device_id
        self._modulus = int(modulus)
        self._coding = coding
        # Advertise params for GRC<->placeKYT sync detection (see dsp_markers).
        self._advertise_grc_params(
            device_id, "DiffEncoderBlock",
            {"modulus": int(modulus), "coding": coding})

    @property
    def modulus(self) -> int:
        return self._modulus

    @property
    def coding(self) -> str:
        return self._coding

    @property
    def cell_count(self) -> int:
        return 1
