#
# Copyright 2026 Lattrex.
#
# SPDX-License-Identifier: GPL-3.0-or-later
#

# NOTE: this module does NOT import simkyt or gr_kyttar at load time. The blocks
# here are thin GNU Radio front-ends that stream to a placeKYT-hosted chip over a
# socket; the simulator and the block-build library live in placeKYT's own
# (Python-3.12) process, never in GNU Radio's. That keeps this OOT loadable under
# any system Python with only gnuradio + numpy. (The self-placing `device` block
# imports gr_kyttar/simkyt lazily, only if a flowgraph actually self-places a
# chip — not on import.)

"""
gr-kyttar: GNURadio OOT module for Kyttar Computer

This module provides GNURadio blocks that run DSP algorithms on the
Kyttar Computer asynchronous processor array.

Usage:
    from gnuradio import kyttar

    # Create flowgraph with Kyttar processing
    self.kyttar_device = kyttar.device(device_id="kyttar_0", chip_type="12x12_dev")
    self.kyttar_source = kyttar.source(device_id="kyttar_0", port_name="x16_in")
    self.kyttar_gain = kyttar.gain(device_id="kyttar_0", gain=0.5)
    self.kyttar_sink = kyttar.sink(device_id="kyttar_0", port_name="x16_out")

    # Connect: gr_source -> kyttar_source -> kyttar_gain -> kyttar_sink -> gr_sink
    self.connect((self.gr_source, 0), (self.kyttar_source, 0))
    self.connect((self.kyttar_source, 0), (self.kyttar_gain, 0))
    self.connect((self.kyttar_gain, 0), (self.kyttar_sink, 0))
    self.connect((self.kyttar_sink, 0), (self.gr_sink, 0))

Available blocks:
    - kyttar.device: Device configuration (no signal ports)
    - kyttar.source: Entry point into chip (GR -> Kyttar)
    - kyttar.sink: Exit point from chip (Kyttar -> GR)
    - kyttar.gain: Simple gain/multiplier
    - kyttar.fir_filter: FIR filter with configurable taps
    - kyttar.dc_blocker: DC offset removal (high-pass filter)

Architecture:
    The Kyttar Computer is a 2D array of asynchronous processing cells.
    Data enters through input ports, is processed by programmed cells,
    and exits through output ports. No central clock - cells operate
    independently using handshake protocols.
"""

# Import pybind11 generated symbols (if any C++ blocks exist)
try:
    from .kyttar_python import *
except ModuleNotFoundError:
    pass

# Import registry (for internal coordination)
from .registry import KyttarRegistry, get_registry, DeviceType

# Import Python blocks - these are the public API
from .device import device
from .source import source
from .sink import sink
from .rx_batch import rx_batch
from .dsp_markers import (complex_rrc_matched_filter, complex_costas_loop,
                          gardner_timing_recovery, bpsk_slicer)
from .gain import gain
from .fir_filter import fir_filter
from .dc_blocker import dc_blocker
from .agc import agc
from .nco import nco
from .complex_mixer import complex_mixer
from .demux import demux
from .mux import mux
from .iir_biquad import iir_biquad
from .decimator import decimator
from .squelch import squelch
from .costas_loop import costas_loop
from .soft_demodulator import soft_demodulator
from .viterbi_bmu import viterbi_bmu
from .viterbi_k7 import viterbi_k7
from .lfsr_scrambler import lfsr_scrambler
from .conv_encoder_k7 import conv_encoder_k7
from .block_interleaver import block_interleaver

__version__ = "1.9.0"
__all__ = [
    # Core blocks
    "device",
    "source",
    "sink",
    "rx_batch",
    "complex_rrc_matched_filter",
    "complex_costas_loop",
    "gardner_timing_recovery",
    "bpsk_slicer",
    # Routing primitives
    "demux",
    "mux",
    # DSP blocks
    "gain",
    "fir_filter",
    "dc_blocker",
    "agc",
    "nco",
    "complex_mixer",
    "iir_biquad",
    "decimator",
    "squelch",
    # Synchronization blocks
    "costas_loop",
    # FEC blocks
    "soft_demodulator",
    "viterbi_bmu",
    "viterbi_k7",
    "lfsr_scrambler",
    "conv_encoder_k7",
    "block_interleaver",
    # Registry (internal)
    "KyttarRegistry",
    "get_registry",
    "DeviceType",
]
