"""M17 4FSK modem demo stimulus (TX bits + RX FM burst) for fsk4_modem.grc.

Imported by the flowgraph as a plain Python module (same pattern as
qpsk_demo_stim). Generates:

  * ``tx_bits(n_bits)`` — a finite 0/1 bit burst for the TX (modulator) chain:
    a fixed **preamble + M17 LSF sync word** (so the RX can lock timing by
    correlation) followed by ``n_bits`` random payload bits, all LSB-first.
  * ``burst(n_syms)`` — the RX stimulus: the same frame (preamble + sync +
    payload), M17 4FSK / C4FM modulated (dibit → PAM level → RRC → FM), delivered
    as COMPLEX I/Q so the RX QuadratureDemod can discriminate it. Scaled so the
    on-chip matched-filter OUTER symbols reach ~full-scale (the sync correlation
    threshold and the slicer's ±2/3 threshold both assume outer ≈ ±1.0).
  * ``rx_dibits(n_syms)`` — the transmitted payload dibits (0..3), the reference
    the batch checker compares the recovered stream against.

M17 4FSK parameters (LOCKED): symbol rate 4800, 2 bits/symbol, sps=2 (fs=9600),
RRC β=0.5 span 8, FM sensitivity 2π·2400/fs = π/2 (a full-scale +3 level advances
π/2 rad/sample = 2400 Hz). Dibit Gray map LSB-first: (b0,b1)=(1,0)→+3, (0,0)→+1,
(0,1)→−1, (1,1)→−3; d = b0 + 2·b1.
"""

import math
import random

import numpy as np

SPS = 2
LEVELS = [1.0 / 3.0, 1.0, -1.0 / 3.0, -1.0]      # index by dibit d = b0 + 2*b1
BETA = 0.5
SPAN = 8
SENSITIVITY = math.pi / 2                         # +3 (+1.0) -> 2400 Hz at fs=9600

# Frame prefix: alternating +3/-3 preamble (AGC/coarse) + the M17 LSF SYNC WORD.
PREAMBLE_D = [1, 3] * 4                            # +3,-3,... (8 symbols)
SYNC_D = [1, 1, 1, 1, 3, 3, 1, 3]                 # M17 LSF sync {+3,+3,+3,+3,-3,-3,+3,-3}
PREFIX_D = PREAMBLE_D + SYNC_D                     # 16 prefix symbols


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


def _payload_dibits(n_syms, seed=5):
    random.seed(seed)
    return [random.randint(0, 3) for _ in range(int(n_syms))]


def _dibit_bits(d):
    # LSB-first: b0 (= d & 1) then b1 (= d >> 1).
    return [d & 1, (d >> 1) & 1]


def rx_dibits(n_syms, seed=5):
    """The transmitted PAYLOAD dibits (0..3), the batch checker's reference."""
    return _payload_dibits(n_syms, seed)


def burst(n_syms, seed=5, amp=0.95):
    """RX stimulus: the framed M17 4FSK burst (preamble + sync + payload), FM
    modulated and delivered as COMPLEX I/Q. Scaled so the on-chip matched-filter
    outer symbols reach ~full-scale."""
    full = PREFIX_D + _payload_dibits(n_syms, seed)
    taps = _rrc(BETA, SPS, SPAN)
    up = np.zeros(len(full) * SPS)
    up[::SPS] = [LEVELS[d] for d in full]
    shaped = np.convolve(up, taps)
    shaped = shaped / (np.max(np.abs(shaped)) + 1e-12) * amp
    iq = np.exp(1j * np.cumsum(SENSITIVITY * shaped)).astype(np.complex64)
    return iq.tolist()


def burst_len(n_syms):
    """Number of complex samples burst() returns for n_syms payload symbols."""
    return (len(PREFIX_D) + int(n_syms)) * SPS + (SPAN * SPS)


def tx_bits(n_bits, seed=7):
    """Finite TX bit burst for the modem's TX (modulator) chain: the fixed frame
    prefix (preamble + sync) followed by ``n_bits`` random payload bits, all
    LSB-first (the FSK4 mapper packs 2 bits/symbol). Returns 0/1 floats."""
    bits = []
    for d in PREFIX_D:
        bits += _dibit_bits(d)
    random.seed(seed)
    n = int(n_bits) - (int(n_bits) % 2)          # even (2 bits/symbol)
    bits += [random.randint(0, 1) for _ in range(n)]
    return [float(b) for b in bits]


# --- qtgui time-sink plot sizing (so the recovered-dibit / TX-passband waveforms
# actually PAINT on a finite burst — a FREE-trigger sink flushes a frame only once a
# sample arrives PAST the frame boundary, so size a guard below the delivered count).
_PLOT_GUARD = 16


def rx_syms_points(n_syms):
    """Points for the recovered-dibit time-sink (a guard below the payload count)."""
    return max(1, int(n_syms) - _PLOT_GUARD)


def tx_pb_len(n_bits):
    """Passband-word count the TX chain emits on x16_out for ``n_bits`` payload bits.
    The mapper packs 2 bits/symbol and the upsampler runs at sps=2, so the chain
    emits sps words per symbol over (prefix + payload) symbols."""
    n_pay = int(n_bits) - (int(n_bits) % 2)
    return (len(PREFIX_D) + n_pay // 2) * SPS


def tx_pb_points(n_bits):
    """Points for the TX-passband time-sink (a guard below the emitted word count)."""
    return max(1, tx_pb_len(n_bits) - _PLOT_GUARD)
