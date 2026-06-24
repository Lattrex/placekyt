"""DCBlockerBlock — see :class:`DCBlockerBlock`."""
import numpy as np
from typing import List
from .fir_filter_block import FIRFilterBlock


def _dc_blocker_taps(length: int, long_form: bool) -> List[float]:
    """Impulse response of GNU Radio's ``filter.dc_blocker_ff(length, long_form)``.

    GR's DC blocker is LINEAR and TIME-INVARIANT — it is a symmetric FIR. It
    subtracts a cascade of length-``D`` moving averagers (each an ``MA_D``,
    impulse ``1/D`` for ``D`` taps) from a delayed copy of the input:

      * SHORT form  (``long_form=False``): ``y[n] = x[n-(D-1)] - MA_D²(x)[n]`` —
        TWO cascaded moving averagers (a triangular kernel, length ``2D-1``,
        group delay ``D-1``).
      * LONG form   (``long_form=True``):  ``y[n] = x[n-(2D-2)] - MA_D⁴(x)[n]`` —
        FOUR cascaded moving averagers (length ``4D-3``, group delay ``2D-2``).

    Both kernels have unit DC gain on the subtracted MA term and a delayed unit
    impulse, so ``Σ taps = 0`` (a true DC notch). The taps are symmetric, so the
    FIR datapath's coefficient order (and GR's reversed-tap convention) is moot.

    Reverse-engineered from GR's impulse/step response and verified bit-for-bit
    against ``filter.dc_blocker_ff`` for D∈{2,4,8,16,32}, both forms.
    """
    D = int(length)
    if D < 1:
        raise ValueError(f"dc_blocker length must be >= 1, got {length}")
    box = np.ones(D, dtype=np.float64) / D
    ma = box.copy()
    for _ in range((4 if long_form else 2) - 1):
        ma = np.convolve(ma, box)
    group_delay = (len(ma) - 1) // 2          # 2D-2 (long) or D-1 (short)
    taps = -ma
    taps[group_delay] += 1.0                   # delayed unit impulse minus the MA
    return [float(t) for t in taps]


class DCBlockerBlock(FIRFilterBlock):
    """
    DC Blocker — drop-in for GNU Radio ``filter.dc_blocker_ff``.

    A computationally-efficient high-pass that notches DC. It is an LTI filter
    (a symmetric FIR), so it is implemented by REUSING the verified
    :class:`FIRFilterBlock` datapath with the dc-blocker impulse response as its
    coefficients (see :func:`_dc_blocker_taps`).

    Parameters mirror GNU Radio's GRC ``dc_blocker_xx`` block VERBATIM:

      * ``length`` (GR ``D``): the moving-averager delay-line length. Longer →
        narrower DC notch. Default 32 (GR's default).
      * ``long_form``: ``True`` (default) uses the long form (flatter passband,
        group delay ``2D-2``, four cascaded averagers → ``4D-3`` taps);
        ``False`` the short form (group delay ``D-1``, two averagers → ``2D-1``
        taps).

    Fixed-point notes (inherited from FIRFilterBlock):
      * The dc-blocker taps have ``Σ|h| ≈ 1.5..2`` (a delayed impulse plus a
        unit-gain MA), so COEFFICIENT HEADROOM (INV-13) engages with shift
        ``S=1``: the coefficients are pre-scaled by ``1/2`` so the Q15
        accumulator can never overflow, then the gain is restored at the END with
        ONE saturating left shift. The block thus SATURATES on overload (no
        rollover), like every production fixed-point filter.
      * That ``S=1`` scaling costs ~1 bit of coefficient precision, so the
        DUT-vs-GR amplitude tolerance is the HEADROOM-AWARE Q15 floor
        ``N·(2^(S-1)+1)+1`` LSB (``q15_quant_floor(N, head_shift=S)``), NOT the
        plain ``N+1``. This is a derived fixed-point worst case, not a loosened
        gate.

    Geometry scales with the tap count exactly as FIRFilterBlock: a small
    ``length`` (e.g. ``length=2, long_form=False`` → 3 taps) is a single cell; the
    GR default ``length=32, long_form=True`` is 125 taps ≈ 26 cells (well inside
    the ~200-tap routing capacity of the 10x12 array).
    """
    CATEGORY = "signal_conditioning"
    TAGS = ["dc_blocker", "highpass", "filter", "signal_conditioning"]

    def __init__(self, name: str, length: int = 32, long_form: bool = True):
        """
        Initialize the DC blocker.

        Args:
            name: Block name.
            length: GR ``D`` — the moving-averager delay-line length (default 32).
            long_form: GR ``long_form`` — long (True, default) or short form.
        """
        self._length = int(length)
        self._long_form = bool(long_form)
        taps = _dc_blocker_taps(self._length, self._long_form)
        super().__init__(name, coefficients=taps)

    @property
    def length(self) -> int:
        return self._length

    @property
    def long_form(self) -> bool:
        return self._long_form
