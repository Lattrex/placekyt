#!/usr/bin/env python3
# -*- coding: utf-8 -*-

#
# SPDX-License-Identifier: GPL-3.0
#
# GNU Radio Python Flow Graph
# Title: GRU modulation classifier (features + recurrent net on one array)
# Author: Lattrex
# Description: GRU modulation classifier on ONE placeKYT array. A complex baseband stream enters the chip and a class index (0..3 = SSB / BPSK / 4-FSK / noise) comes back, one word per 32-sample window. The whole chain is on chip: the RMS arm (ComplexToMagSquared -> MovingAverage(32) -> Sqrt -> KeepOneInN(32)), the ZeroCrossingRate(32) arm on the real rail, a FeaturePairJoin rendezvous that emits the ordered (rms, zcr) pair, and a GRUCellBlock (H=4, I=2) with its 4-class readout head and an INTERNAL recurrence. 102 of 120 cells. The shipped stimulus walks all four classes in order and the Class Index scope shows the chip tracking them.
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
from gnuradio.kyttar import gru_demo_stim as stim
import sip
import threading



class gru_classifier(gr.top_block, Qt.QWidget):

    def __init__(self):
        gr.top_block.__init__(self, "GRU modulation classifier (features + recurrent net on one array)", catch_exceptions=True)
        Qt.QWidget.__init__(self)
        self.setWindowTitle("GRU modulation classifier (features + recurrent net on one array)")
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

        self.settings = Qt.QSettings("gnuradio/flowgraphs", "gru_classifier")

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
        self.window_n = window_n = 32
        self.samp_rate = samp_rate = 32000
        self.n_windows = n_windows = stim.n_windows()
        self.n_samples = n_samples = stim.n_samples()

        ##################################################
        # Blocks
        ##################################################

        self.zcr = kyttar.zero_crossing_rate(device_id="kyttar_0", window_size=32)
        self.truth_src = blocks.vector_source_f(stim.truth(), True, 1, [])
        self.truth_scope = qtgui.time_sink_f(
            (n_windows - 16), #size
            samp_rate/window_n, #samp_rate
            "TRUE class per window (ground truth)", #name
            1, #number of inputs
            None # parent
        )
        self.truth_scope.set_update_time(0.10)
        self.truth_scope.set_y_axis(-0.5, 3.5)

        self.truth_scope.set_y_label('class', "")

        self.truth_scope.enable_tags(True)
        self.truth_scope.set_trigger_mode(qtgui.TRIG_MODE_FREE, qtgui.TRIG_SLOPE_POS, 0.0, 0, 0, "")
        self.truth_scope.enable_autoscale(False)
        self.truth_scope.enable_grid(True)
        self.truth_scope.enable_axis_labels(True)
        self.truth_scope.enable_control_panel(False)
        self.truth_scope.enable_stem_plot(False)


        labels = ['true class', 'Signal 2', 'Signal 3', 'Signal 4', 'Signal 5',
            'Signal 6', 'Signal 7', 'Signal 8', 'Signal 9', 'Signal 10']
        widths = [2, 1, 1, 1, 1,
            1, 1, 1, 1, 1]
        colors = ['blue', 'red', 'green', 'black', 'cyan',
            'magenta', 'yellow', 'dark red', 'dark green', 'dark blue']
        alphas = [1.0, 1.0, 1.0, 1.0, 1.0,
            1.0, 1.0, 1.0, 1.0, 1.0]
        styles = [1, 1, 1, 1, 1,
            1, 1, 1, 1, 1]
        markers = [0, -1, -1, -1, -1,
            -1, -1, -1, -1, -1]


        for i in range(1):
            if len(labels[i]) == 0:
                self.truth_scope.set_line_label(i, "Data {0}".format(i))
            else:
                self.truth_scope.set_line_label(i, labels[i])
            self.truth_scope.set_line_width(i, widths[i])
            self.truth_scope.set_line_color(i, colors[i])
            self.truth_scope.set_line_style(i, styles[i])
            self.truth_scope.set_line_marker(i, markers[i])
            self.truth_scope.set_line_alpha(i, alphas[i])

        self._truth_scope_win = sip.wrapinstance(self.truth_scope.qwidget(), Qt.QWidget)
        self.top_layout.addWidget(self._truth_scope_win)
        self.root = kyttar.sqrt(device_id="kyttar_0")
        self.power = kyttar.complex_to_mag_squared(device_id="kyttar_0")
        self.join = kyttar.feature_pair_join(device_id="kyttar_0")
        self.iq_src = blocks.vector_source_c(stim.iq(), False, 1, [])
        self.gru = kyttar.gru_cell(device_id="kyttar_0", hidden=4, inputs=2, classes=4, weights_file="")
        self.decim = kyttar.keep_one_in_n(device_id="kyttar_0", n=32)
        self.cls_scope = qtgui.time_sink_f(
            (n_windows - 16), #size
            samp_rate/window_n, #samp_rate
            "Class index over time (0=SSB 1=BPSK 2=4FSK 3=noise)", #name
            1, #number of inputs
            None # parent
        )
        self.cls_scope.set_update_time(0.10)
        self.cls_scope.set_y_axis(-0.5, 3.5)

        self.cls_scope.set_y_label('class', "")

        self.cls_scope.enable_tags(True)
        self.cls_scope.set_trigger_mode(qtgui.TRIG_MODE_FREE, qtgui.TRIG_SLOPE_POS, 0.0, 0, 0, "")
        self.cls_scope.enable_autoscale(False)
        self.cls_scope.enable_grid(True)
        self.cls_scope.enable_axis_labels(True)
        self.cls_scope.enable_control_panel(False)
        self.cls_scope.enable_stem_plot(False)


        labels = ['class index', 'Signal 2', 'Signal 3', 'Signal 4', 'Signal 5',
            'Signal 6', 'Signal 7', 'Signal 8', 'Signal 9', 'Signal 10']
        widths = [2, 1, 1, 1, 1,
            1, 1, 1, 1, 1]
        colors = ['blue', 'red', 'green', 'black', 'cyan',
            'magenta', 'yellow', 'dark red', 'dark green', 'dark blue']
        alphas = [1.0, 1.0, 1.0, 1.0, 1.0,
            1.0, 1.0, 1.0, 1.0, 1.0]
        styles = [1, 1, 1, 1, 1,
            1, 1, 1, 1, 1]
        markers = [0, -1, -1, -1, -1,
            -1, -1, -1, -1, -1]


        for i in range(1):
            if len(labels[i]) == 0:
                self.cls_scope.set_line_label(i, "Data {0}".format(i))
            else:
                self.cls_scope.set_line_label(i, labels[i])
            self.cls_scope.set_line_width(i, widths[i])
            self.cls_scope.set_line_color(i, colors[i])
            self.cls_scope.set_line_style(i, styles[i])
            self.cls_scope.set_line_marker(i, markers[i])
            self.cls_scope.set_line_alpha(i, alphas[i])

        self._cls_scope_win = sip.wrapinstance(self.cls_scope.qwidget(), Qt.QWidget)
        self.top_layout.addWidget(self._cls_scope_win)
        self.cls_scale = blocks.multiply_const_ff(32768)
        self.cls_s2f = blocks.short_to_float(1, 1)
        self.chip_src = kyttar.source(device_id="kyttar_0", port_name="x16_in", num_channels=1, server_host="127.0.0.1", server_port=58950, complex_in=True, burst_len=n_samples, stream_id="cls", pipelined=False, schedule="interleaved", repeat=False, output_words="auto")
        self.chip_sink = kyttar.sink(device_id="kyttar_0", port_name="x16_out", num_channels=1, server_port=58950, server_repeat=True, hold_secs=8.0, stream_id="cls", in_type=False)
        self.c2real = blocks.complex_to_real(1)
        self.boxcar = kyttar.moving_average(device_id="kyttar_0", length=32, scale=(1.0/32))


        ##################################################
        # Connections
        ##################################################
        self.connect((self.boxcar, 0), (self.root, 0))
        self.connect((self.c2real, 0), (self.zcr, 0))
        self.connect((self.chip_sink, 0), (self.cls_scale, 0))
        self.connect((self.chip_src, 0), (self.c2real, 0))
        self.connect((self.chip_src, 0), (self.power, 0))
        self.connect((self.cls_s2f, 0), (self.chip_sink, 0))
        self.connect((self.cls_scale, 0), (self.cls_scope, 0))
        self.connect((self.decim, 0), (self.join, 0))
        self.connect((self.gru, 0), (self.cls_s2f, 0))
        self.connect((self.iq_src, 0), (self.chip_src, 0))
        self.connect((self.join, 0), (self.gru, 0))
        self.connect((self.power, 0), (self.boxcar, 0))
        self.connect((self.root, 0), (self.decim, 0))
        self.connect((self.truth_src, 0), (self.truth_scope, 0))
        self.connect((self.zcr, 0), (self.join, 1))


    def closeEvent(self, event):
        self.settings = Qt.QSettings("gnuradio/flowgraphs", "gru_classifier")
        self.settings.setValue("geometry", self.saveGeometry())
        self.stop()
        self.wait()

        event.accept()

    def get_window_n(self):
        return self.window_n

    def set_window_n(self, window_n):
        self.window_n = window_n
        self.cls_scope.set_samp_rate(self.samp_rate/self.window_n)
        self.truth_scope.set_samp_rate(self.samp_rate/self.window_n)

    def get_samp_rate(self):
        return self.samp_rate

    def set_samp_rate(self, samp_rate):
        self.samp_rate = samp_rate
        self.cls_scope.set_samp_rate(self.samp_rate/self.window_n)
        self.truth_scope.set_samp_rate(self.samp_rate/self.window_n)

    def get_n_windows(self):
        return self.n_windows

    def set_n_windows(self, n_windows):
        self.n_windows = n_windows

    def get_n_samples(self):
        return self.n_samples

    def set_n_samples(self, n_samples):
        self.n_samples = n_samples




def main(top_block_cls=gru_classifier, options=None):

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
