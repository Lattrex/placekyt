#!/usr/bin/env python3
# -*- coding: utf-8 -*-

#
# SPDX-License-Identifier: GPL-3.0
#
# GNU Radio Python Flow Graph
# Title: FM transceiver (on-chip, real blocks) — TX modulate + RX demodulate
# Author: Kyttar
# Description: FM TRANSCEIVER from REAL Kyttar blocks. SEPARATE TX chain (audio -> frequency_modulator -> FM passband, stream 'tx') and SEPARATE RX chain (complex FM I/Q -> quadrature_demod -> recovered audio, stream 'rx') sharing ONE chip by stream_id, like the AM transceiver. Verified |corr| ~ 1.0. Imports + auto-P&R-routes into placeKYT.
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
from gnuradio.kyttar import fm_demo_stim as stim
import sip
import threading



class fm_transceiver(gr.top_block, Qt.QWidget):

    def __init__(self):
        gr.top_block.__init__(self, "FM transceiver (on-chip, real blocks) — TX modulate + RX demodulate", catch_exceptions=True)
        Qt.QWidget.__init__(self)
        self.setWindowTitle("FM transceiver (on-chip, real blocks) — TX modulate + RX demodulate")
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

        self.settings = Qt.QSettings("gnuradio/flowgraphs", "fm_transceiver")

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
        self.samp_rate = samp_rate = 32000.0
        self.n_samp = n_samp = 2048
        self.f_dev = f_dev = 1500.0

        ##################################################
        # Blocks
        ##################################################

        self.tx_vco = kyttar.frequency_modulator(device_id="kyttar_0", sensitivity=0.2945243112740431, pipeline_lock=False)
        self.tx_src = kyttar.source(device_id="kyttar_0", port_name="x16_in", num_channels=1, server_host="127.0.0.1", server_port=server_port, complex_in=False, burst_len=n_samp, stream_id="tx", pipelined=True, schedule="interleaved", repeat=False, output_words="auto")
        self.tx_sink = kyttar.sink(device_id="kyttar_0", port_name="x16_out", num_channels=1, server_port=server_port, server_repeat=False, hold_secs=5.0, stream_id="tx", in_type=True)
        self.tx_passband = qtgui.time_sink_f(
            stim.points(n_samp), #size
            samp_rate, #samp_rate
            'FM passband (TX)', #name
            1, #number of inputs
            None # parent
        )
        self.tx_passband.set_update_time(0.10)
        self.tx_passband.set_y_axis(-1, 1)

        self.tx_passband.set_y_label('Amplitude', '')

        self.tx_passband.enable_tags(True)
        self.tx_passband.set_trigger_mode(qtgui.TRIG_MODE_FREE, qtgui.TRIG_SLOPE_POS, 0, 0, 0, '')
        self.tx_passband.enable_autoscale(True)
        self.tx_passband.enable_grid(True)
        self.tx_passband.enable_axis_labels(True)
        self.tx_passband.enable_control_panel(False)
        self.tx_passband.enable_stem_plot(False)


        labels = ['FM passband (TX)', 'Signal 2', 'Signal 3', 'Signal 4', 'Signal 5',
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
        self.tx_audio = blocks.vector_source_f(stim.tx_audio(n_samp), False, 1, [])
        self.rx_src = kyttar.source(device_id="kyttar_0", port_name="x16_in", num_channels=1, server_host="127.0.0.1", server_port=server_port, complex_in=True, burst_len=n_samp, stream_id="rx", pipelined=True, schedule="interleaved", repeat=False, output_words="q15")
        self.rx_sink = kyttar.sink(device_id="kyttar_0", port_name="x16_out", num_channels=1, server_port=server_port, server_repeat=False, hold_secs=5.0, stream_id="rx", in_type=False)
        self.rx_demod = kyttar.quadrature_demod(device_id="kyttar_0", gain=3.3953054526271007)
        self.rx_audio = qtgui.time_sink_f(
            stim.points(n_samp), #size
            samp_rate, #samp_rate
            'recovered audio (RX)', #name
            1, #number of inputs
            None # parent
        )
        self.rx_audio.set_update_time(0.10)
        self.rx_audio.set_y_axis(-1, 1)

        self.rx_audio.set_y_label('Amplitude', '')

        self.rx_audio.enable_tags(True)
        self.rx_audio.set_trigger_mode(qtgui.TRIG_MODE_FREE, qtgui.TRIG_SLOPE_POS, 0, 0, 0, '')
        self.rx_audio.enable_autoscale(True)
        self.rx_audio.enable_grid(True)
        self.rx_audio.enable_axis_labels(True)
        self.rx_audio.enable_control_panel(False)
        self.rx_audio.enable_stem_plot(False)


        labels = ['recovered audio (RX)', 'Signal 2', 'Signal 3', 'Signal 4', 'Signal 5',
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
                self.rx_audio.set_line_label(i, "Data {0}".format(i))
            else:
                self.rx_audio.set_line_label(i, labels[i])
            self.rx_audio.set_line_width(i, widths[i])
            self.rx_audio.set_line_color(i, colors[i])
            self.rx_audio.set_line_style(i, styles[i])
            self.rx_audio.set_line_marker(i, markers[i])
            self.rx_audio.set_line_alpha(i, alphas[i])

        self._rx_audio_win = sip.wrapinstance(self.rx_audio.qwidget(), Qt.QWidget)
        self.top_layout.addWidget(self._rx_audio_win)
        self.fm_rf = blocks.vector_source_c(stim.fm_iq(n_samp), False, 1, [])


        ##################################################
        # Connections
        ##################################################
        self.connect((self.fm_rf, 0), (self.rx_src, 0))
        self.connect((self.rx_demod, 0), (self.rx_sink, 0))
        self.connect((self.rx_sink, 0), (self.rx_audio, 0))
        self.connect((self.rx_src, 0), (self.rx_demod, 0))
        self.connect((self.tx_audio, 0), (self.tx_src, 0))
        self.connect((self.tx_sink, 0), (self.tx_passband, 0))
        self.connect((self.tx_src, 0), (self.tx_vco, 0))
        self.connect((self.tx_vco, 0), (self.tx_sink, 0))


    def closeEvent(self, event):
        self.settings = Qt.QSettings("gnuradio/flowgraphs", "fm_transceiver")
        self.settings.setValue("geometry", self.saveGeometry())
        self.stop()
        self.wait()

        event.accept()

    def get_server_port(self):
        return self.server_port

    def set_server_port(self, server_port):
        self.server_port = server_port

    def get_samp_rate(self):
        return self.samp_rate

    def set_samp_rate(self, samp_rate):
        self.samp_rate = samp_rate
        self.rx_audio.set_samp_rate(self.samp_rate)
        self.tx_passband.set_samp_rate(self.samp_rate)

    def get_n_samp(self):
        return self.n_samp

    def set_n_samp(self, n_samp):
        self.n_samp = n_samp
        self.fm_rf.set_data(stim.fm_iq(self.n_samp), [])
        self.tx_audio.set_data(stim.tx_audio(self.n_samp), [])

    def get_f_dev(self):
        return self.f_dev

    def set_f_dev(self, f_dev):
        self.f_dev = f_dev




def main(top_block_cls=fm_transceiver, options=None):

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
