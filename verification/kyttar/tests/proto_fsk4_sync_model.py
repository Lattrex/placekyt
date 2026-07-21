# SPDX-License-Identifier: GPL-3.0-or-later
"""Self-contained numerical model of the M17 4FSK sync-word timing recovery.

Validates the ALGORITHM the FSK4SyncTimingRecoveryBlock implements, independent of
the on-chip cells: preamble + M17 LSF sync word, sliding ±1 sync correlation
(each sample scaled 1/SYNC_LEN so the sum fits int16), first-local-max-above-
threshold lock, then 2:1 decimation. Recovers the M17 dibits at BER 0 across seeds
on the FM-discriminator 4-PAM signal at 2 sps.

Run:
    KYTTAR_GR_PYTHON=/usr/bin/python3 <venv>/python \
        verification/kyttar/tests/proto_fsk4_sync_model.py
"""
from __future__ import annotations
import math
import random

SPS = 2
SYMRATE = 4800
FS = SPS * SYMRATE
FDEV_MAX = 2400.0
SENS = 2 * math.pi * FDEV_MAX / FS
BETA = 0.5
SPAN = 8
LEVELS = [1.0 / 3.0, 1.0, -1.0 / 3.0, -1.0]     # index by dibit d = b0 + 2*b1
THR = 2.0 / 3.0
SYNC_SIGNS = [+1, +1, +1, +1, -1, -1, +1, -1]   # M17 LSF sync (dibits 1,1,1,1,3,3,1,3)
PRE_D = [1, 3] * 4                               # alternating +3/-3 preamble
SYNC_D = [1, 1, 1, 1, 3, 3, 1, 3]
CORR_SCALE_Q15 = 4096                            # 1/SYNC_LEN in Q15


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
    import numpy as np
    e = math.sqrt(sum(v * v for v in taps))
    return [v / e for v in taps]


def gen_rx_q15(dibits, gain=1.5):
    """Full TX+channel+RX-front-end -> RX matched-filter stream at 2 sps (uint16 Q15).
    Frame = alternating preamble + M17 sync word + payload dibits."""
    import numpy as np
    full = PRE_D + SYNC_D + list(dibits)
    taps = _rrc(BETA, SPS, SPAN)
    up = np.zeros(len(full) * SPS)
    up[::SPS] = [LEVELS[d] for d in full]
    shaped = np.convolve(up, taps)
    shaped = shaped / (np.max(np.abs(shaped)) + 1e-12) * 0.9
    tx = np.exp(1j * np.cumsum(SENS * shaped))
    prev = np.concatenate([[0], tx[:-1]])
    di = np.imag(tx * np.conj(prev))
    mf = np.convolve(di, taps)
    mf = mf / (np.max(np.abs(mf)) + 1e-12) * gain
    return [int(round(np.clip(v, -1, 0.999) * 32768)) & 0xFFFF for v in mf]


def slice4(y):
    a = abs(y)
    if a >= THR:
        return 1 if y >= 0 else 3
    return 0 if y >= 0 else 2


def timing_recover(x_q15, threshold):
    """The block's algorithm: sliding scaled ±1 sync correlation, first-local-max
    lock, 2:1 decimation. Returns the recovered symbol-center values (signed)."""
    def s16(v):
        v = int(v) & 0xFFFF
        return v - 0x10000 if v & 0x8000 else v

    x = [s16(v) for v in x_q15]
    rev = SYNC_SIGNS[::-1]
    reg = [0] * (SPS * len(SYNC_SIGNS))
    out = []
    cm1 = cm2 = 0
    locked = False
    next_emit = -1
    for n, xv in enumerate(x):
        reg = [xv] + reg[:-1]
        c = 0
        for j in range(len(SYNC_SIGNS)):
            c += rev[j] * ((reg[2 * j] * CORR_SCALE_Q15) >> 15)
        if not locked:
            if cm1 >= threshold and cm1 >= cm2 and cm1 >= c:
                locked = True
                next_emit = n + 1
            cm2, cm1 = cm1, c
        else:
            if n == next_emit:
                out.append(s16(reg[0]))
                next_emit += SPS
    return out


def _ber(rx_d, tx_d, guard=2, maxlag=3):
    best = (1.0, 0, 0)
    for lag in range(maxlag + 1):
        a = rx_d[lag:]
        m = min(len(a), len(tx_d))
        if m < guard + 40:
            continue
        e = sum(1 for k in range(guard, m) if a[k] != tx_d[k])
        if e / (m - guard) < best[0]:
            best = (e / (m - guard), e, lag)
    return best


def main():
    ideal = int(0.45 * len(SYNC_SIGNS) * ((32768 * CORR_SCALE_Q15) >> 15))
    worst = 0.0
    for seed in range(60):
        random.seed(seed)
        dib = [random.randint(0, 3) for _ in range(300)]
        x = gen_rx_q15(dib)
        rec = timing_recover(x, ideal)
        dd = [slice4(v / 32768.0) for v in rec]
        b = _ber(dd, dib)
        worst = max(worst, b[0])
    print(f"threshold={ideal}  worst-BER over 60 seeds = {worst:.4f}"
          f"  ({'PASS BER0' if worst == 0.0 else 'FAIL'})")


if __name__ == "__main__":
    main()
