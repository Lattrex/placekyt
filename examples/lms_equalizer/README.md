<!-- SPDX-License-Identifier: GPL-3.0-or-later -->

# LMS adaptive equalizer — the constellation snap

QPSK symbols smeared by a multipath channel go through the placed
**decision-directed complex LMS equalizer** — and the constellation display
shows the cloud **snap** onto the four clean QPSK points *within the burst*,
because the adaptation runs **on the chip, per sample**:

```
QPSK (seed 7) ─▶ channel [1, 0.35, -0.15] + AWGN ─▶ Kyttar LMS EQ (5 taps, μ=0.03) ─▶ constellation
                  (host-side smearing + noise)          (on-chip adaptation)
```

Two synced displays tell the story:

- **Constellation, colored by TIME.** Blue (dimmed) is the channel-distorted
  input — ISI plus AWGN (σ = 0.035/component). The equalized output is split
  into convergence phases so one glance shows *when* each point happened:
  **red** = the first 100 symbols (cold start, scattered), **magenta** =
  symbols 100–250 (pulling in), **green** = 250+ (converged, sitting on the
  ±0.7071 corners). Converged tail: **BER 0** through the multipath and the
  noise. (The QT constellation widget repaints only when its full buffer
  fills — once per burst — so time is encoded as color rather than as
  animation.)
- **The learning curve.** The second scope plots the smoothed distance from
  each equalized symbol to its nearest decision point: it starts high at
  every burst's cold start and *decays* as the on-chip taps adapt — the
  quantitative "it is getting better", animated at the playback rate.

The flowgraph runs **continuously** (the *source's* "Repeat bursts = Yes";
leave the sink's `server_repeat` off — that setting loops one batch's result
and fights the repeat source): each pass dispatches a fresh 600-symbol
burst, the chip cold-starts its taps at the packet boundary, and the
convergence replays. The **"Convergence playback"** slider paces how fast
the equalized stream reaches the displays.

The equalizer is `LMSEqualizerBlock` (15 program cells + 1 transit = 16 cells, an 8×2 fold): a
complex FFE with on-chip decision-directed tap adaptation, GR-equivalent to
`digital.linear_equalizer(5, 1, adaptive_algorithm_lms(...))` up to the
proven scale covariance (`verification/tests/test_lms_equalizer.py`). The
chain drives **per sample** — the LMS contract — which is exactly what the
batch server does.

## Run it

1. Open **`lms_equalizer.kyt`** in placeKYT (or import `lms_equalizer.grc`
   and Auto-P&R).
2. **Run as GNURadio Server** (port 58950).
3. Open `lms_equalizer.grc` in GNU Radio Companion and **Execute** — pull
   the "Convergence playback" slider down and watch the red points snap,
   burst after burst.

Live-view bonus: the placeKYT cell animation shows the gradient broadcast
rippling backward through the tap cells every sample — the adaptation is
*visible* on the array.

## Headless proof

```
PYTHONPATH=runtime/python:placekyt QT_QPA_PLATFORM=offscreen \
    .venv/bin/python examples/lms_equalizer/lms_eq_demo.py
```

imports the .grc, places, routes, builds, drives 600 multipath QPSK symbols
per-sample on real simKYT, and asserts the chip output is **bit-exact** to
the verified reference with converged-tail **BER 0** and the tail clusters
on the unit constellation.

Regenerate the `.kyt` with `build_kyt.py`.
