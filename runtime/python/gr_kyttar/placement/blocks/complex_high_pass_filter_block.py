# SPDX-License-Identifier: GPL-3.0-or-later
"""ComplexHighPassFilter — see :class:`ComplexHighPassFilter`."""
from typing import List

from . import _firdes
from .complex_fir_filter_block import ComplexFIRFilterBlock


class ComplexHighPassFilter(ComplexFIRFilterBlock):
    """
    Complex High Pass Filter — GNU Radio ``filter.fir_filter_ccf`` fed
    ``filter.firdes.high_pass(...)`` taps (GRC's **High Pass Filter** with a complex
    stream). The complex counterpart of :class:`HighPassFilter`: firdes designs the
    real high-pass taps and :class:`ComplexFIRFilterBlock` runs them on both I/Q
    rails. Parameters mirror GRC's **High Pass Filter** verbatim.

    HARDWARE NOTE — a multi-cell complex FIR needs ``Σ|h|≤1`` (see
    :class:`ComplexFIRFilterBlock`); a high-pass at ``gain=1.0`` can exceed that, so
    reduce ``gain`` if the block reports the multi-cell overflow.
    """
    CATEGORY = "filtering"
    TAGS = ["complex_high_pass", "highpass", "complex", "fir", "filter", "firdes",
            "filtering"]

    def __init__(self, name: str, gain: float = 1.0, samp_rate: float = 32000.0,
                 cutoff_freq: float = 4000.0, transition_width: float = 2000.0,
                 window: str = "hamming", beta: float = 6.76):
        self._gain = float(gain)
        self._samp_rate = float(samp_rate)
        self._cutoff_freq = float(cutoff_freq)
        self._transition_width = float(transition_width)
        self._window = window
        self._beta = float(beta)
        taps = _firdes.high_pass(self._gain, self._samp_rate, self._cutoff_freq,
                                 self._transition_width, self._window, self._beta)
        super().__init__(name, coefficients=taps)

    @property
    def design_taps(self) -> List[float]:
        """The firdes-designed float taps (before Q15 quantization)."""
        return list(self._coefficients)
