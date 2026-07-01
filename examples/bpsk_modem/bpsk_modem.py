#!/usr/bin/env python3
# -*- coding: utf-8 -*-

#
# SPDX-License-Identifier: GPL-3.0
#
# GNU Radio Python Flow Graph
# Title: Full-duplex BPSK modem (TX + coherent RX on one array)
# Author: Lattrex
# Description: Full-duplex BPSK modem authored in GNU Radio (GRC-first workflow). The TX chain (PSK symbol mapper -> upsampler -> RRC pulse shaper -> I/Q upconvert) and the coherent RX chain (complex RRC matched filter -> complex Costas loop -> Gardner timing recovery -> BPSK slicer) live on ONE placeKYT array, sharing ONE input port (x16_in) and ONE output port (x16_out): both TX and RX source blocks map to x16_in and both sink blocks map to x16_out (the shared-port duplex). Built from the REAL DSP blocks so it IMPORTS into placeKYT (File -> Import GNURadio Flowgraph): all 8 blocks get placed and the logical nets are recovered; the headless engine.bpsk_modem_demo proves BER 0 on the built bitstream.
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
from gnuradio.kyttar import modem_demo_stim as stim
import sip
import threading



class bpsk_modem(gr.top_block, Qt.QWidget):

    def __init__(self):
        gr.top_block.__init__(self, "Full-duplex BPSK modem (TX + coherent RX on one array)", catch_exceptions=True)
        Qt.QWidget.__init__(self)
        self.setWindowTitle("Full-duplex BPSK modem (TX + coherent RX on one array)")
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

        self.settings = Qt.QSettings("gnuradio/flowgraphs", "bpsk_modem")

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
        self.sps = sps = 4
        self.samp_rate = samp_rate = 32000
        self.n_syms = n_syms = 120
        self.n_bits = n_bits = 64
        self.carrier = carrier = 4000

        ##################################################
        # Blocks
        ##################################################

        self.upc = kyttar.iq_upconvert("kyttar_0", samp_rate, carrier)
        self.up = kyttar.upsampler("kyttar_0", sps)
        self.tx_src = kyttar.source(device_id="kyttar_0", port_name="x16_in", num_channels=1, server_host="127.0.0.1", server_port=58950, complex_in=False, burst_len=n_bits, stream_id="tx")
        self.tx_sink = kyttar.sink(device_id="kyttar_0", port_name="x16_out", num_channels=1, server_port=58950, server_repeat=False, hold_secs=8.0, stream_id="tx")
        self.tx_passband = qtgui.time_sink_f(
            stim.tx_pb_points(n_bits), #size
            samp_rate, #samp_rate
            "TX passband (real)", #name
            1, #number of inputs
            None # parent
        )
        self.tx_passband.set_update_time(0.10)
        self.tx_passband.set_y_axis(-1, 1)

        self.tx_passband.set_y_label('amplitude', "")

        self.tx_passband.enable_tags(True)
        self.tx_passband.set_trigger_mode(qtgui.TRIG_MODE_FREE, qtgui.TRIG_SLOPE_POS, 0.0, 0, 0, "")
        self.tx_passband.enable_autoscale(False)
        self.tx_passband.enable_grid(False)
        self.tx_passband.enable_axis_labels(True)
        self.tx_passband.enable_control_panel(False)
        self.tx_passband.enable_stem_plot(False)


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
                self.tx_passband.set_line_label(i, "Data {0}".format(i))
            else:
                self.tx_passband.set_line_label(i, labels[i])
            self.tx_passband.set_line_width(i, widths[i])
            self.tx_passband.set_line_color(i, colors[i])
            self.tx_passband.set_line_style(i, styles[i])
            self.tx_passband.set_line_marker(i, markers[i])
            self.tx_passband.set_line_alpha(i, alphas[i])

        self._tx_passband_win = sip.wrapinstance(self.tx_passband.qwidget(), Qt.QWidget)
        self.top_layout.addWidget(self._tx_passband_win)
        self.tx_bits = blocks.vector_source_f(stim.tx_bits(n_bits), False, 1, [])
        self.slicer = kyttar.bpsk_slicer("kyttar_0")
        self.rx_src = kyttar.source(device_id="kyttar_0", port_name="x16_in", num_channels=1, server_host="127.0.0.1", server_port=58950, complex_in=True, burst_len=stim.rx_burst_len(n_syms), stream_id="rx")
        self.rx_sink = kyttar.sink(device_id="kyttar_0", port_name="x16_out", num_channels=1, server_port=58950, server_repeat=False, hold_secs=8.0, stream_id="rx")
        self.rx_iq = blocks.vector_source_c(stim.rx_burst(n_syms), False, 1, [])
        self.rx_bits = qtgui.time_sink_f(
            stim.rx_bits_points(n_syms), #size
            samp_rate, #samp_rate
            "Recovered bits", #name
            1, #number of inputs
            None # parent
        )
        self.rx_bits.set_update_time(0.10)
        self.rx_bits.set_y_axis(-1, 1)

        self.rx_bits.set_y_label('bit', "")

        self.rx_bits.enable_tags(True)
        self.rx_bits.set_trigger_mode(qtgui.TRIG_MODE_FREE, qtgui.TRIG_SLOPE_POS, 0.0, 0, 0, "")
        self.rx_bits.enable_autoscale(False)
        self.rx_bits.enable_grid(False)
        self.rx_bits.enable_axis_labels(True)
        self.rx_bits.enable_control_panel(False)
        self.rx_bits.enable_stem_plot(False)


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
                self.rx_bits.set_line_label(i, "Data {0}".format(i))
            else:
                self.rx_bits.set_line_label(i, labels[i])
            self.rx_bits.set_line_width(i, widths[i])
            self.rx_bits.set_line_color(i, colors[i])
            self.rx_bits.set_line_style(i, styles[i])
            self.rx_bits.set_line_marker(i, markers[i])
            self.rx_bits.set_line_alpha(i, alphas[i])

        self._rx_bits_win = sip.wrapinstance(self.rx_bits.qwidget(), Qt.QWidget)
        self.top_layout.addWidget(self._rx_bits_win)
        self.rrc = kyttar.rrc_pulse_shaper("kyttar_0", 0.35, 8)
        self.mf = kyttar.complex_rrc_matched_filter("kyttar_0", 0.35, 8)
        self.mapper = kyttar.psk_symbol_mapper("kyttar_0", "bpsk")
        self.gardner = kyttar.gardner_timing_recovery("kyttar_0", 3, 1)
        self.costas = kyttar.complex_costas_loop("kyttar_0", 0.05, 1.0)


        ##################################################
        # Connections
        ##################################################
        self.connect((self.costas, 0), (self.gardner, 0))
        self.connect((self.gardner, 0), (self.slicer, 0))
        self.connect((self.mapper, 0), (self.up, 0))
        self.connect((self.mf, 0), (self.costas, 0))
        self.connect((self.rrc, 0), (self.upc, 0))
        self.connect((self.rx_iq, 0), (self.rx_src, 0))
        self.connect((self.rx_sink, 0), (self.rx_bits, 0))
        self.connect((self.rx_src, 0), (self.mf, 0))
        self.connect((self.slicer, 0), (self.rx_sink, 0))
        self.connect((self.tx_bits, 0), (self.tx_src, 0))
        self.connect((self.tx_sink, 0), (self.tx_passband, 0))
        self.connect((self.tx_src, 0), (self.mapper, 0))
        self.connect((self.up, 0), (self.rrc, 0))
        self.connect((self.upc, 0), (self.tx_sink, 0))


    def closeEvent(self, event):
        self.settings = Qt.QSettings("gnuradio/flowgraphs", "bpsk_modem")
        self.settings.setValue("geometry", self.saveGeometry())
        self.stop()
        self.wait()

        event.accept()

    def get_sps(self):
        return self.sps

    def set_sps(self, sps):
        self.sps = sps

    def get_samp_rate(self):
        return self.samp_rate

    def set_samp_rate(self, samp_rate):
        self.samp_rate = samp_rate
        self.tx_passband.set_samp_rate(self.samp_rate)
        self.rx_bits.set_samp_rate(self.samp_rate)

    def get_n_syms(self):
        return self.n_syms

    def set_n_syms(self, n_syms):
        self.n_syms = n_syms
        self.rx_iq.set_data(stim.rx_burst(self.n_syms), [])

    def get_n_bits(self):
        return self.n_bits

    def set_n_bits(self, n_bits):
        self.n_bits = n_bits
        self.tx_bits.set_data(stim.tx_bits(self.n_bits), [])

    def get_carrier(self):
        return self.carrier

    def set_carrier(self, carrier):
        self.carrier = carrier




def main(top_block_cls=bpsk_modem, options=None):

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
