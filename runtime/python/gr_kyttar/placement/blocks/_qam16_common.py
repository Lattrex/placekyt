"""Shared QAM16 constellation constants (used by the QAM16 mapper/slicer/demapper)."""
from ._base import float_to_q15

_QAM16_NORM = 1.0 / (10.0 ** 0.5)              # 1/sqrt(10) ~= 0.31623
_QAM16_PAM_LEVELS = [-3.0, -1.0, 3.0, 1.0]      # natural index 0..3 -> level
_QAM16_PAM_Q15 = [float_to_q15(L * _QAM16_NORM) for L in _QAM16_PAM_LEVELS]
