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
from .chip_batch import chip_batch
from .dsp_markers import (complex_rrc_matched_filter, complex_costas_loop,
                          gardner_timing_recovery, mm_timing_recovery,
                          fll_band_edge,
                          lms_equalizer, complex_to_mag, complex_to_arg,
                          bpsk_slicer, qpsk_slicer,
                          psk_symbol_mapper, upsampler, repeat, complex_upsampler,
                          complex_gain, multiply_const_complex, rrc_pulse_shaper,
                          unpack_k_bits, not_bb, map_bb, char_to_float,
                          iq_upconvert, complex_to_float,
                          frequency_modulator, quadrature_demod,
                          fsk4_symbol_mapper, fsk4_slicer,
                          fsk4_sync_timing_recovery, complex_to_real,
                          qam16_symbol_mapper, qam16_slicer, qam16_costas_loop,
                          abs_bb, splitter, conjugate, complex_to_imag,
                          complex_to_mag_squared, float_to_complex,
                          dual_float_to_complex, keep_one_in_n, moving_average,
                          zero_crossing_rate, bin_argmax,
                          rms, rms_cf, sqrt, feature_pair_join, tmr_voter,
                          svpwm,
                          r2_butterfly, twiddle_multiply,
                          sigmoid, tanh,
                          chirp_symbol_mapper, chirp_generator,
                          conj_chirp_mixer, chirp_sync,
                          gru_cell)
from .gain import gain
from .multiply import multiply
from .add import add, subtract
from .add_cc import add_cc, sub_cc
from .multiply_cc import multiply_cc
from .add_const import add_const
from .xor import xor
from .xor_join import xor_join
from .float_to_char import float_to_char
from .fir_filter import fir_filter
from .complex_fir_filter import complex_fir_filter
from .low_pass_filter import low_pass_filter
from .high_pass_filter import high_pass_filter
from .band_pass_filter import band_pass_filter
from .band_reject_filter import band_reject_filter
from .complex_low_pass_filter import complex_low_pass_filter
from .complex_high_pass_filter import complex_high_pass_filter
from .complex_band_pass_filter import complex_band_pass_filter
from .complex_band_reject_filter import complex_band_reject_filter
from .dc_blocker import dc_blocker
from .delay import delay
from .complex_delay_line import complex_delay_line
from .fft16 import fft16
from .fft32 import fft32
from .fft64 import fft64
from .fft128_die import fft128_die0, fft128_die1
from .agc import agc
from .agc_cc import agc_cc
from .nco import nco
from .complex_mixer import complex_mixer
from .freq_xlating_fir import freq_xlating_fir
from .demux import demux
from .mux import mux
from .iir_biquad import iir_biquad
from .decimator import decimator
from .rational_resampler import rational_resampler
from .squelch import squelch
from .costas_loop import costas_loop
from .soft_demodulator import soft_demodulator
from .viterbi_bmu import viterbi_bmu
from .viterbi_k7 import viterbi_k7
from .lfsr_scrambler import lfsr_scrambler
from .pack_k_bits import pack_k_bits
from .crc16 import crc16
from .chacha20_keystream import chacha20_keystream
from .poly1305_mac import poly1305_mac
from .chacha20_qr import chacha20_qr
from .dot_product_mac import dot_product_mac
from .conv_encoder_k7 import conv_encoder_k7
from .hamming_encoder import hamming_encoder
from .hamming_decoder import hamming_decoder
from .golay_encoder import golay_encoder
from .golay_decoder import golay_decoder
from .block_interleaver import block_interleaver
from .diff_decoder import diff_decoder
from .diff_encoder import diff_encoder
from .and_const import and_const
from .nlog10 import nlog10
# SRAM-backed ham blocks + the SRAM controller (placeKYT-native [Kyttar] blocks;
# no GNU Radio counterpart — still fully placeable in GRC with their params).
from .varicode_encoder import varicode_encoder
from .varicode_decoder import varicode_decoder
from .cw_keyer import cw_keyer
from .cw_decoder import cw_decoder
from .raised_cosine_envelope import raised_cosine_envelope
from .sram_controller import sram_controller
from .lz4_decoder import lz4_decoder
from .lz4_encoder import lz4_encoder

__version__ = "1.9.0"
__all__ = [
    # Core blocks
    "device",
    "source",
    "sink",
    "rx_batch",
    "chip_batch",
    "complex_rrc_matched_filter",
    "complex_costas_loop",
    "gardner_timing_recovery",
    "mm_timing_recovery",
    "fll_band_edge",
    "lms_equalizer",
    "complex_to_mag",
    "complex_to_arg",
    "bpsk_slicer",
    "qpsk_slicer",
    "psk_symbol_mapper",
    "fsk4_symbol_mapper",
    "fsk4_slicer",
    "fsk4_sync_timing_recovery",
    "qam16_symbol_mapper",
    "qam16_slicer",
    "qam16_costas_loop",
    "complex_to_real",
    "upsampler",
    "repeat",
    "unpack_k_bits",
    "complex_upsampler",
    "complex_gain",
    "multiply_const_complex",
    "rrc_pulse_shaper",
    "iq_upconvert",
    "frequency_modulator",
    "quadrature_demod",
    # Simple converters / ops
    "abs_bb",
    "splitter",
    "conjugate",
    "complex_to_imag",
    "complex_to_mag_squared",
    "float_to_complex",
    "dual_float_to_complex",
    "keep_one_in_n",
    "moving_average",
    "zero_crossing_rate",
    "bin_argmax",
    "sigmoid",
    "tanh",
    "chirp_symbol_mapper",
    "chirp_generator",
    "conj_chirp_mixer",
    "chirp_sync",
    "gru_cell",
    "rms",
    "rms_cf",
    "sqrt",
    "feature_pair_join",
    "tmr_voter",
    "svpwm",
    "r2_butterfly",
    "twiddle_multiply",
    # Routing primitives
    "demux",
    "mux",
    # DSP blocks
    "gain",
    "multiply",
    "add",
    "subtract",
    "add_cc",
    "sub_cc",
    "multiply_cc",
    "add_const",
    "xor",
    "xor_join",
    "float_to_char",
    "fir_filter",
    "complex_fir_filter",
    "low_pass_filter",
    "high_pass_filter",
    "band_pass_filter",
    "band_reject_filter",
    "complex_low_pass_filter",
    "complex_high_pass_filter",
    "complex_band_pass_filter",
    "complex_band_reject_filter",
    "dc_blocker",
    "delay",
    "complex_delay_line",
    "fft16",
    "fft32",
    "fft64",
    "fft128_die0",
    "fft128_die1",
    "agc",
    "agc_cc",
    "nco",
    "complex_mixer",
    "freq_xlating_fir",
    "iir_biquad",
    "decimator",
    "rational_resampler",
    "squelch",
    # Synchronization blocks
    "costas_loop",
    # FEC blocks
    "soft_demodulator",
    "viterbi_bmu",
    "viterbi_k7",
    "lfsr_scrambler",
    "pack_k_bits",
    "crc16",
    "chacha20_keystream",
    "poly1305_mac",
    "chacha20_qr",
    "dot_product_mac",
    "conv_encoder_k7",
    "hamming_encoder",
    "hamming_decoder",
    "golay_encoder",
    "golay_decoder",
    "block_interleaver",
    "diff_decoder",
    "diff_encoder",
    "and_const",
    "nlog10",
    # SRAM-backed ham blocks + SRAM controller ([Kyttar]-native, no GR counterpart)
    "varicode_encoder",
    "varicode_decoder",
    "cw_keyer",
    "cw_decoder",
    "raised_cosine_envelope",
    "sram_controller",
    "lz4_decoder",
    "lz4_encoder",
    # Registry (internal)
    "KyttarRegistry",
    "get_registry",
    "DeviceType",
]
