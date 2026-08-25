#!/usr/bin/env python3
# -*- coding: utf-8 -*-

#
# SPDX-License-Identifier: GPL-3.0
#
# GNU Radio Python Flow Graph
# Title: CSS receiver - chirp spread spectrum on one placeKYT array
# Author: Lattrex
# Description: CSS (chirp spread spectrum) receiver on one placeKYT array. The WHOLE receive spine is placed on the chip: dechirp (conjugate chirp mixer) -> 16-point streaming FFT -> bin power -> the alignment Delay(1) -> framewise argmax. One continuous burst carries the message 'KYTTAR CSS' twice: segment A at +10 dB SNR decodes exactly, segment B at -10 dB is the on-chip negative control and collapses. The decode map is s = brev4(bin index) because FFT16 emits bins in bit-reversed order. The transmitter and channel are host-side numpy (bit-exact to the TX blocks' own chip-verified goldens) - this is an RX example and does not claim a transmitter on the chip.
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
from gnuradio.kyttar import css_demo_stim as stim
import css_transceiver_bin_to_sym as bin_to_sym  # embedded python block
import sip
import threading



class css_transceiver(gr.top_block, Qt.QWidget):

    def __init__(self):
        gr.top_block.__init__(self, "CSS receiver - chirp spread spectrum on one placeKYT array", catch_exceptions=True)
        Qt.QWidget.__init__(self)
        self.setWindowTitle("CSS receiver - chirp spread spectrum on one placeKYT array")
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

        self.settings = Qt.QSettings("gnuradio/flowgraphs", "css_transceiver")

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
        self.tx_syms = tx_syms = stim.display_symbols()
        self.samp_rate = samp_rate = 32000
        self.n_words = n_words = stim.n_out_words()
        self.n_css = n_css = 16
        self.burst_len = burst_len = stim.burst_len()

        ##################################################
        # Blocks
        ##################################################

        self.tx_ref = blocks.vector_source_f(tx_syms, True, 1, [])
        self.sym_scope = qtgui.time_sink_f(
            n_words, #size
            samp_rate, #samp_rate
            "DECODED SYMBOL vs TRANSMITTED — A locks, B collapses", #name
            2, #number of inputs
            None # parent
        )
        self.sym_scope.set_update_time(0.10)
        self.sym_scope.set_y_axis(-1, 16)

        self.sym_scope.set_y_label('symbol', "")

        self.sym_scope.enable_tags(True)
        self.sym_scope.set_trigger_mode(qtgui.TRIG_MODE_FREE, qtgui.TRIG_SLOPE_POS, 0.0, 0, 0, "")
        self.sym_scope.enable_autoscale(False)
        self.sym_scope.enable_grid(False)
        self.sym_scope.enable_axis_labels(True)
        self.sym_scope.enable_control_panel(False)
        self.sym_scope.enable_stem_plot(False)


        labels = ['decoded symbol (chip)', 'transmitted symbol', 'Signal 3', 'Signal 4', 'Signal 5',
            'Signal 6', 'Signal 7', 'Signal 8', 'Signal 9', 'Signal 10']
        widths = [1, 1, 1, 1, 1,
            1, 1, 1, 1, 1]
        colors = ['blue', 'red', 'green', 'black', 'cyan',
            'magenta', 'yellow', 'dark red', 'dark green', 'dark blue']
        alphas = [1.0, 1.0, 1.0, 1.0, 1.0,
            1.0, 1.0, 1.0, 1.0, 1.0]
        styles = [0, 0, 1, 1, 1,
            1, 1, 1, 1, 1]
        markers = [0, 1, -1, -1, -1,
            -1, -1, -1, -1, -1]


        for i in range(2):
            if len(labels[i]) == 0:
                self.sym_scope.set_line_label(i, "Data {0}".format(i))
            else:
                self.sym_scope.set_line_label(i, labels[i])
            self.sym_scope.set_line_width(i, widths[i])
            self.sym_scope.set_line_color(i, colors[i])
            self.sym_scope.set_line_style(i, styles[i])
            self.sym_scope.set_line_marker(i, markers[i])
            self.sym_scope.set_line_alpha(i, alphas[i])

        self._sym_scope_win = sip.wrapinstance(self.sym_scope.qwidget(), Qt.QWidget)
        self.top_layout.addWidget(self._sym_scope_win)
        self.rx_src = kyttar.source(device_id="kyttar_0", port_name="x16_in", num_channels=1, server_host="127.0.0.1", server_port=58950, complex_in=True, burst_len=burst_len, stream_id="rx", pipelined=False, schedule="interleaved", repeat=False, output_words="auto")
        self.rx_sink = kyttar.sink(device_id="kyttar_0", port_name="x16_out", num_channels=1, server_port=58950, server_repeat=True, hold_secs=8.0, stream_id="rx", in_type=False)
        self.rx_iq = blocks.vector_source_c(stim.rx_burst(), True, 1, [])
        self.magsq = kyttar.complex_to_mag_squared(device_id="kyttar_0")
        self.input_scope = qtgui.time_sink_c(
            (burst_len // 2), #size
            samp_rate, #samp_rate
            "RF burst — CSS up-chirps (A: +10 dB, B: -10 dB)", #name
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
        self.idx_s2f = blocks.short_to_float(1, 1)
        self.fft = kyttar.fft16(device_id="kyttar_0")
        self.dechirp = kyttar.conj_chirp_mixer(device_id="kyttar_0", n=n_css)
        self.bin_to_sym = bin_to_sym.blk(n=n_css)
        self.bin_scope = qtgui.time_sink_f(
            n_words, #size
            samp_rate, #samp_rate
            "Chip output — winning FFT bin (raw index 0..15)", #name
            1, #number of inputs
            None # parent
        )
        self.bin_scope.set_update_time(0.10)
        self.bin_scope.set_y_axis(-1, 16)

        self.bin_scope.set_y_label('bin index', "")

        self.bin_scope.enable_tags(True)
        self.bin_scope.set_trigger_mode(qtgui.TRIG_MODE_FREE, qtgui.TRIG_SLOPE_POS, 0.0, 0, 0, "")
        self.bin_scope.enable_autoscale(False)
        self.bin_scope.enable_grid(False)
        self.bin_scope.enable_axis_labels(True)
        self.bin_scope.enable_control_panel(False)
        self.bin_scope.enable_stem_plot(False)


        labels = ['Signal 1', 'Signal 2', 'Signal 3', 'Signal 4', 'Signal 5',
            'Signal 6', 'Signal 7', 'Signal 8', 'Signal 9', 'Signal 10']
        widths = [1, 1, 1, 1, 1,
            1, 1, 1, 1, 1]
        colors = ['green', 'red', 'green', 'black', 'cyan',
            'magenta', 'yellow', 'dark red', 'dark green', 'dark blue']
        alphas = [1.0, 1.0, 1.0, 1.0, 1.0,
            1.0, 1.0, 1.0, 1.0, 1.0]
        styles = [1, 1, 1, 1, 1,
            1, 1, 1, 1, 1]
        markers = [-1, -1, -1, -1, -1,
            -1, -1, -1, -1, -1]


        for i in range(1):
            if len(labels[i]) == 0:
                self.bin_scope.set_line_label(i, "Data {0}".format(i))
            else:
                self.bin_scope.set_line_label(i, labels[i])
            self.bin_scope.set_line_width(i, widths[i])
            self.bin_scope.set_line_color(i, colors[i])
            self.bin_scope.set_line_style(i, styles[i])
            self.bin_scope.set_line_marker(i, markers[i])
            self.bin_scope.set_line_alpha(i, alphas[i])

        self._bin_scope_win = sip.wrapinstance(self.bin_scope.qwidget(), Qt.QWidget)
        self.top_layout.addWidget(self._bin_scope_win)
        self.argmax = kyttar.bin_argmax(device_id="kyttar_0", n=n_css)
        self.align = kyttar.delay(device_id="kyttar_0", delay=1)


        ##################################################
        # Connections
        ##################################################
        self.connect((self.align, 0), (self.argmax, 0))
        self.connect((self.argmax, 0), (self.idx_s2f, 0))
        self.connect((self.bin_to_sym, 0), (self.sym_scope, 0))
        self.connect((self.dechirp, 0), (self.fft, 0))
        self.connect((self.fft, 0), (self.magsq, 0))
        self.connect((self.idx_s2f, 0), (self.rx_sink, 0))
        self.connect((self.magsq, 0), (self.align, 0))
        self.connect((self.rx_iq, 0), (self.input_scope, 0))
        self.connect((self.rx_iq, 0), (self.rx_src, 0))
        self.connect((self.rx_sink, 0), (self.bin_scope, 0))
        self.connect((self.rx_sink, 0), (self.bin_to_sym, 0))
        self.connect((self.rx_src, 0), (self.dechirp, 0))
        self.connect((self.tx_ref, 0), (self.sym_scope, 1))


    def closeEvent(self, event):
        self.settings = Qt.QSettings("gnuradio/flowgraphs", "css_transceiver")
        self.settings.setValue("geometry", self.saveGeometry())
        self.stop()
        self.wait()

        event.accept()

    def get_tx_syms(self):
        return self.tx_syms

    def set_tx_syms(self, tx_syms):
        self.tx_syms = tx_syms
        self.tx_ref.set_data(self.tx_syms, [])

    def get_samp_rate(self):
        return self.samp_rate

    def set_samp_rate(self, samp_rate):
        self.samp_rate = samp_rate
        self.input_scope.set_samp_rate(self.samp_rate)
        self.bin_scope.set_samp_rate(self.samp_rate)
        self.sym_scope.set_samp_rate(self.samp_rate)

    def get_n_words(self):
        return self.n_words

    def set_n_words(self, n_words):
        self.n_words = n_words

    def get_n_css(self):
        return self.n_css

    def set_n_css(self, n_css):
        self.n_css = n_css
        self.bin_to_sym.n = self.n_css

    def get_burst_len(self):
        return self.burst_len

    def set_burst_len(self, burst_len):
        self.burst_len = burst_len




def main(top_block_cls=css_transceiver, options=None):

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
