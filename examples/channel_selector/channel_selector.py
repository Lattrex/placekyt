#!/usr/bin/env python3
# -*- coding: utf-8 -*-

#
# SPDX-License-Identifier: GPL-3.0
#
# GNU Radio Python Flow Graph
# Title: Complex channel selector (freq-xlating FIR front end)
# Author: Lattrex
# Description: A COMPLEX CHANNEL SELECTOR on one placeKYT chip: a real multi-channel input is made complex, a FreqXlatingFIR mixes the 9 kHz channel to baseband through a 9-tap pre-filter, a firdes complex low-pass (gain 0.9, cutoff 1.2 kHz) selects the channel, a complex constant rotates it, and the imag rail egresses. Golden: the IDENTICAL stock-GNU-Radio chain (float_to_complex, freq_xlating_fir_filter_ccf, fir_filter_ccc(firdes.low_pass), multiply_const_cc, complex_to_imag) within a tolerance DERIVED from the per-block verified Q15 error bounds.
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



class channel_selector(gr.top_block, Qt.QWidget):

    def __init__(self):
        gr.top_block.__init__(self, "Complex channel selector (freq-xlating FIR front end)", catch_exceptions=True)
        Qt.QWidget.__init__(self)
        self.setWindowTitle("Complex channel selector (freq-xlating FIR front end)")
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

        self.settings = Qt.QSettings("gnuradio/flowgraphs", "channel_selector")

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
        self.sig = sig = [0.25*math.sin(2*math.pi*8600*t/32000) + 0.25*math.cos(2*math.pi*9400*t/32000) + 0.2*math.sin(2*math.pi*4000*t/32000) + 0.2*math.sin(2*math.pi*14000*t/32000) for t in range(320)]
        self.samp_rate = samp_rate = 32000
        self.fxf_taps = fxf_taps = [0.0, 0.018715, 0.099838, 0.226239, 0.290416, 0.226239, 0.099838, 0.018715, 0.0]
        self.burst_len = burst_len = len(sig)

        ##################################################
        # Blocks
        ##################################################

        self.scope = qtgui.time_sink_f(
            256, #size
            samp_rate, #samp_rate
            "selected channel (baseband, rotated - imag rail)", #name
            1, #number of inputs
            None # parent
        )
        self.scope.set_update_time(0.10)
        self.scope.set_y_axis(-1, 1)

        self.scope.set_y_label('level', "")

        self.scope.enable_tags(True)
        self.scope.set_trigger_mode(qtgui.TRIG_MODE_FREE, qtgui.TRIG_SLOPE_POS, 0.0, 0, 0, "")
        self.scope.enable_autoscale(False)
        self.scope.enable_grid(False)
        self.scope.enable_axis_labels(True)
        self.scope.enable_control_panel(False)
        self.scope.enable_stem_plot(False)


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
                self.scope.set_line_label(i, "Data {0}".format(i))
            else:
                self.scope.set_line_label(i, labels[i])
            self.scope.set_line_width(i, widths[i])
            self.scope.set_line_color(i, colors[i])
            self.scope.set_line_style(i, styles[i])
            self.scope.set_line_marker(i, markers[i])
            self.scope.set_line_alpha(i, alphas[i])

        self._scope_win = sip.wrapinstance(self.scope.qwidget(), Qt.QWidget)
        self.top_layout.addWidget(self._scope_win)
        self.rot = kyttar.multiply_const_complex("kyttar_0", 0.6, 0.35)
        self.rf_vec = blocks.vector_source_f(sig, False, 1, [])
        self.rf_out = kyttar.sink(device_id="kyttar_0", port_name="x16_out", num_channels=1, server_port=58950, server_repeat=False, hold_secs=8.0, stream_id="rf", in_type=False)
        self.rf_in = kyttar.source(device_id="kyttar_0", port_name="x16_in", num_channels=1, server_host="127.0.0.1", server_port=58950, complex_in=False, burst_len=burst_len, stream_id="rf", pipelined=False, schedule="interleaved", repeat=False, output_words="auto")
        self.qzero = blocks.null_source(gr.sizeof_float*1)
        self.fxf = kyttar.freq_xlating_fir(device_id="kyttar_0", decimation=1, taps=fxf_taps, center_freq=9000, sampling_freq=samp_rate, pipeline_lock=False)
        self.f2c = kyttar.float_to_complex(device_id="kyttar_0")
        self.conj = kyttar.conjugate(device_id="kyttar_0")
        self.clpf = kyttar.complex_low_pass_filter(device_id="kyttar_0", gain=0.9, samp_rate=samp_rate, cutoff_freq=1200, transition_width=2500, window="hamming", beta=6.76)
        self.c2i = kyttar.complex_to_imag(device_id="kyttar_0")


        ##################################################
        # Connections
        ##################################################
        self.connect((self.c2i, 0), (self.rf_out, 0))
        self.connect((self.clpf, 0), (self.rot, 0))
        self.connect((self.conj, 0), (self.c2i, 0))
        self.connect((self.f2c, 0), (self.fxf, 0))
        self.connect((self.fxf, 0), (self.clpf, 0))
        self.connect((self.qzero, 0), (self.f2c, 1))
        self.connect((self.rf_in, 0), (self.f2c, 0))
        self.connect((self.rf_out, 0), (self.scope, 0))
        self.connect((self.rf_vec, 0), (self.rf_in, 0))
        self.connect((self.rot, 0), (self.conj, 0))


    def closeEvent(self, event):
        self.settings = Qt.QSettings("gnuradio/flowgraphs", "channel_selector")
        self.settings.setValue("geometry", self.saveGeometry())
        self.stop()
        self.wait()

        event.accept()

    def get_sig(self):
        return self.sig

    def set_sig(self, sig):
        self.sig = sig
        self.set_burst_len(len(self.sig))
        self.rf_vec.set_data(self.sig, [])

    def get_samp_rate(self):
        return self.samp_rate

    def set_samp_rate(self, samp_rate):
        self.samp_rate = samp_rate
        self.scope.set_samp_rate(self.samp_rate)

    def get_fxf_taps(self):
        return self.fxf_taps

    def set_fxf_taps(self, fxf_taps):
        self.fxf_taps = fxf_taps
        self.fxf.set_taps(self.fxf_taps)

    def get_burst_len(self):
        return self.burst_len

    def set_burst_len(self, burst_len):
        self.burst_len = burst_len




def main(top_block_cls=channel_selector, options=None):

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
