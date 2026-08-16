#!/usr/bin/env python3
# -*- coding: utf-8 -*-

#
# SPDX-License-Identifier: GPL-3.0
#
# GNU Radio Python Flow Graph
# Title: Receiver audio tail + S-meter (duplex on one array)
# Author: Lattrex
# Description: A receiver AUDIO TAIL + S-METER, two streams full-duplex on one placeKYT chip (shared x16 ports, demuxed by stream tags — the same duplex machinery as the BPSK modem). Stream "audio": DC blocker -> AGC (ref 0.5) -> band-pass 300..2700 Hz -> band-reject 3300..3700 Hz -> power squelch (-25 dB) — the classic voice-channel cleanup. Stream "meter": |x| -> moving average (8) -> 10*log10 dB level (Q15 wire value scaled /64 per the Nlog10 block's documented HW representation). Golden: the IDENTICAL stock-GNU-Radio chains (dc_blocker_ff, agc_ff, firdes band filters, pwr_squelch_ff, abs_ff, moving_average_ff, nlog10_ff) within a tolerance DERIVED from the per-block verified Q15 error bounds.
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



class audio_meter(gr.top_block, Qt.QWidget):

    def __init__(self):
        gr.top_block.__init__(self, "Receiver audio tail + S-meter (duplex on one array)", catch_exceptions=True)
        Qt.QWidget.__init__(self)
        self.setWindowTitle("Receiver audio tail + S-meter (duplex on one array)")
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

        self.settings = Qt.QSettings("gnuradio/flowgraphs", "audio_meter")

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
        self.sig = sig = [0.05 + 0.85*math.sin(2*math.pi*1000*t/8000) for t in range(160)] + [0.0]*520
        self.samp_rate = samp_rate = 8000
        self.burst_len = burst_len = len(sig)

        ##################################################
        # Blocks
        ##################################################

        self.db = kyttar.nlog10(device_id="kyttar_0", n=10.0, k=0.0)
        self.sq = kyttar.squelch(device_id="kyttar_0", db=(-25), alpha=0.01, ramp=0, gate=False)
        self.meter_vec = blocks.vector_source_f(sig, False, 1, [])
        self.meter_scope = qtgui.time_sink_f(
            256, #size
            samp_rate, #samp_rate
            "S-meter (10*log10, scaled /64 on the wire)", #name
            1, #number of inputs
            None # parent
        )
        self.meter_scope.set_update_time(0.10)
        self.meter_scope.set_y_axis(-1, 0.2)

        self.meter_scope.set_y_label('level', "")

        self.meter_scope.enable_tags(True)
        self.meter_scope.set_trigger_mode(qtgui.TRIG_MODE_FREE, qtgui.TRIG_SLOPE_POS, 0.0, 0, 0, "")
        self.meter_scope.enable_autoscale(False)
        self.meter_scope.enable_grid(False)
        self.meter_scope.enable_axis_labels(True)
        self.meter_scope.enable_control_panel(False)
        self.meter_scope.enable_stem_plot(False)


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
                self.meter_scope.set_line_label(i, "Data {0}".format(i))
            else:
                self.meter_scope.set_line_label(i, labels[i])
            self.meter_scope.set_line_width(i, widths[i])
            self.meter_scope.set_line_color(i, colors[i])
            self.meter_scope.set_line_style(i, styles[i])
            self.meter_scope.set_line_marker(i, markers[i])
            self.meter_scope.set_line_alpha(i, alphas[i])

        self._meter_scope_win = sip.wrapinstance(self.meter_scope.qwidget(), Qt.QWidget)
        self.top_layout.addWidget(self._meter_scope_win)
        self.meter_out = kyttar.sink(device_id="kyttar_0", port_name="x16_out", num_channels=1, server_port=58950, server_repeat=False, hold_secs=8.0, stream_id="meter", in_type=False)
        self.meter_in = kyttar.source(device_id="kyttar_0", port_name="x16_in", num_channels=1, server_host="127.0.0.1", server_port=58950, complex_in=False, burst_len=burst_len, stream_id="meter", pipelined=True, schedule="interleaved", repeat=False, output_words="auto")
        self.env = kyttar.abs_bb(device_id="kyttar_0")
        self.dcb = kyttar.dc_blocker(device_id="kyttar_0", length=32, long_form=False)
        self.brf = kyttar.band_reject_filter(device_id="kyttar_0", gain=0.999, samp_rate=samp_rate, low_cutoff_freq=3300, high_cutoff_freq=3700, transition_width=400, window="hamming", beta=6.76, decimation=1, interpolation=1)
        self.avg = kyttar.moving_average(device_id="kyttar_0", length=8, scale=0.125)
        self.audio_vec = blocks.vector_source_f(sig, False, 1, [])
        self.audio_scope = qtgui.time_sink_f(
            256, #size
            samp_rate, #samp_rate
            "audio out (DC-blocked, AGC, filtered, squelched)", #name
            1, #number of inputs
            None # parent
        )
        self.audio_scope.set_update_time(0.10)
        self.audio_scope.set_y_axis(-1, 1)

        self.audio_scope.set_y_label('level', "")

        self.audio_scope.enable_tags(True)
        self.audio_scope.set_trigger_mode(qtgui.TRIG_MODE_FREE, qtgui.TRIG_SLOPE_POS, 0.0, 0, 0, "")
        self.audio_scope.enable_autoscale(False)
        self.audio_scope.enable_grid(False)
        self.audio_scope.enable_axis_labels(True)
        self.audio_scope.enable_control_panel(False)
        self.audio_scope.enable_stem_plot(False)


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
                self.audio_scope.set_line_label(i, "Data {0}".format(i))
            else:
                self.audio_scope.set_line_label(i, labels[i])
            self.audio_scope.set_line_width(i, widths[i])
            self.audio_scope.set_line_color(i, colors[i])
            self.audio_scope.set_line_style(i, styles[i])
            self.audio_scope.set_line_marker(i, markers[i])
            self.audio_scope.set_line_alpha(i, alphas[i])

        self._audio_scope_win = sip.wrapinstance(self.audio_scope.qwidget(), Qt.QWidget)
        self.top_layout.addWidget(self._audio_scope_win)
        self.audio_out = kyttar.sink(device_id="kyttar_0", port_name="x16_out", num_channels=1, server_port=58950, server_repeat=False, hold_secs=8.0, stream_id="audio", in_type=False)
        self.audio_in = kyttar.source(device_id="kyttar_0", port_name="x16_in", num_channels=1, server_host="127.0.0.1", server_port=58950, complex_in=False, burst_len=burst_len, stream_id="audio", pipelined=True, schedule="interleaved", repeat=False, output_words="auto")
        self.agc = kyttar.agc(device_id="kyttar_0", rate=0.02, reference=0.3, gain=0.999, max_gain=0.999)


        ##################################################
        # Connections
        ##################################################
        self.connect((self.agc, 0), (self.brf, 0))
        self.connect((self.audio_in, 0), (self.dcb, 0))
        self.connect((self.audio_out, 0), (self.audio_scope, 0))
        self.connect((self.audio_vec, 0), (self.audio_in, 0))
        self.connect((self.avg, 0), (self.db, 0))
        self.connect((self.brf, 0), (self.sq, 0))
        self.connect((self.db, 0), (self.meter_out, 0))
        self.connect((self.dcb, 0), (self.agc, 0))
        self.connect((self.env, 0), (self.avg, 0))
        self.connect((self.meter_in, 0), (self.env, 0))
        self.connect((self.meter_out, 0), (self.meter_scope, 0))
        self.connect((self.meter_vec, 0), (self.meter_in, 0))
        self.connect((self.sq, 0), (self.audio_out, 0))


    def closeEvent(self, event):
        self.settings = Qt.QSettings("gnuradio/flowgraphs", "audio_meter")
        self.settings.setValue("geometry", self.saveGeometry())
        self.stop()
        self.wait()

        event.accept()

    def get_sig(self):
        return self.sig

    def set_sig(self, sig):
        self.sig = sig
        self.set_burst_len(len(self.sig))
        self.audio_vec.set_data(self.sig, [])
        self.meter_vec.set_data(self.sig, [])

    def get_samp_rate(self):
        return self.samp_rate

    def set_samp_rate(self, samp_rate):
        self.samp_rate = samp_rate
        self.audio_scope.set_samp_rate(self.samp_rate)
        self.meter_scope.set_samp_rate(self.samp_rate)

    def get_burst_len(self):
        return self.burst_len

    def set_burst_len(self, burst_len):
        self.burst_len = burst_len




def main(top_block_cls=audio_meter, options=None):

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
