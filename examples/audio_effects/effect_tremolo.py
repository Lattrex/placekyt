#!/usr/bin/env python3
# -*- coding: utf-8 -*-

#
# SPDX-License-Identifier: GPL-3.0
#
# GNU Radio Python Flow Graph
# Title: NCO tremolo (placed audio effect)
# Author: Lattrex
# Description: A placed 250 Hz TREMOLO: the trigger-driven complex NCO's cos rail biased to 0.5 + 0.45*cos and multiplied into the audio (a single-fire Multiply join). Golden: the IDENTICAL stock-GNU-Radio chain (sig_source_f, add_const_ff, multiply_ff) within a DERIVED per-block bound.
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



class effect_tremolo(gr.top_block, Qt.QWidget):

    def __init__(self):
        gr.top_block.__init__(self, "NCO tremolo (placed audio effect)", catch_exceptions=True)
        Qt.QWidget.__init__(self)
        self.setWindowTitle("NCO tremolo (placed audio effect)")
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

        self.settings = Qt.QSettings("gnuradio/flowgraphs", "effect_tremolo")

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
        self.sig = sig = [0.5*math.sin(2*math.pi*330*t/8000) + 0.05*math.sin(2*math.pi*2200*t/8000) for t in range(400)]
        self.samp_rate = samp_rate = 8000
        self.burst_len = burst_len = len(sig)

        ##################################################
        # Blocks
        ##################################################

        self.trem_nco = kyttar.nco(device_id="kyttar_0", sample_rate=samp_rate, frequency=250, amplitude=0.45, offset=0, phase=0, waveform="cos")
        self.trem_mul = kyttar.multiply(device_id="kyttar_0", num_inputs=2)
        self.trem_c2r = kyttar.complex_to_real("kyttar_0")
        self.trem_bias = kyttar.add_const(device_id="kyttar_0", const=0.5)
        self.scope = qtgui.time_sink_f(
            burst_len, #size
            samp_rate, #samp_rate
            "250 Hz tremolo (0.05..0.95 gain)", #name
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
        self.fx_vec = blocks.vector_source_f(sig, False, 1, [])
        self.fx_out = kyttar.sink(device_id="kyttar_0", port_name="x16_out", num_channels=1, server_port=58950, server_repeat=False, hold_secs=8.0, stream_id="fx", in_type=False)
        self.fx_in = kyttar.source(device_id="kyttar_0", port_name="x16_in", num_channels=1, server_host="127.0.0.1", server_port=58950, complex_in=False, burst_len=burst_len, stream_id="fx", pipelined=False, schedule="interleaved", repeat=False, output_words="auto")


        ##################################################
        # Connections
        ##################################################
        self.connect((self.fx_in, 0), (self.trem_mul, 0))
        self.connect((self.fx_in, 0), (self.trem_nco, 0))
        self.connect((self.fx_out, 0), (self.scope, 0))
        self.connect((self.fx_vec, 0), (self.fx_in, 0))
        self.connect((self.trem_bias, 0), (self.trem_mul, 1))
        self.connect((self.trem_c2r, 0), (self.trem_bias, 0))
        self.connect((self.trem_mul, 0), (self.fx_out, 0))
        self.connect((self.trem_nco, 0), (self.trem_c2r, 0))


    def closeEvent(self, event):
        self.settings = Qt.QSettings("gnuradio/flowgraphs", "effect_tremolo")
        self.settings.setValue("geometry", self.saveGeometry())
        self.stop()
        self.wait()

        event.accept()

    def get_sig(self):
        return self.sig

    def set_sig(self, sig):
        self.sig = sig
        self.set_burst_len(len(self.sig))
        self.fx_vec.set_data(self.sig, [])

    def get_samp_rate(self):
        return self.samp_rate

    def set_samp_rate(self, samp_rate):
        self.samp_rate = samp_rate
        self.scope.set_samp_rate(self.samp_rate)

    def get_burst_len(self):
        return self.burst_len

    def set_burst_len(self, burst_len):
        self.burst_len = burst_len




def main(top_block_cls=effect_tremolo, options=None):

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
