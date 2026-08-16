# SPDX-License-Identifier: GPL-3.0-or-later
"""16-QAM modem demo stimulus (TX bits + RX RRC burst) for qam16_modem.grc.

Imported by the flowgraph as a plain Python module (same pattern as
qpsk_demo_stim / fsk4_demo_stim). Generates:

  * ``tx_bits(n_bits)`` — a finite 0/1 bit burst for the TX (modulator) chain:
    ``n_bits`` random payload bits, MSB-first (the QAM16 mapper packs 4
    bits/symbol into the ``digital.constellation_16qam()`` point).
  * ``burst(n_syms)`` — the RX stimulus: a random 16-QAM symbol stream,
    upsampled sps=2 and RRC-shaped (β=0.35, span 8), delivered as COMPLEX I/Q so
    the RX matched filter → complex gain → M&M timing recovery → QAM16 Costas →
    slicer chain recovers it. NO carrier frequency offset: the hosted .kyt runs
    TX and RX on the SAME chip / SAME clock, so foff = 0 by construction (the
    decision-directed M&M TED before the Costas needs foff = 0). Scaled to peak
    ≈ 0.9 (no Q15 clipping on inject); the on-chip ComplexGain(gain=2.4)
    restores the outer constellation level to the nominal 0.949 the
    decision-directed loops need.
  * ``rx_syms(n_syms)`` — the transmitted symbol indices (0..15), the reference
    the batch checker compares the recovered stream against (rotation-aligned).

16-QAM parameters (LOCKED): 4 bits/symbol, sps = 2, RRC β = 0.35 span 8. The
constellation is GNU Radio ``digital.constellation_16qam()``: index 0..15 →
(I, Q) in units {±1, ±3}/√10.
"""

import math
import random

import numpy as np

SPS = 2
BETA = 0.35
SPAN = 8

_NORM = 1.0 / math.sqrt(10.0)
# GNU Radio constellation_16qam() points (index 0..15 -> (I, Q)), {±1,±3}/√10.
_LEVELS = [(+1, -1), (-1, -1), (+3, -3), (-3, -3), (-3, -1), (+3, -1), (-1, -3),
           (+1, -3), (-3, +3), (+3, +3), (-1, +1), (+1, +1), (+1, +3), (-1, +3),
           (+3, +1), (-3, +1)]
_POINTS = [(i * _NORM, q * _NORM) for (i, q) in _LEVELS]

_PLOT_GUARD = 40


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
            v = (math.sin(math.pi * t * (1 - beta))
                 + 4 * beta * t * math.cos(math.pi * t * (1 + beta))) / (
                     math.pi * t * (1 - (4 * beta * t) ** 2))
        taps.append(v)
    e = math.sqrt(sum(x * x for x in taps))
    return [x / e for x in taps]


def _payload_syms(n_syms, seed=5):
    rng = np.random.RandomState(seed)
    return rng.randint(0, 16, int(n_syms)).tolist()


def rx_syms(n_syms, seed=5):
    """The transmitted symbol indices (0..15), the batch checker's reference."""
    return _payload_syms(n_syms, seed)


def burst(n_syms, seed=5, amp=0.9):
    """RX stimulus: a random 16-QAM symbol stream, RRC-shaped (sps=2), delivered
    as COMPLEX I/Q, peak-scaled to ``amp`` (no carrier offset — same-chip TX)."""
    syms = _payload_syms(n_syms, seed)
    base = np.array([complex(*_POINTS[s]) for s in syms], dtype=np.complex128)
    up = np.zeros(len(base) * SPS, dtype=np.complex128)
    up[::SPS] = base
    taps = _rrc(BETA, SPS, SPAN)
    shaped = np.convolve(up, taps)
    shaped = shaped / (np.max(np.abs(shaped)) + 1e-12) * amp
    return shaped.astype(np.complex64).tolist()


def burst_len(n_syms):
    """Number of complex samples burst() returns for n_syms payload symbols."""
    return int(n_syms) * SPS + (SPAN * SPS)


def tx_bits(n_bits, seed=7):
    """Finite TX bit burst for the modem's TX (modulator) chain: ``n_bits``
    random payload bits, MSB-first (the QAM16 mapper packs 4 bits/symbol).
    Returns 0/1 floats."""
    random.seed(seed)
    n = int(n_bits) - (int(n_bits) % 4)              # whole symbols (4 bits each)
    return [float(random.randint(0, 1)) for _ in range(n)]


def tx_syms(n_bits, seed=7):
    """The TRANSMITTED SYMBOL INDICES (0..15) for the SAME burst ``tx_bits(
    n_bits, seed)`` produces — every MSB-first 4-bit group packed to an index."""
    bits = tx_bits(n_bits, seed)
    out = []
    for i in range(0, len(bits) - 3, 4):
        idx = 0
        for b in bits[i:i + 4]:
            idx = (idx << 1) | int(b)
        out.append(float(idx))
    return out


def tx_syms_points(n_bits):
    """Points for the transmitted-symbol time-sink (a guard below the count)."""
    return max(1, len(tx_syms(n_bits)) - _PLOT_GUARD)


def rx_syms_points(n_syms):
    """Points for the recovered-symbol time-sink (a guard below the count)."""
    return max(1, int(n_syms) - _PLOT_GUARD)


def tx_pb_len(n_bits):
    """Passband-word count the TX chain emits on x16_out for ``n_bits`` payload
    bits. The mapper packs 4 bits/symbol and the upsampler runs at sps=2."""
    n_pay = int(n_bits) - (int(n_bits) % 4)
    return (n_pay // 4) * SPS


def tx_pb_points(n_bits):
    """Points for the TX-passband time-sink (a guard below the word count)."""
    return max(1, tx_pb_len(n_bits) - _PLOT_GUARD)
