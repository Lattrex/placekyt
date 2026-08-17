# SPDX-License-Identifier: GPL-3.0-or-later
"""
Kyttar CRC-16 GRC marker — placeKYT ``Crc16Block``.

Frame CRC-16 for the data-link demo: the published bit-serial MSB-first
(non-reflected) CRC-16 (ITU-T V.41 / CRC RevEng catalogue family;
CRC-16/CCITT-FALSE at the defaults poly=0x1021, init=0xFFFF) over fixed-length
frames of a byte stream. Rate-reducing: frame_len bytes in -> ONE 16-bit CRC
word out; the register re-arms to init per frame. There is NO stock GNU Radio
streaming CRC block (GR's CRC blocks are tagged-PDU/packet blocks), so this is
a placeKYT-native ([Kyttar]) block — still fully placeable in GRC with its
parameters.

GR marker; the real DSP runs on the placeKYT-hosted chip. This block keeps the
exact interface (class name, params, ports) so it places/wires identically in
GRC, but does NO in-process placement and streams pure pass-through.
"""

import numpy as np

from .dsp_markers import _PassThrough


class crc16(_PassThrough):
    """
    Frame CRC-16 — placeKYT ``Crc16Block`` (no GR counterpart).

    Parameters (mirror ``Crc16Block`` VERBATIM):
        device_id: Device ID to register with.
        poly: 16-bit generator polynomial (default 0x1021, CCITT/V.41).
        init: CRC register initial value per frame (default 0xFFFF —
            CRC-16/CCITT-FALSE).
        frame_len: bytes per frame; one CRC word out per frame (default 8).

    Input:  Byte stream (low 8 bits of each item used).
    Output: 16-bit CRC word (one per frame_len input bytes).
    """

    def __init__(self, device_id: str = "kyttar_0", poly: int = 0x1021,
                 init: int = 0xFFFF, frame_len: int = 8):
        # Byte stream in, raw 16-bit CRC word out (a _bs-shaped block).
        super().__init__(name="Kyttar CRC-16", n_in=1, n_out=1,
                         in_dtype=np.uint8, out_dtype=np.int16)
        self._device_id = device_id
        self._poly = int(poly) & 0xFFFF
        self._init = int(init) & 0xFFFF
        self._frame_len = int(frame_len)
        # Advertise params for GRC<->placeKYT sync detection (see dsp_markers).
        # Names match Crc16Block's constructor kwargs verbatim.
        self._advertise_grc_params(
            device_id, "Crc16Block",
            {"poly": self._poly, "init": self._init,
             "frame_len": self._frame_len})

    @property
    def frame_len(self) -> int:
        return self._frame_len

    @property
    def cell_count(self) -> int:
        """Number of cells used."""
        return 1
