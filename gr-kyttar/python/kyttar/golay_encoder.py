# SPDX-License-Identifier: GPL-3.0-or-later
"""
Kyttar Golay(24,12) Encoder GRC Block — extended binary Golay systematic
hard-decision FEC encoder (MacWilliams & Sloane, "The Theory of
Error-Correcting Codes", 1977). No stock GNU Radio counterpart.

Consumes 12 data bits (one 0/1 byte per item, LSB read — the pack_k_bits
convention; the FIRST arriving bit is d11) and emits the 24-bit systematic
codeword MSB-first: d11 .. d0 p11 .. p0 with p11..p0 = m . B (mod 2), the
standard G = [I12 | B] (B = the MacWilliams-Sloane bordered reverse
circulant; minimum distance 8). Rate-expanding 12:24.

GR marker; the real DSP runs on the placeKYT-hosted chip. This block keeps the
exact interface (class name, params, ports) so it places/wires identically in
GRC, but does NO in-process placement and streams pure pass-through.
"""

import numpy as np

from .dsp_markers import _PassThrough


class golay_encoder(_PassThrough):
    """
    Golay(24,12) Encoder — extended binary Golay systematic FEC block code.

    Parameters:
        device_id: Device ID to register with.

    Input: data bits (one 0/1 byte per item, LSB read; first bit = d11).
    Output: codeword bits MSB-first (d11 .. d0 p11 .. p0), 24 per 12 inputs.
    """

    def __init__(self, device_id: str = "kyttar_0"):
        # A bit-stream (_bb-style) block — declare the byte itemsize so GRC
        # stream connections match.
        super().__init__(name="Kyttar Golay(24,12) Encoder", n_in=1, n_out=1,
                         in_dtype=np.uint8, out_dtype=np.uint8)
        self._device_id = device_id
        # Advertise for GRC<->placeKYT sync detection (no params beyond the
        # device: the extended Golay (24,12) is a fixed code).
        self._advertise_grc_params(device_id, "GolayEncoderBlock", {})
