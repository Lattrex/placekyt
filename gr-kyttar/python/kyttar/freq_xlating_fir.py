"""
Kyttar Frequency Xlating FIR Filter Block for GNURadio

Workhorse channelizer -- a drop-in for GNU Radio's
filter.freq_xlating_fir_filter_ccf: a frequency shift (a complex mixer with an
NCO) FUSED with a decimating real-tap FIR. Multiplies the complex input by
exp(-j*2*pi*center_freq/sampling_freq*n) (a DOWN-shift), applies the real FIR
taps, and decimates by decimation.

GR marker; the real DSP runs on the placeKYT-hosted chip. This block keeps the
exact GR interface (class name, params, ports) so it places/wires identically in
GRC, but does NO in-process placement and streams pure pass-through.

Copyright 2026 Kyttar Computer Project.
SPDX-License-Identifier: GPL-3.0-or-later
"""

from typing import List

import numpy as np

from .dsp_markers import _PassThrough


class freq_xlating_fir(_PassThrough):
    """
    Kyttar Frequency Xlating FIR -- freq shift (NCO mixer) + decimating FIR.

    out[m] = decimate( FIR( in[n] * exp(-j*2*pi*center_freq/sampling_freq*n) ), M )

    COMPLEX in / COMPLEX out, matching GNU Radio's
    filter.freq_xlating_fir_filter_ccf (see verification/tests/test_freq_xlating_fir.py,
    verified bit-exact vs GR within the NCO-table + FIR floor). GR marker; the real
    DSP runs on the placeKYT-hosted chip.

    Parameters (VERBATIM the GR block's names):
        device_id:     ID of the kyttar.device to use
        decimation:    output decimation factor M (int)
        taps:          real FIR taps (Σ|taps| <= 1 on the chip -- Q15 headroom)
        center_freq:   the frequency shift in Hz
        sampling_freq: sample rate in Hz
    """

    def __init__(
        self,
        device_id: str = "kyttar_0",
        decimation: int = 1,
        taps: List[float] = None,
        center_freq: float = 0.0,
        sampling_freq: float = 32000.0,
        pipeline_lock: bool = False,
    ):
        # COMPLEX -> COMPLEX (freq_xlating_fir_filter_ccf). Explicit dtypes so a real
        # GRC flowgraph wires complex-to-complex (cf. test_grc_block_port_dtypes).
        super().__init__(name="Kyttar Freq Xlating FIR", n_in=1, n_out=1,
                         in_dtype=np.complex64, out_dtype=np.complex64)
        self._device_id = device_id
        if taps is None:
            taps = [1.0]
        self._decimation = int(decimation)
        self._taps = list(taps)
        self._center_freq = center_freq
        self._sampling_freq = sampling_freq
        # pipeline_lock is a HW-only saturation knob (the on-chip block RAISES when
        # True — a documented build-engine gap). The GR marker carries it only so the
        # GRC param round-trips; it has no effect on the pass-through stream.
        self._pipeline_lock = bool(pipeline_lock)
        self._advertise_grc_params(device_id, "FreqXlatingFIRBlock", {
            "decimation": int(decimation), "taps": list(taps),
            "center_freq": center_freq, "sampling_freq": sampling_freq,
            "pipeline_lock": bool(pipeline_lock)})

    def set_center_freq(self, center_freq: float):
        self._center_freq = center_freq

    def get_center_freq(self) -> float:
        return self._center_freq

    def set_taps(self, taps: List[float]):
        self._taps = list(taps)
