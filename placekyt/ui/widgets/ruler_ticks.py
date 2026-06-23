"""Shared 'nice' tick generation for the time/value rulers.

Pure math (no Qt) so it can be unit-tested and reused by the waveform ruler,
the analog amplitude scale, and the timeline scrubber. The tick count scales
with the available pixels, so zooming in fills in MORE labels (the GTKWave /
oscilloscope ruler behaviour) instead of just spreading the same few apart.
"""

from __future__ import annotations

import math


def nice_step(rough: float) -> float:
    """Round ``rough`` up to a 'nice' 1/2/5 × 10ⁿ step."""
    if rough <= 0:
        return 1.0
    exp = math.floor(math.log10(rough))
    base = 10.0 ** exp
    for m in (1.0, 2.0, 5.0, 10.0):
        if rough <= m * base:
            return m * base
    return 10.0 * base


def nice_ticks(lo: float, hi: float, px: float,
               min_px_per_tick: float = 70.0) -> list[float]:
    """A list of 'nice' tick values spanning ``[lo, hi]``. The number of ticks
    scales with ``px`` (≈ one per ``min_px_per_tick``). Returns ``[]`` for a
    degenerate range."""
    span = hi - lo
    if span <= 0 or px <= 0:
        return []
    want = max(2.0, px / min_px_per_tick)        # desired number of intervals
    step = nice_step(span / want)
    first = math.ceil(lo / step) * step
    ticks: list[float] = []
    t = first
    n = 0
    while t <= hi + step * 1e-6 and n < 1000:    # guard float drift runaway
        ticks.append(round(t, 12))
        t += step
        n += 1
    return ticks
