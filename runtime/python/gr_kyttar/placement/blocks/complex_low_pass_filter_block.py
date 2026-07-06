# SPDX-License-Identifier: GPL-3.0-or-later
"""ComplexLowPassFilter — see :class:`ComplexLowPassFilter`."""
from typing import List

from . import _firdes
from .complex_fir_filter_block import ComplexFIRFilterBlock


class ComplexLowPassFilter(ComplexFIRFilterBlock):
    """
    Complex Low Pass Filter — drop-in for GNU Radio's ``filter.fir_filter_ccf``
    fed taps from ``filter.firdes.low_pass(...)`` (GRC's **Low Pass Filter** block
    with a complex input/output).

    The complex counterpart of :class:`LowPassFilter`: the user specifies the
    filter in DSP units (gain, sample rate, cutoff, transition width, window) and
    the block designs the SAME windowed-sinc **real** taps GNU Radio's ``firdes``
    produces, then runs them on the verified two-chain
    :class:`ComplexFIRFilterBlock` datapath — one shared tap set filtering the I
    and Q rails independently (exactly ``fir_filter_ccf``'s semantics). Because
    the pair travels as a complex packet (I=R0, Q=R1) there is NO fan-out / split:
    a single ``ComplexMixer → ComplexLowPassFilter → IQUpconvert`` chain is pure
    same-source complex packets.

    Parameters mirror GRC's **Low Pass Filter** VERBATIM (firdes order):

      * ``gain``            — passband gain (DC gain), default 1.0.
      * ``samp_rate``       — sample rate in Hz.
      * ``cutoff_freq``     — passband-edge cutoff in Hz.
      * ``transition_width``— transition-band width in Hz (sets the tap count).
      * ``window``          — design window: ``hamming`` (GR default), ``hann``,
        ``blackman``, ``rectangular``, ``blackman_harris`` or ``kaiser`` (also
        accepts the GR ``firdes.WIN_*`` enum int).
      * ``beta``            — Kaiser window beta (only used for ``window=kaiser``).

    Fixed-point parity: the Q15-quantized taps are BIT-EXACT to GR's firdes taps
    quantized identically, so the on-chip filter IS the firdes filter on both
    rails.

    HARDWARE NOTE — multi-cell needs Σ|h|≤1. A filter whose taps span more than
    one cell runs a saturating-restore emit on the last cell for EACH rail; those
    two restores overflow the 32-word cell budget when ``Σ|h|>1`` (``head_shift``
    engages). A normalized firdes low-pass at ``gain=1.0`` has ``Σ|h|`` slightly
    above 1 (sidelobes), so a *multi-cell* complex low-pass must use ``gain`` a
    touch below 1 (e.g. ``0.9``) to keep ``Σ|h|≤1``; the block raises a clear
    error otherwise rather than silently rescaling. Single-cell filters (≤3 taps)
    have no such restriction. See :class:`ComplexFIRFilterBlock`.
    """
    CATEGORY = "filtering"
    TAGS = ["complex_low_pass", "lowpass", "complex", "fir", "filter", "firdes",
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
        taps = _firdes.low_pass(self._gain, self._samp_rate, self._cutoff_freq,
                                self._transition_width, self._window, self._beta)
        super().__init__(name, coefficients=taps)

    @property
    def design_taps(self) -> List[float]:
        """The firdes-designed float taps (before Q15 quantization)."""
        return list(self._coefficients)
