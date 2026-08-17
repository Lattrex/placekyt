# SPDX-License-Identifier: GPL-3.0-or-later
"""
Kyttar Golay(24,12) Decoder GRC Block — SRAM-backed extended binary Golay
syndrome decoder (the inverse of the Golay(24,12) Encoder; same convention
pin: wire = d11..d0 p11..p0 MSB-first, G = [I12 | B], MacWilliams & Sloane).
No stock GNU Radio counterpart.

Consumes 24 received bits (one 0/1 byte per item, LSB read) and emits the 12
CORRECTED data bits MSB-first — corrects any error pattern of weight <= 3 per
codeword (min distance 8). The syndrome -> error-pattern LUT lives in the
SRAM panel (verification/SRAM_PANEL.md §6); >= 4 errors pass the received
data half through (documented known limit).

GR marker; the real DSP runs on the placeKYT-hosted chip. This block keeps the
exact interface (class name, params, ports) so it places/wires identically in
GRC, but does NO in-process placement and streams pure pass-through.
"""

import numpy as np

from .dsp_markers import _PassThrough


class golay_decoder(_PassThrough):
    """
    Golay(24,12) Decoder — SRAM-backed extended Golay syndrome decoder.

    Parameters (mirror ``GolayDecoderBlock`` VERBATIM):
        device_id:     which Kyttar device to register with.
        panel_hop:     hops from the companion controller cell to the SRAM
                       panel port (@N).
        read_addr_hop: hops from the correct cell to its read target (@N).
        read_dest:     DEST register the per-codeword read address is sent to
                       (5 = raw panel R5; template mode: the controller's
                       data register).
        read_entry:    JUMP entry the read trigger targets (1 = raw panel R1;
                       template mode: the controller's ``lookup`` entry).
        read_wr_desc:  the controller's push-read WRITE descriptor word.
        read_jp_desc:  the controller's push-read JUMP descriptor word.

    Input:  received codeword bits (one 0/1 byte per item; 24 per codeword).
    Output: corrected data bits (one 0/1 byte per item; 12 per codeword).
    """

    def __init__(self, device_id: str = "kyttar_0", panel_hop: int = 1,
                 read_addr_hop: int = 1, read_dest: int = 5,
                 read_entry: int = 1, read_wr_desc: int = 0,
                 read_jp_desc: int = 0):
        super().__init__(name="Kyttar Golay(24,12) Decoder", n_in=1, n_out=1,
                         in_dtype=np.uint8, out_dtype=np.uint8)
        self._device_id = device_id
        self._panel_hop = int(panel_hop)
        self._read_addr_hop = int(read_addr_hop)
        self._read_dest = int(read_dest)
        self._read_entry = int(read_entry)
        self._read_wr_desc = int(read_wr_desc)
        self._read_jp_desc = int(read_jp_desc)
        self._advertise_grc_params(
            device_id, "GolayDecoderBlock",
            {"panel_hop": self._panel_hop,
             "read_addr_hop": self._read_addr_hop,
             "read_dest": self._read_dest, "read_entry": self._read_entry,
             "read_wr_desc": self._read_wr_desc,
             "read_jp_desc": self._read_jp_desc})
