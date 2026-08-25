#!/usr/bin/env python3
# -*- coding: utf-8 -*-

#
# SPDX-License-Identifier: GPL-3.0
#
# GNU Radio Python Flow Graph
# Title: Kyttar FFT128 — a 128-point transform across the 2P2S board's chain A
# Author: Lattrex
# Description: FFT128 on the 2P2S board: chain A's head (chip 0) runs stage 0, its tail (chip 1) runs stages 1..6, joined by the board's on-carrier series link. Driven through the placeKYT multi-chip GNURadio server.
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



class fft128_2p2s_demo(gr.top_block, Qt.QWidget):

    def __init__(self):
        gr.top_block.__init__(self, "Kyttar FFT128 — a 128-point transform across the 2P2S board's chain A", catch_exceptions=True)
        Qt.QWidget.__init__(self)
        self.setWindowTitle("Kyttar FFT128 — a 128-point transform across the 2P2S board's chain A")
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

        self.settings = Qt.QSettings("gnuradio/flowgraphs", "fft128_2p2s_demo")

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
        self.server_port = server_port = 58950
        self.scope_points = scope_points = 320
        self.burst_len = burst_len = 384

        ##################################################
        # Blocks
        ##################################################

        self.stim = blocks.vector_source_c([0.45*__import__('cmath').exp(2j*3.141592653589793*9*n/128) + 0.35*__import__('cmath').exp(2j*3.141592653589793*37*n/128) for n in range(burst_len)], True, 1, [])
        self.kyt_src = kyttar.source(device_id="kyttar_0", port_name="x16_in", num_channels=1, server_host="127.0.0.1", server_port=server_port, complex_in=True, burst_len=burst_len, stream_id="fft", pipelined=False, schedule="interleaved", repeat=False, output_words="q15")
        self.kyt_sink = kyttar.sink(device_id="kyttar_0", port_name="x16_out", num_channels=1, server_port=server_port, server_repeat=True, hold_secs=8.0, stream_id="fft", in_type=True)
        self.in_scope = qtgui.time_sink_f(
            scope_points, #size
            scope_points, #samp_rate
            "input |x[n]| (two tones)", #name
            1, #number of inputs
            None # parent
        )
        self.in_scope.set_update_time(0.10)
        self.in_scope.set_y_axis(-1, 1)

        self.in_scope.set_y_label('Magnitude', "")

        self.in_scope.enable_tags(True)
        self.in_scope.set_trigger_mode(qtgui.TRIG_MODE_FREE, qtgui.TRIG_SLOPE_POS, 0.0, 0, 0, "")
        self.in_scope.enable_autoscale(True)
        self.in_scope.enable_grid(True)
        self.in_scope.enable_axis_labels(True)
        self.in_scope.enable_control_panel(False)
        self.in_scope.enable_stem_plot(False)


        labels = ['input |x[n]| (two tones)', 'Signal 2', 'Signal 3', 'Signal 4', 'Signal 5',
            'Signal 6', 'Signal 7', 'Signal 8', 'Signal 9', 'Signal 10']
        widths = [1, 1, 1, 1, 1,
            1, 1, 1, 1, 1]
        colors = ['blue', 'blue', 'blue', 'blue', 'blue',
            'blue', 'blue', 'blue', 'blue', 'blue']
        alphas = [1.0, 1.0, 1.0, 1.0, 1.0,
            1.0, 1.0, 1.0, 1.0, 1.0]
        styles = [1, 1, 1, 1, 1,
            1, 1, 1, 1, 1]
        markers = [-1, -1, -1, -1, -1,
            -1, -1, -1, -1, -1]


        for i in range(1):
            if len(labels[i]) == 0:
                self.in_scope.set_line_label(i, "Data {0}".format(i))
            else:
                self.in_scope.set_line_label(i, labels[i])
            self.in_scope.set_line_width(i, widths[i])
            self.in_scope.set_line_color(i, colors[i])
            self.in_scope.set_line_style(i, styles[i])
            self.in_scope.set_line_marker(i, markers[i])
            self.in_scope.set_line_alpha(i, alphas[i])

        self._in_scope_win = sip.wrapinstance(self.in_scope.qwidget(), Qt.QWidget)
        self.top_layout.addWidget(self._in_scope_win)
        self.in_mag = blocks.complex_to_mag(1)
        self.die1 = kyttar.fft128_die1(device_id="kyttar_0")
        self.die0 = kyttar.fft128_die0(device_id="kyttar_0")
        self.bins = qtgui.time_sink_f(
            scope_points, #size
            scope_points, #samp_rate
            "FFT128 output words (I, Q interleaved)", #name
            1, #number of inputs
            None # parent
        )
        self.bins.set_update_time(0.10)
        self.bins.set_y_axis(-1, 1)

        self.bins.set_y_label('Magnitude', "")

        self.bins.enable_tags(True)
        self.bins.set_trigger_mode(qtgui.TRIG_MODE_FREE, qtgui.TRIG_SLOPE_POS, 0.0, 0, 0, "")
        self.bins.enable_autoscale(True)
        self.bins.enable_grid(True)
        self.bins.enable_axis_labels(True)
        self.bins.enable_control_panel(False)
        self.bins.enable_stem_plot(False)


        labels = ['FFT128 output words (I, Q interleaved)', 'Signal 2', 'Signal 3', 'Signal 4', 'Signal 5',
            'Signal 6', 'Signal 7', 'Signal 8', 'Signal 9', 'Signal 10']
        widths = [1, 1, 1, 1, 1,
            1, 1, 1, 1, 1]
        colors = ['blue', 'blue', 'blue', 'blue', 'blue',
            'blue', 'blue', 'blue', 'blue', 'blue']
        alphas = [1.0, 1.0, 1.0, 1.0, 1.0,
            1.0, 1.0, 1.0, 1.0, 1.0]
        styles = [1, 1, 1, 1, 1,
            1, 1, 1, 1, 1]
        markers = [-1, -1, -1, -1, -1,
            -1, -1, -1, -1, -1]


        for i in range(1):
            if len(labels[i]) == 0:
                self.bins.set_line_label(i, "Data {0}".format(i))
            else:
                self.bins.set_line_label(i, labels[i])
            self.bins.set_line_width(i, widths[i])
            self.bins.set_line_color(i, colors[i])
            self.bins.set_line_style(i, styles[i])
            self.bins.set_line_marker(i, markers[i])
            self.bins.set_line_alpha(i, alphas[i])

        self._bins_win = sip.wrapinstance(self.bins.qwidget(), Qt.QWidget)
        self.top_layout.addWidget(self._bins_win)


        ##################################################
        # Connections
        ##################################################
        self.connect((self.die0, 0), (self.die1, 0))
        self.connect((self.die1, 0), (self.kyt_sink, 0))
        self.connect((self.in_mag, 0), (self.in_scope, 0))
        self.connect((self.kyt_sink, 0), (self.bins, 0))
        self.connect((self.kyt_src, 0), (self.die0, 0))
        self.connect((self.stim, 0), (self.in_mag, 0))
        self.connect((self.stim, 0), (self.kyt_src, 0))


    def closeEvent(self, event):
        self.settings = Qt.QSettings("gnuradio/flowgraphs", "fft128_2p2s_demo")
        self.settings.setValue("geometry", self.saveGeometry())
        self.stop()
        self.wait()

        event.accept()

    def get_server_port(self):
        return self.server_port

    def set_server_port(self, server_port):
        self.server_port = server_port

    def get_scope_points(self):
        return self.scope_points

    def set_scope_points(self, scope_points):
        self.scope_points = scope_points
        self.bins.set_samp_rate(self.scope_points)
        self.in_scope.set_samp_rate(self.scope_points)

    def get_burst_len(self):
        return self.burst_len

    def set_burst_len(self, burst_len):
        self.burst_len = burst_len
        self.stim.set_data([0.45*__import__('cmath').exp(2j*3.141592653589793*9*n/128) + 0.35*__import__('cmath').exp(2j*3.141592653589793*37*n/128) for n in range(self.burst_len)], [])




def main(top_block_cls=fft128_2p2s_demo, options=None):

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
