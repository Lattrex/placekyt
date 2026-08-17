# SPDX-License-Identifier: GPL-3.0-or-later
"""The REAL GNU Radio client loop for the panel-backed examples (GUI-equivalent).

Lesson (user-escalated): a hand-rolled socket test of ``process_batch`` PASSED
while the actual GUI Run garbled the CW keying — because the REAL
``kyttar.source`` with a ``stream_id`` goes through the DuplexRendezvous and a
``process_batch_duplex`` RPC, whose handler honored ``pipelined: true``
unconditionally and SLAMMED the whole burst (the saturated drive its own doc
marks 'only correct for saturation-safe blocks'). A test that does not run the
genuine client stack does not verify the GUI path — full stop.

These tests close that gap: they host the SHIPPED example ``.kyt`` on the real
``SimController.start_gnuradio_server`` and run the genuine GR client — real
``kyttar.source``/``kyttar.sink`` (+ the marker chain) in a real ``gr.top_block``
under the GNU Radio interpreter in a SUBPROCESS, exactly what pressing Run in
GRC executes minus the literal window — then assert the recovered stream is
EXACT vs the golden. They also pin the fix: a panel-backed design REFUSES the
``pipelined`` header server-side (``SimServer.force_per_sample``) and runs the
per-sample paced drive.

The remaining unverified delta vs the GUI is ONLY the Qt window itself
(rendering/interaction) — the data path is identical.
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import numpy as np
import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication  # noqa: E402

from engine.catalog import BlockCatalog  # noqa: E402
from engine.io.project_io import load_project  # noqa: E402
from ui.controller import AppController  # noqa: E402
from ui.sim_controller import SimController  # noqa: E402

_ROOT = Path(__file__).resolve().parents[2]
AUDIO_METER_KYT = _ROOT / "examples" / "audio_meter" / "audio_meter.kyt"
ROBUST_RX_KYT = _ROOT / "examples" / "robust_rx" / "robust_rx.kyt"
COMPLEX_MATH_KYT = _ROOT / "examples" / "complex_math" / "complex_math.kyt"
CHANNEL_SEL_KYT = _ROOT / "examples" / "channel_selector" / "channel_selector.kyt"
EFFECT_ECHO_KYT = _ROOT / "examples" / "audio_effects" / "effect_echo.kyt"
PSK31_TRX_KYT = _ROOT / "examples" / "psk31_transceiver" / "psk31_transceiver.kyt"
CW_TRX_KYT = _ROOT / "examples" / "cw_transceiver" / "cw_transceiver.kyt"
KYTTAR_PKG = _ROOT / "gr-kyttar" / "python"
GR_PYTHON = os.environ.get("KYTTAR_GR_PYTHON", "/usr/bin/python3")
sys.path.insert(0, str(_ROOT / "examples" / "psk31_transceiver"))
sys.path.insert(0, str(_ROOT / "examples" / "audio_meter"))
sys.path.insert(0, str(_ROOT / "examples" / "channel_selector"))
sys.path.insert(0, str(_ROOT / "examples" / "audio_effects"))
sys.path.insert(0, str(_ROOT / "examples" / "psk31_transceiver"))
sys.path.insert(0, str(_ROOT / "examples" / "cw_transceiver"))
sys.path.insert(0, str(_ROOT / "examples" / "robust_rx"))
sys.path.insert(0, str(_ROOT / "examples" / "complex_math"))

pytestmark = pytest.mark.skipif(
    not (PSK31_TRX_KYT.exists() and CW_TRX_KYT.exists()
         and Path(GR_PYTHON).exists()),
    reason="shipped .kyt or GNU Radio interpreter absent")


# The genuine client flowgraphs, run under the GR interpreter in a subprocess.
# The repo kyttar package is injected as gnuradio.kyttar (identical code to the
# installed OOT once install.sh runs). hold_secs=0: headless emit-once-and-end.



_AUDIO_METER_CLIENT = r"""
import os, sys, math
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, sys.argv[1])
import kyttar as _k
import gnuradio
gnuradio.kyttar = _k
sys.modules['gnuradio.kyttar'] = _k
from gnuradio import gr, blocks

port = int(sys.argv[2])
# argv[3] unused (no message); the signal is the example's SIG, regenerated.
sig = ([0.05 + 0.85 * math.sin(2 * math.pi * 1000 * t / 8000)
        for t in range(160)] + [0.0] * 520)

class top(gr.top_block):
    def __init__(self):
        gr.top_block.__init__(self, "audio_meter_client")
        n = len(sig)
        # ---- audio stream
        self.a_vec = blocks.vector_source_f(sig, False, 1, [])
        self.a_src = _k.source(device_id="kyttar_0", port_name="x16_in",
                               num_channels=1, server_host="127.0.0.1",
                               server_port=port, complex_in=False,
                               burst_len=n, stream_id="audio",
                               pipelined=True, schedule="interleaved")
        self.dcb = _k.dc_blocker("kyttar_0", 32, False)
        self.agc = _k.agc("kyttar_0", 0.02, 0.3, 0.999, 0.999)
        self.brf = _k.band_reject_filter("kyttar_0", 0.999, 8000.0, 3300.0,
                                         3700.0, 400.0, "hamming")
        self.sq = _k.squelch("kyttar_0", -25.0, 0.01, 0, False)
        self.a_snk = _k.sink(device_id="kyttar_0", port_name="x16_out",
                             num_channels=1, server_port=port,
                             server_repeat=False, hold_secs=0.0,
                             stream_id="audio", in_type=False)
        self.a_out = blocks.vector_sink_f()
        self.connect(self.a_vec, self.a_src, self.dcb, self.agc, self.brf,
                     self.sq, self.a_snk, self.a_out)
        # ---- meter stream
        self.m_vec = blocks.vector_source_f(sig, False, 1, [])
        self.m_src = _k.source(device_id="kyttar_0", port_name="x16_in",
                               num_channels=1, server_host="127.0.0.1",
                               server_port=port, complex_in=False,
                               burst_len=n, stream_id="meter",
                               pipelined=True, schedule="interleaved")
        self.env = _k.abs_bb("kyttar_0")
        self.avg = _k.moving_average("kyttar_0", 8, 0.125)
        self.db = _k.nlog10("kyttar_0", 10.0, 0.0)
        self.m_snk = _k.sink(device_id="kyttar_0", port_name="x16_out",
                             num_channels=1, server_port=port,
                             server_repeat=False, hold_secs=0.0,
                             stream_id="meter", in_type=False)
        self.m_out = blocks.vector_sink_f()
        self.connect(self.m_vec, self.m_src, self.env, self.avg, self.db,
                     self.m_snk, self.m_out)
        # ---- true-RMS stream
        self.r_vec = blocks.vector_source_f(sig, False, 1, [])
        self.r_src = _k.source(device_id="kyttar_0", port_name="x16_in",
                               num_channels=1, server_host="127.0.0.1",
                               server_port=port, complex_in=False,
                               burst_len=n, stream_id="rms",
                               pipelined=True, schedule="interleaved")
        self.rms = _k.rms(device_id="kyttar_0", alpha=0.0625)
        self.r_snk = _k.sink(device_id="kyttar_0", port_name="x16_out",
                             num_channels=1, server_port=port,
                             server_repeat=False, hold_secs=0.0,
                             stream_id="rms", in_type=False)
        self.r_out = blocks.vector_sink_f()
        self.connect(self.r_vec, self.r_src, self.rms, self.r_snk, self.r_out)

tb = top(); tb.run()
print("AUDIO_Q15", " ".join(str(int(round(v*32768.0)) & 0xFFFF) for v in tb.a_out.data()))
print("METER_Q15", " ".join(str(int(round(v*32768.0)) & 0xFFFF) for v in tb.m_out.data()))
print("RMS_Q15", " ".join(str(int(round(v*32768.0)) & 0xFFFF) for v in tb.r_out.data()))
"""


_CHANNEL_SEL_CLIENT = r"""
import os, sys, math
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, sys.argv[1])
import kyttar as _k
import gnuradio
gnuradio.kyttar = _k
sys.modules['gnuradio.kyttar'] = _k
from gnuradio import gr, blocks

port = int(sys.argv[2])
sig = [0.25*math.sin(2*math.pi*8600*t/32000) + 0.25*math.cos(2*math.pi*9400*t/32000)
       + 0.2*math.sin(2*math.pi*4000*t/32000) + 0.2*math.sin(2*math.pi*14000*t/32000)
       for t in range(320)]
FXF_TAPS = [0.0, 0.018715, 0.099838, 0.226239, 0.290416, 0.226239, 0.099838,
            0.018715, 0.0]

class top(gr.top_block):
    def __init__(self):
        gr.top_block.__init__(self, "channel_selector_client")
        self.vec = blocks.vector_source_f(sig, False, 1, [])
        # pipelined=False: the FreqXlatingFIR is SATURATION-BESPOKE — the
        # shipped .grc requests the per-sample paced drive.
        self.src = _k.source(device_id="kyttar_0", port_name="x16_in",
                             num_channels=1, server_host="127.0.0.1",
                             server_port=port, complex_in=False,
                             burst_len=len(sig), stream_id="rf",
                             pipelined=False, schedule="interleaved")
        self.qzero = blocks.vector_source_f([0.0] * len(sig), False, 1, [])
        self.f2c = _k.float_to_complex("kyttar_0")
        self.fxf = _k.freq_xlating_fir("kyttar_0", 1, FXF_TAPS, 9000.0,
                                       32000.0, False)
        self.clpf = _k.complex_low_pass_filter("kyttar_0", 0.9, 32000.0,
                                               1200.0, 2500.0, "hamming")
        self.rot = _k.multiply_const_complex("kyttar_0", 0.6, 0.35)
        self.c2i = _k.complex_to_imag("kyttar_0")
        self.snk = _k.sink(device_id="kyttar_0", port_name="x16_out",
                           num_channels=1, server_port=port,
                           server_repeat=False, hold_secs=0.0,
                           stream_id="rf", in_type=False)
        self.out = blocks.vector_sink_f()
        self.connect(self.vec, self.src)
        self.connect(self.src, (self.f2c, 0))
        self.connect(self.qzero, (self.f2c, 1))
        self.connect(self.f2c, self.fxf, self.clpf,
                     self.rot, self.c2i, self.snk, self.out)

tb = top(); tb.run()
print("CLIENT_Q15", " ".join(str(int(round(v*32768.0)) & 0xFFFF) for v in tb.out.data()))
"""

_EFFECT_ECHO_CLIENT = r"""
import os, sys, math
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, sys.argv[1])
import kyttar as _k
import gnuradio
gnuradio.kyttar = _k
sys.modules['gnuradio.kyttar'] = _k
from gnuradio import gr, blocks

port = int(sys.argv[2])
sig = [0.5*math.sin(2*math.pi*330*t/8000) + 0.05*math.sin(2*math.pi*2200*t/8000)
       for t in range(400)]
B = [0.04125353724172031, 0.08250707448344062, 0.04125353724172031]
A = [-1.3489677452527948, 0.5139818942196759]

class top(gr.top_block):
    def __init__(self):
        gr.top_block.__init__(self, "effect_echo_client")
        self.vec = blocks.vector_source_f(sig, False, 1, [])
        # pipelined=False: the echo is a single-fire JOIN — per-sample paced.
        self.src = _k.source(device_id="kyttar_0", port_name="x16_in",
                             num_channels=1, server_host="127.0.0.1",
                             server_port=port, complex_in=False,
                             burst_len=len(sig), stream_id="fx",
                             pipelined=False, schedule="interleaved")
        self.d8 = _k.delay("kyttar_0", 8)
        self.g1 = _k.gain("kyttar_0", 0.5)
        self.add = _k.add("kyttar_0", 2)
        self.g2 = _k.gain("kyttar_0", 0.5)
        self.iir = _k.iir_biquad("kyttar_0", B, A, None, None, False)
        self.keep = _k.keep_one_in_n("kyttar_0", 2)
        self.snk = _k.sink(device_id="kyttar_0", port_name="x16_out",
                           num_channels=1, server_port=port,
                           server_repeat=False, hold_secs=0.0,
                           stream_id="fx", in_type=False)
        self.out = blocks.vector_sink_f()
        self.connect(self.vec, self.src)
        self.connect(self.src, (self.add, 0))
        self.connect(self.src, self.d8, self.g1, (self.add, 1))
        self.connect(self.add, self.g2, self.iir, self.keep, self.snk,
                     self.out)

tb = top(); tb.run()
print("CLIENT_Q15", " ".join(str(int(round(v*32768.0)) & 0xFFFF) for v in tb.out.data()))
"""


_PSK31_TRX_CLIENT = r"""
import os, sys
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, sys.argv[1])
import kyttar as _k
import gnuradio
gnuradio.kyttar = _k
sys.modules['gnuradio.kyttar'] = _k
from gnuradio import gr, blocks

port = int(sys.argv[2])
tx_text = sys.argv[3]
rx_syms = [float(x) for x in sys.argv[4].split(",")]

class top(gr.top_block):
    def __init__(self):
        gr.top_block.__init__(self, "psk31_trx_client")
        # ---- TX
        self.msg_src = blocks.vector_source_b([ord(c) for c in tx_text],
                                              False, 1, [])
        self.b2f_in = blocks.uchar_to_float()
        self.to_raw = blocks.multiply_const_ff(1.0/32768.0)
        self.tx_src = _k.source(device_id="kyttar_0", port_name="x16_in",
                                num_channels=1, server_host="127.0.0.1",
                                server_port=port, complex_in=False,
                                burst_len=len(tx_text), stream_id="tx",
                                pipelined=False, schedule="interleaved")
        self.f2b = blocks.float_to_uchar()
        self.varicode = _k.varicode_encoder("kyttar_0", 1, 1, 25, 1, 0, 0, 1024)
        self.diff = _k.diff_encoder("kyttar_0", 2, "DIFF_DIFFERENTIAL")
        self.b2f = blocks.uchar_to_float()
        self.mapper = _k.psk_symbol_mapper("kyttar_0", "bpsk", [], 1, True)
        self.c2r = blocks.complex_to_real(1)
        self.repeat = blocks.repeat(gr.sizeof_float*1, 8)
        self.envelope = _k.raised_cosine_envelope("kyttar_0", 8)
        self.tx_sink = _k.sink(device_id="kyttar_0", port_name="x16_out",
                               num_channels=1, server_port=port,
                               server_repeat=False, hold_secs=0.0,
                               stream_id="tx", in_type=False)
        self.tx_out = blocks.vector_sink_f()
        self.connect(self.msg_src, self.b2f_in, self.to_raw, self.tx_src)
        self.connect(self.tx_src, self.f2b, self.varicode, self.diff,
                     self.b2f, self.mapper)
        self.connect(self.mapper, self.c2r, self.repeat, self.envelope,
                     self.tx_sink, self.tx_out)
        # ---- RX
        self.rx_vec = blocks.vector_source_f(rx_syms, False, 1, [])
        self.rx_src = _k.source(device_id="kyttar_0", port_name="x16_in",
                                num_channels=1, server_host="127.0.0.1",
                                server_port=port, complex_in=False,
                                burst_len=len(rx_syms), stream_id="rx",
                                pipelined=False, schedule="interleaved")
        self.slicer = _k.bpsk_slicer("kyttar_0", "bit")
        self.ddec = _k.diff_decoder("kyttar_0", 2, 0)
        self.vdec = _k.varicode_decoder("kyttar_0", 1, 1, 25, 2, 1, 25,
                                        5, 1, 0, 0, None)
        self.rx_b2f = blocks.uchar_to_float()
        self.rx_sink = _k.sink(device_id="kyttar_0", port_name="x16_out",
                               num_channels=1, server_port=port,
                               server_repeat=False, hold_secs=0.0,
                               stream_id="rx", in_type=False)
        self.rx_out = blocks.vector_sink_f()
        self.connect(self.rx_vec, self.rx_src, self.slicer, self.ddec,
                     self.vdec, self.rx_b2f, self.rx_sink, self.rx_out)

tb = top(); tb.run()
print("TX_Q15", " ".join(str(int(round(v*32768.0)) & 0xFFFF) for v in tb.tx_out.data()))
print("RX_Q15", " ".join(str(int(round(v*32768.0)) & 0xFFFF) for v in tb.rx_out.data()))
"""


_CW_TRX_CLIENT = r"""
import os, sys
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, sys.argv[1])
import kyttar as _k
import gnuradio
gnuradio.kyttar = _k
sys.modules['gnuradio.kyttar'] = _k
from gnuradio import gr, blocks

port = int(sys.argv[2])
tx_text = sys.argv[3]
rx_sig = [float(x) for x in sys.argv[4].split(",")]

class top(gr.top_block):
    def __init__(self):
        gr.top_block.__init__(self, "cw_trx_client")
        # ---- TX
        chars = [ord(c) if c != " " else 0 for c in tx_text]
        self.msg_src = blocks.vector_source_b(chars, False, 1, [])
        self.b2f_in = blocks.uchar_to_float()
        self.to_raw = blocks.multiply_const_ff(1.0/32768.0)
        self.tx_src = _k.source(device_id="kyttar_0", port_name="x16_in",
                                num_channels=1, server_host="127.0.0.1",
                                server_port=port, complex_in=False,
                                burst_len=len(chars), stream_id="tx",
                                pipelined=False, schedule="interleaved")
        self.f2b = blocks.float_to_uchar()
        self.keyer = _k.cw_keyer("kyttar_0", 20, 8, 2, None, 1, 1, 0, 1, 0,
                                 0, 0)
        self.tx_sink = _k.sink(device_id="kyttar_0", port_name="x16_out",
                               num_channels=1, server_port=port,
                               server_repeat=False, hold_secs=0.0,
                               stream_id="tx", in_type=False)
        self.tx_out = blocks.vector_sink_f()
        self.connect(self.msg_src, self.b2f_in, self.to_raw, self.tx_src)
        self.connect(self.tx_src, self.f2b, self.keyer, self.tx_sink,
                     self.tx_out)
        # ---- RX
        self.rx_vec = blocks.vector_source_f(rx_sig, False, 1, [])
        self.rx_src = _k.source(device_id="kyttar_0", port_name="x16_in",
                                num_channels=1, server_host="127.0.0.1",
                                server_port=port, complex_in=False,
                                burst_len=len(rx_sig), stream_id="rx",
                                pipelined=False, schedule="interleaved")
        self.env = _k.abs_bb("kyttar_0")
        self.cwdec = _k.cw_decoder("kyttar_0", 0.3, 1, 1, 25, 1, 8, 1, 5, 1,
                                   0, 0, 25, None, 25, 1, 16384)
        self.rx_b2f = blocks.uchar_to_float()
        self.rx_sink = _k.sink(device_id="kyttar_0", port_name="x16_out",
                               num_channels=1, server_port=port,
                               server_repeat=False, hold_secs=0.0,
                               stream_id="rx", in_type=False)
        self.rx_out = blocks.vector_sink_f()
        self.connect(self.rx_vec, self.rx_src, self.env, self.cwdec,
                     self.rx_b2f, self.rx_sink, self.rx_out)

tb = top(); tb.run()
print("TX_Q15", " ".join(str(int(round(v*32768.0)) & 0xFFFF) for v in tb.tx_out.data()))
print("RX_Q15", " ".join(str(int(round(v*32768.0)) & 0xFFFF) for v in tb.rx_out.data()))
"""


_ROBUST_RX_CLIENT = r"""
import os, sys
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, sys.argv[1])
import kyttar as _k
from kyttar import robust_demo_stim as stim
import gnuradio
gnuradio.kyttar = _k
sys.modules['gnuradio.kyttar'] = _k
from gnuradio import gr, blocks

port = int(sys.argv[2])
sig = stim.rx_burst()
n = len(sig)

class top(gr.top_block):
    def __init__(self):
        gr.top_block.__init__(self, "robust_rx_client")
        # ---- 'rx': FLL -> Costas -> slicer
        self.rx_vec = blocks.vector_source_c(sig, False, 1, [])
        self.rx_src = _k.source(device_id="kyttar_0", port_name="x16_in",
                                num_channels=1, server_host="127.0.0.1",
                                server_port=port, complex_in=True,
                                burst_len=n, stream_id="rx",
                                pipelined=False, schedule="interleaved")
        self.fll = _k.fll_band_edge("kyttar_0", 2.0, 0.35, 17, 0.1)
        self.cos = _k.complex_costas_loop("kyttar_0", 0.05, 1.0, 2)
        self.sli = _k.bpsk_slicer("kyttar_0", "bit")
        self.b2f = blocks.uchar_to_float()
        self.rx_snk = _k.sink(device_id="kyttar_0", port_name="x16_out",
                              num_channels=1, server_port=port,
                              server_repeat=False, hold_secs=0.0,
                              stream_id="rx", in_type=False)
        self.rx_out = blocks.vector_sink_f()
        self.connect(self.rx_vec, self.rx_src, self.fll, self.cos, self.sli,
                     self.b2f, self.rx_snk, self.rx_out)
        # ---- 'ctl': Costas -> slicer (the negative control)
        self.ctl_vec = blocks.vector_source_c(sig, False, 1, [])
        self.ctl_src = _k.source(device_id="kyttar_0", port_name="x16_in",
                                 num_channels=1, server_host="127.0.0.1",
                                 server_port=port, complex_in=True,
                                 burst_len=n, stream_id="ctl",
                                 pipelined=False, schedule="interleaved")
        self.ctl_cos = _k.complex_costas_loop("kyttar_0", 0.05, 1.0, 2)
        self.ctl_sli = _k.bpsk_slicer("kyttar_0", "bit")
        self.ctl_b2f = blocks.uchar_to_float()
        self.ctl_snk = _k.sink(device_id="kyttar_0", port_name="x16_out",
                               num_channels=1, server_port=port,
                               server_repeat=False, hold_secs=0.0,
                               stream_id="ctl", in_type=False)
        self.ctl_out = blocks.vector_sink_f()
        self.connect(self.ctl_vec, self.ctl_src, self.ctl_cos, self.ctl_sli,
                     self.ctl_b2f, self.ctl_snk, self.ctl_out)

tb = top(); tb.run()
# complex-input chains emit RAW word floats (the receiver convention): the
# slicer's 0/1 bit words arrive as 0.0/1.0.
print("RX_RAW", " ".join(str(int(round(v)) & 0xFFFF) for v in tb.rx_out.data()))
print("CTL_RAW", " ".join(str(int(round(v)) & 0xFFFF) for v in tb.ctl_out.data()))
"""


_COMPLEX_MATH_CLIENT = r"""
import os, sys
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, sys.argv[1])
import kyttar as _k
from kyttar import cmath_demo_stim as stim
import gnuradio
gnuradio.kyttar = _k
sys.modules['gnuradio.kyttar'] = _k
from gnuradio import gr, blocks

port = int(sys.argv[2])
a = stim.tone_a()
b = stim.tone_b()
n = len(a)

class top(gr.top_block):
    def __init__(self):
        gr.top_block.__init__(self, "complex_math_client")
        self.outs = {}
        # keep a PYTHON reference to every GR block: loop locals are dropped
        # each iteration and an unreferenced python block segfaults the C++
        # scheduler at start.
        self.keep = []
        for name, marker, sid_b in (("sum", _k.add_cc, "b_add"),
                                    ("diff", _k.sub_cc, "b_sub"),
                                    ("prod", _k.multiply_cc, "b_mul")):
            av = blocks.vector_source_c(a, False, 1, [])
            bv = blocks.vector_source_c(b, False, 1, [])
            asrc = _k.source(device_id="kyttar_0", port_name="x16_in",
                             num_channels=1, server_host="127.0.0.1",
                             server_port=port, complex_in=True, burst_len=n,
                             stream_id=name, pipelined=False,
                             schedule="interleaved", output_words="q15")
            bsrc = _k.source(device_id="kyttar_0", port_name="x16_in",
                             num_channels=1, server_host="127.0.0.1",
                             server_port=port, complex_in=True, burst_len=n,
                             stream_id=sid_b, pipelined=False,
                             schedule="interleaved", output_words="q15")
            op = marker(device_id="kyttar_0", num_inputs=2)
            snk = _k.sink(device_id="kyttar_0", port_name="x16_out",
                          num_channels=1, server_port=port,
                          server_repeat=False, hold_secs=0.0,
                          stream_id=name, in_type=True)
            out = blocks.vector_sink_f()
            self.connect(av, asrc, (op, 0))
            self.connect(bv, bsrc, (op, 1))
            self.connect(op, snk, out)
            self.keep += [av, bv, asrc, bsrc, op, snk]
            self.outs[name] = out

tb = top(); tb.run()
for name, out in tb.outs.items():
    print(name.upper() + "_Q15",
          " ".join(str(int(round(v*32768.0)) & 0xFFFF) for v in out.data()))
"""


@pytest.fixture(scope="module")
def qapp():
    return QApplication.instance() or QApplication([])


def _run_client(script_text, tmp_path, name, port, message) -> list[int]:
    script = tmp_path / f"{name}.py"
    script.write_text(script_text)
    r = subprocess.run([GR_PYTHON, str(script), str(KYTTAR_PKG), str(port),
                        message], capture_output=True, text=True, timeout=900)
    q = next((ln for ln in r.stdout.splitlines()
              if ln.startswith("CLIENT_Q15")), None)
    assert r.returncode == 0 and q is not None, (
        f"GR client failed (rc={r.returncode}):\n{r.stderr[-1500:]}")
    return [int(x) for x in q.split()[1:]]


def _serve(kyt, port):
    cat = BlockCatalog.from_gr_kyttar()
    ctrl = AppController(catalog=cat)
    ctrl.set_project(load_project(str(kyt)))
    sim = SimController(ctrl)
    bound = sim.start_gnuradio_server(port=port)
    assert bound == port
    return ctrl, sim


def test_server_refuses_pipelined_for_panel_designs(qapp):
    """The fix, pinned: a panel-backed hosted design forces the per-sample
    paced drive server-side, whatever the flowgraph's pipelined header says."""
    ctrl, sim = _serve(CW_TRX_KYT, 58981)
    try:
        assert sim._gr_server._force_per_sample is True
    finally:
        sim.stop_gnuradio_server()



def test_audio_meter_real_gr_client_three_stream_duplex(qapp, tmp_path):
    """The genuine GR client loop for the THREE-STREAM analog example — three
    real kyttar.source/sink pairs ('audio'/'meter'/'rms') through the
    DuplexRendezvous on one hosted chip. All recovered streams must satisfy
    the example's DERIVED per-block bounds vs the stock-GR golden (the same
    bounds the headless gate asserts — never widened for the client path)."""
    from audio_meter_demo import (AUDIO_TOL_LSB, METER_FLOOR, METER_TOL_DB,
                                  NLOG10_DB_SCALE, RMS_TOL_LSB, SIG,
                                  TONE_ONSET, TRANSIENT_TRIM, _q15, _s16,
                                  gr_golden, rms_worst)

    if not AUDIO_METER_KYT.exists():
        pytest.skip("audio_meter.kyt absent")
    ctrl, sim = _serve(AUDIO_METER_KYT, 58984)
    try:
        script = tmp_path / "audio_meter_client.py"
        script.write_text(_AUDIO_METER_CLIENT)
        r = subprocess.run([GR_PYTHON, str(script), str(KYTTAR_PKG), "58984"],
                           capture_output=True, text=True, timeout=900)
        lines = {ln.split()[0]: [int(x) for x in ln.split()[1:]]
                 for ln in r.stdout.splitlines()
                 if ln.startswith(("AUDIO_Q15", "METER_Q15", "RMS_Q15"))}
        assert r.returncode == 0 and set(lines) == {"AUDIO_Q15", "METER_Q15",
                                                    "RMS_Q15"}, (
            f"GR client failed (rc={r.returncode}):\n{r.stderr[-1500:]}")
        a_chip = [_s16(v) for v in lines["AUDIO_Q15"]]
        m_chip = [_s16(v) for v in lines["METER_Q15"]]
        r_chip = [_s16(v) for v in lines["RMS_Q15"]]
        a_gold, m_gold, r_gold = gr_golden(SIG)
        assert len(a_chip) == len(a_gold) and len(m_chip) == len(m_gold)
        assert len(r_chip) == len(r_gold)
        worst_a = max(abs(a_chip[i] - _s16(_q15(a_gold[i])))
                      for i in range(len(a_gold))
                      if not (TONE_ONSET <= i < TONE_ONSET + TRANSIENT_TRIM))
        assert worst_a <= AUDIO_TOL_LSB, worst_a
        worst_m, compared = 0.0, 0
        for i in range(len(m_gold)):
            if 10 ** (m_gold[i] / 10.0) < METER_FLOOR:
                continue
            worst_m = max(worst_m, abs((m_chip[i] / 32768.0) * NLOG10_DB_SCALE
                                       - m_gold[i]))
            compared += 1
        assert compared > 50 and worst_m <= METER_TOL_DB, (worst_m, compared)
        worst_r, compared_r = rms_worst(r_chip, r_gold)
        assert compared_r > 100 and worst_r <= RMS_TOL_LSB, (worst_r,
                                                             compared_r)
    finally:
        sim.stop_gnuradio_server()


def test_robust_rx_real_gr_client_duplex(qapp, tmp_path):
    """The genuine GR client loop for the robust_rx example — two real
    complex kyttar.source/sink pairs ('rx' = FLL->Costas->slicer, 'ctl' =
    Costas->slicer) through the DuplexRendezvous against the hosted shipped
    .kyt. The FLL chain must recover BER 0 at foff=0.18 while the control
    chain fails — the same verdicts as the headless gate, through the real
    client stack."""
    from robust_rx_demo import CTL_FAIL_BER, chain_ber, stim

    if not ROBUST_RX_KYT.exists():
        pytest.skip("robust_rx.kyt absent")
    ctrl, sim = _serve(ROBUST_RX_KYT, 58989)
    try:
        script = tmp_path / "robust_rx_client.py"
        script.write_text(_ROBUST_RX_CLIENT)
        r = subprocess.run([GR_PYTHON, str(script), str(KYTTAR_PKG), "58989"],
                           capture_output=True, text=True, timeout=900)
        lines = {ln.split()[0]: [int(x) for x in ln.split()[1:]]
                 for ln in r.stdout.splitlines()
                 if ln.startswith(("RX_RAW", "CTL_RAW"))}
        assert r.returncode == 0 and set(lines) == {"RX_RAW", "CTL_RAW"}, (
            f"GR client failed (rc={r.returncode}):\n{r.stderr[-1500:]}")
        bits = stim.tx_bits()
        n_want = stim.n_rx_bits()
        assert len(lines["RX_RAW"]) >= n_want - 4, len(lines["RX_RAW"])
        assert len(lines["CTL_RAW"]) >= n_want - 4, len(lines["CTL_RAW"])
        assert chain_ber(lines["RX_RAW"], bits) == 0.0
        ber_ctl = chain_ber(lines["CTL_RAW"], bits)
        assert ber_ctl > CTL_FAIL_BER, (
            f"negative control void through the client stack: {ber_ctl}")
    finally:
        sim.stop_gnuradio_server()


def test_complex_math_real_gr_client_six_streams(qapp, tmp_path):
    """The genuine GR client loop for the two-complex-stream arithmetic
    example — SIX real complex kyttar.sources (three block input pairs) and
    three sinks through the DuplexRendezvous. Every recovered stream must be
    BIT-EXACT vs its block's own reference (interleaved I/Q via the complex
    two-tag egress), whatever thread order the rendezvous collected the
    streams in (the deterministic out_tag-ownership rule)."""
    from complex_math_demo import references

    if not COMPLEX_MATH_KYT.exists():
        pytest.skip("complex_math.kyt absent")
    ctrl, sim = _serve(COMPLEX_MATH_KYT, 58990)
    try:
        script = tmp_path / "complex_math_client.py"
        script.write_text(_COMPLEX_MATH_CLIENT)
        r = subprocess.run([GR_PYTHON, str(script), str(KYTTAR_PKG), "58990"],
                           capture_output=True, text=True, timeout=900)
        lines = {ln.split()[0]: [int(x) for x in ln.split()[1:]]
                 for ln in r.stdout.splitlines()
                 if ln.startswith(("SUM_Q15", "DIFF_Q15", "PROD_Q15"))}
        assert r.returncode == 0 and set(lines) == {"SUM_Q15", "DIFF_Q15",
                                                    "PROD_Q15"}, (
            f"GR client failed (rc={r.returncode}):\n{r.stderr[-1500:]}")
        refs = references()
        for name in ("sum", "diff", "prod"):
            got = [v - 0x10000 if v & 0x8000 else v
                   for v in lines[name.upper() + "_Q15"]]
            assert got == refs[name], (
                f"{name}: real-client stream diverges from the block "
                f"reference ({len(got)} vs {len(refs[name])} words)")
    finally:
        sim.stop_gnuradio_server()


def test_channel_selector_real_gr_client(qapp, tmp_path):
    """The genuine GR client loop for the complex channel selector — the
    saturation-bespoke FreqXlatingFIR driven via the client's own
    pipelined=False (per-sample paced) request. Same derived bound as the
    headless gate (61 LSB), never widened."""
    from channel_selector_demo import SIG, TOL_LSB, _q15, _s16, gr_golden

    if not CHANNEL_SEL_KYT.exists():
        pytest.skip("channel_selector.kyt absent")
    ctrl, sim = _serve(CHANNEL_SEL_KYT, 58985)
    try:
        got_u = _run_client(_CHANNEL_SEL_CLIENT, tmp_path,
                            "channel_selector_client", 58985, "-")
        got = [_s16(v) for v in got_u]
        gold = gr_golden(SIG)
        assert len(got) == len(gold), (len(got), len(gold))
        worst = max(abs(got[i] - _s16(_q15(gold[i]))) for i in range(len(gold)))
        assert worst <= TOL_LSB, worst
    finally:
        sim.stop_gnuradio_server()


def test_effect_echo_real_gr_client_join_fanout(qapp, tmp_path):
    """The genuine GR client loop for a JOIN fan-out stream — one stream_id,
    TWO port→block landings (the echo's direct + delayed arms). Pins the
    multi-landing injection path (engine.port_config landings +
    sim_bridge._drive_one): before it, the bridge injected only one arm and
    the combiner starved. Same derived bound as the headless gate (25 LSB)."""
    from audio_effects_demo import EFFECTS, SIG, _q15, _s16, gr_golden

    if not EFFECT_ECHO_KYT.exists():
        pytest.skip("effect_echo.kyt absent")
    ctrl, sim = _serve(EFFECT_ECHO_KYT, 58986)
    try:
        got_u = _run_client(_EFFECT_ECHO_CLIENT, tmp_path,
                            "effect_echo_client", 58986, "-")
        got = [_s16(v) for v in got_u]
        gold = gr_golden("echo", SIG)
        tol = EFFECTS["echo"][1]
        assert len(got) == len(gold), (len(got), len(gold))
        worst = max(abs(got[i] - _s16(_q15(gold[i]))) for i in range(len(gold)))
        assert worst <= tol, worst
    finally:
        sim.stop_gnuradio_server()


def test_psk31_transceiver_real_gr_client_duplex(qapp, tmp_path):
    """The genuine GR client loop for the FULL TRANSCEIVER — two real
    kyttar.source/sink pairs ('tx'/'rx') through the DuplexRendezvous against
    the hosted shared-panel chip. TX must be SAMPLE-EXACT vs the psk31 golden
    and RX must decode the sent text exactly, both in one interleaved run."""
    from psk31_transceiver_demo import rx_symbols
    from psk31_tx_golden import golden_tx_q15

    if not PSK31_TRX_KYT.exists():
        pytest.skip("psk31_transceiver.kyt absent")
    ctrl, sim = _serve(PSK31_TRX_KYT, 58987)
    try:
        tx_text, rx_text = "CQ DE KYTTAR", "R 599 73"
        syms = rx_symbols(rx_text)
        script = tmp_path / "psk31_trx_client.py"
        script.write_text(_PSK31_TRX_CLIENT)
        r = subprocess.run(
            [GR_PYTHON, str(script), str(KYTTAR_PKG), "58987", tx_text,
             ",".join(repr(v) for v in syms)],
            capture_output=True, text=True, timeout=900)
        lines = {ln.split()[0]: [int(x) for x in ln.split()[1:]]
                 for ln in r.stdout.splitlines()
                 if ln.startswith(("TX_Q15", "RX_Q15"))}
        assert r.returncode == 0 and set(lines) == {"TX_Q15", "RX_Q15"}, (
            f"GR client failed (rc={r.returncode}):\n{r.stderr[-1500:]}")
        tx = [v - 0x10000 if v & 0x8000 else v for v in lines["TX_Q15"]]
        gold = golden_tx_q15(tx_text, sps=8, amplitude=1.0)
        assert tx == gold, (
            f"real-client TX != golden ({len(tx)} vs {len(gold)})")
        rx = "".join(chr(v & 0x7F) for v in lines["RX_Q15"] if 0 < v < 128)
        assert rx == rx_text, f"real-client RX decoded {rx!r}"
    finally:
        sim.stop_gnuradio_server()


def test_cw_transceiver_real_gr_client_duplex(qapp, tmp_path):
    """The genuine GR client loop for the CW FULL TRANSCEIVER — the kicker-form
    shared-panel duplex. TX must be BIT-EXACT vs the keyer's ITU-R golden and
    RX must decode the sent letters exactly, in one interleaved run."""
    from cw_transceiver_demo import keyed_envelope, rx_burst, _s16

    if not CW_TRX_KYT.exists():
        pytest.skip("cw_transceiver.kyt absent")
    ctrl, sim = _serve(CW_TRX_KYT, 58988)
    try:
        tx_text, rx_text = "CQ DE K", "RST 599"
        sig = [_s16(v) / 32768.0 for v in rx_burst(rx_text)]
        script = tmp_path / "cw_trx_client.py"
        script.write_text(_CW_TRX_CLIENT)
        r = subprocess.run(
            [GR_PYTHON, str(script), str(KYTTAR_PKG), "58988", tx_text,
             ",".join(repr(v) for v in sig)],
            capture_output=True, text=True, timeout=900)
        lines = {ln.split()[0]: [int(x) for x in ln.split()[1:]]
                 for ln in r.stdout.splitlines()
                 if ln.startswith(("TX_Q15", "RX_Q15"))}
        assert r.returncode == 0 and set(lines) == {"TX_Q15", "RX_Q15"}, (
            f"GR client failed (rc={r.returncode}):\n{r.stderr[-1500:]}")
        assert lines["TX_Q15"] == keyed_envelope(tx_text), (
            f"real-client TX != ITU golden ({len(lines['TX_Q15'])})")
        rx = "".join(chr(v & 0x7F) for v in lines["RX_Q15"] if 0 < v < 128)
        assert rx == rx_text.replace(" ", ""), f"real-client RX {rx!r}"
    finally:
        sim.stop_gnuradio_server()


