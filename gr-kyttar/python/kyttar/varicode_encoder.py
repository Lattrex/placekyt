# SPDX-License-Identifier: GPL-3.0-or-later
"""Kyttar PSK31 Varicode Encoder GRC marker — placeKYT ``VaricodeEncoderBlock``.

SRAM-backed PSK31 Varicode encoder (G3PLX / Peter Martinez). Each ASCII character
maps to a variable-length '1'/'0' code that starts+ends with '1' and contains no
two consecutive '0's; characters are separated on the wire by a "00" gap. There is
NO stock GNU Radio Varicode block, so this is a placeKYT-native ([Kyttar]) block —
still fully placeable in GRC with its parameters. It is the FIRST SRAM-backed DSP
block; the real per-character emit runs on the placeKYT-hosted chip against an SRAM
panel (SRAM_PANEL.md §6). This class is a pass-through GR MARKER that carries the
byte graph so a flowgraph imports + runs; the actual encode happens on the chip.
"""

from .dsp_markers import _PassThrough
import numpy as np


class varicode_encoder(_PassThrough):
    """PSK31 Varicode encoder — placeKYT ``VaricodeEncoderBlock`` (SRAM-backed).

    Parameters (mirror ``VaricodeEncoderBlock`` VERBATIM):
        device_id:  which Kyttar device to register with.
        panel_hop:  hops from this cell to the SRAM panel port (@N).
        emit_hop:   hops to the emit destination cell (@N).
        emit_dest:  destination register for the emitted bit burst.
        emit_entry: entry point invoked to emit each character's bit burst.
        read_wr_desc / read_jp_desc: raw 16-bit push-read delivery descriptors for
            the embedded SRAM controller (placement-derived; normally set by
            placeKYT's route builder, not by hand).

    Input:  ASCII character words.
    Output: Varicode bit stream ('1'/'0' bits, MSB first, "00" between chars).
    """

    def __init__(self, device_id: str = "kyttar_0", panel_hop: int = 1,
                 emit_hop: int = 1, emit_dest: int = 25, emit_entry: int = 1,
                 read_wr_desc: int = 0, read_jp_desc: int = 0,
                 addr_base: int = 0):
        super().__init__(name="Kyttar Varicode Encoder", n_in=1, n_out=1,
                         in_dtype=np.uint8, out_dtype=np.uint8)
        self._device_id = device_id
        self._panel_hop = int(panel_hop)
        self._emit_hop = int(emit_hop)
        self._emit_dest = int(emit_dest)
        self._emit_entry = int(emit_entry)
        self._read_wr_desc = int(read_wr_desc) & 0xFFFF
        self._read_jp_desc = int(read_jp_desc) & 0xFFFF
        self._advertise_grc_params(
            device_id, "VaricodeEncoderBlock",
            {"panel_hop": self._panel_hop, "emit_hop": self._emit_hop,
             "emit_dest": self._emit_dest, "emit_entry": self._emit_entry,
             "read_wr_desc": self._read_wr_desc,
             "read_jp_desc": self._read_jp_desc})

    @property
    def cell_count(self) -> int:
        return 1
