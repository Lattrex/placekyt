# SPDX-License-Identifier: GPL-3.0-or-later
"""LowPassFilter — see :class:`LowPassFilter`."""
from typing import List

from . import _firdes
from .fir_filter_block import FIRFilterBlock


class LowPassFilter(FIRFilterBlock):
    """
    Low Pass Filter — drop-in for GNU Radio's ``filter.fir_filter_fff`` fed taps
    from ``filter.firdes.low_pass(...)`` (GRC's **Low Pass Filter** block).

    A convenience FIR: the user specifies the filter in DSP units (gain, sample
    rate, cutoff frequency, transition width, window) and the block designs the
    windowed-sinc taps with the SAME algorithm GNU Radio's ``firdes`` uses, then
    runs them on the verified :class:`FIRFilterBlock` datapath (all the Q15
    coefficient-headroom / saturation / multi-cell fold machinery inherited
    unchanged). The taps are linear-phase SYMMETRIC, so the FIR's reversed-tap
    convention is moot.

    Parameters mirror GRC's **Low Pass Filter** VERBATIM (firdes order):

      * ``gain``            — passband gain (DC gain), default 1.0.
      * ``samp_rate``       — sample rate in Hz.
      * ``cutoff_freq``     — passband-edge cutoff in Hz.
      * ``transition_width``— transition-band width in Hz (sets the tap count).
      * ``window``          — design window: ``hamming`` (GR default), ``hann``,
        ``blackman``, ``rectangular``, ``blackman_harris`` or ``kaiser`` (also
        accepts the GR ``firdes.WIN_*`` enum int).
      * ``beta``            — Kaiser window beta (only used for ``window=kaiser``).

    The tap count is ``firdes``' own ``ntaps`` (derived from the window
    attenuation and the transition width), so the footprint scales exactly like a
    hand-specified FIR: a 39-tap low-pass (fs 32k, cutoff 4k, tw 2k, Hamming) is
    an 8-cell wavefront, well inside the 10x12 array's ~200-tap routing capacity.

    Fixed-point parity: the Q15-quantized taps are BIT-EXACT to GR's firdes taps
    quantized identically, so the on-chip filter IS the firdes filter. A
    normalized firdes low-pass has ``Σ|h|`` slightly above 1 (sidelobes), so
    COEFFICIENT HEADROOM (INV-13) typically engages with shift ``S=1`` and the
    block saturates on overload, exactly like FIRFilterBlock / DCBlockerBlock.
    """
    CATEGORY = "filtering"
    TAGS = ["low_pass", "lowpass", "fir", "filter", "firdes", "filtering"]

    def __init__(self, name: str, gain: float = 1.0, samp_rate: float = 32000.0,
                 cutoff_freq: float = 4000.0, transition_width: float = 2000.0,
                 window: str = "hamming", beta: float = 6.76):
        self._gain = float(gain)
        self._samp_rate = float(samp_rate)
        self._cutoff_freq = float(cutoff_freq)
        self._transition_width = float(transition_width)
        self._window = window
        self._beta = float(beta)
        taps = _firdes.low_pass(self._gain, self._samp_rate, self._cutoff_freq,
                                self._transition_width, self._window, self._beta)
        super().__init__(name, coefficients=taps)

    @property
    def design_taps(self) -> List[float]:
        """The firdes-designed float taps (before Q15 quantization)."""
        return list(self._coefficients)
