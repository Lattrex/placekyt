# SPDX-License-Identifier: GPL-3.0-or-later
"""
Kyttar LZ4 Decoder GRC Block — SRAM-backed LZ4 BLOCK-FORMAT decompressor
(the published LZ4 Block Format Description, Yann Collet). No stock GNU Radio
counterpart.

Consumes the compressed block one byte per item and emits the decompressed byte
stream one byte per item. LZ4 is a back-reference format with a 16-bit offset
field, so a conformant decoder must retain a 64 KB history window — 2048x a
single 32-word cell. The window lives in the SRAM panel
(verification/SRAM_PANEL.md) and only the parse FSM stays in cells; every match
byte is a push-read at ``(wpos - offset) & 0xFFFF``, an address the chip derives
at run time from its own output position.

GR marker; the real DSP runs on the placeKYT-hosted chip. This block keeps the
exact interface (class name, params, ports) so it places/wires identically in
GRC, but does NO in-process placement and streams pure pass-through.
"""

import numpy as np

from .dsp_markers import _PassThrough


class lz4_decoder(_PassThrough):
    """
    LZ4 block-format decoder — SRAM-backed, streaming, byte in / byte out.

    Parameters (mirror ``LZ4DecoderBlock`` VERBATIM):
        device_id:     which Kyttar device to register with.
        window_words:  history-window size in panel words. Must be a power of
                       two and at most 65536 (the 16-bit LZ4 offset field);
                       65536 is the only value that decodes every conformant
                       block.
        panel_hop:     hops from the embedded controller cell to the SRAM panel
                       port (@N).
        read_wr_desc:  the controller's push-read WRITE descriptor word.
        read_jp_desc:  the controller's push-read JUMP descriptor word.
        addr_base:     constant offset added to every lookup key (how a SHARED
                       panel keeps two clients' regions disjoint).
        emit_hop:      hops from the OUT cell through the egress corridor and
                       out of the port (@N).
        out_dest:      destination register / output tag for the emitted byte.
        emit_entry:    entry the emitted byte's JUMP targets.

    The last six are PLACEMENT-DERIVED: the auto-P&R panel template fills them
    in from the geometry it chooses, and hand-setting them is only for a
    hand-placed design.

    Input:  the compressed LZ4 block (one byte per item).
    Output: the decompressed byte stream (one byte per item).
    """

    def __init__(self, device_id: str = "kyttar_0", window_words: int = 65536,
                 panel_hop: int = 1, read_wr_desc: int = 0,
                 read_jp_desc: int = 0, addr_base: int = 0,
                 emit_hop: int = 2, out_dest: int = 0, emit_entry: int = 0):
        super().__init__(name="Kyttar LZ4 Decoder", n_in=1, n_out=1,
                         in_dtype=np.uint8, out_dtype=np.uint8)
        self._device_id = device_id
        self._window_words = int(window_words)
        self._panel_hop = int(panel_hop)
        self._read_wr_desc = int(read_wr_desc)
        self._read_jp_desc = int(read_jp_desc)
        self._addr_base = int(addr_base)
        self._emit_hop = int(emit_hop)
        self._out_dest = int(out_dest)
        self._emit_entry = int(emit_entry)
        self._advertise_grc_params(
            device_id, "LZ4DecoderBlock",
            {"window_words": self._window_words,
             "panel_hop": self._panel_hop,
             "read_wr_desc": self._read_wr_desc,
             "read_jp_desc": self._read_jp_desc,
             "addr_base": self._addr_base,
             "emit_hop": self._emit_hop,
             "out_dest": self._out_dest,
             "emit_entry": self._emit_entry})

    @property
    def cell_count(self) -> int:
        # router + token + literal + offset + matchlen + emit + controller + out
        return 8
