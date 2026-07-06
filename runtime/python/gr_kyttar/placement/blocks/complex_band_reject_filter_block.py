# SPDX-License-Identifier: GPL-3.0-or-later
"""ComplexBandRejectFilter — see :class:`ComplexBandRejectFilter`."""
from typing import List

from . import _firdes
from .complex_fir_filter_block import ComplexFIRFilterBlock


class ComplexBandRejectFilter(ComplexFIRFilterBlock):
    """
    Complex Band Reject (notch) Filter — GNU Radio ``filter.fir_filter_ccf`` fed
    ``filter.firdes.band_reject(...)`` taps (GRC's **Band Reject Filter** with a
    complex stream). The complex counterpart of :class:`BandRejectFilter`: firdes
    designs the real band-reject taps and :class:`ComplexFIRFilterBlock` runs them
    on both I/Q rails. Parameters mirror GRC's **Band Reject Filter** verbatim.

    HARDWARE NOTE — a multi-cell complex FIR needs ``Σ|h|≤1`` (see
    :class:`ComplexFIRFilterBlock`). A band-reject filter's DC tap dominates so
    ``Σ|h|`` is typically already ≤1 at ``gain=1.0``, but reduce ``gain`` if the
    block reports the multi-cell overflow.
    """
    CATEGORY = "filtering"
    TAGS = ["complex_band_reject", "bandreject", "notch", "complex", "fir",
            "filter", "firdes", "filtering"]

    def __init__(self, name: str, gain: float = 1.0, samp_rate: float = 32000.0,
                 low_cutoff_freq: float = 4000.0, high_cutoff_freq: float = 8000.0,
                 transition_width: float = 2000.0, window: str = "hamming",
                 beta: float = 6.76):
        self._gain = float(gain)
        self._samp_rate = float(samp_rate)
        self._low_cutoff_freq = float(low_cutoff_freq)
        self._high_cutoff_freq = float(high_cutoff_freq)
        self._transition_width = float(transition_width)
        self._window = window
        self._beta = float(beta)
        taps = _firdes.band_reject(self._gain, self._samp_rate,
                                   self._low_cutoff_freq, self._high_cutoff_freq,
                                   self._transition_width, self._window, self._beta)
        super().__init__(name, coefficients=taps)

    @property
    def design_taps(self) -> List[float]:
        """The firdes-designed float taps (before Q15 quantization)."""
        return list(self._coefficients)
