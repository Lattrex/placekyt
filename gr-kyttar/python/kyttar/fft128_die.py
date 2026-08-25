"""
Kyttar FFT128 two-die blocks for GNURadio

N=128 does not fit ONE die: the 7-stage ctl/out spine needs 14 rows in a single
column against a 12-row array. The supported topology is a STAGE-BOUNDARY
SPLIT across two chips, cut after stage 0:

    fft128_die0  -- stage 0 alone (the period-64 octant fold), 30 cells
    fft128_die1  -- stages 1..6, 84 cells

Chained die0 -> die1 they compute the whole 128-point transform: the
composition identity ``whole(x) == die1(die0(x))`` holds word for word,
because R2SDF stages are a pure feed-forward pipeline whose only feedback is
inside a stage.

A SINGLE DIE'S OUTPUT IS NOT FREQUENCY BINS. die0 emits a partially
transformed stream — the thing die1 consumes. Only die1's output, with die0
upstream of it, carries the transform's bins.

GR markers; the real DSP runs on the placeKYT-hosted chips. These keep the
exact GR interface (complex ports, 1:1 rate) so they place and wire
identically in GRC, but do NO in-process computation.

Copyright 2026 Kyttar Computer Project.
SPDX-License-Identifier: GPL-3.0-or-later
"""

import numpy as np

from .dsp_markers import _PassThrough


class fft128_die0(_PassThrough):
    """
    Kyttar FFT128 die 0 — stage 0 of the 128-point R2SDF transform.

    COMPLEX in / COMPLEX out, one output per input. Contributes 64 of the
    transform's 127 samples of latency. Its output is a PARTIALLY transformed
    stream, not frequency bins — wire it to ``fft128_die1`` on the next chip.

    Parameters:
        device_id: ID of the kyttar.device to use
    """

    def __init__(self, device_id: str = "kyttar_0"):
        super().__init__(name="Kyttar FFT128 die0", n_in=1, n_out=1,
                         in_dtype=np.complex64, out_dtype=np.complex64)
        self._device_id = device_id
        self._advertise_grc_params(device_id, "FFT128Die0", {})


class fft128_die1(_PassThrough):
    """
    Kyttar FFT128 die 1 — stages 1..6 of the 128-point R2SDF transform.

    COMPLEX in / COMPLEX out, one output per input. Consumes ``fft128_die0``'s
    output stream and emits the transform's bins in BIT-REVERSED order at
    scale FFT/128. Contributes 63 of the transform's 127 samples of latency.

    Parameters:
        device_id: ID of the kyttar.device to use
    """

    def __init__(self, device_id: str = "kyttar_0"):
        super().__init__(name="Kyttar FFT128 die1", n_in=1, n_out=1,
                         in_dtype=np.complex64, out_dtype=np.complex64)
        self._device_id = device_id
        self._advertise_grc_params(device_id, "FFT128Die1", {})
