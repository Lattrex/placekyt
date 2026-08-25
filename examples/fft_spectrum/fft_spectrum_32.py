#!/usr/bin/env python3
# -*- coding: utf-8 -*-

#
# SPDX-License-Identifier: GPL-3.0
#
# GNU Radio Python Flow Graph
# Title: Kyttar FFT32 spectrum analyzer — the smaller streaming FFT
# Author: Lattrex
# Description: LIVE SPECTRUM ANALYZER on the Kyttar array (the SMALLER variant, N=32). A complex I/Q burst drives the placed 32-point streaming R2SDF FFT (FFT32, 60 cells) whose complex output feeds a placed Complex-to-Mag^2 stage, so the transform AND the per-bin power both run ON CHIP. The chip emits bins in BIT-REVERSED (DIF) order; the 'unreverse' block undoes that map and then fftshifts, emitting a 32-bin vector in ascending FREQUENCY order from -samp_rate/2. Bin k of N at rate fs is k*fs/N Hz (bins >= N/2 are the negative frequencies (k-N)*fs/N), so at the default samp_rate = 32000 each bin is 1000 Hz wide and the demo tone at bin 11 (which leaves the chip at slot 26) appears at +11000 Hz on the plot. A second scope shows the stimulus itself, so you can see the sinusoid the spike comes from. Open fft_spectrum_32.kyt in placeKYT -> Run as GNURadio Server (port 58950), then Execute here.
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
import fft_spectrum_32_to_db as to_db  # embedded python block
import fft_spectrum_32_unreverse as unreverse  # embedded python block
import numpy as np
import sip
import threading



class fft_spectrum_32(gr.top_block, Qt.QWidget):

    def __init__(self):
        gr.top_block.__init__(self, "Kyttar FFT32 spectrum analyzer — the smaller streaming FFT", catch_exceptions=True)
        Qt.QWidget.__init__(self)
        self.setWindowTitle("Kyttar FFT32 spectrum analyzer — the smaller streaming FFT")
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

        self.settings = Qt.QSettings("gnuradio/flowgraphs", "fft_spectrum_32")

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
        self.n_fft = n_fft = 32
        self.latency = latency = 31
        self.frames = frames = 3
        self.tone_bin = tone_bin = 11
        self.samp_rate = samp_rate = 32000
        self.burst_len = burst_len = latency + n_fft * frames
        self.amplitude = amplitude = 0.9
        self.server_port = server_port = 58950
        self.iq_stim = iq_stim = list(amplitude*np.exp(2j*np.pi*tone_bin*np.arange(burst_len)/n_fft))
        self.bin_hz = bin_hz = samp_rate / n_fft

        ##################################################
        # Blocks
        ##################################################

        self.unreverse = unreverse.blk(n_fft=n_fft, latency=latency, burst_len=burst_len)
        self.to_db = to_db.blk(n_fft=n_fft, floor_db=-90.0)
        self.stim_scope = qtgui.time_sink_c(
            (4 * n_fft), #size
            samp_rate, #samp_rate
            "Stimulus into x16_in — I/Q of the 11000 Hz tone", #name
            1, #number of inputs
            None # parent
        )
        self.stim_scope.set_update_time(0.10)
        self.stim_scope.set_y_axis(-1.1, 1.1)

        self.stim_scope.set_y_label('amplitude', "")

        self.stim_scope.enable_tags(True)
        self.stim_scope.set_trigger_mode(qtgui.TRIG_MODE_FREE, qtgui.TRIG_SLOPE_POS, 0.0, 0, 0, "")
        self.stim_scope.enable_autoscale(False)
        self.stim_scope.enable_grid(True)
        self.stim_scope.enable_axis_labels(True)
        self.stim_scope.enable_control_panel(False)
        self.stim_scope.enable_stem_plot(False)


        labels = ['I (xi rail)', 'Q (xq rail)', 'Signal 3', 'Signal 4', 'Signal 5',
            'Signal 6', 'Signal 7', 'Signal 8', 'Signal 9', 'Signal 10']
        widths = [2, 2, 1, 1, 1,
            1, 1, 1, 1, 1]
        colors = ['blue', 'red', 'green', 'black', 'cyan',
            'magenta', 'yellow', 'dark red', 'dark green', 'dark blue']
        alphas = [1.0, 1.0, 1.0, 1.0, 1.0,
            1.0, 1.0, 1.0, 1.0, 1.0]
        styles = [1, 1, 1, 1, 1,
            1, 1, 1, 1, 1]
        markers = [-1, -1, -1, -1, -1,
            -1, -1, -1, -1, -1]


        for i in range(2):
            if len(labels[i]) == 0:
                if (i % 2 == 0):
                    self.stim_scope.set_line_label(i, "Re{{Data {0}}}".format(i/2))
                else:
                    self.stim_scope.set_line_label(i, "Im{{Data {0}}}".format(i/2))
            else:
                self.stim_scope.set_line_label(i, labels[i])
            self.stim_scope.set_line_width(i, widths[i])
            self.stim_scope.set_line_color(i, colors[i])
            self.stim_scope.set_line_style(i, styles[i])
            self.stim_scope.set_line_marker(i, markers[i])
            self.stim_scope.set_line_alpha(i, alphas[i])

        self._stim_scope_win = sip.wrapinstance(self.stim_scope.qwidget(), Qt.QWidget)
        self.top_layout.addWidget(self._stim_scope_win)
        self.src = blocks.vector_source_c(iq_stim, True, 1, [])
        self.spectrum_sink = qtgui.vector_sink_f(
            n_fft,
            (-samp_rate / 2),
            bin_hz,
            "frequency (Hz) — bin k of 32 at samp_rate = k*samp_rate/32",
            "power (dBFS)",
            "On-chip FFT32 spectrum — 1000 Hz/bin, tone at +11000 Hz (dBFS)",
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


        labels = ['on-chip FFT32 power spectrum (bin k = k*samp_rate/32 Hz)', '', '', '', '',
            '', '', '', '', '']
        widths = [3, 1, 1, 1, 1,
            1, 1, 1, 1, 1]
        colors = ["blue", "red", "green", "black", "cyan",
            "magenta", "yellow", "dark red", "dark green", "dark blue"]
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
        self.mag2 = kyttar.complex_to_mag_squared(device_id="kyttar_0")
        self.ksrc = kyttar.source(device_id="kyttar_0", port_name="x16_in", num_channels=1, server_host="127.0.0.1", server_port=server_port, complex_in=True, burst_len=burst_len, stream_id="spectrum", pipelined=False, schedule="interleaved", repeat=False, output_words="q15")
        self.ksink = kyttar.sink(device_id="kyttar_0", port_name="x16_out", num_channels=1, server_port=server_port, server_repeat=True, hold_secs=8.0, stream_id="spectrum", in_type=False)
        self.fft = kyttar.fft32(device_id="kyttar_0")


        ##################################################
        # Connections
        ##################################################
        self.connect((self.fft, 0), (self.mag2, 0))
        self.connect((self.ksink, 0), (self.unreverse, 0))
        self.connect((self.ksrc, 0), (self.fft, 0))
        self.connect((self.mag2, 0), (self.ksink, 0))
        self.connect((self.src, 0), (self.ksrc, 0))
        self.connect((self.src, 0), (self.stim_scope, 0))
        self.connect((self.to_db, 0), (self.spectrum_sink, 0))
        self.connect((self.unreverse, 0), (self.to_db, 0))


    def closeEvent(self, event):
        self.settings = Qt.QSettings("gnuradio/flowgraphs", "fft_spectrum_32")
        self.settings.setValue("geometry", self.saveGeometry())
        self.stop()
        self.wait()

        event.accept()

    def get_n_fft(self):
        return self.n_fft

    def set_n_fft(self, n_fft):
        self.n_fft = n_fft
        self.set_bin_hz(self.samp_rate / self.n_fft)
        self.set_burst_len(self.latency + self.n_fft * self.frames)
        self.set_iq_stim(list(self.amplitude*np.exp(2j*np.pi*self.tone_bin*np.arange(self.burst_len)/self.n_fft)))

    def get_latency(self):
        return self.latency

    def set_latency(self, latency):
        self.latency = latency
        self.set_burst_len(self.latency + self.n_fft * self.frames)
        self.unreverse.latency = self.latency

    def get_frames(self):
        return self.frames

    def set_frames(self, frames):
        self.frames = frames
        self.set_burst_len(self.latency + self.n_fft * self.frames)

    def get_tone_bin(self):
        return self.tone_bin

    def set_tone_bin(self, tone_bin):
        self.tone_bin = tone_bin
        self.set_iq_stim(list(self.amplitude*np.exp(2j*np.pi*self.tone_bin*np.arange(self.burst_len)/self.n_fft)))

    def get_samp_rate(self):
        return self.samp_rate

    def set_samp_rate(self, samp_rate):
        self.samp_rate = samp_rate
        self.set_bin_hz(self.samp_rate / self.n_fft)
        self.spectrum_sink.set_x_axis((-self.samp_rate / 2), self.bin_hz)
        self.stim_scope.set_samp_rate(self.samp_rate)

    def get_burst_len(self):
        return self.burst_len

    def set_burst_len(self, burst_len):
        self.burst_len = burst_len
        self.set_iq_stim(list(self.amplitude*np.exp(2j*np.pi*self.tone_bin*np.arange(self.burst_len)/self.n_fft)))

    def get_amplitude(self):
        return self.amplitude

    def set_amplitude(self, amplitude):
        self.amplitude = amplitude
        self.set_iq_stim(list(self.amplitude*np.exp(2j*np.pi*self.tone_bin*np.arange(self.burst_len)/self.n_fft)))

    def get_server_port(self):
        return self.server_port

    def set_server_port(self, server_port):
        self.server_port = server_port

    def get_iq_stim(self):
        return self.iq_stim

    def set_iq_stim(self, iq_stim):
        self.iq_stim = iq_stim
        self.src.set_data(self.iq_stim, [])

    def get_bin_hz(self):
        return self.bin_hz

    def set_bin_hz(self, bin_hz):
        self.bin_hz = bin_hz
        self.spectrum_sink.set_x_axis((-self.samp_rate / 2), self.bin_hz)




def main(top_block_cls=fft_spectrum_32, options=None):

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
