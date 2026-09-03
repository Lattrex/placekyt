<!-- SPDX-License-Identifier: GPL-3.0-or-later -->

# CORDIC polar decomposition (on-chip magnitude + phase)

ONE complex signal — an amplitude-modulated rotating phasor — split into the two
**CORDIC vectoring** chains on a single Kyttar array:

```
stream 'mag':  I/Q ─▶ Complex To Mag ─▶ AM envelope        (|x + jy|)
stream 'arg':  I/Q ─▶ Complex To Arg ─▶ phase sawtooth     (atan2, half-turns)
```

Both chains share the chip through the stream-id duplex (streams `"mag"` /
`"arg"` on `x16_in`/`x16_out`), and each scope overlays the chip trace on the
**stock GNU Radio reference block** (`blocks.complex_to_mag` /
`blocks.complex_to_arg`) — the traces sit on top of each other:

- **Envelope window** — GR `|x|` (blue) vs the chip's CORDIC magnitude (red):
  the 0.25–0.80 AM envelope, worst error ≲ 18 Q15 LSB.
- **Phase window** — GR `arg/π` (blue) vs the chip's half-turn angle (red): a
  ±1 sawtooth (the 10-cycle phase ramp wrapping at ±π). The chip emits
  **half-turn units** (`word/32768 × π` radians — 16-bit wrap *is* mod 2π);
  the stock reference is scaled by `1/π` so the traces align.

Each chain is a fully unrolled 14-iteration CORDIC pipeline
(`ComplexToMagBlock` 17 cells, `ComplexToArgBlock` 30 cells) — stateless,
feed-forward, saturation-safe, and **bit-exact** to its verified reference
model (`verification/tests/test_cordic_blocks.py`).

## Run it

> **Open the `.kyt`, not the `.grc`.** Importing `cordic_polar.grc` into
> placeKYT does **not** auto-route it cleanly. Always open the shipped
> `cordic_polar.kyt`, which is already placed and routed.

1. Open **`cordic_polar.kyt`** in placeKYT. (Open the `.kyt` directly — the
   two CORDIC chains are dense guided-anchor placements the auto-router does
   not currently route cleanly from a fresh `.grc` import; an import can end
   with an unrouted fly line even after the full placement sweep.)
2. **Run as GNURadio Server** (port 58950).
3. Open `cordic_polar.grc` in GNU Radio Companion and **Execute**.

## Headless proof

```
PYTHONPATH=runtime/python:placekyt QT_QPA_PLATFORM=offscreen \
    .venv/bin/python examples/cordic_polar/cordic_polar_demo.py
```

imports the .grc, places, routes, builds, drives both streams per-sample
interleaved on real simKYT, and asserts each stream is **bit-exact** to the
block reference (256/256 samples) with the float-truth error inside the
verification gates (mag ≤ 40 LSB, arg ≤ 0.006 rad).

Regenerate the `.kyt` with `build_kyt.py`.
