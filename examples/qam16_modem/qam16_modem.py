#!/usr/bin/env python3
# -*- coding: utf-8 -*-

#
# SPDX-License-Identifier: GPL-3.0
#
# GNU Radio Python Flow Graph
# Title: Coherent 16-QAM RX (DD Costas + slicer) — input vs recovered symbols
# Author: Lattrex
# Description: Coherent 16-QAM receiver on the Kyttar array: a decision-directed complex Costas loop recovers the carrier, then a 16-QAM hard-decision slicer emits the 4-bit symbol index. Built from the REAL DSP blocks (QAM16ComplexCostasLoop -> QAM16Slicer) so it IMPORTS into placeKYT. NOTE: this is a dense hand-placed design — OPEN qam16_modem.kyt to host the chip (importing this .grc auto-places the blocks away from the input port and the DD loop won't lock). Simulation -> Run as GNURadio Server, set server_port to the printed port, then Execute here.
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
import numpy as np, random, math
_N=1.0/math.sqrt(10.0)
_L=[(1,-1),(-1,-1),(3,-3),(-3,-3),(-3,-1),(3,-1),(-1,-3),(1,-3),(-3,3),(3,3),(-1,1),(1,1),(1,3),(-1,3),(3,1),(-3,1)]
_P=[complex(i*_N,q*_N) for (i,q) in _L]
def qam16_burst(n, foff=0.002, seed=5):
    random.seed(seed); s=[random.randint(0,15) for _ in range(n)]
    return [ _P[sm]*complex(math.cos(2*math.pi*foff*k),math.sin(2*math.pi*foff*k)) for k,sm in enumerate(s) ]
import sip
import threading



class qam16_modem(gr.top_block, Qt.QWidget):

    def __init__(self):
        gr.top_block.__init__(self, "Coherent 16-QAM RX (DD Costas + slicer) — input vs recovered symbols", catch_exceptions=True)
        Qt.QWidget.__init__(self)
        self.setWindowTitle("Coherent 16-QAM RX (DD Costas + slicer) — input vs recovered symbols")
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

        self.settings = Qt.QSettings("gnuradio/flowgraphs", "qam16_modem")

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
        self.n_syms = n_syms = 400

        ##################################################
        # Blocks
        ##################################################

        self.src = blocks.vector_source_c(qam16_burst(n_syms), True, 1, [])
        self.slicer = kyttar.qam16_slicer("kyttar_0")
        self.msrc = kyttar.source(device_id="kyttar_0", port_name="x16_in", num_channels=1, server_host="127.0.0.1", server_port=server_port, complex_in=True, burst_len=n_syms, stream_id="", pipelined=True)
        self.msink = kyttar.sink(device_id="kyttar_0", port_name="x16_out", num_channels=1, server_port=server_port, server_repeat=False, hold_secs=8.0, stream_id="", in_type=False)
        self.input_sink = qtgui.time_sink_c(
            256, #size
            1, #samp_rate
            "Input waveform (RRC BPSK, in-phase)", #name
            1, #number of inputs
            None # parent
        )
        self.input_sink.set_update_time(0.10)
        self.input_sink.set_y_axis(-1.2, 1.2)

        self.input_sink.set_y_label('amplitude', "")

        self.input_sink.enable_tags(True)
        self.input_sink.set_trigger_mode(qtgui.TRIG_MODE_FREE, qtgui.TRIG_SLOPE_POS, 0.0, 0, 0, "")
        self.input_sink.enable_autoscale(False)
        self.input_sink.enable_grid(True)
        self.input_sink.enable_axis_labels(True)
        self.input_sink.enable_control_panel(False)
        self.input_sink.enable_stem_plot(False)


        labels = ['input I', 'Signal 2', 'Signal 3', 'Signal 4', 'Signal 5',
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


        for i in range(2):
            if len(labels[i]) == 0:
                if (i % 2 == 0):
                    self.input_sink.set_line_label(i, "Re{{Data {0}}}".format(i/2))
                else:
                    self.input_sink.set_line_label(i, "Im{{Data {0}}}".format(i/2))
            else:
                self.input_sink.set_line_label(i, labels[i])
            self.input_sink.set_line_width(i, widths[i])
            self.input_sink.set_line_color(i, colors[i])
            self.input_sink.set_line_style(i, styles[i])
            self.input_sink.set_line_marker(i, markers[i])
            self.input_sink.set_line_alpha(i, alphas[i])

        self._input_sink_win = sip.wrapinstance(self.input_sink.qwidget(), Qt.QWidget)
        self.top_grid_layout.addWidget(self._input_sink_win, 0, 0, 1, 1)
        for r in range(0, 1):
            self.top_grid_layout.setRowStretch(r, 1)
        for c in range(0, 1):
            self.top_grid_layout.setColumnStretch(c, 1)
        self.costas = kyttar.qam16_costas_loop("kyttar_0", 2048, 32)
        self.bit_sink = qtgui.time_sink_f(
            128, #size
            1, #samp_rate
            "Recovered bits (chip x16_out)", #name
            1, #number of inputs
            None # parent
        )
        self.bit_sink.set_update_time(0.10)
        self.bit_sink.set_y_axis(-0.3, 1.3)

        self.bit_sink.set_y_label('bit', "")

        self.bit_sink.enable_tags(True)
        self.bit_sink.set_trigger_mode(qtgui.TRIG_MODE_FREE, qtgui.TRIG_SLOPE_POS, 0.0, 0, 0, "")
        self.bit_sink.enable_autoscale(False)
        self.bit_sink.enable_grid(True)
        self.bit_sink.enable_axis_labels(True)
        self.bit_sink.enable_control_panel(False)
        self.bit_sink.enable_stem_plot(False)


        labels = ['recovered bit', 'Signal 2', 'Signal 3', 'Signal 4', 'Signal 5',
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
                self.bit_sink.set_line_label(i, "Data {0}".format(i))
            else:
                self.bit_sink.set_line_label(i, labels[i])
            self.bit_sink.set_line_width(i, widths[i])
            self.bit_sink.set_line_color(i, colors[i])
            self.bit_sink.set_line_style(i, styles[i])
            self.bit_sink.set_line_marker(i, markers[i])
            self.bit_sink.set_line_alpha(i, alphas[i])

        self._bit_sink_win = sip.wrapinstance(self.bit_sink.qwidget(), Qt.QWidget)
        self.top_grid_layout.addWidget(self._bit_sink_win, 1, 0, 1, 1)
        for r in range(1, 2):
            self.top_grid_layout.setRowStretch(r, 1)
        for c in range(0, 1):
            self.top_grid_layout.setColumnStretch(c, 1)


        ##################################################
        # Connections
        ##################################################
        self.connect((self.costas, 0), (self.slicer, 0))
        self.connect((self.msink, 0), (self.bit_sink, 0))
        self.connect((self.msrc, 0), (self.costas, 0))
        self.connect((self.slicer, 0), (self.msink, 0))
        self.connect((self.src, 0), (self.input_sink, 0))
        self.connect((self.src, 0), (self.msrc, 0))


    def closeEvent(self, event):
        self.settings = Qt.QSettings("gnuradio/flowgraphs", "qam16_modem")
        self.settings.setValue("geometry", self.saveGeometry())
        self.stop()
        self.wait()

        event.accept()

    def get_server_port(self):
        return self.server_port

    def set_server_port(self, server_port):
        self.server_port = server_port

    def get_n_syms(self):
        return self.n_syms

    def set_n_syms(self, n_syms):
        self.n_syms = n_syms
        self.src.set_data(qam16_burst(self.n_syms), [])




def main(top_block_cls=qam16_modem, options=None):

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
