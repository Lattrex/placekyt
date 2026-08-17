# SPDX-License-Identifier: GPL-3.0-or-later
"""Kyttar SRAM Controller GRC marker — placeKYT ``SramControllerBlock``.

Single-cell memory controller for an SRAM panel (SRAM_PANEL.md). Owns ALL panel
sequencing (auto-incrementing write/read addresses) so upstream blocks just stream
data; drives the SRAM panel register protocol over the chip port it faces. There is
NO stock GNU Radio counterpart, so this is a placeKYT-native ([Kyttar]) block — still
fully placeable in GRC with its parameters. This class is a pass-through GR MARKER
that carries the graph so a flowgraph imports + runs; the controller runs on the
placeKYT-hosted chip.
"""

from .dsp_markers import _PassThrough
import numpy as np


class sram_controller(_PassThrough):
    """SRAM panel memory-controller — placeKYT ``SramControllerBlock``.

    Parameters (mirror ``SramControllerBlock`` VERBATIM):
        device_id:    which Kyttar device to register with.
        panel_hop:    hops from this cell to exit the panel port (@N). Default 1.
        read_wr_desc: raw 16-bit WRITE descriptor the panel re-emits on a read
                      (the push-read WRITE target; see SRAM_PANEL.md §3).
        read_jp_desc: raw 16-bit JUMP descriptor the panel re-emits on a read
                      (the push-read JUMP target; see SRAM_PANEL.md §3).
        primary_entry: which entry a plain upstream net JUMPs into — 'write'
                      (default, the load-phase streaming convention) or 'lookup'
                      (random-access read keyed by each incoming word).

    Input:  data words to write / read-address triggers.
    Output: panel-protocol WRITE/JUMP hops (read values land per the descriptors).
    """

    def __init__(self, device_id: str = "kyttar_0", panel_hop: int = 1,
                 read_wr_desc: int = 0, read_jp_desc: int = 0,
                 primary_entry: str = "write", addr_base: int = 0):
        super().__init__(name="Kyttar SRAM Controller", n_in=1, n_out=1,
                         in_dtype=np.uint8, out_dtype=np.uint8)
        self._device_id = device_id
        self._panel_hop = int(panel_hop)
        self._read_wr_desc = int(read_wr_desc) & 0xFFFF
        self._read_jp_desc = int(read_jp_desc) & 0xFFFF
        self._primary_entry = str(primary_entry)
        self._advertise_grc_params(
            device_id, "SramControllerBlock",
            {"panel_hop": self._panel_hop, "read_wr_desc": self._read_wr_desc,
             "read_jp_desc": self._read_jp_desc,
             "primary_entry": self._primary_entry})

    @property
    def cell_count(self) -> int:
        return 1
