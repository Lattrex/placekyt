# SPDX-License-Identifier: GPL-3.0-or-later
"""MovingAverageBlock — see :class:`MovingAverageBlock`."""
from typing import List

from .fir_filter_block import FIRFilterBlock


class MovingAverageBlock(FIRFilterBlock):
    """
    Moving Average (box filter) — drop-in for GNU Radio ``blocks.moving_average_ff``:
    ``out[n] = scale · Σ_{k=0}^{length-1} x[n-k]`` (a running sum of the last
    ``length`` samples, times ``scale``).

    A moving average IS an FIR whose ``length`` taps are all equal to ``scale``
    (``Σ coeff[k]·x[n-k] = scale·Σ x[n-k]``), so this SUBCLASSES the verified
    :class:`FIRFilterBlock` with constant taps — all the Q15 datapath / multi-cell
    fold / COEFFICIENT-HEADROOM saturation machinery inherited unchanged (exactly
    the LowPassFilter pattern). The constant taps are symmetric, so the FIR's
    reversed-tap convention is moot and the group delay is 0 (aligned with GR's
    causal running sum).

    Parameters mirror GRC's **Moving Average** block:

      * ``length`` — the window length (number of samples averaged).
      * ``scale``  — the multiplier applied to the running sum. Use ``1/length``
        for a true average (then ``Σ|tap| = 1`` ⇒ no coefficient headroom, S=0); a
        larger scale engages the inherited saturating headroom restore (S>0).

    (GR's ``max_iter`` is an internal output-buffer bound and ``vlen`` is vector
    length 1 — neither affects the sample math, so neither is a Kyttar param.)
    """
    CATEGORY = "filtering"
    TAGS = ["moving_average", "moving_average_ff", "box_filter", "smoother",
            "fir", "filtering"]

    def __init__(self, name: str, length: int = 4, scale: float = 0.25):
        if int(length) < 1:
            raise ValueError(f"length must be >= 1, got {length}")
        self._length = int(length)
        self._scale = float(scale)
        taps = [self._scale] * self._length
        super().__init__(name, coefficients=taps)

    @property
    def length(self) -> int:
        return self._length

    @property
    def scale(self) -> float:
        return self._scale

    @property
    def design_taps(self) -> List[float]:
        """The constant box taps ([scale]*length) before Q15 quantization."""
        return list(self._coefficients)
