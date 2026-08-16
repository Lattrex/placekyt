#!/usr/bin/env python3
# -*- coding: utf-8 -*-

#
# SPDX-License-Identifier: GPL-3.0
#
# GNU Radio Python Flow Graph
# Title: Kyttar LMS equalizer — multipath QPSK constellation snap
# Author: Lattrex
# Description: Adaptive equalizer CONSTELLATION SNAP on the Kyttar array: QPSK symbols smeared by a multipath channel ([1, 0.35, -0.15]) go through the placed decision-directed LMS equalizer; the constellation display shows the channel-distorted cloud (input) versus the equalized output converging onto the four clean decision points (+-0.707 +-0.707j) WITHIN the burst — the adaptation runs live on the chip, per sample. Run as GNURadio Server in placeKYT (port 58950), then Execute here.
# GNU Radio version: 3.10.12.0

from PyQt5 import Qt
from gnuradio import qtgui
from PyQt5 import QtCore
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
import lms_equalizer_errcurve as errcurve  # embedded python block
import lms_equalizer_iq2c as iq2c  # embedded python block
import lms_equalizer_phases as phases  # embedded python block
import numpy as np
import sip
import threading



class lms_equalizer(gr.top_block, Qt.QWidget):

    def __init__(self):
        gr.top_block.__init__(self, "Kyttar LMS equalizer — multipath QPSK constellation snap", catch_exceptions=True)
        Qt.QWidget.__init__(self)
        self.setWindowTitle("Kyttar LMS equalizer — multipath QPSK constellation snap")
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

        self.settings = Qt.QSettings("gnuradio/flowgraphs", "lms_equalizer")

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
        self.burst_len = burst_len = 600
        self.server_port = server_port = 58950
        self.playback_sps = playback_sps = 250
        self.iq_stim = iq_stim = list(((np.convolve(np.array([1+1j,1-1j,-1+1j,-1-1j])[np.random.default_rng(7).integers(0,4,burst_len)],[1.0,0.35,-0.15])[:burst_len]/2.4+0.035*(np.random.default_rng(11).standard_normal(burst_len)+1j*np.random.default_rng(12).standard_normal(burst_len)))).astype(complex))

        ##################################################
        # Blocks
        ##################################################

        self._playback_sps_range = qtgui.Range(50, 2000, 50, 250, 200)
        self._playback_sps_win = qtgui.RangeWidget(self._playback_sps_range, self.set_playback_sps, "Convergence playback (symbols/s)", "counter_slider", float, QtCore.Qt.Horizontal)
        self.top_layout.addWidget(self._playback_sps_win)
        self.src = blocks.vector_source_c(iq_stim, True, 1, [])
        self.playback = blocks.throttle(gr.sizeof_gr_complex*1, playback_sps,True)
        self.phases = phases.blk(burst=burst_len, early=100, mid=250)
        self.ksrc = kyttar.source(device_id="kyttar_0", port_name="x16_in", num_channels=1, server_host="127.0.0.1", server_port=server_port, complex_in=True, burst_len=burst_len, stream_id="", pipelined=False, schedule="interleaved", repeat=True, output_words="q15")
        self.ksink = kyttar.sink(device_id="kyttar_0", port_name="x16_out", num_channels=1, server_port=server_port, server_repeat=False, hold_secs=8.0, stream_id="", in_type=True)
        self.iq2c = iq2c.blk()
        self.errcurve = errcurve.blk()
        self.err_sink = qtgui.time_sink_f(
            600, #size
            playback_sps, #samp_rate
            "Convergence: error vs symbol (one burst = one decay)", #name
            1, #number of inputs
            None # parent
        )
        self.err_sink.set_update_time(0.10)
        self.err_sink.set_y_axis(0.0, 0.8)

        self.err_sink.set_y_label('|error|', "")

        self.err_sink.enable_tags(True)
        self.err_sink.set_trigger_mode(qtgui.TRIG_MODE_FREE, qtgui.TRIG_SLOPE_POS, 0.0, 0, 0, "")
        self.err_sink.enable_autoscale(False)
        self.err_sink.enable_grid(True)
        self.err_sink.enable_axis_labels(True)
        self.err_sink.enable_control_panel(False)
        self.err_sink.enable_stem_plot(False)


        labels = ['distance to decision (smoothed)', '', '', '', '',
            '', '', '', '', 'Signal 10']
        widths = [2, 1, 1, 1, 1,
            1, 1, 1, 1, 1]
        colors = ['dark red', 'red', 'green', 'black', 'cyan',
            'magenta', 'yellow', 'dark red', 'dark green', 'dark blue']
        alphas = [1.0, 1.0, 1.0, 1.0, 1.0,
            1.0, 1.0, 1.0, 1.0, 1.0]
        styles = [1, 1, 1, 1, 1,
            1, 1, 1, 1, 1]
        markers = [-1, -1, -1, -1, -1,
            -1, -1, -1, -1, -1]


        for i in range(1):
            if len(labels[i]) == 0:
                self.err_sink.set_line_label(i, "Data {0}".format(i))
            else:
                self.err_sink.set_line_label(i, labels[i])
            self.err_sink.set_line_width(i, widths[i])
            self.err_sink.set_line_color(i, colors[i])
            self.err_sink.set_line_style(i, styles[i])
            self.err_sink.set_line_marker(i, markers[i])
            self.err_sink.set_line_alpha(i, alphas[i])

        self._err_sink_win = sip.wrapinstance(self.err_sink.qwidget(), Qt.QWidget)
        self.top_layout.addWidget(self._err_sink_win)
        self.eq = kyttar.lms_equalizer("kyttar_0", 5, 0.03, 1, block_name="")
        self.const_sink = qtgui.const_sink_c(
            600, #size
            "LMS constellation snap: color = when (red early, green converged)", #name
            4, #number of inputs
            None # parent
        )
        self.const_sink.set_update_time(0.10)
        self.const_sink.set_y_axis((-1.2), 1.2)
        self.const_sink.set_x_axis((-1.2), 1.2)
        self.const_sink.set_trigger_mode(qtgui.TRIG_MODE_FREE, qtgui.TRIG_SLOPE_POS, 0.0, 0, "")
        self.const_sink.enable_autoscale(False)
        self.const_sink.enable_grid(True)
        self.const_sink.enable_axis_labels(True)


        labels = ['channel-distorted input', 'equalized: first 100 (cold start)', 'equalized: 100-250 (adapting)', 'equalized: 250+ (converged)', '',
            '', '', '', '', '']
        widths = [1, 1, 1, 1, 1,
            1, 1, 1, 1, 1]
        colors = ["blue", "red", "magenta", "green", "red",
            "red", "red", "red", "red", "red"]
        styles = [0, 0, 0, 0, 0,
            0, 0, 0, 0, 0]
        markers = [0, 0, 0, 0, 0,
            0, 0, 0, 0, 0]
        alphas = [0.35, 1.0, 1.0, 1.0, 1.0,
            1.0, 1.0, 1.0, 1.0, 1.0]

        for i in range(4):
            if len(labels[i]) == 0:
                self.const_sink.set_line_label(i, "Data {0}".format(i))
            else:
                self.const_sink.set_line_label(i, labels[i])
            self.const_sink.set_line_width(i, widths[i])
            self.const_sink.set_line_color(i, colors[i])
            self.const_sink.set_line_style(i, styles[i])
            self.const_sink.set_line_marker(i, markers[i])
            self.const_sink.set_line_alpha(i, alphas[i])

        self._const_sink_win = sip.wrapinstance(self.const_sink.qwidget(), Qt.QWidget)
        self.top_layout.addWidget(self._const_sink_win)


        ##################################################
        # Connections
        ##################################################
        self.connect((self.eq, 0), (self.ksink, 0))
        self.connect((self.errcurve, 0), (self.err_sink, 0))
        self.connect((self.iq2c, 0), (self.playback, 0))
        self.connect((self.ksink, 0), (self.iq2c, 0))
        self.connect((self.ksrc, 0), (self.eq, 0))
        self.connect((self.phases, 1), (self.const_sink, 2))
        self.connect((self.phases, 0), (self.const_sink, 1))
        self.connect((self.phases, 2), (self.const_sink, 3))
        self.connect((self.playback, 0), (self.errcurve, 0))
        self.connect((self.playback, 0), (self.phases, 0))
        self.connect((self.src, 0), (self.const_sink, 0))
        self.connect((self.src, 0), (self.ksrc, 0))


    def closeEvent(self, event):
        self.settings = Qt.QSettings("gnuradio/flowgraphs", "lms_equalizer")
        self.settings.setValue("geometry", self.saveGeometry())
        self.stop()
        self.wait()

        event.accept()

    def get_burst_len(self):
        return self.burst_len

    def set_burst_len(self, burst_len):
        self.burst_len = burst_len
        self.set_iq_stim(list(((np.convolve(np.array([1+1j,1-1j,-1+1j,-1-1j])[np.random.default_rng(7).integers(0,4,self.burst_len)],[1.0,0.35,-0.15])[:self.burst_len]/2.4+0.035*(np.random.default_rng(11).standard_normal(self.burst_len)+1j*np.random.default_rng(12).standard_normal(self.burst_len)))).astype(complex)))
        self.phases.burst = self.burst_len

    def get_server_port(self):
        return self.server_port

    def set_server_port(self, server_port):
        self.server_port = server_port

    def get_playback_sps(self):
        return self.playback_sps

    def set_playback_sps(self, playback_sps):
        self.playback_sps = playback_sps
        self.err_sink.set_samp_rate(self.playback_sps)
        self.playback.set_sample_rate(self.playback_sps)

    def get_iq_stim(self):
        return self.iq_stim

    def set_iq_stim(self, iq_stim):
        self.iq_stim = iq_stim
        self.src.set_data(self.iq_stim, [])




def main(top_block_cls=lms_equalizer, options=None):

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
