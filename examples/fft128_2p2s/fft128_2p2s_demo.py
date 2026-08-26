#!/usr/bin/env python3
# -*- coding: utf-8 -*-

#
# SPDX-License-Identifier: GPL-3.0
#
# GNU Radio Python Flow Graph
# Title: Kyttar FFT128 — a 128-point spectrum across the 2P2S board's chain A (250 Hz/bin, tones at +2250 and +9250 Hz)
# Author: Lattrex
# Description: FFT128 on the 2P2S board: chain A's head (chip 0) runs stage 0, its tail (chip 1) runs stages 1..6, joined by the board's on-carrier series link. Driven through the placeKYT multi-chip GNURadio server. The spectrum plot reads in Hz: bin k of 128 at samp_rate is k*fs/N, so at 32000 Hz each bin is 250 Hz wide and the two demo tones (bins 9 and 37) peak at +2250 Hz and +9250 Hz.
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
import fft128_2p2s_demo_spectrum as spectrum  # embedded python block
import fft128_2p2s_demo_to_db as to_db  # embedded python block
import sip
import threading



class fft128_2p2s_demo(gr.top_block, Qt.QWidget):

    def __init__(self):
        gr.top_block.__init__(self, "Kyttar FFT128 — a 128-point spectrum across the 2P2S board's chain A (250 Hz/bin, tones at +2250 and +9250 Hz)", catch_exceptions=True)
        Qt.QWidget.__init__(self)
        self.setWindowTitle("Kyttar FFT128 — a 128-point spectrum across the 2P2S board's chain A (250 Hz/bin, tones at +2250 and +9250 Hz)")
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
        self.samp_rate = samp_rate = 32000
        self.n_fft = n_fft = 128
        self.tone_b = tone_b = 37
        self.tone_a = tone_a = 9
        self.server_port = server_port = 58950
        self.scope_points = scope_points = 320
        self.latency = latency = 127
        self.burst_len = burst_len = 384
        self.bin_hz = bin_hz = samp_rate / n_fft

        ##################################################
        # Blocks
        ##################################################

        self.to_db = to_db.blk(n_fft=n_fft, floor_db=-90.0)
        self.stim = blocks.vector_source_c([0.45*__import__('cmath').exp(2j*3.141592653589793*tone_a*n/n_fft) + 0.35*__import__('cmath').exp(2j*3.141592653589793*tone_b*n/n_fft) for n in range(burst_len)], True, 1, [])
        self.spectrum_sink = qtgui.vector_sink_f(
            n_fft,
            (-samp_rate / 2),
            bin_hz,
            "frequency (Hz) — bin k of 128 at samp_rate = k*samp_rate/128",
            "power (dBFS)",
            "On-chip FFT128 spectrum — 250 Hz/bin, tones at +2250 Hz and +9250 Hz (dBFS)",
            1, # Number of inputs
            None # parent
        )
        self.spectrum_sink.set_update_time(0.10)
        self.spectrum_sink.set_y_axis((-95), 5)
        self.spectrum_sink.enable_autoscale(False)
        self.spectrum_sink.enable_grid(True)
        self.spectrum_sink.set_x_axis_units("Hz")
        self.spectrum_sink.set_y_axis_units("")
        self.spectrum_sink.set_ref_level(0)


        labels = ['on-chip FFT128 power spectrum (bin k = k*samp_rate/128 Hz)', '', '', '', '',
            '', '', '', '', '']
        widths = [3, 1, 1, 1, 1,
            1, 1, 1, 1, 1]
        colors = ["blue", "red", "blue", "blue", "blue",
            "blue", "blue", "blue", "blue", "blue"]
        alphas = [1.0, 1.0, 1.0, 1.0, 1.0,
            1.0, 1.0, 1.0, 1.0, 1.0]

        for i in range(1):
            if len(labels[i]) == 0:
                self.spectrum_sink.set_line_label(i, "Data {0}".format(i))
            else:
                self.spectrum_sink.set_line_label(i, labels[i])
            self.spectrum_sink.set_line_width(i, widths[i])
            self.spectrum_sink.set_line_color(i, colors[i])
            self.spectrum_sink.set_line_alpha(i, alphas[i])

        self._spectrum_sink_win = sip.wrapinstance(self.spectrum_sink.qwidget(), Qt.QWidget)
        self.top_layout.addWidget(self._spectrum_sink_win)
        self.spectrum = spectrum.blk(n_fft=n_fft, latency=latency, burst_len=burst_len)
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


        ##################################################
        # Connections
        ##################################################
        self.connect((self.die0, 0), (self.die1, 0))
        self.connect((self.die1, 0), (self.kyt_sink, 0))
        self.connect((self.in_mag, 0), (self.in_scope, 0))
        self.connect((self.kyt_sink, 0), (self.spectrum, 0))
        self.connect((self.kyt_src, 0), (self.die0, 0))
        self.connect((self.spectrum, 0), (self.to_db, 0))
        self.connect((self.stim, 0), (self.in_mag, 0))
        self.connect((self.stim, 0), (self.kyt_src, 0))
        self.connect((self.to_db, 0), (self.spectrum_sink, 0))


    def closeEvent(self, event):
        self.settings = Qt.QSettings("gnuradio/flowgraphs", "fft128_2p2s_demo")
        self.settings.setValue("geometry", self.saveGeometry())
        self.stop()
        self.wait()

        event.accept()

    def get_samp_rate(self):
        return self.samp_rate

    def set_samp_rate(self, samp_rate):
        self.samp_rate = samp_rate
        self.set_bin_hz(self.samp_rate / self.n_fft)
        self.spectrum_sink.set_x_axis((-self.samp_rate / 2), self.bin_hz)

    def get_n_fft(self):
        return self.n_fft

    def set_n_fft(self, n_fft):
        self.n_fft = n_fft
        self.set_bin_hz(self.samp_rate / self.n_fft)
        self.stim.set_data([0.45*__import__('cmath').exp(2j*3.141592653589793*self.tone_a*n/self.n_fft) + 0.35*__import__('cmath').exp(2j*3.141592653589793*self.tone_b*n/self.n_fft) for n in range(self.burst_len)], [])

    def get_tone_b(self):
        return self.tone_b

    def set_tone_b(self, tone_b):
        self.tone_b = tone_b
        self.stim.set_data([0.45*__import__('cmath').exp(2j*3.141592653589793*self.tone_a*n/self.n_fft) + 0.35*__import__('cmath').exp(2j*3.141592653589793*self.tone_b*n/self.n_fft) for n in range(self.burst_len)], [])

    def get_tone_a(self):
        return self.tone_a

    def set_tone_a(self, tone_a):
        self.tone_a = tone_a
        self.stim.set_data([0.45*__import__('cmath').exp(2j*3.141592653589793*self.tone_a*n/self.n_fft) + 0.35*__import__('cmath').exp(2j*3.141592653589793*self.tone_b*n/self.n_fft) for n in range(self.burst_len)], [])

    def get_server_port(self):
        return self.server_port

    def set_server_port(self, server_port):
        self.server_port = server_port

    def get_scope_points(self):
        return self.scope_points

    def set_scope_points(self, scope_points):
        self.scope_points = scope_points
        self.in_scope.set_samp_rate(self.scope_points)

    def get_latency(self):
        return self.latency

    def set_latency(self, latency):
        self.latency = latency
        self.spectrum.latency = self.latency

    def get_burst_len(self):
        return self.burst_len

    def set_burst_len(self, burst_len):
        self.burst_len = burst_len
        self.stim.set_data([0.45*__import__('cmath').exp(2j*3.141592653589793*self.tone_a*n/self.n_fft) + 0.35*__import__('cmath').exp(2j*3.141592653589793*self.tone_b*n/self.n_fft) for n in range(self.burst_len)], [])

    def get_bin_hz(self):
        return self.bin_hz

    def set_bin_hz(self, bin_hz):
        self.bin_hz = bin_hz
        self.spectrum_sink.set_x_axis((-self.samp_rate / 2), self.bin_hz)




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
