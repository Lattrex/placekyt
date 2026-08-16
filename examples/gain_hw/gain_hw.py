#!/usr/bin/env python3
# -*- coding: utf-8 -*-

#
# SPDX-License-Identifier: GPL-3.0
#
# GNU Radio Python Flow Graph
# Title: Kyttar TWO-GAIN multiplex — HARDWARE streaming (live sliders)
# Author: Lattrex
# Description: HARDWARE Kyttar TWO-GAIN MULTIPLEX demo. Two independent gain blocks share ONE input port (x16_in) and ONE output port (x16_out) on the same chip, distinguished by stream tags (stream_id "a"/"b") — the multiplexed shared-port model, like the AM/SSB transceiver examples. Each chain has its own sig_source, kyttar_source, kyttar_gain, kyttar_sink, and its own LIVE gain slider. Toggle Hardware Mode in placeKYT, Run as GNURadio Server, then Execute here. The plot shows both gained outputs; move each slider to retune that stream's gain live.
# GNU Radio version: 3.10.12.0

from PyQt5 import Qt
from gnuradio import qtgui
from PyQt5 import QtCore
from gnuradio import analog
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
import sip
import threading



class gain_hw(gr.top_block, Qt.QWidget):

    def __init__(self):
        gr.top_block.__init__(self, "Kyttar TWO-GAIN multiplex — HARDWARE streaming (live sliders)", catch_exceptions=True)
        Qt.QWidget.__init__(self)
        self.setWindowTitle("Kyttar TWO-GAIN multiplex — HARDWARE streaming (live sliders)")
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

        self.settings = Qt.QSettings("gnuradio/flowgraphs", "gain_hw")

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
        self.samp_rate = samp_rate = 4000
        self.gain_b = gain_b = 0.5
        self.gain_a = gain_a = 0.5

        ##################################################
        # Blocks
        ##################################################

        self._gain_b_range = qtgui.Range(-1.0, 1.0, 0.01, 0.5, 200)
        self._gain_b_win = qtgui.RangeWidget(self._gain_b_range, self.set_gain_b, "gain B", "counter_slider", float, QtCore.Qt.Horizontal)
        self.top_layout.addWidget(self._gain_b_win)
        self._gain_a_range = qtgui.Range(-1.0, 1.0, 0.01, 0.5, 200)
        self._gain_a_win = qtgui.RangeWidget(self._gain_a_range, self.set_gain_a, "gain A", "counter_slider", float, QtCore.Qt.Horizontal)
        self.top_layout.addWidget(self._gain_a_win)
        self.time_sink = qtgui.time_sink_f(
            1024, #size
            samp_rate, #samp_rate
            "Two multiplexed gain streams (hardware): A & B in/out", #name
            4, #number of inputs
            None # parent
        )
        self.time_sink.set_update_time(0.10)
        self.time_sink.set_y_axis(-1.2, 1.2)

        self.time_sink.set_y_label('amplitude', "")

        self.time_sink.enable_tags(True)
        self.time_sink.set_trigger_mode(qtgui.TRIG_MODE_FREE, qtgui.TRIG_SLOPE_POS, 0.0, 0, 0, "")
        self.time_sink.enable_autoscale(False)
        self.time_sink.enable_grid(True)
        self.time_sink.enable_axis_labels(True)
        self.time_sink.enable_control_panel(False)
        self.time_sink.enable_stem_plot(False)


        labels = ['A input', 'A gained', 'B input', 'B gained', 'Signal 5',
            'Signal 6', 'Signal 7', 'Signal 8', 'Signal 9', 'Signal 10']
        widths = [1, 1, 1, 1, 1,
            1, 1, 1, 1, 1]
        colors = ['blue', 'cyan', 'red', 'magenta', 'green',
            'yellow', 'black', 'dark red', 'dark green', 'dark blue']
        alphas = [1.0, 1.0, 1.0, 1.0, 1.0,
            1.0, 1.0, 1.0, 1.0, 1.0]
        styles = [1, 1, 1, 1, 1,
            1, 1, 1, 1, 1]
        markers = [-1, -1, -1, -1, -1,
            -1, -1, -1, -1, -1]


        for i in range(4):
            if len(labels[i]) == 0:
                self.time_sink.set_line_label(i, "Data {0}".format(i))
            else:
                self.time_sink.set_line_label(i, labels[i])
            self.time_sink.set_line_width(i, widths[i])
            self.time_sink.set_line_color(i, colors[i])
            self.time_sink.set_line_style(i, styles[i])
            self.time_sink.set_line_marker(i, markers[i])
            self.time_sink.set_line_alpha(i, alphas[i])

        self._time_sink_win = sip.wrapinstance(self.time_sink.qwidget(), Qt.QWidget)
        self.top_layout.addWidget(self._time_sink_win)
        self.src_b = analog.sig_source_f(samp_rate, analog.GR_SIN_WAVE, 7, 0.8, 0, 0)
        self.src_a = analog.sig_source_f(samp_rate, analog.GR_SIN_WAVE, 3, 0.8, 0, 0)
        self.msrc_b = kyttar.source(device_id="kyttar_0", port_name="x16_in", num_channels=1, server_host="127.0.0.1", server_port=server_port, complex_in=False, burst_len=0, stream_id="b", pipelined=False, schedule="interleaved", repeat=True)
        self.msrc_a = kyttar.source(device_id="kyttar_0", port_name="x16_in", num_channels=1, server_host="127.0.0.1", server_port=server_port, complex_in=False, burst_len=0, stream_id="a", pipelined=False, schedule="interleaved", repeat=True)
        self.msink_b = kyttar.sink(device_id="kyttar_0", port_name="x16_out", num_channels=1, server_port=server_port, server_repeat=True, hold_secs=8.0, stream_id="b", in_type=False)
        self.msink_a = kyttar.sink(device_id="kyttar_0", port_name="x16_out", num_channels=1, server_port=server_port, server_repeat=True, hold_secs=8.0, stream_id="a", in_type=False)
        self.gain_blk_b = kyttar.gain(device_id="kyttar_0", gain=gain_b, block_name="gain_2")
        self.gain_blk_a = kyttar.gain(device_id="kyttar_0", gain=gain_a, block_name="gain")


        ##################################################
        # Connections
        ##################################################
        self.connect((self.gain_blk_a, 0), (self.msink_a, 0))
        self.connect((self.gain_blk_b, 0), (self.msink_b, 0))
        self.connect((self.msink_a, 0), (self.time_sink, 1))
        self.connect((self.msink_b, 0), (self.time_sink, 3))
        self.connect((self.msrc_a, 0), (self.gain_blk_a, 0))
        self.connect((self.msrc_b, 0), (self.gain_blk_b, 0))
        self.connect((self.src_a, 0), (self.msrc_a, 0))
        self.connect((self.src_a, 0), (self.time_sink, 0))
        self.connect((self.src_b, 0), (self.msrc_b, 0))
        self.connect((self.src_b, 0), (self.time_sink, 2))


    def closeEvent(self, event):
        self.settings = Qt.QSettings("gnuradio/flowgraphs", "gain_hw")
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
        self.src_a.set_sampling_freq(self.samp_rate)
        self.src_b.set_sampling_freq(self.samp_rate)
        self.time_sink.set_samp_rate(self.samp_rate)

    def get_gain_b(self):
        return self.gain_b

    def set_gain_b(self, gain_b):
        self.gain_b = gain_b
        self.gain_blk_b.set_gain(self.gain_b)

    def get_gain_a(self):
        return self.gain_a

    def set_gain_a(self, gain_a):
        self.gain_a = gain_a
        self.gain_blk_a.set_gain(self.gain_a)




def main(top_block_cls=gain_hw, options=None):

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
