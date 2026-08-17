#!/usr/bin/env python3
# -*- coding: utf-8 -*-

#
# SPDX-License-Identifier: GPL-3.0
#
# GNU Radio Python Flow Graph
# Title: Kyttar CORDIC polar — on-chip magnitude + phase vs stock GNU Radio
# Author: Lattrex
# Description: CORDIC polar decomposition on the Kyttar array: ONE complex signal (an amplitude-modulated rotating phasor) split into two placed CORDIC chains — Complex To Mag recovers the AM ENVELOPE, Complex To Arg recovers the PHASE RAMP — each overlaid on the stock GNU Radio reference block so the chip and GR traces sit on top of each other. Both chains share the chip via the stream-id duplex (streams "mag" / "arg" on x16_in/x16_out). The Arg chain emits HALF-TURN units (word/32768*pi rad); the stock reference is scaled by 1/pi so the traces align. Run as GNURadio Server in placeKYT (port 58950), then Execute here.
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
import sip
import threading



class cordic_polar(gr.top_block, Qt.QWidget):

    def __init__(self):
        gr.top_block.__init__(self, "Kyttar CORDIC polar — on-chip magnitude + phase vs stock GNU Radio", catch_exceptions=True)
        Qt.QWidget.__init__(self)
        self.setWindowTitle("Kyttar CORDIC polar — on-chip magnitude + phase vs stock GNU Radio")
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

        self.settings = Qt.QSettings("gnuradio/flowgraphs", "cordic_polar")

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
        self.burst_len = burst_len = 256
        self.server_port = server_port = 58950
        self.iq_stim = iq_stim = [(0.25+0.55*(0.5+0.5*__import__('math').sin(2*3.141592653589793*2*n/burst_len)))*complex(__import__('math').cos(2*3.141592653589793*10*n/burst_len),__import__('math').sin(2*3.141592653589793*10*n/burst_len)) for n in range(burst_len)]

        ##################################################
        # Blocks
        ##################################################

        self.time_mag = qtgui.time_sink_f(
            256, #size
            1, #samp_rate
            "Envelope: stock GNU Radio vs on-chip CORDIC", #name
            2, #number of inputs
            None # parent
        )
        self.time_mag.set_update_time(0.10)
        self.time_mag.set_y_axis(0.0, 1.0)

        self.time_mag.set_y_label('magnitude', "")

        self.time_mag.enable_tags(True)
        self.time_mag.set_trigger_mode(qtgui.TRIG_MODE_FREE, qtgui.TRIG_SLOPE_POS, 0.0, 0, 0, "")
        self.time_mag.enable_autoscale(False)
        self.time_mag.enable_grid(True)
        self.time_mag.enable_axis_labels(True)
        self.time_mag.enable_control_panel(False)
        self.time_mag.enable_stem_plot(False)


        labels = ['GNU Radio |x|', 'Kyttar CORDIC |x|', 'Signal 3', 'Signal 4', 'Signal 5',
            'Signal 6', 'Signal 7', 'Signal 8', 'Signal 9', 'Signal 10']
        widths = [2, 1, 1, 1, 1,
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
                self.time_mag.set_line_label(i, "Data {0}".format(i))
            else:
                self.time_mag.set_line_label(i, labels[i])
            self.time_mag.set_line_width(i, widths[i])
            self.time_mag.set_line_color(i, colors[i])
            self.time_mag.set_line_style(i, styles[i])
            self.time_mag.set_line_marker(i, markers[i])
            self.time_mag.set_line_alpha(i, alphas[i])

        self._time_mag_win = sip.wrapinstance(self.time_mag.qwidget(), Qt.QWidget)
        self.top_layout.addWidget(self._time_mag_win)
        self.time_arg = qtgui.time_sink_f(
            256, #size
            1, #samp_rate
            "Phase: stock GNU Radio vs on-chip CORDIC (half-turn units)", #name
            2, #number of inputs
            None # parent
        )
        self.time_arg.set_update_time(0.10)
        self.time_arg.set_y_axis(-1.2, 1.2)

        self.time_arg.set_y_label('half-turns', "")

        self.time_arg.enable_tags(True)
        self.time_arg.set_trigger_mode(qtgui.TRIG_MODE_FREE, qtgui.TRIG_SLOPE_POS, 0.0, 0, 0, "")
        self.time_arg.enable_autoscale(False)
        self.time_arg.enable_grid(True)
        self.time_arg.enable_axis_labels(True)
        self.time_arg.enable_control_panel(False)
        self.time_arg.enable_stem_plot(False)


        labels = ['GNU Radio arg/pi', 'Kyttar CORDIC arg (half-turns)', 'Signal 3', 'Signal 4', 'Signal 5',
            'Signal 6', 'Signal 7', 'Signal 8', 'Signal 9', 'Signal 10']
        widths = [2, 1, 1, 1, 1,
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
                self.time_arg.set_line_label(i, "Data {0}".format(i))
            else:
                self.time_arg.set_line_label(i, labels[i])
            self.time_arg.set_line_width(i, widths[i])
            self.time_arg.set_line_color(i, colors[i])
            self.time_arg.set_line_style(i, styles[i])
            self.time_arg.set_line_marker(i, markers[i])
            self.time_arg.set_line_alpha(i, alphas[i])

        self._time_arg_win = sip.wrapinstance(self.time_arg.qwidget(), Qt.QWidget)
        self.top_layout.addWidget(self._time_arg_win)
        self.src_mag = blocks.vector_source_c(iq_stim, True, 1, [])
        self.src_arg = blocks.vector_source_c(iq_stim, True, 1, [])
        self.ref_mag = blocks.complex_to_mag(1)
        self.ref_arg_scale = blocks.multiply_const_ff(0.3183098861837907)
        self.ref_arg = blocks.complex_to_arg(1)
        self.ksrc_mag = kyttar.source(device_id="kyttar_0", port_name="x16_in", num_channels=1, server_host="127.0.0.1", server_port=server_port, complex_in=True, burst_len=burst_len, stream_id="mag", pipelined=True, schedule="interleaved", repeat=False, output_words="q15")
        self.ksrc_arg = kyttar.source(device_id="kyttar_0", port_name="x16_in", num_channels=1, server_host="127.0.0.1", server_port=server_port, complex_in=True, burst_len=burst_len, stream_id="arg", pipelined=True, schedule="interleaved", repeat=False, output_words="q15")
        self.ksink_mag = kyttar.sink(device_id="kyttar_0", port_name="x16_out", num_channels=1, server_port=server_port, server_repeat=False, hold_secs=8.0, stream_id="mag", in_type=False)
        self.ksink_arg = kyttar.sink(device_id="kyttar_0", port_name="x16_out", num_channels=1, server_port=server_port, server_repeat=False, hold_secs=8.0, stream_id="arg", in_type=False)
        self.cmag = kyttar.complex_to_mag("kyttar_0", block_name="")
        self.carg = kyttar.complex_to_arg("kyttar_0", block_name="")


        ##################################################
        # Connections
        ##################################################
        self.connect((self.carg, 0), (self.ksink_arg, 0))
        self.connect((self.cmag, 0), (self.ksink_mag, 0))
        self.connect((self.ksink_arg, 0), (self.time_arg, 1))
        self.connect((self.ksink_mag, 0), (self.time_mag, 1))
        self.connect((self.ksrc_arg, 0), (self.carg, 0))
        self.connect((self.ksrc_mag, 0), (self.cmag, 0))
        self.connect((self.ref_arg, 0), (self.ref_arg_scale, 0))
        self.connect((self.ref_arg_scale, 0), (self.time_arg, 0))
        self.connect((self.ref_mag, 0), (self.time_mag, 0))
        self.connect((self.src_arg, 0), (self.ksrc_arg, 0))
        self.connect((self.src_arg, 0), (self.ref_arg, 0))
        self.connect((self.src_mag, 0), (self.ksrc_mag, 0))
        self.connect((self.src_mag, 0), (self.ref_mag, 0))


    def closeEvent(self, event):
        self.settings = Qt.QSettings("gnuradio/flowgraphs", "cordic_polar")
        self.settings.setValue("geometry", self.saveGeometry())
        self.stop()
        self.wait()

        event.accept()

    def get_burst_len(self):
        return self.burst_len

    def set_burst_len(self, burst_len):
        self.burst_len = burst_len
        self.set_iq_stim([(0.25+0.55*(0.5+0.5*__import__('math').sin(2*3.141592653589793*2*n/self.burst_len)))*complex(__import__('math').cos(2*3.141592653589793*10*n/self.burst_len),__import__('math').sin(2*3.141592653589793*10*n/self.burst_len)) for n in range(self.burst_len)])

    def get_server_port(self):
        return self.server_port

    def set_server_port(self, server_port):
        self.server_port = server_port

    def get_iq_stim(self):
        return self.iq_stim

    def set_iq_stim(self, iq_stim):
        self.iq_stim = iq_stim
        self.src_mag.set_data(self.iq_stim, [])
        self.src_arg.set_data(self.iq_stim, [])




def main(top_block_cls=cordic_polar, options=None):

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
