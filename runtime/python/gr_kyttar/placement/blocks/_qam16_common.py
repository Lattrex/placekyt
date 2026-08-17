# SPDX-License-Identifier: GPL-3.0-or-later
"""Shared 16-QAM constellation constants — the EXACT GNU Radio
``digital.constellation_16qam()`` point table, used by the QAM16 mapper/slicer.

The Kyttar 16-QAM blocks are 1:1 drop-ins for GNU Radio's ``constellation_16qam()``
(the standard ``digital.constellation_modulator`` / ``constellation_decoder_cb`` path),
so the on-chip constellation MUST be GR's, bit-for-bit — not an invented separable Gray
grid. ``constellation_16qam()`` is a {±1,±3}/sqrt(10) rectangular grid, but its
bit->point assignment is an idiosyncratic permutation, NOT ``(I_bits<<2)|Q_bits``.

The table below is the value of ``digital.constellation_16qam().points()`` (index 0..15
-> complex point), captured from GNU Radio and quantized to Q15. The provenance is
pinned by ``verification/tests/test_qam16_mapper.py`` /
``test_qam16_slicer.py``, which re-derive it from GR itself (via ``KYTTAR_GR_PYTHON``)
and assert this table matches — so a GR version bump that changed the map would fail the
gate, never silently drift.

    idx : (I, Q) in units of {-3,-1,+1,+3}/sqrt(10)
      0:(+1,-1)  1:(-1,-1)  2:(+3,-3)  3:(-3,-3)  4:(-3,-1)  5:(+3,-1)
      6:(-1,-3)  7:(+1,-3)  8:(-3,+3)  9:(+3,+3) 10:(-1,+1) 11:(+1,+1)
     12:(+1,+3) 13:(-1,+3) 14:(+3,+1) 15:(-3,+1)
"""
from ._base import float_to_q15

_NORM = 1.0 / (10.0 ** 0.5)  # 1/sqrt(10) ~= 0.31623

# GR constellation_16qam().points(), index 0..15 -> (I_level, Q_level) in {-3,-1,1,3}.
# (This is exactly what GNU Radio returns; see the module docstring for provenance.)
_QAM16_LEVELS = [
    (+1, -1), (-1, -1), (+3, -3), (-3, -3),
    (-3, -1), (+3, -1), (-1, -3), (+1, -3),
    (-3, +3), (+3, +3), (-1, +1), (+1, +1),
    (+1, +3), (-1, +3), (+3, +1), (-3, +1),
]

# 4-PAM decision levels (ascending) and the level->2bit index used by the slicer's
# per-axis slice: level -3 -> 0, -1 -> 1, +1 -> 2, +3 -> 3.
_QAM16_PAM_ASC = [-3, -1, 1, 3]
_QAM16_NORM = _NORM


def qam16_points_q15():
    """Return the GR constellation_16qam() points as [(I_q15, Q_q15)], index 0..15."""
    return [(float_to_q15(i * _NORM) & 0xFFFF, float_to_q15(q * _NORM) & 0xFFFF)
            for (i, q) in _QAM16_LEVELS]


def qam16_level_lut():
    """Return the 16-entry LUT mapping ``I_lvl*4 + Q_lvl`` (each 0..3, ascending
    levels -3,-1,+1,+3) to the GR symbol index — the separable form of
    ``decision_maker`` (verified equal to GR over the whole plane)."""
    lvl_to_idx = {-3: 0, -1: 1, 1: 2, 3: 3}
    lut = [None] * 16
    for sym, (i, q) in enumerate(_QAM16_LEVELS):
        lut[lvl_to_idx[i] * 4 + lvl_to_idx[q]] = sym
    return lut


def qam16_sign_outer_lut():
    """Return the 16-entry LUT keyed by the per-axis (sign, outer) bits:

        key = (Isign<<3) | (Iouter<<2) | (Qsign<<1) | Qouter

    where ``sign = (v >= 0)`` and ``outer = (|v| >= 2/sqrt(10))``. LUT[key] is the GR
    ``constellation_16qam().decision_maker()`` symbol index — VERIFIED equal to GR over
    the whole plane. This is the slicer's form: each axis is two branchless tests
    (a sign test + a magnitude-vs-threshold test) building 2 key bits, then one
    LOAD-indirect lookup — no per-axis level arithmetic, so the whole slice fits one
    cell (the compact idiom the legacy QPSK/BPSK slicers use)."""
    def level(sign, outer):
        if sign == 0:
            return -3 if outer else -1
        return 3 if outer else 1
    lvl = qam16_level_lut()
    lvl_to_idx = {-3: 0, -1: 1, 1: 2, 3: 3}
    lut = [None] * 16
    for isign in (0, 1):
        for iouter in (0, 1):
            for qsign in (0, 1):
                for qouter in (0, 1):
                    key = (isign << 3) | (iouter << 2) | (qsign << 1) | qouter
                    il = lvl_to_idx[level(isign, iouter)]
                    ql = lvl_to_idx[level(qsign, qouter)]
                    lut[key] = lvl[il * 4 + ql]
    return lut
