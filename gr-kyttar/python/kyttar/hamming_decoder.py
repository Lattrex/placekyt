# SPDX-License-Identifier: GPL-3.0-or-later
"""
Kyttar Hamming(7,4) Decoder GRC Block — systematic hard-decision syndrome
decoder (NO stock GNU Radio counterpart; golden = the standard textbook
syndrome decoder, Hamming 1950 / Lin & Costello §3.3).

Consumes 7 received bits (one 0/1 byte per item, LSB used), corrects any
single-bit error via the 3-bit syndrome, and emits the 4 corrected data bits
MSB-first (rate-REDUCING 7:4). Convention (shared verbatim with the Hamming
encoder): codeword MSB-first on the wire = d3 d2 d1 d0 p2 p1 p0, even parity
p2=d3^d2^d1, p1=d3^d2^d0, p0=d3^d1^d0. Double-bit errors are uncorrectable
(distance 3) — the code's standard limit.

GR marker; the real DSP runs on the placeKYT-hosted chip. This block keeps the
exact interface (class name, params, ports) so it places/wires identically in
GRC, but does NO in-process placement and streams pure pass-through.
"""

import numpy as np

from .dsp_markers import _PassThrough


class hamming_decoder(_PassThrough):
    """
    Hamming(7,4) Decoder — systematic hard-decision syndrome decoder.

    Parameters:
        device_id: Device ID to register with. (No DSP parameters — the code,
            the systematic layout and the bit order are the pinned convention;
            there is no GNU Radio counterpart whose parameter set could be
            mirrored.)

    Input: received bits (byte 0/1, LSB used), 7 per codeword, MSB (d3) first.
    Output: corrected data bits (byte 0/1), 4 per codeword, MSB (d3) first.
    """

    def __init__(self, device_id: str = "kyttar_0"):
        # A bit-stream (_bb-style) block: declare byte itemsize so GRC stream
        # connections match the Pack/Unpack K Bits convention.
        super().__init__(name="Kyttar Hamming(7,4) Decoder", n_in=1, n_out=1,
                         in_dtype=np.uint8, out_dtype=np.uint8)
        self._device_id = device_id
        # Advertise for GRC<->placeKYT sync detection (see dsp_markers). The
        # block class takes no DSP params.
        self._advertise_grc_params(device_id, "HammingDecoderBlock", {})

    @property
    def cell_count(self) -> int:
        """Number of cells used on the chip."""
        return 2
