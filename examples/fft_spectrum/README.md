<!-- SPDX-License-Identifier: GPL-3.0-or-later -->

# fft_spectrum — a live spectrum analyzer on the fabric

A streaming FFT and its per-bin power stage, both **placed on the chip**. What
leaves `x16_out` is already a real power word per frequency bin; the flowgraph
un-reverses the bin order and plots it.

Two sizes ship, as two independent `.kyt` / `.grc` pairs:

| variant | transform | FFT block | FFT cells | chip total | latency | burst | tone slot |
|---|---|---|---|---|---|---|---|
| **headline** | 64-point | `FFT64Block` | 84 | **104**/120 | 63 | 255 | 52 |
| smaller | 32-point | `FFT32Block` | 60 | **80**/120 | 31 | 127 | 26 |

"chip total" counts the FFT, the 1-cell `ComplexToMagSquaredBlock`, and every
routing cell the auto-router drew.

```
 x16_in ──▶ FFT64 (84 cells, 6 stages) ──▶ ComplexToMag² (1 cell) ──▶ x16_out
            complex I/Q in, complex out      per-bin power, real out
```

---

## Run it

1. **placeKYT** — open `fft_spectrum.kyt` (or `fft_spectrum_32.kyt`), then
   **Simulation → Run as GNURadio Server**. It binds port **58950**.
2. **GNU Radio Companion** — open `fft_spectrum.grc` (or
   `fft_spectrum_32.grc`) and press **Run**.
3. A **QT GUI Vector Sink** paints the spectrum: 64 (or 32) natural-order bins
   in dBFS. The demo tone sits at **bin 11**, at about **−0.9 dBFS**, and every
   other bin is at the −90 dBFS floor.

> **Open the `.kyt` — do not re-import the `.grc`.** See
> [Why the placement is pinned](#why-the-placement-is-pinned).

Headless (no GUI, both sizes, builds + runs + checks on real simKYT):

```bash
PYTHONPATH=runtime/python:placekyt QT_QPA_PLATFORM=offscreen \
    .venv/bin/python examples/fft_spectrum/fft_spectrum_demo.py
```

Regenerate both `.kyt` files:

```bash
PYTHONPATH=runtime/python:placekyt QT_QPA_PLATFORM=offscreen \
    .venv/bin/python examples/fft_spectrum/build_kyt.py
```

---

## The three contracts you must respect

These come from the verified block. A consumer that ignores any of them gets a
plausible-looking, wrong plot.

### 1. Output is in BIT-REVERSED bin order

The block is a decimation-in-frequency R2SDF FFT with **deliberately no
reorder buffer**: output slot `k` of each frame carries frequency bin
`bit_reverse(k, log2 N)`.

At N=64 that sends bin 11 out on **slot 52**; at N=32, on **slot 26**. Slot 1
is bin 32 (N=64) or bin 16 (N=32). The map is an involution — applying it twice
returns the natural order — so the same permutation both scrambles and
unscrambles.

The `unreverse` embedded block in each `.grc` is exactly this map, and it is
the reason the tone appears at 11 on the plot rather than at 52. The gate
attacks it directly: a display path that skips the un-reversal, or applies a
5-bit map to 64 slots, must FAIL.

### 2. Scale is FFT/N

One round-half-to-even `>>1` per stage over `log2 N` stages. An **on-bin**
complex exponential of amplitude `A` therefore lands essentially all its energy
in one bin at power `A²`. The shipped tone is `A = 0.9`, so the coherent bin
reads **0.81** (≈ −0.9 dBFS) and everything else is zero.

### 3. Latency is N−1 samples

The first `N−1` outputs of a burst are the deterministic startup values of the
zero-initialized pipeline — **not a frame**. Frame `f` occupies output samples
`N−1 + N·f .. 2N−2 + N·f`. The `unreverse` block strips exactly `N−1` samples
per burst before it emits its first vector.

---

## Why the placement is pinned

`FFT64Block` and `FFT32Block` are **CHIP_SCALE** blocks: their verified layout
is a vertical `ctl`/`out` spine (12 rows at N=64) that occupies most of the die
and cannot rotate. The generic auto-packer does not model that class — asked to
pack this design it shifts the spine off the array:

```
auto-placement is illegal (4 problem(s)) ...
  block 'fft64' cell (2,12) is off the 10x12 array
```

So the builder **pins the two block anchors** — the FFT at its own
`default_layout()` anchor `(0, 0)`, the one-cell power stage at `(9, 1)` — and
then calls the **real `auto_route_all`**. Every corridor, every broker, the DRC
and the bitstream are the normal engine; only the two anchors are chosen for it.

That is why the shipped `.kyt` is the artifact to open. Re-importing the `.grc`
in placeKYT runs the auto-packer, which cannot place a chip-scale spine.

Two placement details are load-bearing and were measured, not guessed:

* the power stage's resting **face must be NORTH** at `(9, 1)`. `default_cells`
  picks EAST, which points off the array; the egress net then fails with
  `no bus path from source to the broker tap`.
* the FFT→power link is **one** logical connection.
  `add_logical_connection` synthesises the Q-half sibling automatically. Adding
  the Q net by hand as well creates a duplicate net onto the same input
  register — it routes, it builds, and the chain then emits a frame of pure
  zeros with no error anywhere.

---

## Three live-path lessons this example paid for

All three produced a *plausible* spectrum that was wrong, and none was visible
headlessly. They are recorded here because the next chained example will hit
them.

1. **Name the stream, or the I/Q lands on the wrong registers.** A hosted batch
   carries the injection landing in its request header, and the GR client fills
   it from its own default `data_addrs=(0, 1)`. The server only overrides that
   with the build-resolved landing when the burst names a **`stream_id`** it
   knows. This chain lands on registers `[1, 2]`, so without a stream id the
   real part went to register 0 and the imaginary part to register 1 — the
   block saw a **real** input, whose spectrum is conjugate-symmetric, and the
   tone split into two quarter-power peaks at bins 11 and 53. Both `.grc`s and
   both `.kyt`s now name the stream `spectrum`.

2. **A repeat-burst source rotates the frame grid.** With `repeat = yes` the
   source re-arms mid-vector, so the next burst starts at an arbitrary rotation
   of the stimulus. A rotation by `r` slides the frame boundary by `r mod N` and
   moves the peak to the wrong slot (measured: a 55-sample rotation moved it
   from slot 52 to 43). The fix is `repeat = no` on the source plus
   `server_repeat = yes` on the sink — one genuine burst from index 0, looped
   for the display. The gate asserts the loop is a byte-identical replay.

3. **Full-scale input is clipped by the host conversion.** The server converts
   an injected float with `max(-1.0, min(0.999, f))`, so a sample at
   `32767/32768 = 0.99997` lands as word 32735 instead of 32767 — 8 samples of
   a 255-sample full-scale burst. The demo uses amplitude **0.9**, where the
   server's conversion and the example's reference agree on every sample, which
   is what lets the user-path gate demand bit-exactness instead of a tolerance.

---

## Verification

```bash
# STANDALONE — the user-path gates bind port 58950
KYTTAR_GR_PYTHON=/usr/bin/python3 QT_QPA_PLATFORM=offscreen \
    .venv/bin/python -m pytest verification/tests/test_fft_spectrum_example.py -q
```

26 gates. The two that matter most:

* **`test_shipped_grc_user_path[64]` / `[32]`** — host the SHIPPED `.kyt`
  exactly as the GUI does, GRC-generate the SHIPPED `.grc`, run it under the
  real GNU Radio interpreter, and assert the recovered stream is bit-exact to
  the reference with the tone on its bit-reversed slot and, after un-reversal,
  in bin 11. **Observed**, N=64: `bin 11 at −0.9 dBFS, all 63 other bins at
  −90.0`; N=32: `bin 11 at −0.9 dBFS, all 31 other bins at −90.0`.
* **`test_second_variant_chain_on_chip[64]` / `[32]`** — the same claim on the
  real built chip: 255/255 and 127/127 power words bit-exact against the
  composed block references.

Plus: a bin sweep (1, 5, 11, 23, 32, 47, 63), a two-tone case that must show
exactly two lines, a noise case that must NOT concentrate in one bin, the three
pinned contracts, and six **mutations that must fail** — no un-reversal, a
wrong-width un-reversal, a wrong scale, a wrong frame offset, an all-zero
spectrum and a flat full-scale spectrum.

The `.grc`s are also covered by the repo-wide `test_examples_grc_valid.py` and
`test_examples_grc_instantiate.py` gates, which discover them by glob.

## Files

| file | what |
|---|---|
| `fft_spectrum.kyt` / `fft_spectrum_32.kyt` | the placed + routed designs — **open these** |
| `fft_spectrum.grc` / `fft_spectrum_32.grc` | the flowgraphs — open in GRC, press Run |
| `fft_spectrum_demo.py` | the headless demo, the stimulus, the un-reversal map, and the chain builder |
| `build_kyt.py` | regenerates both `.kyt` files |
| `fft_spectrum.py` / `fft_spectrum_32.py` | the GRC-generated flowgraph scripts |
