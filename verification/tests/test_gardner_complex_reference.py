# SPDX-License-Identifier: GPL-3.0-or-later
"""GardnerTimingRecovery(complex=True) — Q15 REFERENCE gate.

The 2-rail (I/Q) timing recovery reference must (a) keep its I channel BIT-EXACT to
the shipped real (BPSK) Gardner on the same I stimulus, and (b) recover a QPSK stream
with a fractional timing offset at BER 0. The on-chip complex cells are WIP (build
raises NotImplementedError — this gate covers ONLY the proven reference).
"""
from __future__ import annotations

import math
import random
import sys
from pathlib import Path

import numpy as np
import pytest

ROOT = Path("/home/system/placekyt")
for p in (ROOT / "runtime" / "python",):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

from gr_kyttar.placement import GardnerTimingRecovery  # noqa: E402


def _s16(v):
    v = int(v) & 0xFFFF
    return v - 0x10000 if v & 0x8000 else v


def _rrc(beta, sps, span):
    n = span * sps
    taps = []
    for i in range(n + 1):
        t = (i - n / 2) / sps
        if abs(t) < 1e-8:
            v = 1 - beta + 4 * beta / math.pi
        elif abs(abs(4 * beta * t) - 1.0) < 1e-8:
            v = (beta / math.sqrt(2)) * (
                (1 + 2 / math.pi) * math.sin(math.pi / (4 * beta))
                + (1 - 2 / math.pi) * math.cos(math.pi / (4 * beta)))
        else:
            num = (math.sin(math.pi * t * (1 - beta))
                   + 4 * beta * t * math.cos(math.pi * t * (1 + beta)))
            den = math.pi * t * (1 - (4 * beta * t) ** 2)
            v = num / den
        taps.append(v)
    e = math.sqrt(sum(v * v for v in taps))
    return [v / e for v in taps]


def _shape(syms, taps, sps=2):
    up = []
    for s in syms:
        up.append(s)
        up.extend([0.0] * (sps - 1))
    out = []
    for n in range(len(up)):
        acc = 0.0
        for k in range(len(taps)):
            if 0 <= n - k < len(up):
                acc += taps[k] * up[n - k]
        out.append(acc)
    return out


def _timing_shift(x, toff):
    out = []
    for n in range(len(x) - 1):
        i = n + int(math.floor(toff))
        frac = toff - math.floor(toff)
        out.append(x[i] * (1 - frac) + x[i + 1] * frac
                   if 0 <= i < len(x) - 1 else x[n])
    return out


def test_complex_reference_i_channel_bit_exact():
    """The complex reference's I channel == the real (BPSK) reference on the same I
    stimulus, bit-for-bit (the I-driven timing loop is copied verbatim)."""
    rng = np.random.RandomState(0)
    ivals = (rng.randn(500) * 0.3).astype(np.float32)
    r_real = GardnerTimingRecovery("r").process_reference(ivals)
    r_cplx = GardnerTimingRecovery("c", complex=True).process_reference(
        np.array([complex(x, 0.0) for x in ivals]))
    ci = [int(a) for a, _b in r_cplx]
    m = min(len(r_real), len(ci))
    assert m > 50
    assert [int(r_real[i]) for i in range(m)] == ci[:m], \
        "complex-ref I channel diverged from the real Gardner reference"


def test_complex_reference_recovers_qpsk_ber0():
    """A QPSK stream (RRC 2 sps + fractional timing offset) recovers at BER 0 through
    the complex reference (lag-aligned, QPSK 90-degree-ambiguity tolerant)."""
    random.seed(3)
    N = 300
    tb = [(random.randint(0, 1), random.randint(0, 1)) for _ in range(N)]
    si = [(1 if bi == 0 else -1) / math.sqrt(2) for bi, _ in tb]
    sq = [(1 if bq == 0 else -1) / math.sqrt(2) for _, bq in tb]
    taps = _rrc(0.35, 2, 8)
    xi = _timing_shift(_shape(si, taps), 0.4)
    xq = _timing_shift(_shape(sq, taps), 0.4)
    iq = np.array([complex(a, b) for a, b in zip(xi, xq)])
    out = GardnerTimingRecovery("c", complex=True).process_reference(iq)
    assert len(out) >= N - 5

    rx = [((2 if _s16(b) >= 0 else 0) | (1 if _s16(a) >= 0 else 0))
          for a, b in out]
    tx = [(2 if bq == 0 else 0) | (1 if bi == 0 else 0) for bi, bq in tb]

    def _rot(sym, r):
        i = 1 if sym & 1 else -1
        q = 1 if sym & 2 else -1
        for _ in range(r):
            i, q = -q, i
        return (2 if q >= 0 else 0) | (1 if i >= 0 else 0)

    best = 1.0
    for r in range(4):
        for lag in range(0, 15):
            a = [_rot(x, r) for x in rx[lag:]]
            m = min(len(a), len(tx))
            if m < 60:
                continue
            err = sum(1 for k in range(20, m) if a[k] != tx[k]) / (m - 20)
            best = min(best, err)
    assert best == 0.0, f"complex Gardner reference QPSK BER = {best:.4f} (expected 0)"


def test_complex_on_chip_build_raises_until_wip_lands():
    """The on-chip complex cells are WIP; building must RAISE (never silently ship a
    wrong bitstream). Remove this guard when the on-chip complex path is bit-exact."""
    with pytest.raises(NotImplementedError):
        GardnerTimingRecovery("c", complex=True).build_cell_programs()
