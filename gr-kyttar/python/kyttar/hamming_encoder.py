# SPDX-License-Identifier: GPL-3.0-or-later
"""
Kyttar Hamming(7,4) Encoder GRC Block — systematic hard-decision FEC encoder
(R. W. Hamming, 1950). No stock GNU Radio counterpart.

Consumes 4 data bits (one 0/1 byte per item, LSB read — the pack_k_bits
convention; the FIRST arriving bit is d3) and emits the 7-bit systematic
codeword MSB-first: d3 d2 d1 d0 p2 p1 p0 with even parity p2 = d3^d2^d1,
p1 = d3^d2^d0, p0 = d3^d1^d0 (the standard G = [I4 | P]). Rate-expanding 4:7.

GR marker; the real DSP runs on the placeKYT-hosted chip. This block keeps the
exact interface (class name, params, ports) so it places/wires identically in
GRC, but does NO in-process placement and streams pure pass-through.
"""

import numpy as np

from .dsp_markers import _PassThrough


class hamming_encoder(_PassThrough):
    """
    Hamming(7,4) Encoder — systematic hard-decision FEC block code.

    Parameters:
        device_id: Device ID to register with.

    Input: data bits (one 0/1 byte per item, LSB read; first bit = d3).
    Output: codeword bits MSB-first (d3 d2 d1 d0 p2 p1 p0), 7 per 4 inputs.
    """

    def __init__(self, device_id: str = "kyttar_0"):
        # A bit-stream (_bb-style) block — declare the byte itemsize so GRC
        # stream connections match.
        super().__init__(name="Kyttar Hamming(7,4) Encoder", n_in=1, n_out=1,
                         in_dtype=np.uint8, out_dtype=np.uint8)
        self._device_id = device_id
        # Advertise for GRC<->placeKYT sync detection (no params beyond the
        # device: Hamming(7,4) is a fixed code).
        self._advertise_grc_params(device_id, "HammingEncoderBlock", {})

    @property
    def cell_count(self) -> int:
        """Number of cells used."""
        return 2
