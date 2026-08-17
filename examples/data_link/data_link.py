#!/usr/bin/env python3
# -*- coding: utf-8 -*-

#
# SPDX-License-Identifier: GPL-3.0
#
# GNU Radio Python Flow Graph
# Title: Scrambled data link (unpack -> NOT/AND/map -> LFSR -> diff -> loopback)
# Author: Lattrex
# Description: A scrambled DATA-LINK LOOPBACK entirely on the placeKYT chip: payload bytes -> unpack to bits (MSB first) -> bitwise NOT -> AND 0x01 (extract the complemented bit) -> map_bb [1,0] (re-invert) -> additive LFSR scrambler (CCSDS-style mask 0x8A, seed 0x7F, len 7) -> differential encode (mod 2) -> differential decode -> the SAME additive scrambler again (self-inverse in sync = descrambler) -> char/float converter pair -> pack 8 bits -> the original bytes emerge at x16_out. Every stage is a placed Kyttar block; the golden is the IDENTICAL stock-GNU-Radio flowgraph, so the gate proves the whole placed composition is GR-equivalent, and the loopback identity (bytes out == bytes in) is asserted on top. The uchar/float casts at the chip ports are GRC type glue only (spliced on import).
# GNU Radio version: 3.10.12.0

from PyQt5 import Qt
from gnuradio import qtgui
from gnuradio import blocks
from gnuradio import gr
from gnuradio.filter import firdes
from gnuradio.fft import window
import sys
import signal
from PyQt5 import Qt
from argparse import ArgumentParser
from gnuradio.eng_arg import eng_float, intx
from gnuradio import eng_notation
from gnuradio import kyttar
import sip
import threading



class data_link(gr.top_block, Qt.QWidget):

    def __init__(self):
        gr.top_block.__init__(self, "Scrambled data link (unpack -> NOT/AND/map -> LFSR -> diff -> loopback)", catch_exceptions=True)
        Qt.QWidget.__init__(self)
        self.setWindowTitle("Scrambled data link (unpack -> NOT/AND/map -> LFSR -> diff -> loopback)")
        qtgui.util.check_set_qss()
        try:
            self.setWindowIcon(Qt.QIcon.fromTheme('gnuradio-grc'))
        except BaseException as exc:
            print(f"Qt GUI: Could not set Icon: {str(exc)}", file=sys.stderr)
        self.top_scroll_layout = Qt.QVBoxLayout()
        self.setLayout(self.top_scroll_layout)
        self.top_scroll = Qt.QScrollArea()
        self.top_scroll.setFrameStyle(Qt.QFrame.NoFrame)
        self.top_scroll_layout.addWidget(self.top_scroll)
        self.top_scroll.setWidgetResizable(True)
        self.top_widget = Qt.QWidget()
        self.top_scroll.setWidget(self.top_widget)
        self.top_layout = Qt.QVBoxLayout(self.top_widget)
        self.top_grid_layout = Qt.QGridLayout()
        self.top_layout.addLayout(self.top_grid_layout)

        self.settings = Qt.QSettings("gnuradio/flowgraphs", "data_link")

        try:
            geometry = self.settings.value("geometry")
            if geometry:
                self.restoreGeometry(geometry)
        except BaseException as exc:
            print(f"Qt GUI: Could not restore geometry: {str(exc)}", file=sys.stderr)
        self.flowgraph_started = threading.Event()

        ##################################################
        # Variables
        ##################################################
        self.message = message = "KYTTAR DATA LINK 73"
        self.samp_rate = samp_rate = 8000
        self.burst_len = burst_len = len(message)

        ##################################################
        # Blocks
        ##################################################

        self.unpack = kyttar.unpack_k_bits("kyttar_0", 8)
        self.tx_src = kyttar.source(device_id="kyttar_0", port_name="x16_in", num_channels=1, server_host="127.0.0.1", server_port=58950, complex_in=False, burst_len=burst_len, stream_id="tx", pipelined=True, schedule="interleaved", repeat=False, output_words="auto")
        self.tx_sink = kyttar.sink(device_id="kyttar_0", port_name="x16_out", num_channels=1, server_port=58950, server_repeat=True, hold_secs=8.0, stream_id="tx", in_type=False)
        self.to_raw = blocks.multiply_const_ff((1.0/32768.0))
        self.scramble = kyttar.lfsr_scrambler("kyttar_0", 0x8A, 0x7F, 7, 0, 1)
        self.rx_words = blocks.multiply_const_ff(32768.0)
        self.rx_bytes = qtgui.time_sink_f(
            burst_len, #size
            samp_rate, #samp_rate
            "recovered bytes", #name
            1, #number of inputs
            None # parent
        )
        self.rx_bytes.set_update_time(0.10)
        self.rx_bytes.set_y_axis(0, 256)

        self.rx_bytes.set_y_label('byte value', "")

        self.rx_bytes.enable_tags(True)
        self.rx_bytes.set_trigger_mode(qtgui.TRIG_MODE_FREE, qtgui.TRIG_SLOPE_POS, 0.0, 0, 0, "")
        self.rx_bytes.enable_autoscale(True)
        self.rx_bytes.enable_grid(False)
        self.rx_bytes.enable_axis_labels(True)
        self.rx_bytes.enable_control_panel(False)
        self.rx_bytes.enable_stem_plot(True)


        labels = ['Signal 1', 'Signal 2', 'Signal 3', 'Signal 4', 'Signal 5',
            'Signal 6', 'Signal 7', 'Signal 8', 'Signal 9', 'Signal 10']
        widths = [1, 1, 1, 1, 1,
            1, 1, 1, 1, 1]
        colors = ['blue', 'red', 'green', 'black', 'cyan',
            'magenta', 'yellow', 'dark red', 'dark green', 'dark blue']
        alphas = [1.0, 1.0, 1.0, 1.0, 1.0,
            1.0, 1.0, 1.0, 1.0, 1.0]
        styles = [1, 1, 1, 1, 1,
            1, 1, 1, 1, 1]
        markers = [-1, -1, -1, -1, -1,
            -1, -1, -1, -1, -1]


        for i in range(1):
            if len(labels[i]) == 0:
                self.rx_bytes.set_line_label(i, "Data {0}".format(i))
            else:
                self.rx_bytes.set_line_label(i, labels[i])
            self.rx_bytes.set_line_width(i, widths[i])
            self.rx_bytes.set_line_color(i, colors[i])
            self.rx_bytes.set_line_style(i, styles[i])
            self.rx_bytes.set_line_marker(i, markers[i])
            self.rx_bytes.set_line_alpha(i, alphas[i])

        self._rx_bytes_win = sip.wrapinstance(self.rx_bytes.qwidget(), Qt.QWidget)
        self.top_layout.addWidget(self._rx_bytes_win)
        self.remap = kyttar.map_bb("kyttar_0", [1, 0])
        self.pack_b2f = blocks.uchar_to_float()
        self.pack = kyttar.pack_k_bits("kyttar_0", 8)
        self.msg_src = blocks.vector_source_b([ord(c) for c in message], False, 1, [])
        self.mask = kyttar.and_const(device_id="kyttar_0", constant=1)
        self.inv = kyttar.not_bb(device_id="kyttar_0")
        self.f2c = kyttar.float_to_char(device_id="kyttar_0", scale=128.0)
        self.f2b_in = blocks.float_to_uchar(1, 1, 0)
        self.descramble = kyttar.lfsr_scrambler("kyttar_0", 0x8A, 0x7F, 7, 0, 1)
        self.denc = kyttar.diff_encoder("kyttar_0", 2, "DIFF_DIFFERENTIAL")
        self.ddec = kyttar.diff_decoder("kyttar_0", 2, 0)
        self.c2f = kyttar.char_to_float(device_id="kyttar_0", scale=128.0)
        self.b2f_in = blocks.uchar_to_float()


        ##################################################
        # Connections
        ##################################################
        self.connect((self.b2f_in, 0), (self.to_raw, 0))
        self.connect((self.c2f, 0), (self.f2c, 0))
        self.connect((self.ddec, 0), (self.descramble, 0))
        self.connect((self.denc, 0), (self.ddec, 0))
        self.connect((self.descramble, 0), (self.c2f, 0))
        self.connect((self.f2b_in, 0), (self.unpack, 0))
        self.connect((self.f2c, 0), (self.pack, 0))
        self.connect((self.inv, 0), (self.mask, 0))
        self.connect((self.mask, 0), (self.remap, 0))
        self.connect((self.msg_src, 0), (self.b2f_in, 0))
        self.connect((self.pack, 0), (self.pack_b2f, 0))
        self.connect((self.pack_b2f, 0), (self.tx_sink, 0))
        self.connect((self.remap, 0), (self.scramble, 0))
        self.connect((self.rx_words, 0), (self.rx_bytes, 0))
        self.connect((self.scramble, 0), (self.denc, 0))
        self.connect((self.to_raw, 0), (self.tx_src, 0))
        self.connect((self.tx_sink, 0), (self.rx_words, 0))
        self.connect((self.tx_src, 0), (self.f2b_in, 0))
        self.connect((self.unpack, 0), (self.inv, 0))


    def closeEvent(self, event):
        self.settings = Qt.QSettings("gnuradio/flowgraphs", "data_link")
        self.settings.setValue("geometry", self.saveGeometry())
        self.stop()
        self.wait()

        event.accept()

    def get_message(self):
        return self.message

    def set_message(self, message):
        self.message = message
        self.set_burst_len(len(self.message))
        self.msg_src.set_data([ord(c) for c in self.message], [])

    def get_samp_rate(self):
        return self.samp_rate

    def set_samp_rate(self, samp_rate):
        self.samp_rate = samp_rate
        self.rx_bytes.set_samp_rate(self.samp_rate)

    def get_burst_len(self):
        return self.burst_len

    def set_burst_len(self, burst_len):
        self.burst_len = burst_len




def main(top_block_cls=data_link, options=None):

    qapp = Qt.QApplication(sys.argv)

    tb = top_block_cls()

    tb.start()
    tb.flowgraph_started.set()

    tb.show()

    def sig_handler(sig=None, frame=None):
        tb.stop()
        tb.wait()

        Qt.QApplication.quit()

    signal.signal(signal.SIGINT, sig_handler)
    signal.signal(signal.SIGTERM, sig_handler)

    timer = Qt.QTimer()
    timer.start(500)
    timer.timeout.connect(lambda: None)

    qapp.exec_()

if __name__ == '__main__':
    main()
