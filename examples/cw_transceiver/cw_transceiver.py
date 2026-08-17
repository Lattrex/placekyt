#!/usr/bin/env python3
# -*- coding: utf-8 -*-

#
# SPDX-License-Identifier: GPL-3.0
#
# GNU Radio Python Flow Graph
# Title: CW full transceiver (duplex, shared SRAM panel)
# Author: Lattrex
# Description: A FULL CW (Morse) TRANSCEIVER on one placeKYT chip: TX (chars -> SRAM-backed keyer -> ITU-R keyed envelope) and RX (keyed audio -> Abs envelope detector -> STREAMING fixed-unit SRAM-backed Morse decoder -> chars) duplexed by stream tags, the keyer ROM and the reverse-Morse LUT (at addr_base 12288) SHARING the single SRAM panel. The decoder is a skimmer locked to the keyer's configured unit (samples_per_dot == unit_samples). Word gaps decode as character boundaries (no spaces - documented v1 cell-budget limit); terminate an RX burst with an EOT blip. PER-SAMPLE PACED (the panel contract).
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



class cw_transceiver(gr.top_block, Qt.QWidget):

    def __init__(self):
        gr.top_block.__init__(self, "CW full transceiver (duplex, shared SRAM panel)", catch_exceptions=True)
        Qt.QWidget.__init__(self)
        self.setWindowTitle("CW full transceiver (duplex, shared SRAM panel)")
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

        self.settings = Qt.QSettings("gnuradio/flowgraphs", "cw_transceiver")

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
        self.unit = unit = 8
        self.rx_sig = rx_sig = sum([sum([[1.0]*(unit if e=='.' else 3*unit)+[0.0]*unit for e in m],[])+[0.0]*2*unit for m in ['.-.','...','-','.....','----.','----.','--...','...--']],[]) + [0.0]*3*unit + [0.9]*2
        self.msg = msg = [ord(c) if c != ' ' else 0 for c in 'CQ CQ DE KYTTAR']
        self.samp_rate = samp_rate = 8000
        self.rx_burst_len = rx_burst_len = len(rx_sig)
        self.burst_len = burst_len = len(msg)

        ##################################################
        # Blocks
        ##################################################

        self.tx_src = kyttar.source(device_id="kyttar_0", port_name="x16_in", num_channels=1, server_host="127.0.0.1", server_port=58950, complex_in=False, burst_len=burst_len, stream_id="tx", pipelined=False, schedule="interleaved", repeat=False, output_words="auto")
        self.tx_sink = kyttar.sink(device_id="kyttar_0", port_name="x16_out", num_channels=1, server_port=58950, server_repeat=False, hold_secs=8.0, stream_id="tx", in_type=False)
        self.tx_scope = qtgui.time_sink_f(
            256, #size
            samp_rate, #samp_rate
            "TX keyed envelope (ITU-R Morse)", #name
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
        self.keyer = kyttar.cw_keyer("kyttar_0", 20, unit, 2, None, 1, 1, 0, 1, 0, 0, 0)
        self.f2b = blocks.float_to_uchar(1, 1, 0)
        self.env = kyttar.abs_bb(device_id="kyttar_0")
        self.cwdec = kyttar.cw_decoder("kyttar_0", 0.3, 1, 1, 25, 1, unit, 1, 5, 1, 0, 0, 25, None, 25, 1, 16384)
        self.b2f_in = blocks.uchar_to_float()


        ##################################################
        # Connections
        ##################################################
        self.connect((self.b2f_in, 0), (self.to_raw, 0))
        self.connect((self.cwdec, 0), (self.rx_b2f, 0))
        self.connect((self.env, 0), (self.cwdec, 0))
        self.connect((self.f2b, 0), (self.keyer, 0))
        self.connect((self.keyer, 0), (self.tx_sink, 0))
        self.connect((self.msg_src, 0), (self.b2f_in, 0))
        self.connect((self.rx_b2f, 0), (self.rx_sink, 0))
        self.connect((self.rx_chars, 0), (self.rx_scope, 0))
        self.connect((self.rx_sink, 0), (self.rx_chars, 0))
        self.connect((self.rx_src, 0), (self.env, 0))
        self.connect((self.rx_vec, 0), (self.rx_src, 0))
        self.connect((self.to_raw, 0), (self.tx_src, 0))
        self.connect((self.tx_sink, 0), (self.tx_scope, 0))
        self.connect((self.tx_src, 0), (self.f2b, 0))


    def closeEvent(self, event):
        self.settings = Qt.QSettings("gnuradio/flowgraphs", "cw_transceiver")
        self.settings.setValue("geometry", self.saveGeometry())
        self.stop()
        self.wait()

        event.accept()

    def get_unit(self):
        return self.unit

    def set_unit(self, unit):
        self.unit = unit
        self.set_rx_sig(sum([sum([[1.0]*(self.unit if e=='.' else 3*self.unit)+[0.0]*self.unit for e in m],[])+[0.0]*2*self.unit for m in ['.-.','...','-','.....','----.','----.','--...','...--']],[]) + [0.0]*3*self.unit + [0.9]*2)

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




def main(top_block_cls=cw_transceiver, options=None):

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
