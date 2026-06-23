# Copyright 2026 Kyttar Computer Project.
#
# SPDX-License-Identifier: GPL-3.0-or-later

########################################################################
# Python module installation
########################################################################
include(GrPython)

# Install Python files to gnuradio/kyttar (making `from gnuradio import kyttar` work)
GR_PYTHON_INSTALL(
    FILES
    __init__.py
    registry.py
    device.py
    source.py
    sink.py
    gain.py
    fir_filter.py
    dc_blocker.py
    agc.py
    nco.py
    complex_mixer.py
    demux.py
    mux.py
    iir_biquad.py
    decimator.py
    squelch.py
    costas_loop.py
    gardner_ted.py
    soft_demodulator.py
    viterbi_bmu.py
    viterbi_k7.py
    lfsr_scrambler.py
    conv_encoder_k7.py
    block_interleaver.py
    DESTINATION ${GR_PYTHON_DIR}/gnuradio/kyttar
)
