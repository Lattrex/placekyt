#!/usr/bin/env python3
# -*- coding: utf-8 -*-

#
# SPDX-License-Identifier: GPL-3.0
#
# GNU Radio Python Flow Graph
# Title: Robust RX — FLL coarse frequency recovery vs Costas-only
# Author: Lattrex
# Description: ROBUST RX — coarse frequency recovery on one placeKYT array. One raised-cosine BPSK burst carries a LARGE carrier offset (0.18 cycles/sample, far beyond a Costas loop's pull-in) into TWO placed receiver chains: 'rx' = FLL band-edge (coarse frequency recovery) -> Costas(order 2) -> BPSK slicer, which LOCKS and recovers the bits; 'ctl' = the same Costas -> slicer WITHOUT the FLL (the classic coherent chain's carrier-recovery core), which cannot pull the offset — its recovered bits are garbage. The on-screen story: at real-world frequency offsets the old chain dies, the FLL-fronted one locks.
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
from gnuradio.kyttar import robust_demo_stim as stim
import sip
import threading



class robust_rx(gr.top_block, Qt.QWidget):

    def __init__(self):
        gr.top_block.__init__(self, "Robust RX — FLL coarse frequency recovery vs Costas-only", catch_exceptions=True)
        Qt.QWidget.__init__(self)
        self.setWindowTitle("Robust RX — FLL coarse frequency recovery vs Costas-only")
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

        self.settings = Qt.QSettings("gnuradio/flowgraphs", "robust_rx")

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
        self.n_syms = n_syms = 600
        self.samp_rate = samp_rate = 32000
        self.burst_len = burst_len = stim.n_rx(n_syms)

        ##################################################
        # Blocks
        ##################################################

        self.slicer_b2f = blocks.uchar_to_float()
        self.slicer = kyttar.bpsk_slicer("kyttar_0", "bit")
        self.rx_sink = kyttar.sink(device_id="kyttar_0", port_name="x16_out", num_channels=1, server_port=58950, server_repeat=True, hold_secs=8.0, stream_id="rx", in_type=False)
        self.rx_iq = blocks.vector_source_c(stim.rx_burst(n_syms), False, 1, [])
        self.input_scope = qtgui.time_sink_c(
            (burst_len - 16), #size
            samp_rate, #samp_rate
            "RF burst (foff = 0.18 cyc/sample)", #name
            1, #number of inputs
            None # parent
        )
        self.input_scope.set_update_time(0.10)
        self.input_scope.set_y_axis(-1, 1)

        self.input_scope.set_y_label('level', "")

        self.input_scope.enable_tags(True)
        self.input_scope.set_trigger_mode(qtgui.TRIG_MODE_FREE, qtgui.TRIG_SLOPE_POS, 0.0, 0, 0, "")
        self.input_scope.enable_autoscale(False)
        self.input_scope.enable_grid(False)
        self.input_scope.enable_axis_labels(True)
        self.input_scope.enable_control_panel(False)
        self.input_scope.enable_stem_plot(False)


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


        for i in range(2):
            if len(labels[i]) == 0:
                if (i % 2 == 0):
                    self.input_scope.set_line_label(i, "Re{{Data {0}}}".format(i/2))
                else:
                    self.input_scope.set_line_label(i, "Im{{Data {0}}}".format(i/2))
            else:
                self.input_scope.set_line_label(i, labels[i])
            self.input_scope.set_line_width(i, widths[i])
            self.input_scope.set_line_color(i, colors[i])
            self.input_scope.set_line_style(i, styles[i])
            self.input_scope.set_line_marker(i, markers[i])
            self.input_scope.set_line_alpha(i, alphas[i])

        self._input_scope_win = sip.wrapinstance(self.input_scope.qwidget(), Qt.QWidget)
        self.top_layout.addWidget(self._input_scope_win)
        self.fll_src = kyttar.source(device_id="kyttar_0", port_name="x16_in", num_channels=1, server_host="127.0.0.1", server_port=58950, complex_in=True, burst_len=burst_len, stream_id="rx", pipelined=False, schedule="interleaved", repeat=False, output_words="auto")
        self.fll_bits_scope = qtgui.time_sink_f(
            stim.n_rx_bits(n_syms), #size
            samp_rate, #samp_rate
            "Recovered bits — FLL + Costas (locks)", #name
            1, #number of inputs
            None # parent
        )
        self.fll_bits_scope.set_update_time(0.10)
        self.fll_bits_scope.set_y_axis(-0.5, 1.5)

        self.fll_bits_scope.set_y_label('bit', "")

        self.fll_bits_scope.enable_tags(True)
        self.fll_bits_scope.set_trigger_mode(qtgui.TRIG_MODE_FREE, qtgui.TRIG_SLOPE_POS, 0.0, 0, 0, "")
        self.fll_bits_scope.enable_autoscale(False)
        self.fll_bits_scope.enable_grid(False)
        self.fll_bits_scope.enable_axis_labels(True)
        self.fll_bits_scope.enable_control_panel(False)
        self.fll_bits_scope.enable_stem_plot(False)


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
                self.fll_bits_scope.set_line_label(i, "Data {0}".format(i))
            else:
                self.fll_bits_scope.set_line_label(i, labels[i])
            self.fll_bits_scope.set_line_width(i, widths[i])
            self.fll_bits_scope.set_line_color(i, colors[i])
            self.fll_bits_scope.set_line_style(i, styles[i])
            self.fll_bits_scope.set_line_marker(i, markers[i])
            self.fll_bits_scope.set_line_alpha(i, alphas[i])

        self._fll_bits_scope_win = sip.wrapinstance(self.fll_bits_scope.qwidget(), Qt.QWidget)
        self.top_layout.addWidget(self._fll_bits_scope_win)
        self.fll = kyttar.fll_band_edge("kyttar_0", 2.0, 0.35, 17, 0.1)
        self.ctl_src = kyttar.source(device_id="kyttar_0", port_name="x16_in", num_channels=1, server_host="127.0.0.1", server_port=58950, complex_in=True, burst_len=burst_len, stream_id="ctl", pipelined=False, schedule="interleaved", repeat=False, output_words="auto")
        self.ctl_slicer = kyttar.bpsk_slicer("kyttar_0", "bit")
        self.ctl_sink = kyttar.sink(device_id="kyttar_0", port_name="x16_out", num_channels=1, server_port=58950, server_repeat=True, hold_secs=8.0, stream_id="ctl", in_type=False)
        self.ctl_costas = kyttar.complex_costas_loop("kyttar_0", 0.05, 1.0, 2)
        self.ctl_bits_scope = qtgui.time_sink_f(
            stim.n_rx_bits(n_syms), #size
            samp_rate, #samp_rate
            "Recovered bits — Costas only (fails)", #name
            1, #number of inputs
            None # parent
        )
        self.ctl_bits_scope.set_update_time(0.10)
        self.ctl_bits_scope.set_y_axis(-0.5, 1.5)

        self.ctl_bits_scope.set_y_label('bit', "")

        self.ctl_bits_scope.enable_tags(True)
        self.ctl_bits_scope.set_trigger_mode(qtgui.TRIG_MODE_FREE, qtgui.TRIG_SLOPE_POS, 0.0, 0, 0, "")
        self.ctl_bits_scope.enable_autoscale(False)
        self.ctl_bits_scope.enable_grid(False)
        self.ctl_bits_scope.enable_axis_labels(True)
        self.ctl_bits_scope.enable_control_panel(False)
        self.ctl_bits_scope.enable_stem_plot(False)


        labels = ['Signal 1', 'Signal 2', 'Signal 3', 'Signal 4', 'Signal 5',
            'Signal 6', 'Signal 7', 'Signal 8', 'Signal 9', 'Signal 10']
        widths = [1, 1, 1, 1, 1,
            1, 1, 1, 1, 1]
        colors = ['red', 'red', 'green', 'black', 'cyan',
            'magenta', 'yellow', 'dark red', 'dark green', 'dark blue']
        alphas = [1.0, 1.0, 1.0, 1.0, 1.0,
            1.0, 1.0, 1.0, 1.0, 1.0]
        styles = [1, 1, 1, 1, 1,
            1, 1, 1, 1, 1]
        markers = [-1, -1, -1, -1, -1,
            -1, -1, -1, -1, -1]


        for i in range(1):
            if len(labels[i]) == 0:
                self.ctl_bits_scope.set_line_label(i, "Data {0}".format(i))
            else:
                self.ctl_bits_scope.set_line_label(i, labels[i])
            self.ctl_bits_scope.set_line_width(i, widths[i])
            self.ctl_bits_scope.set_line_color(i, colors[i])
            self.ctl_bits_scope.set_line_style(i, styles[i])
            self.ctl_bits_scope.set_line_marker(i, markers[i])
            self.ctl_bits_scope.set_line_alpha(i, alphas[i])

        self._ctl_bits_scope_win = sip.wrapinstance(self.ctl_bits_scope.qwidget(), Qt.QWidget)
        self.top_layout.addWidget(self._ctl_bits_scope_win)
        self.ctl_b2f = blocks.uchar_to_float()
        self.costas = kyttar.complex_costas_loop("kyttar_0", 0.05, 1.0, 2)


        ##################################################
        # Connections
        ##################################################
        self.connect((self.costas, 0), (self.slicer, 0))
        self.connect((self.ctl_b2f, 0), (self.ctl_sink, 0))
        self.connect((self.ctl_costas, 0), (self.ctl_slicer, 0))
        self.connect((self.ctl_sink, 0), (self.ctl_bits_scope, 0))
        self.connect((self.ctl_slicer, 0), (self.ctl_b2f, 0))
        self.connect((self.ctl_src, 0), (self.ctl_costas, 0))
        self.connect((self.fll, 0), (self.costas, 0))
        self.connect((self.fll_src, 0), (self.fll, 0))
        self.connect((self.rx_iq, 0), (self.ctl_src, 0))
        self.connect((self.rx_iq, 0), (self.fll_src, 0))
        self.connect((self.rx_iq, 0), (self.input_scope, 0))
        self.connect((self.rx_sink, 0), (self.fll_bits_scope, 0))
        self.connect((self.slicer, 0), (self.slicer_b2f, 0))
        self.connect((self.slicer_b2f, 0), (self.rx_sink, 0))


    def closeEvent(self, event):
        self.settings = Qt.QSettings("gnuradio/flowgraphs", "robust_rx")
        self.settings.setValue("geometry", self.saveGeometry())
        self.stop()
        self.wait()

        event.accept()

    def get_n_syms(self):
        return self.n_syms

    def set_n_syms(self, n_syms):
        self.n_syms = n_syms
        self.set_burst_len(stim.n_rx(self.n_syms))
        self.rx_iq.set_data(stim.rx_burst(self.n_syms), [])

    def get_samp_rate(self):
        return self.samp_rate

    def set_samp_rate(self, samp_rate):
        self.samp_rate = samp_rate
        self.input_scope.set_samp_rate(self.samp_rate)
        self.fll_bits_scope.set_samp_rate(self.samp_rate)
        self.ctl_bits_scope.set_samp_rate(self.samp_rate)

    def get_burst_len(self):
        return self.burst_len

    def set_burst_len(self, burst_len):
        self.burst_len = burst_len




def main(top_block_cls=robust_rx, options=None):

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
