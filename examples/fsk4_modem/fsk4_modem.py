#!/usr/bin/env python3
# -*- coding: utf-8 -*-

#
# SPDX-License-Identifier: GPL-3.0
#
# GNU Radio Python Flow Graph
# Title: Full-duplex M17 4FSK modem (TX + sync-timed RX on one array)
# Author: Lattrex
# Description: Full-duplex M17 4FSK (C4FM) modem authored in GNU Radio (GRC-first workflow). The TX chain (4FSK symbol mapper -> upsampler -> RRC pulse shaper -> frequency modulator) and the RX chain (quadrature demod -> RRC matched filter -> 4FSK sync-word timing recovery -> 4FSK slicer) live on ONE placeKYT array, sharing ONE input port (x16_in) and ONE output port (x16_out): both TX and RX source blocks map to x16_in and both sink blocks map to x16_out (the shared-port duplex). Gardner does NOT lock a 4-level FSK signal, so timing is recovered by SYNC-WORD CORRELATION (the M17 LSF sync word) -- exactly what real M17 receivers do. Built from the REAL DSP blocks so it IMPORTS into placeKYT (File -> Import GNURadio Flowgraph) and recovers the M17 dibits at BER 0.
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
from gnuradio.kyttar import fsk4_demo_stim as stim
import sip
import threading



class fsk4_modem(gr.top_block, Qt.QWidget):

    def __init__(self):
        gr.top_block.__init__(self, "Full-duplex M17 4FSK modem (TX + sync-timed RX on one array)", catch_exceptions=True)
        Qt.QWidget.__init__(self)
        self.setWindowTitle("Full-duplex M17 4FSK modem (TX + sync-timed RX on one array)")
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

        self.settings = Qt.QSettings("gnuradio/flowgraphs", "fsk4_modem")

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
        self.sps = sps = 2
        self.sensitivity = sensitivity = 1.5707963267948966
        self.samp_rate = samp_rate = 9600
        self.n_syms = n_syms = 160
        self.n_bits = n_bits = 64

        ##################################################
        # Blocks
        ##################################################

        self.tx_syms = qtgui.time_sink_f(
            stim.tx_syms_points(n_bits), #size
            samp_rate, #samp_rate
            "Transmitted dibits", #name
            1, #number of inputs
            None # parent
        )
        self.tx_syms.set_update_time(0.10)
        self.tx_syms.set_y_axis(0, 3)

        self.tx_syms.set_y_label('dibit', "")

        self.tx_syms.enable_tags(True)
        self.tx_syms.set_trigger_mode(qtgui.TRIG_MODE_FREE, qtgui.TRIG_SLOPE_POS, 0.0, 0, 0, "")
        self.tx_syms.enable_autoscale(False)
        self.tx_syms.enable_grid(False)
        self.tx_syms.enable_axis_labels(True)
        self.tx_syms.enable_control_panel(False)
        self.tx_syms.enable_stem_plot(False)


        labels = ['dibit', 'Signal 2', 'Signal 3', 'Signal 4', 'Signal 5',
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
                self.tx_syms.set_line_label(i, "Data {0}".format(i))
            else:
                self.tx_syms.set_line_label(i, labels[i])
            self.tx_syms.set_line_width(i, widths[i])
            self.tx_syms.set_line_color(i, colors[i])
            self.tx_syms.set_line_style(i, styles[i])
            self.tx_syms.set_line_marker(i, markers[i])
            self.tx_syms.set_line_alpha(i, alphas[i])

        self._tx_syms_win = sip.wrapinstance(self.tx_syms.qwidget(), Qt.QWidget)
        self.top_layout.addWidget(self._tx_syms_win)
        self.tx_bits = blocks.vector_source_f(stim.tx_bits(n_bits), False, 1, [])
        self.up = kyttar.upsampler("kyttar_0", 2, io_type=float)
        self.tx_syms_src = blocks.vector_source_f(stim.tx_syms(n_bits), False, 1, [])
        self.tx_src = kyttar.source(device_id="kyttar_0", port_name="x16_in", num_channels=1, server_host="127.0.0.1", server_port=58950, complex_in=False, burst_len=len(stim.tx_bits(n_bits)), stream_id="tx", pipelined=False)
        self.tx_sink = kyttar.sink(device_id="kyttar_0", port_name="x16_out", num_channels=1, server_port=58950, server_repeat=False, hold_secs=8.0, stream_id="tx", in_type=True)
        self.tx_passband = qtgui.time_sink_f(
            stim.tx_pb_points(n_bits), #size
            samp_rate, #samp_rate
            "TX baseband I/Q", #name
            2, #number of inputs
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


        labels = ['I', 'Q', 'Signal 3', 'Signal 4', 'Signal 5',
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
        self.tx_iq_split = blocks.deinterleave(gr.sizeof_float*1, 1)
        self.timing = kyttar.fsk4_sync_timing_recovery("kyttar_0")
        self.slicer = kyttar.fsk4_slicer("kyttar_0")
        self.rx_syms = qtgui.time_sink_f(
            stim.rx_syms_points(n_syms), #size
            samp_rate, #samp_rate
            "Recovered dibits", #name
            1, #number of inputs
            None # parent
        )
        self.rx_syms.set_update_time(0.10)
        self.rx_syms.set_y_axis(0, 3)

        self.rx_syms.set_y_label('dibit', "")

        self.rx_syms.enable_tags(True)
        self.rx_syms.set_trigger_mode(qtgui.TRIG_MODE_FREE, qtgui.TRIG_SLOPE_POS, 0.0, 0, 0, "")
        self.rx_syms.enable_autoscale(False)
        self.rx_syms.enable_grid(False)
        self.rx_syms.enable_axis_labels(True)
        self.rx_syms.enable_control_panel(False)
        self.rx_syms.enable_stem_plot(False)


        labels = ['dibit', 'Signal 2', 'Signal 3', 'Signal 4', 'Signal 5',
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
                self.rx_syms.set_line_label(i, "Data {0}".format(i))
            else:
                self.rx_syms.set_line_label(i, labels[i])
            self.rx_syms.set_line_width(i, widths[i])
            self.rx_syms.set_line_color(i, colors[i])
            self.rx_syms.set_line_style(i, styles[i])
            self.rx_syms.set_line_marker(i, markers[i])
            self.rx_syms.set_line_alpha(i, alphas[i])

        self._rx_syms_win = sip.wrapinstance(self.rx_syms.qwidget(), Qt.QWidget)
        self.top_layout.addWidget(self._rx_syms_win)
        self.rx_src = kyttar.source(device_id="kyttar_0", port_name="x16_in", num_channels=1, server_host="127.0.0.1", server_port=58950, complex_in=True, burst_len=stim.burst_len(n_syms), stream_id="rx", pipelined=False)
        self.rx_sink = kyttar.sink(device_id="kyttar_0", port_name="x16_out", num_channels=1, server_port=58950, server_repeat=False, hold_secs=8.0, stream_id="rx", in_type=False)
        self.rx_iq = blocks.vector_source_c(stim.burst(n_syms), False, 1, [])
        self.rrc = kyttar.rrc_pulse_shaper("kyttar_0", 0.5, 8, sps=2, io_type=float)
        self.qd = kyttar.quadrature_demod(device_id="kyttar_0", gain=1.0)
        self.mf = kyttar.rrc_pulse_shaper("kyttar_0", 0.5, 8, sps=2, io_type=float)
        self.mapper = kyttar.fsk4_symbol_mapper("kyttar_0")
        self.fm = kyttar.frequency_modulator(device_id="kyttar_0", sensitivity=1.5707963267948966, pipeline_lock=True)


        ##################################################
        # Connections
        ##################################################
        self.connect((self.fm, 0), (self.tx_sink, 0))
        self.connect((self.mapper, 0), (self.up, 0))
        self.connect((self.mf, 0), (self.timing, 0))
        self.connect((self.qd, 0), (self.mf, 0))
        self.connect((self.rrc, 0), (self.fm, 0))
        self.connect((self.rx_iq, 0), (self.rx_src, 0))
        self.connect((self.rx_sink, 0), (self.rx_syms, 0))
        self.connect((self.rx_src, 0), (self.qd, 0))
        self.connect((self.slicer, 0), (self.rx_sink, 0))
        self.connect((self.timing, 0), (self.slicer, 0))
        self.connect((self.tx_bits, 0), (self.tx_src, 0))
        self.connect((self.tx_iq_split, 0), (self.tx_passband, 0))
        self.connect((self.tx_iq_split, 1), (self.tx_passband, 1))
        self.connect((self.tx_sink, 0), (self.tx_iq_split, 0))
        self.connect((self.tx_src, 0), (self.mapper, 0))
        self.connect((self.tx_syms_src, 0), (self.tx_syms, 0))
        self.connect((self.up, 0), (self.rrc, 0))


    def closeEvent(self, event):
        self.settings = Qt.QSettings("gnuradio/flowgraphs", "fsk4_modem")
        self.settings.setValue("geometry", self.saveGeometry())
        self.stop()
        self.wait()

        event.accept()

    def get_sps(self):
        return self.sps

    def set_sps(self, sps):
        self.sps = sps

    def get_sensitivity(self):
        return self.sensitivity

    def set_sensitivity(self, sensitivity):
        self.sensitivity = sensitivity

    def get_samp_rate(self):
        return self.samp_rate

    def set_samp_rate(self, samp_rate):
        self.samp_rate = samp_rate
        self.tx_passband.set_samp_rate(self.samp_rate)
        self.rx_syms.set_samp_rate(self.samp_rate)
        self.tx_syms.set_samp_rate(self.samp_rate)

    def get_n_syms(self):
        return self.n_syms

    def set_n_syms(self, n_syms):
        self.n_syms = n_syms
        self.rx_iq.set_data(stim.burst(self.n_syms), [])

    def get_n_bits(self):
        return self.n_bits

    def set_n_bits(self, n_bits):
        self.n_bits = n_bits
        self.tx_bits.set_data(stim.tx_bits(self.n_bits), [])
        self.tx_syms_src.set_data(stim.tx_syms(self.n_bits), [])




def main(top_block_cls=fsk4_modem, options=None):

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
