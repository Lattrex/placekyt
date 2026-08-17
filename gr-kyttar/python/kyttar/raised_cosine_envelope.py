# SPDX-License-Identifier: GPL-3.0-or-later
"""Kyttar Raised-Cosine Envelope GRC marker — placeKYT ``RaisedCosineEnvelopeBlock``.

Generates the PSK31 raised-cosine (half-sine) symbol-shaping envelope
``env[n] = sin((n+0.5)*pi/N)`` on the fly for any ``samples_per_symbol`` (N), reusing
the proven NCO quarter-wave sine machinery (a 33-entry table independent of N). There
is NO stock GNU Radio counterpart, so this is a placeKYT-native ([Kyttar]) block —
still fully placeable in GRC with its parameter. This class is a pass-through GR
MARKER that carries the graph so a flowgraph imports + runs; the shaping runs on the
placeKYT-hosted chip.
"""

from .dsp_markers import _PassThrough
import numpy as np


class raised_cosine_envelope(_PassThrough):
    """Raised-cosine (half-sine) symbol envelope — placeKYT
    ``RaisedCosineEnvelopeBlock``.

    Parameters (mirror ``RaisedCosineEnvelopeBlock`` VERBATIM):
        device_id:           which Kyttar device to register with.
        samples_per_symbol:  envelope length N (PSK31 default 256).

    Input:  symbol values (Q15).
    Output: raised-cosine-shaped samples (Q15).
    """

    def __init__(self, device_id: str = "kyttar_0", samples_per_symbol: int = 256):
        super().__init__(name="Kyttar Raised-Cosine Envelope", n_in=1, n_out=1,
                         in_dtype=np.float32, out_dtype=np.float32)
        self._device_id = device_id
        self._samples_per_symbol = int(samples_per_symbol)
        self._advertise_grc_params(
            device_id, "RaisedCosineEnvelopeBlock",
            {"samples_per_symbol": self._samples_per_symbol})

    @property
    def cell_count(self) -> int:
        return 1
