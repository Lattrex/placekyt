# SPDX-License-Identifier: GPL-3.0-or-later
"""HighPassFilter — see :class:`HighPassFilter`."""
from typing import List

from . import _firdes
from .fir_filter_block import FIRFilterBlock


class HighPassFilter(FIRFilterBlock):
    """
    High Pass Filter — drop-in for GNU Radio's ``filter.fir_filter_fff`` fed taps
    from ``filter.firdes.high_pass(...)`` (GRC's **High Pass Filter** block).

    A convenience FIR: the user specifies the filter in DSP units (gain, sample
    rate, cutoff frequency, transition width, window) and the block designs the
    windowed-sinc taps with the SAME algorithm GNU Radio's ``firdes`` uses (a
    high-pass is normalized to unity gain at Nyquist), then runs them on the
    verified :class:`FIRFilterBlock` datapath (Q15 coefficient-headroom /
    saturation / multi-cell fold inherited unchanged). The taps are linear-phase
    SYMMETRIC, so the FIR's reversed-tap convention is moot.

    Parameters mirror GRC's **High Pass Filter** VERBATIM (firdes order): ``gain``,
    ``samp_rate`` (Hz), ``cutoff_freq`` (Hz), ``transition_width`` (Hz),
    ``window`` (``hamming`` default / ``hann`` / ``blackman`` / ``rectangular`` /
    ``blackman_harris`` / ``kaiser``, also accepts the GR ``firdes.WIN_*`` enum
    int), ``beta`` (Kaiser only).

    Fixed-point parity: the Q15-quantized taps are BIT-EXACT to GR's firdes taps
    quantized identically (INV-16), so the on-chip filter IS the firdes filter. A
    firdes high-pass has ``Σ|h| > 1`` (the large centre tap plus the negative
    sidelobes), so COEFFICIENT HEADROOM (INV-13) engages and the block saturates
    on overload, exactly like FIRFilterBlock.
    """
    CATEGORY = "filtering"
    TAGS = ["high_pass", "highpass", "fir", "filter", "firdes", "filtering"]

    def __init__(self, name: str, gain: float = 1.0, samp_rate: float = 32000.0,
                 cutoff_freq: float = 4000.0, transition_width: float = 2000.0,
                 window: str = "hamming", beta: float = 6.76,
                 decimation: int = 1, interpolation: int = 1):
        self._gain = float(gain)
        self._samp_rate = float(samp_rate)
        self._cutoff_freq = float(cutoff_freq)
        self._transition_width = float(transition_width)
        self._window = window
        self._beta = float(beta)
        taps = _firdes.high_pass(self._gain, self._samp_rate, self._cutoff_freq,
                                 self._transition_width, self._window, self._beta)
        super().__init__(name, coefficients=taps,
                         decimation=decimation, interpolation=interpolation)

    @property
    def design_taps(self) -> List[float]:
        """The firdes-designed float taps (before Q15 quantization)."""
        return list(self._coefficients)
