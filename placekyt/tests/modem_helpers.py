# SPDX-License-Identifier: GPL-3.0-or-later
"""Shared BPSK-modem TEST helpers: stimulus generation, BER scoring, word
conversion, and the shared-port demux tags.

These are pure math/stimulus utilities used by the modem tests that drive the
REAL path (import the .grc, or open the shipped .kyt). They contain NO chip
placement/build — a test builds the design via the real import/open path and uses
these only to generate an RRC-shaped BPSK burst and score the recovered bits.

Tags ``RX_TAG``/``TX_TAG`` are the shared-x16_out demux tags the modem's output
nets carry (rx=5, tx=10)."""
from __future__ import annotations

import math

RX_TAG = 5    # recovered RX bits
TX_TAG = 10   # TX passband samples


def make_rrc(beta, sps, span):
    """Unit-energy root-raised-cosine taps (span*sps+1 taps)."""
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


def tx_signal(bits, sps=2, beta=0.35, span=6, timing_offset=0.0, amp=0.9):
    """RRC-shaped BPSK baseband from a bit list, with an optional fractional
    timing offset, peak-scaled to ``amp``. Returns (samples, symbols)."""
    syms = [1.0 if b == 0 else -1.0 for b in bits]
    taps = make_rrc(beta, sps, span)
    up = []
    for s in syms:
        up.append(s)
        up.extend([0.0] * (sps - 1))
    shaped = []
    L = len(taps)
    for n in range(len(up)):
        acc = 0.0
        for k in range(L):
            if 0 <= n - k < len(up):
                acc += taps[k] * up[n - k]
        shaped.append(acc)
    out = []
    for n in range(len(shaped) - 1):
        i = n + int(math.floor(timing_offset))
        frac = timing_offset - math.floor(timing_offset)
        if 0 <= i < len(shaped) - 1:
            out.append(shaped[i] * (1 - frac) + shaped[i + 1] * frac)
        else:
            out.append(shaped[n])
    pk = max(abs(b) for b in out) or 1.0
    out = [amp * b / pk for b in out]
    return out, syms


def ber_with_lag(rx, tx, max_lag=24, min_overlap=40):
    """Min BER over a small lag window, inversion-tolerant (BPSK 180° ambiguity).
    Returns (errors, overlap, lag)."""
    best = (10 ** 9, 0, 0)
    for lag in range(0, max_lag + 1):
        a, b = rx[lag:], tx[: len(rx) - lag]
        m = min(len(a), len(b))
        if m < min_overlap:
            continue
        e = sum(1 for i in range(m) if a[i] != b[i])
        e = min(e, m - e)
        if e < best[0]:
            best = (e, m, lag)
    return best


def fq(f):
    """Float in [-1,1) → uint16 Q15 word."""
    return int(round(max(-1.0, min(0.999, f)) * 32768)) & 0xFFFF


def s16(w):
    """uint16 word → signed int16."""
    return w - 0x10000 if w & 0x8000 else w


# Backward-compatible aliases (the tests historically used underscore-prefixed
# names when these lived in the now-deleted engine.bpsk_modem_demo).
_tx_signal = tx_signal
_ber_with_lag = ber_with_lag
_s16 = s16
_fq = fq
_make_rrc = make_rrc
