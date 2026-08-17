# SPDX-License-Identifier: GPL-3.0-or-later
"""Kyttar PSK31 Varicode Decoder GRC marker — placeKYT ``VaricodeDecoderBlock``.

SRAM-backed PSK31 Varicode DECODER (the inverse of the encoder; G3PLX / Peter
Martinez table). The reverse code->char map (codeword integer -> ASCII) lives in the
SRAM panel; a small in-cell bit-accumulator forms each codeword between "00"
delimiters and does an SRAM push-read to fetch + emit the char. There is NO stock
GNU Radio Varicode block, so this is a placeKYT-native ([Kyttar]) block — still fully
placeable in GRC with its parameters. This class is a pass-through GR MARKER that
carries the byte graph so a flowgraph imports + runs; the decode runs on the chip.
"""

from .dsp_markers import _PassThrough
import numpy as np


class varicode_decoder(_PassThrough):
    """PSK31 Varicode decoder — placeKYT ``VaricodeDecoderBlock`` (SRAM-backed).

    Parameters (mirror ``VaricodeDecoderBlock`` VERBATIM):
        device_id:     which Kyttar device to register with.
        panel_hop:     hops from this cell to the SRAM panel port (@N).
        read_addr_hop: hops for the panel read-address descriptor (@N).
        char_dest:     destination register for the assembled codeword read.
        emit_entry:    entry point invoked to emit each decoded character.
        emit_hop:      hops to the emit destination cell (@N).
        out_dest:      destination register for the emitted ASCII char.

    Input:  Varicode bit stream ('1'/'0' bits, "00" between chars).
    Output: decoded ASCII character words.
    """

    def __init__(self, device_id: str = "kyttar_0", panel_hop: int = 1,
                 read_addr_hop: int = 1, char_dest: int = 25, emit_entry: int = 2,
                 emit_hop: int = 1, out_dest: int = 25,
                 read_dest: int = 5, read_entry: int = 1,
                 read_wr_desc: int = 0, read_jp_desc: int = 0,
                 emit_jump_entry=None):
        super().__init__(name="Kyttar Varicode Decoder", n_in=1, n_out=1,
                         in_dtype=np.uint8, out_dtype=np.uint8)
        self._device_id = device_id
        self._panel_hop = int(panel_hop)
        self._read_addr_hop = int(read_addr_hop)
        self._char_dest = int(char_dest)
        self._emit_entry = int(emit_entry)
        self._emit_hop = int(emit_hop)
        self._out_dest = int(out_dest)
        self._advertise_grc_params(
            device_id, "VaricodeDecoderBlock",
            {"panel_hop": self._panel_hop, "read_addr_hop": self._read_addr_hop,
             "char_dest": self._char_dest, "emit_entry": self._emit_entry,
             "emit_hop": self._emit_hop, "out_dest": self._out_dest})

    @property
    def cell_count(self) -> int:
        return 1
