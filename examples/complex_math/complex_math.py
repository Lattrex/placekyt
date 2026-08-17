#!/usr/bin/env python3
# -*- coding: utf-8 -*-

#
# SPDX-License-Identifier: GPL-3.0
#
# GNU Radio Python Flow Graph
# Title: Complex math — AddCC / SubCC / MultiplyCC (two-tone mixer)
# Author: Lattrex
# Description: COMPLEX TWO-STREAM ARITHMETIC on one placeKYT array: two analytic complex tones (f_a = 10/256, f_b = 17/256 cyc/sample) drive three placed two-stream blocks — Add CC (a+b: the two tones superpose into a beat envelope), Sub CC (a-b: the same superposition, second tone flipped) and Multiply CC (a*b: THE MIXER — multiplying analytic tones adds their frequencies, one clean tone at f_a+f_b = 27/256). Each block gets its own ingress stream pair (a complex stream cannot fan out on-chip — the fan-out relay is single-rail), and each landing cell pairs the two per-sample packets with its counting join, in any arrival order (the two-external-complex-stream contract). The wiring pattern to copy for any GRC design that combines two complex streams.
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
from gnuradio.kyttar import cmath_demo_stim as stim
import complex_math_diff_iq2c as diff_iq2c  # embedded python block
import complex_math_prod_iq2c as prod_iq2c  # embedded python block
import complex_math_sum_iq2c as sum_iq2c  # embedded python block
import sip
import threading



class complex_math(gr.top_block, Qt.QWidget):

    def __init__(self):
        gr.top_block.__init__(self, "Complex math — AddCC / SubCC / MultiplyCC (two-tone mixer)", catch_exceptions=True)
        Qt.QWidget.__init__(self)
        self.setWindowTitle("Complex math — AddCC / SubCC / MultiplyCC (two-tone mixer)")
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

        self.settings = Qt.QSettings("gnuradio/flowgraphs", "complex_math")

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
        self.samp_rate = samp_rate = 32000
        self.n_pts = n_pts = stim.n_samples()

        ##################################################
        # Blocks
        ##################################################

        self.sum_sink = kyttar.sink(device_id="kyttar_0", port_name="x16_out", num_channels=1, server_port=58950, server_repeat=True, hold_secs=8.0, stream_id="sum", in_type=True)
        self.sum_scope = qtgui.time_sink_c(
            n_pts, #size
            samp_rate, #samp_rate
            "Add CC: a + b (chip) — two-tone beat", #name
            1, #number of inputs
            None # parent
        )
        self.sum_scope.set_update_time(0.10)
        self.sum_scope.set_y_axis(-1, 1)

        self.sum_scope.set_y_label('level', "")

        self.sum_scope.enable_tags(True)
        self.sum_scope.set_trigger_mode(qtgui.TRIG_MODE_FREE, qtgui.TRIG_SLOPE_POS, 0.0, 0, 0, "")
        self.sum_scope.enable_autoscale(False)
        self.sum_scope.enable_grid(False)
        self.sum_scope.enable_axis_labels(True)
        self.sum_scope.enable_control_panel(False)
        self.sum_scope.enable_stem_plot(False)


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
                    self.sum_scope.set_line_label(i, "Re{{Data {0}}}".format(i/2))
                else:
                    self.sum_scope.set_line_label(i, "Im{{Data {0}}}".format(i/2))
            else:
                self.sum_scope.set_line_label(i, labels[i])
            self.sum_scope.set_line_width(i, widths[i])
            self.sum_scope.set_line_color(i, colors[i])
            self.sum_scope.set_line_style(i, styles[i])
            self.sum_scope.set_line_marker(i, markers[i])
            self.sum_scope.set_line_alpha(i, alphas[i])

        self._sum_scope_win = sip.wrapinstance(self.sum_scope.qwidget(), Qt.QWidget)
        self.top_layout.addWidget(self._sum_scope_win)
        self.sum_iq2c = sum_iq2c.blk()
        self.prod_sink = kyttar.sink(device_id="kyttar_0", port_name="x16_out", num_channels=1, server_port=58950, server_repeat=True, hold_secs=8.0, stream_id="prod", in_type=True)
        self.prod_scope = qtgui.time_sink_c(
            n_pts, #size
            samp_rate, #samp_rate
            "Multiply CC: a * b (chip) — the mixer, tone at 27/256", #name
            1, #number of inputs
            None # parent
        )
        self.prod_scope.set_update_time(0.10)
        self.prod_scope.set_y_axis(-0.3, 0.3)

        self.prod_scope.set_y_label('level', "")

        self.prod_scope.enable_tags(True)
        self.prod_scope.set_trigger_mode(qtgui.TRIG_MODE_FREE, qtgui.TRIG_SLOPE_POS, 0.0, 0, 0, "")
        self.prod_scope.enable_autoscale(False)
        self.prod_scope.enable_grid(False)
        self.prod_scope.enable_axis_labels(True)
        self.prod_scope.enable_control_panel(False)
        self.prod_scope.enable_stem_plot(False)


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
                    self.prod_scope.set_line_label(i, "Re{{Data {0}}}".format(i/2))
                else:
                    self.prod_scope.set_line_label(i, "Im{{Data {0}}}".format(i/2))
            else:
                self.prod_scope.set_line_label(i, labels[i])
            self.prod_scope.set_line_width(i, widths[i])
            self.prod_scope.set_line_color(i, colors[i])
            self.prod_scope.set_line_style(i, styles[i])
            self.prod_scope.set_line_marker(i, markers[i])
            self.prod_scope.set_line_alpha(i, alphas[i])

        self._prod_scope_win = sip.wrapinstance(self.prod_scope.qwidget(), Qt.QWidget)
        self.top_layout.addWidget(self._prod_scope_win)
        self.prod_iq2c = prod_iq2c.blk()
        self.ksub = kyttar.sub_cc(device_id="kyttar_0", num_inputs=2)
        self.kmul = kyttar.multiply_cc(device_id="kyttar_0", num_inputs=2)
        self.kadd = kyttar.add_cc(device_id="kyttar_0", num_inputs=2)
        self.in_scope = qtgui.time_sink_c(
            (n_pts - 16), #size
            samp_rate, #samp_rate
            "Input tones A (10/256) and B (17/256)", #name
            2, #number of inputs
            None # parent
        )
        self.in_scope.set_update_time(0.10)
        self.in_scope.set_y_axis(-0.6, 0.6)

        self.in_scope.set_y_label('level', "")

        self.in_scope.enable_tags(True)
        self.in_scope.set_trigger_mode(qtgui.TRIG_MODE_FREE, qtgui.TRIG_SLOPE_POS, 0.0, 0, 0, "")
        self.in_scope.enable_autoscale(False)
        self.in_scope.enable_grid(False)
        self.in_scope.enable_axis_labels(True)
        self.in_scope.enable_control_panel(False)
        self.in_scope.enable_stem_plot(False)


        labels = ['tone A', '', 'tone B', '', 'Signal 5',
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


        for i in range(4):
            if len(labels[i]) == 0:
                if (i % 2 == 0):
                    self.in_scope.set_line_label(i, "Re{{Data {0}}}".format(i/2))
                else:
                    self.in_scope.set_line_label(i, "Im{{Data {0}}}".format(i/2))
            else:
                self.in_scope.set_line_label(i, labels[i])
            self.in_scope.set_line_width(i, widths[i])
            self.in_scope.set_line_color(i, colors[i])
            self.in_scope.set_line_style(i, styles[i])
            self.in_scope.set_line_marker(i, markers[i])
            self.in_scope.set_line_alpha(i, alphas[i])

        self._in_scope_win = sip.wrapinstance(self.in_scope.qwidget(), Qt.QWidget)
        self.top_layout.addWidget(self._in_scope_win)
        self.diff_sink = kyttar.sink(device_id="kyttar_0", port_name="x16_out", num_channels=1, server_port=58950, server_repeat=True, hold_secs=8.0, stream_id="diff", in_type=True)
        self.diff_scope = qtgui.time_sink_c(
            n_pts, #size
            samp_rate, #samp_rate
            "Sub CC: a - b (chip)", #name
            1, #number of inputs
            None # parent
        )
        self.diff_scope.set_update_time(0.10)
        self.diff_scope.set_y_axis(-1, 1)

        self.diff_scope.set_y_label('level', "")

        self.diff_scope.enable_tags(True)
        self.diff_scope.set_trigger_mode(qtgui.TRIG_MODE_FREE, qtgui.TRIG_SLOPE_POS, 0.0, 0, 0, "")
        self.diff_scope.enable_autoscale(False)
        self.diff_scope.enable_grid(False)
        self.diff_scope.enable_axis_labels(True)
        self.diff_scope.enable_control_panel(False)
        self.diff_scope.enable_stem_plot(False)


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
                    self.diff_scope.set_line_label(i, "Re{{Data {0}}}".format(i/2))
                else:
                    self.diff_scope.set_line_label(i, "Im{{Data {0}}}".format(i/2))
            else:
                self.diff_scope.set_line_label(i, labels[i])
            self.diff_scope.set_line_width(i, widths[i])
            self.diff_scope.set_line_color(i, colors[i])
            self.diff_scope.set_line_style(i, styles[i])
            self.diff_scope.set_line_marker(i, markers[i])
            self.diff_scope.set_line_alpha(i, alphas[i])

        self._diff_scope_win = sip.wrapinstance(self.diff_scope.qwidget(), Qt.QWidget)
        self.top_layout.addWidget(self._diff_scope_win)
        self.diff_iq2c = diff_iq2c.blk()
        self.b_vec = blocks.vector_source_c(stim.tone_b(), False, 1, [])
        self.b_sub_src = kyttar.source(device_id="kyttar_0", port_name="x16_in", num_channels=1, server_host="127.0.0.1", server_port=58950, complex_in=True, burst_len=n_pts, stream_id="b_sub", pipelined=False, schedule="interleaved", repeat=False, output_words="q15")
        self.b_mul_src = kyttar.source(device_id="kyttar_0", port_name="x16_in", num_channels=1, server_host="127.0.0.1", server_port=58950, complex_in=True, burst_len=n_pts, stream_id="b_mul", pipelined=False, schedule="interleaved", repeat=False, output_words="q15")
        self.b_add_src = kyttar.source(device_id="kyttar_0", port_name="x16_in", num_channels=1, server_host="127.0.0.1", server_port=58950, complex_in=True, burst_len=n_pts, stream_id="b_add", pipelined=False, schedule="interleaved", repeat=False, output_words="q15")
        self.a_vec = blocks.vector_source_c(stim.tone_a(), False, 1, [])
        self.a_sub_src = kyttar.source(device_id="kyttar_0", port_name="x16_in", num_channels=1, server_host="127.0.0.1", server_port=58950, complex_in=True, burst_len=n_pts, stream_id="diff", pipelined=False, schedule="interleaved", repeat=False, output_words="q15")
        self.a_mul_src = kyttar.source(device_id="kyttar_0", port_name="x16_in", num_channels=1, server_host="127.0.0.1", server_port=58950, complex_in=True, burst_len=n_pts, stream_id="prod", pipelined=False, schedule="interleaved", repeat=False, output_words="q15")
        self.a_add_src = kyttar.source(device_id="kyttar_0", port_name="x16_in", num_channels=1, server_host="127.0.0.1", server_port=58950, complex_in=True, burst_len=n_pts, stream_id="sum", pipelined=False, schedule="interleaved", repeat=False, output_words="q15")


        ##################################################
        # Connections
        ##################################################
        self.connect((self.a_add_src, 0), (self.kadd, 0))
        self.connect((self.a_mul_src, 0), (self.kmul, 0))
        self.connect((self.a_sub_src, 0), (self.ksub, 0))
        self.connect((self.a_vec, 0), (self.a_add_src, 0))
        self.connect((self.a_vec, 0), (self.a_mul_src, 0))
        self.connect((self.a_vec, 0), (self.a_sub_src, 0))
        self.connect((self.a_vec, 0), (self.in_scope, 0))
        self.connect((self.b_add_src, 0), (self.kadd, 1))
        self.connect((self.b_mul_src, 0), (self.kmul, 1))
        self.connect((self.b_sub_src, 0), (self.ksub, 1))
        self.connect((self.b_vec, 0), (self.b_add_src, 0))
        self.connect((self.b_vec, 0), (self.b_mul_src, 0))
        self.connect((self.b_vec, 0), (self.b_sub_src, 0))
        self.connect((self.b_vec, 0), (self.in_scope, 1))
        self.connect((self.diff_iq2c, 0), (self.diff_scope, 0))
        self.connect((self.diff_sink, 0), (self.diff_iq2c, 0))
        self.connect((self.kadd, 0), (self.sum_sink, 0))
        self.connect((self.kmul, 0), (self.prod_sink, 0))
        self.connect((self.ksub, 0), (self.diff_sink, 0))
        self.connect((self.prod_iq2c, 0), (self.prod_scope, 0))
        self.connect((self.prod_sink, 0), (self.prod_iq2c, 0))
        self.connect((self.sum_iq2c, 0), (self.sum_scope, 0))
        self.connect((self.sum_sink, 0), (self.sum_iq2c, 0))


    def closeEvent(self, event):
        self.settings = Qt.QSettings("gnuradio/flowgraphs", "complex_math")
        self.settings.setValue("geometry", self.saveGeometry())
        self.stop()
        self.wait()

        event.accept()

    def get_samp_rate(self):
        return self.samp_rate

    def set_samp_rate(self, samp_rate):
        self.samp_rate = samp_rate
        self.in_scope.set_samp_rate(self.samp_rate)
        self.sum_scope.set_samp_rate(self.samp_rate)
        self.diff_scope.set_samp_rate(self.samp_rate)
        self.prod_scope.set_samp_rate(self.samp_rate)

    def get_n_pts(self):
        return self.n_pts

    def set_n_pts(self, n_pts):
        self.n_pts = n_pts




def main(top_block_cls=complex_math, options=None):

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
