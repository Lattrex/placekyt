# SPDX-License-Identifier: GPL-3.0-or-later
"""
Kyttar LZ4 Encoder GRC Block — SRAM-backed LZ4 BLOCK-FORMAT compressor
(the published LZ4 Block Format Description, Yann Collet). No stock GNU Radio
counterpart.

Consumes the raw byte stream one byte per item, terminated by an out-of-band
END-OF-BLOCK word (256, above any byte value), and emits the compressed LZ4
block one byte per item.

Compression is a SEARCH, not a transform: to emit a match the encoder compares
the four bytes at the current position against the four at a candidate position
it has already seen, then walks both forward. Both operands are arbitrary
earlier input, so the encoder needs random access to the whole input AND a hash
table to find the candidate. Neither fits in 32-word cells, so both live in the
SRAM panel (verification/SRAM_PANEL.md) in two DISJOINT regions — a split that
is a correctness property, since the panel wraps every address modulo its size
and an overlapping table aliases onto the stored input.

GR marker; the real DSP runs on the placeKYT-hosted chip. This block keeps the
exact interface (class name, params, ports) so it places/wires identically in
GRC, but does NO in-process placement and streams pure pass-through.
"""

import numpy as np

from .dsp_markers import _PassThrough


class lz4_encoder(_PassThrough):
    """
    LZ4 block-format encoder — SRAM-backed, byte in / byte out.

    Parameters (mirror ``LZ4EncoderBlock`` VERBATIM):
        device_id:     which Kyttar device to register with.
        window_words:  size of the panel's history region in words (a power of
                       two). Positions are addressed directly, so this is also
                       the largest input compressed in one block. It must leave
                       room for the hash table inside the 65536-word panel —
                       the two regions MUST be disjoint or the encoder reads
                       hash slots as input bytes and returns a wrong answer
                       silently.
        hash_bits:     the hash table has ``2**hash_bits`` slots. Affects the
                       compression RATIO only, never correctness: every
                       candidate the hash proposes is confirmed by a real
                       four-byte comparison before a match is emitted.
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

    Input:  the raw byte stream (one byte per item), then the 256 sentinel.
    Output: the compressed LZ4 block (one byte per item).
    """

    def __init__(self, device_id: str = "kyttar_0", window_words: int = 32768,
                 hash_bits: int = 12, panel_hop: int = 1,
                 read_wr_desc: int = 0, read_jp_desc: int = 0,
                 addr_base: int = 0, emit_hop: int = 2, out_dest: int = 0,
                 emit_entry: int = 0):
        super().__init__(name="Kyttar LZ4 Encoder", n_in=1, n_out=1,
                         in_dtype=np.uint8, out_dtype=np.uint8)
        self._device_id = device_id
        self._window_words = int(window_words)
        self._hash_bits = int(hash_bits)
        self._panel_hop = int(panel_hop)
        self._read_wr_desc = int(read_wr_desc)
        self._read_jp_desc = int(read_jp_desc)
        self._addr_base = int(addr_base)
        self._emit_hop = int(emit_hop)
        self._out_dest = int(out_dest)
        self._emit_entry = int(emit_entry)
        self._advertise_grc_params(
            device_id, "LZ4EncoderBlock",
            {"window_words": self._window_words,
             "hash_bits": self._hash_bits,
             "panel_hop": self._panel_hop,
             "read_wr_desc": self._read_wr_desc,
             "read_jp_desc": self._read_jp_desc,
             "addr_base": self._addr_base,
             "emit_hop": self._emit_hop,
             "out_dest": self._out_dest,
             "emit_entry": self._emit_entry})

    @property
    def cell_count(self) -> int:
        # ingest, seq, hash, verify, match, token, lenrun, lits, frame, addr,
        # ret, egress, controller, seal
        return 14
