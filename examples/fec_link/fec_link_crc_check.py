# SPDX-License-Identifier: GPL-3.0-or-later
"""FRAME VERDICT — recompute the CRC of the recovered bytes host-side and
hold it next to the CHIP-computed TX CRC word. Equality = the frame survived
the channel burst (dispersed by the interleaver, corrected by the Hamming
decoder). An embedded block on purpose: this is host-side display glue, and
stock converter ids are chip-side splice markers to the placeKYT importer.

in0 = the 'txcrc' kyttar sink output (the chip CRC word as a word/32768
      float); in1 = the 'rx' kyttar sink output (recovered bytes, word/32768
      floats; the message starts after n_skip alignment zero bytes).
out0 = chip CRC (0..65535), out1 = host-recomputed CRC, out2 = match flag."""
import numpy as np
from gnuradio import gr


class blk(gr.basic_block):
    def __init__(self, n_skip=6, frame_len=12):
        gr.basic_block.__init__(self, name='CRC Frame Verdict',
                                in_sig=[np.float32, np.float32],
                                out_sig=[np.float32] * 3)
        self.n_skip = int(n_skip)
        self.frame_len = int(frame_len)
        self.chip_crc = None
        self.rx_bytes = []

    def forecast(self, noutput_items, ninputs):
        return [0] * ninputs

    @staticmethod
    def _crc16(data):
        crc = 0xFFFF                       # CRC-16/CCITT-FALSE
        for b in data:
            crc ^= (int(b) & 0xFF) << 8
            for _ in range(8):
                crc = (((crc << 1) ^ 0x1021) if crc & 0x8000
                       else (crc << 1)) & 0xFFFF
        return crc

    def general_work(self, input_items, output_items):
        need = self.n_skip + self.frame_len
        if self.chip_crc is None and len(input_items[0]):
            w = int(round(float(input_items[0][0]) * 32768.0)) & 0xFFFF
            self.chip_crc = w
        if len(self.rx_bytes) < need and len(input_items[1]):
            take = input_items[1][:need - len(self.rx_bytes)]
            self.rx_bytes += [int(round(float(v) * 32768.0)) & 0xFF
                              for v in take]
        for i in range(2):
            if len(input_items[i]):
                self.consume(i, len(input_items[i]))
        if self.chip_crc is None or len(self.rx_bytes) < need:
            return 0
        host = self._crc16(self.rx_bytes[self.n_skip:need])
        n = min(len(o) for o in output_items)
        output_items[0][:n] = float(self.chip_crc)
        output_items[1][:n] = float(host)
        output_items[2][:n] = 1.0 if host == self.chip_crc else 0.0
        return n
