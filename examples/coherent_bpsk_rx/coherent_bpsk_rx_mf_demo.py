#!/usr/bin/env python3
# -*- coding: utf-8 -*-

#
# SPDX-License-Identifier: GPL-3.0
#
# GNU Radio Python Flow Graph
# Title: Coherent BPSK RX (RRC matched filter) — input vs recovered bits
# Author: Lattrex
# Description: PRODUCTION coherent BPSK RX demo with the RRC matched-filter front end (GRC-first workflow). Built from the REAL DSP blocks so it IMPORTS into placeKYT: ComplexRRCMatchedFilter -> ComplexCostasLoop -> GardnerTimingRecovery -> BPSKSlicer get placed + bus-routed; source/sink -> chip ports. The SAME flowgraph RUNS linked to the placeKYT-hosted chip: Simulation -> Run as GNURadio Server in placeKYT, set server_port below to the printed port, then Execute here. kyttar_source batches the whole I/Q burst through the hosted chip in one RPC. The QT time sinks show the INPUT waveform (top) versus the RECOVERED bits (bottom) — the input-vs-output view.
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
from gnuradio.kyttar import coherent_demo_stim as stim
import sip
import threading



class coherent_bpsk_rx_mf_demo(gr.top_block, Qt.QWidget):

    def __init__(self):
        gr.top_block.__init__(self, "Coherent BPSK RX (RRC matched filter) — input vs recovered bits", catch_exceptions=True)
        Qt.QWidget.__init__(self)
        self.setWindowTitle("Coherent BPSK RX (RRC matched filter) — input vs recovered bits")
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

        self.settings = Qt.QSettings("gnuradio/flowgraphs", "coherent_bpsk_rx_mf_demo")

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
        self.n_syms = n_syms = 160

        ##################################################
        # Blocks
        ##################################################

        self.src = blocks.vector_source_c(stim.burst(n_syms), True, 1, [])
        self.slicer_b2f = blocks.uchar_to_float()
        self.slicer = kyttar.bpsk_slicer("kyttar_0", "bit")
        self.msrc = kyttar.source(device_id="kyttar_0", port_name="x16_in", num_channels=1, server_host="127.0.0.1", server_port=server_port, complex_in=True, burst_len=stim.burst_len(n_syms), stream_id='', pipelined=True, schedule="interleaved", repeat=False, output_words="auto")
        self.msink = kyttar.sink(device_id="kyttar_0", port_name="x16_out", num_channels=1, server_port=server_port, server_repeat=False, hold_secs=8.0, stream_id='', in_type=False)
        self.mf = kyttar.complex_rrc_matched_filter(device_id="kyttar_0", gain=0.7105, samp_rate=2.0, sym_rate=1.0, alpha=0.35, ntaps=17, decimation=1)
        self.input_sink = qtgui.time_sink_f(
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


        for i in range(1):
            if len(labels[i]) == 0:
                self.input_sink.set_line_label(i, "Data {0}".format(i))
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
        self.gardner = kyttar.gardner_timing_recovery("kyttar_0", 3, 1, False)
        self.costas = kyttar.complex_costas_loop("kyttar_0", 0.05, 1.0, 2)
        self.c2f = blocks.complex_to_float(1)
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
        self.connect((self.c2f, 0), (self.input_sink, 0))
        self.connect((self.costas, 0), (self.gardner, 0))
        self.connect((self.gardner, 0), (self.slicer, 0))
        self.connect((self.mf, 0), (self.costas, 0))
        self.connect((self.msink, 0), (self.bit_sink, 0))
        self.connect((self.msrc, 0), (self.mf, 0))
        self.connect((self.slicer, 0), (self.slicer_b2f, 0))
        self.connect((self.slicer_b2f, 0), (self.msink, 0))
        self.connect((self.src, 0), (self.c2f, 0))
        self.connect((self.src, 0), (self.msrc, 0))


    def closeEvent(self, event):
        self.settings = Qt.QSettings("gnuradio/flowgraphs", "coherent_bpsk_rx_mf_demo")
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
        self.src.set_data(stim.burst(self.n_syms), [])




def main(top_block_cls=coherent_bpsk_rx_mf_demo, options=None):

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
