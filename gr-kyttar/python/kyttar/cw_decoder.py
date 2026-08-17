# SPDX-License-Identifier: GPL-3.0-or-later
"""Kyttar CW / Morse Decoder GRC marker — placeKYT ``CWDecoderBlock``.

SRAM-backed CW/Morse decoder (ITU-R M.1677). Decodes an ON/OFF keying envelope
(the ``CWKeyerBlock`` output) back to ASCII text using an adaptive-unit +
reverse-Morse LUT held in the SRAM panel (two passes over panel scratch for the
global-min unit lock). There is NO stock GNU Radio CW decoder, so this is a
placeKYT-native ([Kyttar]) block — still fully placeable in GRC with its parameters.
This class is a pass-through GR MARKER carrying the graph so a flowgraph imports +
runs; the decode runs on the placeKYT-hosted chip.
"""

from .dsp_markers import _PassThrough
import numpy as np


class cw_decoder(_PassThrough):
    """CW / Morse decoder — placeKYT ``CWDecoderBlock`` (SRAM-backed).

    Parameters (mirror ``CWDecoderBlock`` VERBATIM):
        device_id: which Kyttar device to register with.
        threshold: envelope ON/OFF keying threshold (fraction of peak).
        panel_hop: hops from this cell to the SRAM panel port (@N).
        emit_hop:  hops to the emit destination cell (@N).
        emit_dest: destination register for the emitted ASCII char.
        emit_entry: entry point invoked to emit each decoded character.

    Input:  ON/OFF keying envelope samples (Q15).
    Output: decoded ASCII character words.
    """

    def __init__(self, device_id: str = "kyttar_0", threshold: float = 0.3,
                 panel_hop: int = 1, emit_hop: int = 1, emit_dest: int = 25,
                 emit_entry: int = 1, unit_samples: int = 0,
                 read_addr_hop: int = 1, read_dest: int = 5,
                 read_entry: int = 1, read_wr_desc: int = 0,
                 read_jp_desc: int = 0, out_dest: int = 25,
                 emit_jump_entry=None, run_dest: int = 25,
                 run_entry: int = 1, addr_base: int = 0):
        super().__init__(name="Kyttar CW Decoder", n_in=1, n_out=1,
                         in_dtype=np.float32, out_dtype=np.uint8)
        self._device_id = device_id
        self._threshold = float(threshold)
        self._panel_hop = int(panel_hop)
        self._emit_hop = int(emit_hop)
        self._emit_dest = int(emit_dest)
        self._emit_entry = int(emit_entry)
        self._advertise_grc_params(
            device_id, "CWDecoderBlock",
            {"threshold": self._threshold, "panel_hop": self._panel_hop,
             "emit_hop": self._emit_hop, "emit_dest": self._emit_dest,
             "emit_entry": self._emit_entry})

    @property
    def cell_count(self) -> int:
        return 1
