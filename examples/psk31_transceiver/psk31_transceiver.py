#!/usr/bin/env python3
# -*- coding: utf-8 -*-

#
# SPDX-License-Identifier: GPL-3.0
#
# GNU Radio Python Flow Graph
# Title: PSK31 full transceiver (duplex, shared SRAM panel)
# Author: Lattrex
# Description: A FULL PSK31 TRANSCEIVER on one placeKYT chip: TX (chars -> SRAM-backed Varicode encoder -> differential BPSK -> hold x8 -> raised-cosine envelope) and RX (symbols -> BPSK slicer -> diff decoder -> SRAM-backed Varicode DECODER -> chars) duplexed by stream tags, with BOTH Varicode tables SHARING the single SRAM panel (the encoder's table at addr_base 1024, clear of the decoder's 1..955 reverse map; every panel read carries its own push-read descriptors). PER-SAMPLE PACED (the panel contract; the server enforces it).
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
import math
import sip
import threading



class psk31_transceiver(gr.top_block, Qt.QWidget):

    def __init__(self):
        gr.top_block.__init__(self, "PSK31 full transceiver (duplex, shared SRAM panel)", catch_exceptions=True)
        Qt.QWidget.__init__(self)
        self.setWindowTitle("PSK31 full transceiver (duplex, shared SRAM panel)")
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

        self.settings = Qt.QSettings("gnuradio/flowgraphs", "psk31_transceiver")

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
        self.rx_sig = rx_sig = [0.9 if int(v) else -0.9 for v in __import__('itertools').accumulate([int(c) for c in '001010111100100101011011001101101110011011011100100110101101001111111100'], lambda a,x:(a+x)%2)]
        self.msg = msg = [ord(c) for c in 'CQ CQ DE KYTTAR']
        self.sps = sps = 8
        self.samp_rate = samp_rate = 250
        self.rx_burst_len = rx_burst_len = len(rx_sig)
        self.burst_len = burst_len = len(msg)

        ##################################################
        # Blocks
        ##################################################

        self.repeat = blocks.repeat(gr.sizeof_float*1, sps)
        self.vdec = kyttar.varicode_decoder("kyttar_0", 1, 1, 25, 2, 1, 25, 5, 1, 0, 0, None)
        self.varicode = kyttar.varicode_encoder("kyttar_0", 1, 1, 25, 1, 0, 0, 1024)
        self.tx_src = kyttar.source(device_id="kyttar_0", port_name="x16_in", num_channels=1, server_host="127.0.0.1", server_port=58950, complex_in=False, burst_len=burst_len, stream_id="tx", pipelined=False, schedule="interleaved", repeat=False, output_words="auto")
        self.tx_sink = kyttar.sink(device_id="kyttar_0", port_name="x16_out", num_channels=1, server_port=58950, server_repeat=False, hold_secs=8.0, stream_id="tx", in_type=False)
        self.tx_scope = qtgui.time_sink_f(
            256, #size
            samp_rate, #samp_rate
            "TX baseband (PSK31 shaped)", #name
            1, #number of inputs
            None # parent
        )
        self.tx_scope.set_update_time(0.10)
        self.tx_scope.set_y_axis(-1, 1)

        self.tx_scope.set_y_label('level', "")

        self.tx_scope.enable_tags(True)
        self.tx_scope.set_trigger_mode(qtgui.TRIG_MODE_FREE, qtgui.TRIG_SLOPE_POS, 0.0, 0, 0, "")
        self.tx_scope.enable_autoscale(False)
        self.tx_scope.enable_grid(False)
        self.tx_scope.enable_axis_labels(True)
        self.tx_scope.enable_control_panel(False)
        self.tx_scope.enable_stem_plot(False)


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
                self.tx_scope.set_line_label(i, "Data {0}".format(i))
            else:
                self.tx_scope.set_line_label(i, labels[i])
            self.tx_scope.set_line_width(i, widths[i])
            self.tx_scope.set_line_color(i, colors[i])
            self.tx_scope.set_line_style(i, styles[i])
            self.tx_scope.set_line_marker(i, markers[i])
            self.tx_scope.set_line_alpha(i, alphas[i])

        self._tx_scope_win = sip.wrapinstance(self.tx_scope.qwidget(), Qt.QWidget)
        self.top_layout.addWidget(self._tx_scope_win)
        self.to_raw = blocks.multiply_const_ff((1.0/32768.0))
        self.slicer = kyttar.bpsk_slicer("kyttar_0", "bit")
        self.rx_vec = blocks.vector_source_f(rx_sig, False, 1, [])
        self.rx_src = kyttar.source(device_id="kyttar_0", port_name="x16_in", num_channels=1, server_host="127.0.0.1", server_port=58950, complex_in=False, burst_len=rx_burst_len, stream_id="rx", pipelined=False, schedule="interleaved", repeat=False, output_words="auto")
        self.rx_sink = kyttar.sink(device_id="kyttar_0", port_name="x16_out", num_channels=1, server_port=58950, server_repeat=True, hold_secs=8.0, stream_id="rx", in_type=False)
        self.rx_scope = qtgui.time_sink_f(
            8, #size
            samp_rate, #samp_rate
            "RX decoded chars (ASCII codes)", #name
            1, #number of inputs
            None # parent
        )
        self.rx_scope.set_update_time(0.10)
        self.rx_scope.set_y_axis(0, 128)

        self.rx_scope.set_y_label('level', "")

        self.rx_scope.enable_tags(True)
        self.rx_scope.set_trigger_mode(qtgui.TRIG_MODE_FREE, qtgui.TRIG_SLOPE_POS, 0.0, 0, 0, "")
        self.rx_scope.enable_autoscale(False)
        self.rx_scope.enable_grid(False)
        self.rx_scope.enable_axis_labels(True)
        self.rx_scope.enable_control_panel(False)
        self.rx_scope.enable_stem_plot(False)


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
                self.rx_scope.set_line_label(i, "Data {0}".format(i))
            else:
                self.rx_scope.set_line_label(i, labels[i])
            self.rx_scope.set_line_width(i, widths[i])
            self.rx_scope.set_line_color(i, colors[i])
            self.rx_scope.set_line_style(i, styles[i])
            self.rx_scope.set_line_marker(i, markers[i])
            self.rx_scope.set_line_alpha(i, alphas[i])

        self._rx_scope_win = sip.wrapinstance(self.rx_scope.qwidget(), Qt.QWidget)
        self.top_layout.addWidget(self._rx_scope_win)
        self.rx_chars = blocks.multiply_const_ff(32768.0)
        self.rx_b2f = blocks.uchar_to_float()
        self.msg_src = blocks.vector_source_b(msg, False, 1, [])
        self.mapper = kyttar.psk_symbol_mapper("kyttar_0", "bpsk", [], 1, True)
        self.f2b = blocks.float_to_uchar(1, 1, 0)
        self.envelope = kyttar.raised_cosine_envelope("kyttar_0", sps)
        self.diff = kyttar.diff_encoder("kyttar_0", 2, "DIFF_DIFFERENTIAL")
        self.ddec = kyttar.diff_decoder("kyttar_0", 2, 0)
        self.c2r = blocks.complex_to_real(1)
        self.b2f_in = blocks.uchar_to_float()
        self.b2f = blocks.uchar_to_float()


        ##################################################
        # Connections
        ##################################################
        self.connect((self.b2f, 0), (self.mapper, 0))
        self.connect((self.b2f_in, 0), (self.to_raw, 0))
        self.connect((self.c2r, 0), (self.repeat, 0))
        self.connect((self.ddec, 0), (self.vdec, 0))
        self.connect((self.diff, 0), (self.b2f, 0))
        self.connect((self.envelope, 0), (self.tx_sink, 0))
        self.connect((self.f2b, 0), (self.varicode, 0))
        self.connect((self.mapper, 0), (self.c2r, 0))
        self.connect((self.msg_src, 0), (self.b2f_in, 0))
        self.connect((self.repeat, 0), (self.envelope, 0))
        self.connect((self.rx_b2f, 0), (self.rx_sink, 0))
        self.connect((self.rx_chars, 0), (self.rx_scope, 0))
        self.connect((self.rx_sink, 0), (self.rx_chars, 0))
        self.connect((self.rx_src, 0), (self.slicer, 0))
        self.connect((self.rx_vec, 0), (self.rx_src, 0))
        self.connect((self.slicer, 0), (self.ddec, 0))
        self.connect((self.to_raw, 0), (self.tx_src, 0))
        self.connect((self.tx_sink, 0), (self.tx_scope, 0))
        self.connect((self.tx_src, 0), (self.f2b, 0))
        self.connect((self.varicode, 0), (self.diff, 0))
        self.connect((self.vdec, 0), (self.rx_b2f, 0))


    def closeEvent(self, event):
        self.settings = Qt.QSettings("gnuradio/flowgraphs", "psk31_transceiver")
        self.settings.setValue("geometry", self.saveGeometry())
        self.stop()
        self.wait()

        event.accept()

    def get_rx_sig(self):
        return self.rx_sig

    def set_rx_sig(self, rx_sig):
        self.rx_sig = rx_sig
        self.set_rx_burst_len(len(self.rx_sig))
        self.rx_vec.set_data(self.rx_sig, [])

    def get_msg(self):
        return self.msg

    def set_msg(self, msg):
        self.msg = msg
        self.set_burst_len(len(self.msg))
        self.msg_src.set_data(self.msg, [])

    def get_sps(self):
        return self.sps

    def set_sps(self, sps):
        self.sps = sps
        self.repeat.set_interpolation(self.sps)

    def get_samp_rate(self):
        return self.samp_rate

    def set_samp_rate(self, samp_rate):
        self.samp_rate = samp_rate
        self.tx_scope.set_samp_rate(self.samp_rate)
        self.rx_scope.set_samp_rate(self.samp_rate)

    def get_rx_burst_len(self):
        return self.rx_burst_len

    def set_rx_burst_len(self, rx_burst_len):
        self.rx_burst_len = rx_burst_len

    def get_burst_len(self):
        return self.burst_len

    def set_burst_len(self, burst_len):
        self.burst_len = burst_len




def main(top_block_cls=psk31_transceiver, options=None):

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
