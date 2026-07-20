"""RRC-shaped QPSK burst generator (carrier + timing offset) for the QPSK modem
demo flowgraph. Imported by qpsk_modem.grc as a plain Python module (the same
pattern as coherent_demo_stim, so there is no fragile inline epy_module source).

QPSK constellation: +-1/sqrt(2) per axis (constant modulus). The recovered symbol
index matches GNU Radio ``digital.constellation_qpsk()``:
    symbol = (Q >= 0 ? 2 : 0) | (I >= 0 ? 1 : 0)
"""

import math
import random

import numpy as np


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


def _shape(syms, taps, sps):
    up = []
    for s in syms:
        up += [s] + [0.0] * (sps - 1)
    return [sum(taps[k] * up[m - k]
                for k in range(len(taps)) if 0 <= m - k < len(up))
            for m in range(len(up))]


def _timing_shift(sh, toff):
    out = []
    for m in range(len(sh) - 1):
        i = m + int(math.floor(toff))
        fr = toff - math.floor(toff)
        out.append(sh[i] * (1 - fr) + sh[i + 1] * fr
                   if 0 <= i < len(sh) - 1 else sh[m])
    return out


def burst(n_syms, sps=2, beta=0.35, span=8, toff=0.45, foff=0.008, seed=5,
          amp=0.7):
    """Return a list of complex64 I/Q samples: random QPSK (+-1/sqrt(2) per axis),
    RRC pulse-shaped at `sps` samples/symbol, with a fractional timing offset and a
    carrier offset. Peak-normalised to ~`amp` (full-scale ADC-grade drive) so the
    on-chip matched filter sees real energy (un-normalised RRC vanishes in Q15)."""
    random.seed(seed)
    symbols = [(random.randint(0, 1), random.randint(0, 1)) for _ in range(n_syms)]
    si = [(1 if bi == 0 else -1) / math.sqrt(2) for bi, _ in symbols]
    sq = [(1 if bq == 0 else -1) / math.sqrt(2) for _, bq in symbols]
    taps = _rrc(beta, sps, span)
    xi = _timing_shift(_shape(si, taps, sps), toff)
    xq = _timing_shift(_shape(sq, taps, sps), toff)
    pk = max(max(abs(a) for a in xi), max(abs(b) for b in xq)) or 1.0
    xi = [amp * a / pk for a in xi]
    xq = [amp * b / pk for b in xq]
    k = np.arange(len(xi))
    base = np.asarray(xi) + 1j * np.asarray(xq)
    iq = (base * np.exp(1j * 2 * np.pi * foff * k)).astype(np.complex64)
    return iq.tolist()


def burst_len(n_syms, sps=2, span=8):
    """The number of complex samples burst() returns for n_syms (so the Source's
    Burst length parameter can be set without running the generator twice)."""
    return n_syms * sps - 1


def tx_symbols(n_syms, seed=5):
    """The transmitted QPSK symbol indices (0..3, GR constellation_qpsk map) for
    the same seed as burst() — the reference the batch checker compares against."""
    random.seed(seed)
    symbols = [(random.randint(0, 1), random.randint(0, 1)) for _ in range(n_syms)]
    return [(2 if bq == 0 else 0) | (1 if bi == 0 else 0) for bi, bq in symbols]


def tx_bits(n_bits, seed=7):
    """A finite TX bit burst for the modem's TX (modulator) chain. The QPSK mapper
    accumulates 2 bits/symbol, so ``n_bits`` should be EVEN (each pair -> one QPSK
    constellation point). Returns a list of 0/1 floats (a vector_source_f burst)."""
    random.seed(seed)
    n = n_bits - (n_bits % 2)   # even
    return [float(random.randint(0, 1)) for _ in range(n)]


# --- qtgui time-sink plot sizing (so the recovered-symbol / TX-passband waveforms
# actually PAINT on a finite burst). A FREE-trigger time_sink only flushes a
# completed frame once a sample arrives PAST the frame boundary, so on a FINITE
# batch a ``size`` EQUAL to the delivered count leaves the last frame un-flushed =>
# a FLAT plot. Size the sinks a guard BELOW the guaranteed delivered count so a full
# frame always completes and a trailing sample flushes it (mirrors modem_demo_stim).
_PLOT_GUARD = 16


def tx_pb_len(n_bits):
    """Passband-word count the QPSK TX chain (mapper -> complex upsampler -> complex
    RRC shaper -> I/Q upconvert) emits on x16_out for ``n_bits`` input bits. The QPSK
    mapper packs 2 bits/symbol and the upsampler runs at ``sps`` (2), so the chain
    emits ``sps`` passband words per SYMBOL => ``sps * (n_bits // 2)`` = ``n_bits``
    words (sps=2)."""
    return int(n_bits)


def rx_syms_points(n_syms):
    """Number of Points for the RECOVERED-SYMBOLS time-sink. The RX chain recovers
    ~``n_syms`` 2-bit symbols (may be a few short on a warm chip); a frame a guard
    below that always completes and flushes so the symbol waveform PAINTS."""
    return max(1, int(n_syms) - _PLOT_GUARD)


def tx_pb_points(n_bits):
    """Number of Points for the TX-PASSBAND time-sink: a guard below the emitted
    passband-word count so its FREE-trigger frame completes and flushes on the
    finite burst."""
    return max(1, tx_pb_len(n_bits) - _PLOT_GUARD)
