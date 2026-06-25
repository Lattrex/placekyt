# SPDX-License-Identifier: GPL-3.0-or-later
"""BandRejectFilter — see :class:`BandRejectFilter`."""
from typing import List

from . import _firdes
from .fir_filter_block import FIRFilterBlock


class BandRejectFilter(FIRFilterBlock):
    """
    Band Reject Filter — drop-in for GNU Radio's ``filter.fir_filter_fff`` fed taps
    from ``filter.firdes.band_reject(...)`` (GRC's **Band Reject Filter** block, a
    band-stop / notch).

    A convenience FIR: the user specifies the stop-band in DSP units (gain, sample
    rate, low/high cutoff, transition width, window) and the block designs the
    windowed-sinc taps with the SAME algorithm GNU Radio's ``firdes`` uses (a
    band-reject is normalized to unity gain at DC), then runs them on the verified
    :class:`FIRFilterBlock` datapath (Q15 coefficient-headroom / saturation /
    multi-cell fold inherited unchanged). The taps are linear-phase SYMMETRIC, so
    the FIR's reversed-tap convention is moot.

    Parameters mirror GRC's **Band Reject Filter** VERBATIM (firdes order):
    ``gain``, ``samp_rate`` (Hz), ``low_cutoff_freq`` (Hz), ``high_cutoff_freq``
    (Hz), ``transition_width`` (Hz), ``window`` (``hamming`` default / ``hann`` /
    ``blackman`` / ``rectangular`` / ``blackman_harris`` / ``kaiser``, also accepts
    the GR ``firdes.WIN_*`` enum int), ``beta`` (Kaiser only).

    Fixed-point parity: the Q15-quantized taps are BIT-EXACT to GR's firdes taps
    quantized identically (INV-16), so the on-chip filter IS the firdes filter. A
    firdes band-reject has ``Σ|h| > 1`` (the large centre tap), so COEFFICIENT
    HEADROOM (INV-13) engages and the block saturates on overload, exactly like
    FIRFilterBlock.
    """
    CATEGORY = "filtering"
    TAGS = ["band_reject", "bandstop", "notch", "fir", "filter", "firdes", "filtering"]

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
        taps = _firdes.band_reject(self._gain, self._samp_rate, self._low_cutoff_freq,
                                   self._high_cutoff_freq, self._transition_width,
                                   self._window, self._beta)
        super().__init__(name, coefficients=taps)

    @property
    def design_taps(self) -> List[float]:
        """The firdes-designed float taps (before Q15 quantization)."""
        return list(self._coefficients)
