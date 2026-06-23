"""RRC-shaped BPSK burst generator (carrier + timing offset) for the coherent RX
demo flowgraph. Imported by coherent_bpsk_rx_demo.grc as a plain Python module so
there is no fragile inline epy_module source (which produced a SyntaxError when
GRC regenerated the flowgraph)."""

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


def burst(n_syms, sps=2, beta=0.35, span=6, toff=0.45, foff=0.008, seed=5):
    """Return a list of complex64 I/Q samples: random BPSK, RRC pulse-shaped at
    `sps` samples/symbol, with a fractional timing offset and a carrier offset."""
    random.seed(seed)
    bits = [random.randint(0, 1) for _ in range(n_syms)]
    syms = [1.0 if b == 0 else -1.0 for b in bits]
    taps = _rrc(beta, sps, span)
    up = []
    for s in syms:
        up += [s] + [0.0] * (sps - 1)
    sh = [sum(taps[k] * up[m - k] for k in range(len(taps)) if 0 <= m - k < len(up))
          for m in range(len(up))]
    out = []
    for m in range(len(sh) - 1):
        i = m + int(math.floor(toff))
        fr = toff - math.floor(toff)
        out.append(sh[i] * (1 - fr) + sh[i + 1] * fr if 0 <= i < len(sh) - 1 else sh[m])
    k = np.arange(len(out))
    iq = (np.asarray(out) * np.exp(1j * 2 * np.pi * foff * k)).astype(np.complex64)
    return iq.tolist()


def burst_len(n_syms, sps=2, span=6):
    """The number of complex samples burst() returns for n_syms (so the Source's
    Burst length parameter can be set without running the generator twice)."""
    return n_syms * sps - 1
