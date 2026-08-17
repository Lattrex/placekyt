"""
Kyttar Rational Resampler Block for GNURadio

Resample by interpolation/decimation (L/M) through one polyphase FIR — the
GNU Radio ``filter.rational_resampler_fff`` drop-in.

GR marker; the real DSP runs on the placeKYT-hosted chip. This block keeps the
exact GR interface (class name, params, ports) so it places/wires identically
in GRC, but does NO in-process placement and streams pure pass-through (the
actual resampling runs on the chip).

Copyright 2026 Kyttar Computer Project.
SPDX-License-Identifier: GPL-3.0-or-later
"""

from typing import List

from .dsp_markers import _PassThrough


class rational_resampler(_PassThrough):
    """
    Kyttar Rational Resampler - polyphase L/M resampling FIR

    Implements GNU Radio's rational_resampler_fff on the chip. GR marker; the
    real DSP runs on the placeKYT-hosted chip (this GR block passes samples
    through).

    Parameters:
        device_id: ID of the kyttar.device to use
        interpolation: L (>= 1)
        decimation: M (>= 1)
        taps: FIR taps; EMPTY selects GR's auto-designed Kaiser anti-image
            low-pass (which exceeds the single-cell chip budget and raises at
            placement with a composed Upsampler -> FIR workaround)
        fractional_bw: transition band fraction, only used when taps is empty
            (<= 0 selects GR's default 0.4; must be < 0.5)
    """

    def __init__(
        self,
        device_id: str = "kyttar_0",
        interpolation: int = 1,
        decimation: int = 1,
        taps: List[float] = None,
        fractional_bw: float = 0.0,
    ):
        super().__init__(name="Kyttar Rational Resampler", n_in=1, n_out=1)

        self._device_id = device_id
        self._interpolation = int(interpolation)
        self._decimation = int(decimation)
        self._taps = list(taps or [])
        self._fractional_bw = float(fractional_bw)
        # Advertise params for GRC<->placeKYT sync detection (see dsp_markers).
        self._advertise_grc_params(
            device_id, "RationalResamplerBlock",
            {"interpolation": self._interpolation,
             "decimation": self._decimation,
             "taps": self._taps,
             "fractional_bw": self._fractional_bw})

    def set_taps(self, taps: List[float]):
        """Set filter taps."""
        self._taps = list(taps or [])
