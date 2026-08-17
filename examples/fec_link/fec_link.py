#!/usr/bin/env python3
# -*- coding: utf-8 -*-

#
# SPDX-License-Identifier: GPL-3.0
#
# GNU Radio Python Flow Graph
# Title: FEC protocol link (interleaver + Hamming + CRC on one array)
# Author: Lattrex
# Description: FEC protocol link on ONE placeKYT array, three streams on the shared duplex ports: 'tx' = message bytes -> UnpackKBits(8) -> Hamming(7,4) encoder -> 4x3 block interleaver (the interleaved coded bits egress); 'txcrc' = the same bytes -> on-chip CRC-16/CCITT-FALSE over the 12-byte frame; 'rx' = the burst-corrupted channel bits -> deinterleaver -> Hamming syndrome decoder -> PackKBits(8) (the recovered bytes egress). The demo story: a 2-bit channel burst that would be UNCORRECTABLE inside one Hamming codeword is dispersed by the interleaver into two codewords (one correctable error each); the CRC Frame Verdict panel shows the chip-computed TX CRC equal to the CRC recomputed over the recovered bytes.
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
from gnuradio.kyttar import fec_demo_stim as stim
import fec_link_crc_check as crc_check  # embedded python block
import sip
import threading



class fec_link(gr.top_block, Qt.QWidget):

    def __init__(self):
        gr.top_block.__init__(self, "FEC protocol link (interleaver + Hamming + CRC on one array)", catch_exceptions=True)
        Qt.QWidget.__init__(self)
        self.setWindowTitle("FEC protocol link (interleaver + Hamming + CRC on one array)")
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

        self.settings = Qt.QSettings("gnuradio/flowgraphs", "fec_link")

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
        self.tx_len = tx_len = stim.n_tx_bytes()
        self.samp_rate = samp_rate = 32000
        self.chan_len = chan_len = stim.n_channel_bits()

        ##################################################
        # Blocks
        ##################################################

        self.tx_src = kyttar.source(device_id="kyttar_0", port_name="x16_in", num_channels=1, server_host="127.0.0.1", server_port=58950, complex_in=False, burst_len=tx_len, stream_id="tx", pipelined=False, schedule="interleaved", repeat=False, output_words="auto")
        self.tx_sink = kyttar.sink(device_id="kyttar_0", port_name="x16_out", num_channels=1, server_port=58950, server_repeat=True, hold_secs=8.0, stream_id="tx", in_type=False)
        self.tx_scale = blocks.multiply_const_ff(32768.0)
        self.tx_f2b = blocks.float_to_uchar(1, 1, 0)
        self.tx_bits_scope = qtgui.time_sink_f(
            stim.n_tx_bits(), #size
            samp_rate, #samp_rate
            "TX interleaved coded bits (chip)", #name
            1, #number of inputs
            None # parent
        )
        self.tx_bits_scope.set_update_time(0.10)
        self.tx_bits_scope.set_y_axis(-0.5, 1.5)

        self.tx_bits_scope.set_y_label('bit', "")

        self.tx_bits_scope.enable_tags(True)
        self.tx_bits_scope.set_trigger_mode(qtgui.TRIG_MODE_FREE, qtgui.TRIG_SLOPE_POS, 0.0, 0, 0, "")
        self.tx_bits_scope.enable_autoscale(False)
        self.tx_bits_scope.enable_grid(False)
        self.tx_bits_scope.enable_axis_labels(True)
        self.tx_bits_scope.enable_control_panel(False)
        self.tx_bits_scope.enable_stem_plot(False)


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
                self.tx_bits_scope.set_line_label(i, "Data {0}".format(i))
            else:
                self.tx_bits_scope.set_line_label(i, labels[i])
            self.tx_bits_scope.set_line_width(i, widths[i])
            self.tx_bits_scope.set_line_color(i, colors[i])
            self.tx_bits_scope.set_line_style(i, styles[i])
            self.tx_bits_scope.set_line_marker(i, markers[i])
            self.tx_bits_scope.set_line_alpha(i, alphas[i])

        self._tx_bits_scope_win = sip.wrapinstance(self.tx_bits_scope.qwidget(), Qt.QWidget)
        self.top_layout.addWidget(self._tx_bits_scope_win)
        self.rx_src = kyttar.source(device_id="kyttar_0", port_name="x16_in", num_channels=1, server_host="127.0.0.1", server_port=58950, complex_in=False, burst_len=chan_len, stream_id="rx", pipelined=False, schedule="interleaved", repeat=False, output_words="auto")
        self.rx_sink = kyttar.sink(device_id="kyttar_0", port_name="x16_out", num_channels=1, server_port=58950, server_repeat=True, hold_secs=8.0, stream_id="rx", in_type=False)
        self.rx_scale = blocks.multiply_const_ff(32768.0)
        self.rx_bytes_scope = qtgui.time_sink_f(
            stim.n_rx_bytes(), #size
            samp_rate, #samp_rate
            "Recovered bytes (chip)", #name
            1, #number of inputs
            None # parent
        )
        self.rx_bytes_scope.set_update_time(0.10)
        self.rx_bytes_scope.set_y_axis(0, 256)

        self.rx_bytes_scope.set_y_label('byte value', "")

        self.rx_bytes_scope.enable_tags(True)
        self.rx_bytes_scope.set_trigger_mode(qtgui.TRIG_MODE_FREE, qtgui.TRIG_SLOPE_POS, 0.0, 0, 0, "")
        self.rx_bytes_scope.enable_autoscale(False)
        self.rx_bytes_scope.enable_grid(False)
        self.rx_bytes_scope.enable_axis_labels(True)
        self.rx_bytes_scope.enable_control_panel(False)
        self.rx_bytes_scope.enable_stem_plot(True)


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
                self.rx_bytes_scope.set_line_label(i, "Data {0}".format(i))
            else:
                self.rx_bytes_scope.set_line_label(i, labels[i])
            self.rx_bytes_scope.set_line_width(i, widths[i])
            self.rx_bytes_scope.set_line_color(i, colors[i])
            self.rx_bytes_scope.set_line_style(i, styles[i])
            self.rx_bytes_scope.set_line_marker(i, markers[i])
            self.rx_bytes_scope.set_line_alpha(i, alphas[i])

        self._rx_bytes_scope_win = sip.wrapinstance(self.rx_bytes_scope.qwidget(), Qt.QWidget)
        self.top_layout.addWidget(self._rx_bytes_scope_win)
        self.pack_b2f = blocks.uchar_to_float()
        self.msg_src = blocks.vector_source_b(stim.tx_bytes(), False, 1, [])
        self.msg_q15 = blocks.multiply_const_ff((1.0/32768.0))
        self.msg_b2f = blocks.uchar_to_float()
        self.k_unpack = kyttar.unpack_k_bits("kyttar_0", 8)
        self.k_pack = kyttar.pack_k_bits("kyttar_0", 8)
        self.k_ileave = kyttar.block_interleaver("kyttar_0", 4, 3, False)
        self.k_henc = kyttar.hamming_encoder("kyttar_0")
        self.k_hdec = kyttar.hamming_decoder("kyttar_0")
        self.k_dileave = kyttar.block_interleaver("kyttar_0", 4, 3, True)
        self.k_crc = kyttar.crc16("kyttar_0", 0x1021, 0xFFFF, 12)
        self.henc_b2f = blocks.uchar_to_float()
        self.dil_f2b = blocks.float_to_uchar(1, 1, 0)
        self.crc_verdict = qtgui.number_sink(
            gr.sizeof_float,
            0,
            qtgui.NUM_GRAPH_NONE,
            3,
            None # parent
        )
        self.crc_verdict.set_update_time(0.10)
        self.crc_verdict.set_title("CRC frame verdict")

        labels = ['TX CRC (chip)', 'RX CRC (recomputed)', 'FRAME OK (1 = match)', '', '',
            '', '', '', '', '']
        units = ['', '', '', '', '',
            '', '', '', '', '']
        colors = [("black", "black"), ("black", "black"), ("black", "black"), ("black", "black"), ("black", "black"),
            ("black", "black"), ("black", "black"), ("black", "black"), ("black", "black"), ("black", "black")]
        factor = [1, 1, 1, 1, 1,
            1, 1, 1, 1, 1]

        for i in range(3):
            self.crc_verdict.set_min(i, 0)
            self.crc_verdict.set_max(i, 65536)
            self.crc_verdict.set_color(i, colors[i][0], colors[i][1])
            if len(labels[i]) == 0:
                self.crc_verdict.set_label(i, "Data {0}".format(i))
            else:
                self.crc_verdict.set_label(i, labels[i])
            self.crc_verdict.set_unit(i, units[i])
            self.crc_verdict.set_factor(i, factor[i])

        self.crc_verdict.enable_autoscale(False)
        self._crc_verdict_win = sip.wrapinstance(self.crc_verdict.qwidget(), Qt.QWidget)
        self.top_layout.addWidget(self._crc_verdict_win)
        self.crc_src = kyttar.source(device_id="kyttar_0", port_name="x16_in", num_channels=1, server_host="127.0.0.1", server_port=58950, complex_in=False, burst_len=tx_len, stream_id="txcrc", pipelined=False, schedule="interleaved", repeat=False, output_words="auto")
        self.crc_sink = kyttar.sink(device_id="kyttar_0", port_name="x16_out", num_channels=1, server_port=58950, server_repeat=True, hold_secs=8.0, stream_id="txcrc", in_type=False)
        self.crc_s2f = blocks.short_to_float(1, 1)
        self.crc_f2b = blocks.float_to_uchar(1, 1, 0)
        self.crc_check = crc_check.blk(n_skip=stim.rx_msg_offset(), frame_len=stim.crc_frame_len())
        self.chan_src = blocks.vector_source_b(stim.channel_bits(), True, 1, [])
        self.chan_scope = qtgui.time_sink_f(
            chan_len, #size
            samp_rate, #samp_rate
            "Channel bits (2-bit burst)", #name
            1, #number of inputs
            None # parent
        )
        self.chan_scope.set_update_time(0.10)
        self.chan_scope.set_y_axis(-0.5, 1.5)

        self.chan_scope.set_y_label('bit', "")

        self.chan_scope.enable_tags(True)
        self.chan_scope.set_trigger_mode(qtgui.TRIG_MODE_FREE, qtgui.TRIG_SLOPE_POS, 0.0, 0, 0, "")
        self.chan_scope.enable_autoscale(False)
        self.chan_scope.enable_grid(False)
        self.chan_scope.enable_axis_labels(True)
        self.chan_scope.enable_control_panel(False)
        self.chan_scope.enable_stem_plot(False)


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
                self.chan_scope.set_line_label(i, "Data {0}".format(i))
            else:
                self.chan_scope.set_line_label(i, labels[i])
            self.chan_scope.set_line_width(i, widths[i])
            self.chan_scope.set_line_color(i, colors[i])
            self.chan_scope.set_line_style(i, styles[i])
            self.chan_scope.set_line_marker(i, markers[i])
            self.chan_scope.set_line_alpha(i, alphas[i])

        self._chan_scope_win = sip.wrapinstance(self.chan_scope.qwidget(), Qt.QWidget)
        self.top_layout.addWidget(self._chan_scope_win)
        self.chan_q15 = blocks.multiply_const_ff((1.0/32768.0))
        self.chan_b2f = blocks.uchar_to_float()


        ##################################################
        # Connections
        ##################################################
        self.connect((self.chan_b2f, 0), (self.chan_q15, 0))
        self.connect((self.chan_b2f, 0), (self.chan_scope, 0))
        self.connect((self.chan_q15, 0), (self.rx_src, 0))
        self.connect((self.chan_src, 0), (self.chan_b2f, 0))
        self.connect((self.crc_check, 2), (self.crc_verdict, 2))
        self.connect((self.crc_check, 0), (self.crc_verdict, 0))
        self.connect((self.crc_check, 1), (self.crc_verdict, 1))
        self.connect((self.crc_f2b, 0), (self.k_crc, 0))
        self.connect((self.crc_s2f, 0), (self.crc_sink, 0))
        self.connect((self.crc_sink, 0), (self.crc_check, 0))
        self.connect((self.crc_src, 0), (self.crc_f2b, 0))
        self.connect((self.dil_f2b, 0), (self.k_hdec, 0))
        self.connect((self.henc_b2f, 0), (self.k_ileave, 0))
        self.connect((self.k_crc, 0), (self.crc_s2f, 0))
        self.connect((self.k_dileave, 0), (self.dil_f2b, 0))
        self.connect((self.k_hdec, 0), (self.k_pack, 0))
        self.connect((self.k_henc, 0), (self.henc_b2f, 0))
        self.connect((self.k_ileave, 0), (self.tx_sink, 0))
        self.connect((self.k_pack, 0), (self.pack_b2f, 0))
        self.connect((self.k_unpack, 0), (self.k_henc, 0))
        self.connect((self.msg_b2f, 0), (self.msg_q15, 0))
        self.connect((self.msg_q15, 0), (self.crc_src, 0))
        self.connect((self.msg_q15, 0), (self.tx_src, 0))
        self.connect((self.msg_src, 0), (self.msg_b2f, 0))
        self.connect((self.pack_b2f, 0), (self.rx_sink, 0))
        self.connect((self.rx_scale, 0), (self.rx_bytes_scope, 0))
        self.connect((self.rx_sink, 0), (self.crc_check, 1))
        self.connect((self.rx_sink, 0), (self.rx_scale, 0))
        self.connect((self.rx_src, 0), (self.k_dileave, 0))
        self.connect((self.tx_f2b, 0), (self.k_unpack, 0))
        self.connect((self.tx_scale, 0), (self.tx_bits_scope, 0))
        self.connect((self.tx_sink, 0), (self.tx_scale, 0))
        self.connect((self.tx_src, 0), (self.tx_f2b, 0))


    def closeEvent(self, event):
        self.settings = Qt.QSettings("gnuradio/flowgraphs", "fec_link")
        self.settings.setValue("geometry", self.saveGeometry())
        self.stop()
        self.wait()

        event.accept()

    def get_tx_len(self):
        return self.tx_len

    def set_tx_len(self, tx_len):
        self.tx_len = tx_len

    def get_samp_rate(self):
        return self.samp_rate

    def set_samp_rate(self, samp_rate):
        self.samp_rate = samp_rate
        self.tx_bits_scope.set_samp_rate(self.samp_rate)
        self.chan_scope.set_samp_rate(self.samp_rate)
        self.rx_bytes_scope.set_samp_rate(self.samp_rate)

    def get_chan_len(self):
        return self.chan_len

    def set_chan_len(self, chan_len):
        self.chan_len = chan_len




def main(top_block_cls=fec_link, options=None):

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
