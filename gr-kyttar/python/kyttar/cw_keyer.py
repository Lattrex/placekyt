# SPDX-License-Identifier: GPL-3.0-or-later
"""Kyttar Morse / CW Keyer GRC marker — placeKYT ``CWKeyerBlock``.

SRAM-backed Morse/CW keyer (ITU-R M.1677 International Morse code + standard CW
timing: dot=1u, dash=3u, intra-char gap=1u, inter-char gap=3u, word gap=7u). Turns
ASCII text into an ON/OFF keying envelope at the configured WPM. There is NO stock
GNU Radio CW keyer, so this is a placeKYT-native ([Kyttar]) block — still fully
placeable in GRC with its parameters. This class is a pass-through GR MARKER that
carries the graph so a flowgraph imports + runs; the keying runs on the chip.
"""

from .dsp_markers import _PassThrough
import numpy as np


class cw_keyer(_PassThrough):
    """Morse / CW keyer — placeKYT ``CWKeyerBlock`` (SRAM-backed).

    Parameters (mirror ``CWKeyerBlock`` VERBATIM):
        device_id:       which Kyttar device to register with.
        wpm:             keying speed in words-per-minute (PARIS standard).
        samples_per_dot: envelope samples per dot unit.
        edge_samples:    raised-cosine rise/fall length (samples) per element edge.
        charset:         optional restricted character set (None = full ITU table).
        panel_hop:       hops from this cell to the SRAM panel port (@N).
        emit_hop:        hops to the emit destination cell (@N).
        emit_entry:      entry point invoked to emit each character's envelope.

    Input:  ASCII text words (base/step/cnt drive the envelope generator).
    Output: ON/OFF keying envelope samples (Q15).
    """

    def __init__(self, device_id: str = "kyttar_0", wpm: int = 20,
                 samples_per_dot: int = 4800, edge_samples: int = 4,
                 charset=None, panel_hop: int = 1, emit_hop: int = 1,
                 emit_dest: int = 0, emit_entry: int = 1, done_entry: int = 0,
                 read_wr_desc: int = 0, read_jp_desc: int = 0):
        super().__init__(name="Kyttar CW Keyer", n_in=1, n_out=1,
                         in_dtype=np.uint8, out_dtype=np.float32)
        self._device_id = device_id
        self._wpm = int(wpm)
        self._samples_per_dot = int(samples_per_dot)
        self._edge_samples = int(edge_samples)
        self._charset = charset
        self._panel_hop = int(panel_hop)
        self._emit_hop = int(emit_hop)
        self._emit_dest = int(emit_dest) & 0x1F
        self._emit_entry = int(emit_entry)
        self._done_entry = int(done_entry) & 0x1F
        self._read_wr_desc = int(read_wr_desc) & 0xFFFF
        self._read_jp_desc = int(read_jp_desc) & 0xFFFF
        self._advertise_grc_params(
            device_id, "CWKeyerBlock",
            {"wpm": self._wpm, "samples_per_dot": self._samples_per_dot,
             "edge_samples": self._edge_samples, "charset": self._charset,
             "panel_hop": self._panel_hop, "emit_hop": self._emit_hop,
             "emit_dest": self._emit_dest, "emit_entry": self._emit_entry,
             "done_entry": self._done_entry,
             "read_wr_desc": self._read_wr_desc,
             "read_jp_desc": self._read_jp_desc})

    @property
    def cell_count(self) -> int:
        return 1
