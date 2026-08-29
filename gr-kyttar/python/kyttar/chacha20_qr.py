# SPDX-License-Identifier: GPL-3.0-or-later
"""
Kyttar ChaCha20 quarter-round GRC marker — placeKYT ``ChaCha20QRBlock``.

One ChaCha20 quarter round (RFC 8439 §2.1) on four 32-bit words, each carried
as a hi/lo pair of raw 16-bit words. Rate 8:8 — one input word per item, the
8-word result frame bursting on the eighth. There is no stock GNU Radio
counterpart, so this is a placeKYT-native ([Kyttar]) block — still fully
placeable in GRC.

GR marker; the real DSP runs on the placeKYT-hosted chip. This block keeps the
exact interface (class name, params, ports) so it places/wires identically in
GRC, but does NO in-process placement and streams pure pass-through.
"""

import numpy as np

from .dsp_markers import _PassThrough


class chacha20_qr(_PassThrough):
    """
    ChaCha20 quarter round — placeKYT ``ChaCha20QRBlock`` (no GR counterpart).

    Parameters (mirror ``ChaCha20QRBlock`` VERBATIM — it takes none beyond the
    device):
        device_id: Device ID to register with.

    Input:  raw 16-bit words, 8 per quarter round, in the order
            ``a_hi a_lo b_hi b_lo c_hi c_lo d_hi d_lo``.
    Output: raw 16-bit words, the 8-word result frame in the same order.

    These are EXACT 32-bit integers carried as hi/lo halves, not Q15 samples:
    read the stream raw.
    """

    def __init__(self, device_id: str = "kyttar_0"):
        # Raw 16-bit words in, raw 16-bit words out.
        super().__init__(name="Kyttar ChaCha20 Quarter Round", n_in=1, n_out=1,
                         in_dtype=np.int16, out_dtype=np.int16)
        self._device_id = device_id
        # Advertise params for GRC<->placeKYT sync detection (see dsp_markers).
        # ChaCha20QRBlock's constructor takes no params beyond the name.
        self._advertise_grc_params(device_id, "ChaCha20QRBlock", {})

    @property
    def frame_words(self) -> int:
        """Words per quarter-round frame (four 32-bit values as hi/lo pairs)."""
        return 8

    @property
    def cell_count(self) -> int:
        """Number of cells used."""
        return 17
