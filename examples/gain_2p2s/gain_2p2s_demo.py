#!/usr/bin/env python3
# -*- coding: utf-8 -*-

#
# SPDX-License-Identifier: GPL-3.0
#
# GNU Radio Python Flow Graph
# Title: Kyttar 2P2S — four multiplexed gain streams (0.5x each)
# Author: Lattrex
# Description: 2P2S multi-chip demo: FOUR gain streams (one per chip) across two parallel daisy-chains, multiplexed over the placeKYT multi-chip GNURadio server. Each stream -> 0.5x.
# GNU Radio version: 3.10.12.0

from PyQt5 import Qt
from gnuradio import qtgui
from PyQt5 import QtCore
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
import sip
import threading



class gain_2p2s_demo(gr.top_block, Qt.QWidget):

    def __init__(self):
        gr.top_block.__init__(self, "Kyttar 2P2S — four multiplexed gain streams (0.5x each)", catch_exceptions=True)
        Qt.QWidget.__init__(self)
        self.setWindowTitle("Kyttar 2P2S — four multiplexed gain streams (0.5x each)")
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

        self.settings = Qt.QSettings("gnuradio/flowgraphs", "gain_2p2s_demo")

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
        self.gain_d = gain_d = 0.5
        self.gain_c = gain_c = 0.5
        self.gain_b = gain_b = 0.5
        self.gain_a = gain_a = 0.5
        self.burst_len = burst_len = 256

        ##################################################
        # Blocks
        ##################################################

        self._gain_d_range = qtgui.Range(-1.0, 1.0, 0.01, 0.5, 200)
        self._gain_d_win = qtgui.RangeWidget(self._gain_d_range, self.set_gain_d, "gain D (chip3, live)", "counter_slider", float, QtCore.Qt.Horizontal)
        self.top_layout.addWidget(self._gain_d_win)
        self._gain_c_range = qtgui.Range(-1.0, 1.0, 0.01, 0.5, 200)
        self._gain_c_win = qtgui.RangeWidget(self._gain_c_range, self.set_gain_c, "gain C (chip2, live)", "counter_slider", float, QtCore.Qt.Horizontal)
        self.top_layout.addWidget(self._gain_c_win)
        self._gain_b_range = qtgui.Range(-1.0, 1.0, 0.01, 0.5, 200)
        self._gain_b_win = qtgui.RangeWidget(self._gain_b_range, self.set_gain_b, "gain B (chip1, live)", "counter_slider", float, QtCore.Qt.Horizontal)
        self.top_layout.addWidget(self._gain_b_win)
        self._gain_a_range = qtgui.Range(-1.0, 1.0, 0.01, 0.5, 200)
        self._gain_a_win = qtgui.RangeWidget(self._gain_a_range, self.set_gain_a, "gain A (chip0, live)", "counter_slider", float, QtCore.Qt.Horizontal)
        self.top_layout.addWidget(self._gain_a_win)
        self.time_sink_D = qtgui.time_sink_f(
            burst_len, #size
            burst_len, #samp_rate
            "Stream D", #name
            2, #number of inputs
            None # parent
        )
        self.time_sink_D.set_update_time(0.10)
        self.time_sink_D.set_y_axis(-1, 1)

        self.time_sink_D.set_y_label('Amplitude', "")

        self.time_sink_D.enable_tags(True)
        self.time_sink_D.set_trigger_mode(qtgui.TRIG_MODE_FREE, qtgui.TRIG_SLOPE_POS, 0.0, 0, 0, "")
        self.time_sink_D.enable_autoscale(False)
        self.time_sink_D.enable_grid(True)
        self.time_sink_D.enable_axis_labels(True)
        self.time_sink_D.enable_control_panel(False)
        self.time_sink_D.enable_stem_plot(False)


        labels = ['input', 'Stream D out (0.5x)', 'Signal 3', 'Signal 4', 'Signal 5',
            'Signal 6', 'Signal 7', 'Signal 8', 'Signal 9', 'Signal 10']
        widths = [1, 1, 1, 1, 1,
            1, 1, 1, 1, 1]
        colors = ['blue', 'magenta', 'blue', 'blue', 'blue',
            'blue', 'blue', 'blue', 'blue', 'blue']
        alphas = [1.0, 1.0, 1.0, 1.0, 1.0,
            1.0, 1.0, 1.0, 1.0, 1.0]
        styles = [1, 1, 1, 1, 1,
            1, 1, 1, 1, 1]
        markers = [-1, -1, -1, -1, -1,
            -1, -1, -1, -1, -1]


        for i in range(2):
            if len(labels[i]) == 0:
                self.time_sink_D.set_line_label(i, "Data {0}".format(i))
            else:
                self.time_sink_D.set_line_label(i, labels[i])
            self.time_sink_D.set_line_width(i, widths[i])
            self.time_sink_D.set_line_color(i, colors[i])
            self.time_sink_D.set_line_style(i, styles[i])
            self.time_sink_D.set_line_marker(i, markers[i])
            self.time_sink_D.set_line_alpha(i, alphas[i])

        self._time_sink_D_win = sip.wrapinstance(self.time_sink_D.qwidget(), Qt.QWidget)
        self.top_layout.addWidget(self._time_sink_D_win)
        self.time_sink_C = qtgui.time_sink_f(
            burst_len, #size
            burst_len, #samp_rate
            "Stream C", #name
            2, #number of inputs
            None # parent
        )
        self.time_sink_C.set_update_time(0.10)
        self.time_sink_C.set_y_axis(-1, 1)

        self.time_sink_C.set_y_label('Amplitude', "")

        self.time_sink_C.enable_tags(True)
        self.time_sink_C.set_trigger_mode(qtgui.TRIG_MODE_FREE, qtgui.TRIG_SLOPE_POS, 0.0, 0, 0, "")
        self.time_sink_C.enable_autoscale(False)
        self.time_sink_C.enable_grid(True)
        self.time_sink_C.enable_axis_labels(True)
        self.time_sink_C.enable_control_panel(False)
        self.time_sink_C.enable_stem_plot(False)


        labels = ['input', 'Stream C out (0.5x)', 'Signal 3', 'Signal 4', 'Signal 5',
            'Signal 6', 'Signal 7', 'Signal 8', 'Signal 9', 'Signal 10']
        widths = [1, 1, 1, 1, 1,
            1, 1, 1, 1, 1]
        colors = ['blue', 'green', 'blue', 'blue', 'blue',
            'blue', 'blue', 'blue', 'blue', 'blue']
        alphas = [1.0, 1.0, 1.0, 1.0, 1.0,
            1.0, 1.0, 1.0, 1.0, 1.0]
        styles = [1, 1, 1, 1, 1,
            1, 1, 1, 1, 1]
        markers = [-1, -1, -1, -1, -1,
            -1, -1, -1, -1, -1]


        for i in range(2):
            if len(labels[i]) == 0:
                self.time_sink_C.set_line_label(i, "Data {0}".format(i))
            else:
                self.time_sink_C.set_line_label(i, labels[i])
            self.time_sink_C.set_line_width(i, widths[i])
            self.time_sink_C.set_line_color(i, colors[i])
            self.time_sink_C.set_line_style(i, styles[i])
            self.time_sink_C.set_line_marker(i, markers[i])
            self.time_sink_C.set_line_alpha(i, alphas[i])

        self._time_sink_C_win = sip.wrapinstance(self.time_sink_C.qwidget(), Qt.QWidget)
        self.top_layout.addWidget(self._time_sink_C_win)
        self.time_sink_B = qtgui.time_sink_f(
            burst_len, #size
            burst_len, #samp_rate
            "Stream B", #name
            2, #number of inputs
            None # parent
        )
        self.time_sink_B.set_update_time(0.10)
        self.time_sink_B.set_y_axis(-1, 1)

        self.time_sink_B.set_y_label('Amplitude', "")

        self.time_sink_B.enable_tags(True)
        self.time_sink_B.set_trigger_mode(qtgui.TRIG_MODE_FREE, qtgui.TRIG_SLOPE_POS, 0.0, 0, 0, "")
        self.time_sink_B.enable_autoscale(False)
        self.time_sink_B.enable_grid(True)
        self.time_sink_B.enable_axis_labels(True)
        self.time_sink_B.enable_control_panel(False)
        self.time_sink_B.enable_stem_plot(False)


        labels = ['input', 'Stream B out (0.5x)', 'Signal 3', 'Signal 4', 'Signal 5',
            'Signal 6', 'Signal 7', 'Signal 8', 'Signal 9', 'Signal 10']
        widths = [1, 1, 1, 1, 1,
            1, 1, 1, 1, 1]
        colors = ['blue', 'red', 'blue', 'blue', 'blue',
            'blue', 'blue', 'blue', 'blue', 'blue']
        alphas = [1.0, 1.0, 1.0, 1.0, 1.0,
            1.0, 1.0, 1.0, 1.0, 1.0]
        styles = [1, 1, 1, 1, 1,
            1, 1, 1, 1, 1]
        markers = [-1, -1, -1, -1, -1,
            -1, -1, -1, -1, -1]


        for i in range(2):
            if len(labels[i]) == 0:
                self.time_sink_B.set_line_label(i, "Data {0}".format(i))
            else:
                self.time_sink_B.set_line_label(i, labels[i])
            self.time_sink_B.set_line_width(i, widths[i])
            self.time_sink_B.set_line_color(i, colors[i])
            self.time_sink_B.set_line_style(i, styles[i])
            self.time_sink_B.set_line_marker(i, markers[i])
            self.time_sink_B.set_line_alpha(i, alphas[i])

        self._time_sink_B_win = sip.wrapinstance(self.time_sink_B.qwidget(), Qt.QWidget)
        self.top_layout.addWidget(self._time_sink_B_win)
        self.time_sink_A = qtgui.time_sink_f(
            burst_len, #size
            burst_len, #samp_rate
            "Stream A", #name
            2, #number of inputs
            None # parent
        )
        self.time_sink_A.set_update_time(0.10)
        self.time_sink_A.set_y_axis(-1, 1)

        self.time_sink_A.set_y_label('Amplitude', "")

        self.time_sink_A.enable_tags(True)
        self.time_sink_A.set_trigger_mode(qtgui.TRIG_MODE_FREE, qtgui.TRIG_SLOPE_POS, 0.0, 0, 0, "")
        self.time_sink_A.enable_autoscale(False)
        self.time_sink_A.enable_grid(True)
        self.time_sink_A.enable_axis_labels(True)
        self.time_sink_A.enable_control_panel(False)
        self.time_sink_A.enable_stem_plot(False)


        labels = ['input', 'Stream A out (0.5x)', 'Signal 3', 'Signal 4', 'Signal 5',
            'Signal 6', 'Signal 7', 'Signal 8', 'Signal 9', 'Signal 10']
        widths = [1, 1, 1, 1, 1,
            1, 1, 1, 1, 1]
        colors = ['blue', 'blue', 'blue', 'blue', 'blue',
            'blue', 'blue', 'blue', 'blue', 'blue']
        alphas = [1.0, 1.0, 1.0, 1.0, 1.0,
            1.0, 1.0, 1.0, 1.0, 1.0]
        styles = [1, 1, 1, 1, 1,
            1, 1, 1, 1, 1]
        markers = [-1, -1, -1, -1, -1,
            -1, -1, -1, -1, -1]


        for i in range(2):
            if len(labels[i]) == 0:
                self.time_sink_A.set_line_label(i, "Data {0}".format(i))
            else:
                self.time_sink_A.set_line_label(i, labels[i])
            self.time_sink_A.set_line_width(i, widths[i])
            self.time_sink_A.set_line_color(i, colors[i])
            self.time_sink_A.set_line_style(i, styles[i])
            self.time_sink_A.set_line_marker(i, markers[i])
            self.time_sink_A.set_line_alpha(i, alphas[i])

        self._time_sink_A_win = sip.wrapinstance(self.time_sink_A.qwidget(), Qt.QWidget)
        self.top_layout.addWidget(self._time_sink_A_win)
        self.srcD = blocks.vector_source_f([0.8*float(__import__('math').sin(2*3.141592653589793*5*n/burst_len)) for n in range(burst_len)], True, 1, [])
        self.srcC = blocks.vector_source_f([0.8*float(__import__('math').sin(2*3.141592653589793*4*n/burst_len)) for n in range(burst_len)], True, 1, [])
        self.srcB = blocks.vector_source_f([0.8*float(__import__('math').sin(2*3.141592653589793*3*n/burst_len)) for n in range(burst_len)], True, 1, [])
        self.srcA = blocks.vector_source_f([0.8*float(__import__('math').sin(2*3.141592653589793*2*n/burst_len)) for n in range(burst_len)], True, 1, [])
        self.msrcD = kyttar.source(device_id="kyttar_0", port_name="x16_in", num_channels=1, server_host="127.0.0.1", server_port=server_port, complex_in=False, burst_len=burst_len, stream_id="D", pipelined=False, schedule="interleaved", repeat=True, output_words="auto")
        self.msrcC = kyttar.source(device_id="kyttar_0", port_name="x16_in", num_channels=1, server_host="127.0.0.1", server_port=server_port, complex_in=False, burst_len=burst_len, stream_id="C", pipelined=False, schedule="interleaved", repeat=True, output_words="auto")
        self.msrcB = kyttar.source(device_id="kyttar_0", port_name="x16_in", num_channels=1, server_host="127.0.0.1", server_port=server_port, complex_in=False, burst_len=burst_len, stream_id="B", pipelined=False, schedule="interleaved", repeat=True, output_words="auto")
        self.msrcA = kyttar.source(device_id="kyttar_0", port_name="x16_in", num_channels=1, server_host="127.0.0.1", server_port=server_port, complex_in=False, burst_len=burst_len, stream_id="A", pipelined=False, schedule="interleaved", repeat=True, output_words="auto")
        self.msinkD = kyttar.sink(device_id="kyttar_0", port_name="x16_out", num_channels=1, server_port=server_port, server_repeat=True, hold_secs=8.0, stream_id="D", in_type=False)
        self.msinkC = kyttar.sink(device_id="kyttar_0", port_name="x16_out", num_channels=1, server_port=server_port, server_repeat=True, hold_secs=8.0, stream_id="C", in_type=False)
        self.msinkB = kyttar.sink(device_id="kyttar_0", port_name="x16_out", num_channels=1, server_port=server_port, server_repeat=True, hold_secs=8.0, stream_id="B", in_type=False)
        self.msinkA = kyttar.sink(device_id="kyttar_0", port_name="x16_out", num_channels=1, server_port=server_port, server_repeat=True, hold_secs=8.0, stream_id="A", in_type=False)
        self.gainD = kyttar.gain(device_id="kyttar_0", gain=gain_d, block_name="gain_3")
        self.gainC = kyttar.gain(device_id="kyttar_0", gain=gain_c, block_name="gain_2")
        self.gainB = kyttar.gain(device_id="kyttar_0", gain=gain_b, block_name="gain_1")
        self.gainA = kyttar.gain(device_id="kyttar_0", gain=gain_a, block_name="gain")


        ##################################################
        # Connections
        ##################################################
        self.connect((self.gainA, 0), (self.msinkA, 0))
        self.connect((self.gainB, 0), (self.msinkB, 0))
        self.connect((self.gainC, 0), (self.msinkC, 0))
        self.connect((self.gainD, 0), (self.msinkD, 0))
        self.connect((self.msinkA, 0), (self.time_sink_A, 1))
        self.connect((self.msinkB, 0), (self.time_sink_B, 1))
        self.connect((self.msinkC, 0), (self.time_sink_C, 1))
        self.connect((self.msinkD, 0), (self.time_sink_D, 1))
        self.connect((self.msrcA, 0), (self.gainA, 0))
        self.connect((self.msrcB, 0), (self.gainB, 0))
        self.connect((self.msrcC, 0), (self.gainC, 0))
        self.connect((self.msrcD, 0), (self.gainD, 0))
        self.connect((self.srcA, 0), (self.msrcA, 0))
        self.connect((self.srcA, 0), (self.time_sink_A, 0))
        self.connect((self.srcB, 0), (self.msrcB, 0))
        self.connect((self.srcB, 0), (self.time_sink_B, 0))
        self.connect((self.srcC, 0), (self.msrcC, 0))
        self.connect((self.srcC, 0), (self.time_sink_C, 0))
        self.connect((self.srcD, 0), (self.msrcD, 0))
        self.connect((self.srcD, 0), (self.time_sink_D, 0))


    def closeEvent(self, event):
        self.settings = Qt.QSettings("gnuradio/flowgraphs", "gain_2p2s_demo")
        self.settings.setValue("geometry", self.saveGeometry())
        self.stop()
        self.wait()

        event.accept()

    def get_server_port(self):
        return self.server_port

    def set_server_port(self, server_port):
        self.server_port = server_port

    def get_gain_d(self):
        return self.gain_d

    def set_gain_d(self, gain_d):
        self.gain_d = gain_d
        self.gainD.set_gain(self.gain_d)

    def get_gain_c(self):
        return self.gain_c

    def set_gain_c(self, gain_c):
        self.gain_c = gain_c
        self.gainC.set_gain(self.gain_c)

    def get_gain_b(self):
        return self.gain_b

    def set_gain_b(self, gain_b):
        self.gain_b = gain_b
        self.gainB.set_gain(self.gain_b)

    def get_gain_a(self):
        return self.gain_a

    def set_gain_a(self, gain_a):
        self.gain_a = gain_a
        self.gainA.set_gain(self.gain_a)

    def get_burst_len(self):
        return self.burst_len

    def set_burst_len(self, burst_len):
        self.burst_len = burst_len
        self.srcA.set_data([0.8*float(__import__('math').sin(2*3.141592653589793*2*n/self.burst_len)) for n in range(self.burst_len)], [])
        self.srcB.set_data([0.8*float(__import__('math').sin(2*3.141592653589793*3*n/self.burst_len)) for n in range(self.burst_len)], [])
        self.srcC.set_data([0.8*float(__import__('math').sin(2*3.141592653589793*4*n/self.burst_len)) for n in range(self.burst_len)], [])
        self.srcD.set_data([0.8*float(__import__('math').sin(2*3.141592653589793*5*n/self.burst_len)) for n in range(self.burst_len)], [])
        self.time_sink_A.set_samp_rate(self.burst_len)
        self.time_sink_B.set_samp_rate(self.burst_len)
        self.time_sink_C.set_samp_rate(self.burst_len)
        self.time_sink_D.set_samp_rate(self.burst_len)




def main(top_block_cls=gain_2p2s_demo, options=None):

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
