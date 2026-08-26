<!-- SPDX-License-Identifier: GPL-3.0-or-later -->

# fft_spectrum — a live spectrum analyzer on the fabric

A streaming FFT and its per-bin power stage, both **placed on the chip**. What
leaves `x16_out` is already a real power word per frequency bin; the flowgraph
un-reverses the bin order, centres it on 0 Hz, and plots it against a real
**frequency axis in Hz**.

Two sizes ship, as two independent `.kyt` / `.grc` pairs:

| variant | transform | FFT block | FFT cells | chip total | latency | burst | tone slot | bin width | tone |
|---|---|---|---|---|---|---|---|---|---|
| **headline** | 64-point | `FFT64Block` | 84 | **104**/120 | 63 | 255 | 52 | 500 Hz | **+5500 Hz** |
| smaller | 32-point | `FFT32Block` | 60 | **80**/120 | 31 | 127 | 26 | 1000 Hz | **+11000 Hz** |

Bin widths and tone frequencies are at the shipped `samp_rate = 32000` — see
[What frequency is a bin?](#what-frequency-is-a-bin-bin--hz).

"chip total" counts the FFT, the 1-cell `ComplexToMagSquaredBlock`, and every
routing cell the auto-router drew.

```
 x16_in ──▶ FFT64 (84 cells, 6 stages) ──▶ ComplexToMag² (1 cell) ──▶ x16_out
   │        complex I/Q in, complex out      per-bin power, real out
   │
   └── carries TWO tagged rails on one cell's registers 1 and 2:
         xi = real (cos)      xq = imaginary (sin)
```

---

## Run it

1. **placeKYT** — open `fft_spectrum.kyt` (or `fft_spectrum_32.kyt`), then
   **Simulation → Run as GNURadio Server**. It binds port **58950**.
2. **GNU Radio Companion** — open `fft_spectrum.grc` (or
   `fft_spectrum_32.grc`) and press **Run**.
3. Two windows open.
   * A **QT GUI Vector Sink** paints the spectrum on a real **frequency axis
     in Hz**, running from −16000 Hz to just under +16000 Hz. The demo tone is
     the single line at **+5500 Hz** (N=64) or **+11000 Hz** (N=32), at about
     **−0.9 dBFS**; every other bin is at the −90 dBFS floor.
   * A **QT GUI Time Sink** shows the **stimulus** — the I and Q rails of the
     tone being fed to `x16_in`, so you can see the sinusoid the spectral
     spike comes from.

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

## The three CHIP contracts you must respect

(There is a fourth, DISPLAY-side one — the
[bin ↔ Hz mapping](#what-frequency-is-a-bin-bin--hz) — documented after these.)

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

## What frequency is a bin? (bin ↔ Hz)

A bin index is **dimensionless**. The array is **asynchronous** — it has no
clock of its own and no notion of seconds, so it transforms whatever word
stream you hand it. The **sample rate is a property of your stimulus**, which
you declare: it is the `samp_rate` variable in each `.grc` (and `SAMP_RATE` in
`fft_spectrum_demo.py`), shipped at **32000 Hz**, the repo-wide example
convention. Change it and the whole frequency axis rescales; the chip does not
change at all.

Given a declared sample rate `fs`, **natural bin `k` of an `N`-point transform
is the frequency**

```
f(k) = k·fs/N              for k <  N/2      (positive frequencies)
f(k) = (k−N)·fs/N          for k >= N/2      (negative frequencies)
```

The **bin width** is `fs/N` — the `bin_hz` variable in each `.grc`.

| variant | N | fs | bin width `fs/N` | tone at bin 11 | Nyquist `fs/2` |
|---|---|---|---|---|---|
| headline | 64 | 32000 Hz | **500 Hz** | **+5500 Hz** | ±16000 Hz |
| smaller | 32 | 32000 Hz | **1000 Hz** | **+11000 Hz** | ±16000 Hz |

Note the same bin **index** is twice the **frequency** at N=32: a half-length
transform has twice the bin width.

**Measured on the real placed chip** (`fft_spectrum_demo.py`, and gated by
`test_peak_frequency_on_the_real_chip`): N=64 peaks at plot point 43 =
**+5500.0 Hz** at −0.92 dBFS; N=32 peaks at plot point 27 = **+11000.0 Hz** at
−0.92 dBFS. Both are exactly `11 · fs/N`.

### Why the plot is centred on 0 Hz

Natural bin order runs `0 → +fs/2` and then **jumps** to `−fs/2 → 0`. No single
linear axis can label that, which is why the x axis used to read only "FFT bin
(natural order)" — an honest label for an unlabellable order. So the display
block now applies `numpy.fft.fftshift`'s permutation after the un-reversal
(rolling by `N/2`), which makes the vector **monotonic in frequency**. The sink
is then configured with a genuine axis:

```
set_x_axis(-samp_rate/2, bin_hz)      # origin −16000 Hz, step fs/N
set_x_axis_units("Hz")
```

so plotted point `i` is `−fs/2 + i·fs/N` Hz. Natural bin 11 lands at index
`11 + N/2` — 43 at N=64, 27 at N=32 — and both read `+11·fs/N`.

`fft_spectrum_demo.py` exports this mapping as `bin_hz()`, `bin_to_hz()`,
`fftshift_order()`, `centred_spectrum()` and `axis_hz()`, so the README, the
`.grc`s' axis config and the gates all cite **one** copy of the arithmetic.

---

## What the placeKYT waveform traces show

Open a run in placeKYT's waveform pane and the `x16_in` port splits into **two
tagged traces**:

| trace | rail | register | what it carries |
|---|---|---|---|
| `fft64.xi` / `fft32.xi` | real (I) | 1 | `cos` of the tone |
| `fft64.xq` / `fft32.xq` | imaginary (Q) | 2 | `sin` of the tone |

The block lands both rails on **two consecutive registers of one cell**
(measured: entry 12, hop 26, `data_addrs [1, 2]`), which is what makes the
input genuinely complex. They carry **different** data — verified live against
this example's own reference, sample for sample:

```
N=64, first words delivered to each rail
  xi:  29491,  13902, -16384, -29349, -11286,  18709, ...
  xq:      0,  26009,  24521,  -2891, -27246, -22797, ...
```

`xq` starts at `sin(0) = 0` while `xi` starts at the amplitude
`round(0.9·32768) = 29491` — the cheapest way to see the rails are neither
duplicated nor swapped. If both rails ever carried the *same* words the block
would see a **real** input, whose spectrum is conjugate-symmetric: the tone
would split into two quarter-power peaks at bins `b` and `N−b`. That failure
has happened on this example before (the un-named-stream landing bug, below),
so `test_the_two_input_rails_carry_different_data` gates it at both sizes.

> Earlier builds labelled **both** traces `xi`, because GNU Radio collapses an
> I/Q pair into one complex port — the project therefore stores **one**
> connection (`x16_in → fft64.xi`) and the Q half is *synthesised*, so naming a
> trace by its net alone gave the same string to both rails. The pane now
> resolves the rail from the register the word was addressed to.
> `test_waveform_labels_the_two_rails_distinguishably` keeps them distinct.

### Why the traces look like a staircase, not a sine

Because that is exactly what they are: the **Q15 word stream at the port**, one
16-bit word per sample, drawn as one step per word. The staircase is expected
and correct, not a defect — the shipped stimulus simply has very few samples
per cycle:

| variant | samples per cycle (`N / tone_bin`) |
|---|---|
| N=64, bin 11 | 64/11 ≈ **5.8** |
| N=32, bin 11 | 32/11 ≈ **2.9** |

A trace reads as a smooth sinusoid at roughly ten-plus samples per cycle, so
under six steps per period necessarily looks blocky. Two ways to see the
sinusoid properly:

* **Use the stimulus scope.** Both `.grc`s ship a `qtgui_time_sink_x` on the
  I/Q burst (`stim_scope`), sized to four whole frames, which spans ~44 cycles
  and interpolates between samples — the sinusoid is unmistakable there.
* **Slow the tone down.** Lower the `tone_bin` variable in the `.grc`.
  `tone_bin = 1` gives exactly **one full cycle per frame** (64 or 32 samples
  per cycle) — the slowest and smoothest stimulus this example can show. The
  peak then moves to `1 · fs/N` = 500 Hz (N=64) or 1000 Hz (N=32). The bin
  sweep gate already covers bins 1, 5, 11, 23, 32, 47 and 63, so any of those
  is a verified choice.

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

## Contracts a hosted (GNU Radio) run depends on

Three settings must be right for a hosted run to deliver a correct spectrum.
They are already correct in the shipped files; they are listed because anyone
building a *new* chained flowgraph has to set them too, and getting one wrong
produces a **plausible spectrum that is quietly wrong** rather than an error.

1. **Name the stream.** A hosted batch carries its injection landing in the
   request header, and the GR client fills it from its own default
   `data_addrs=(0, 1)`; the server overrides that with the build-resolved
   landing only when the burst names a **`stream_id`** it knows. This chain
   lands on registers `[1, 2]`, so an unnamed stream puts the real part on
   register 0 and the imaginary on 1 — the block then sees a **real** input,
   whose spectrum is conjugate-symmetric, and the tone splits into two
   quarter-power peaks. Both `.grc`s and both `.kyt`s name the stream
   `spectrum`.

2. **Use `repeat = no` on the source, `server_repeat = yes` on the sink.** With
   `repeat = yes` the source re-arms mid-vector, so each burst starts at an
   arbitrary rotation of the stimulus; a rotation by `r` slides the frame
   boundary by `r mod N` and moves the peak to the wrong slot. The shipped
   setting sends one genuine burst from index 0 and loops it for the display.

3. **Leave headroom — the demo uses amplitude 0.9.** The host converts an
   injected float with `max(-1.0, min(0.999, f))`, so a full-scale sample at
   `32767/32768` lands as word 32735 rather than 32767. At 0.9 the conversion
   and the reference agree on every sample, which is what lets the gate demand
   bit-exactness instead of a tolerance.

---

## Verification

```bash
# STANDALONE — the user-path gates bind port 58950
KYTTAR_GR_PYTHON=/usr/bin/python3 QT_QPA_PLATFORM=offscreen \
    .venv/bin/python -m pytest verification/tests/test_fft_spectrum_example.py -q
```

51 gates. The ones that matter most:

* **`test_shipped_grc_user_path[64]` / `[32]`** — host the SHIPPED `.kyt`
  exactly as the GUI does, GRC-generate the SHIPPED `.grc`, run it under the
  real GNU Radio interpreter, and assert the recovered stream is bit-exact to
  the reference with the tone on its bit-reversed slot and, after un-reversal,
  in bin 11. **Observed**, N=64: `bin 11 at −0.9 dBFS, all 63 other bins at
  −90.0`; N=32: `bin 11 at −0.9 dBFS, all 31 other bins at −90.0`.
* **`test_second_variant_chain_on_chip[64]` / `[32]`** — the same claim on the
  real built chip: 255/255 and 127/127 power words bit-exact against the
  composed block references.
* **`test_the_two_input_rails_carry_different_data[64]` / `[32]`** — drive the
  built chip with the trace on and read the words delivered to **each rail**
  through the same `TraceModel.port_streams_by_tag` the GUI plots. Asserts the
  landing is complex (two distinct registers), that the rails carry
  **different** words, and that each is bit-exact to the tone's real /
  imaginary part. **Observed**, N=64: `xi = 29491, 13902, -16384, …`,
  `xq = 0, 26009, 24521, …`.
* **`test_waveform_labels_the_two_rails_distinguishably[64]` / `[32]`** — the
  pane names them `fft64.xi` / `fft64.xq` (and `fft32.…`), and above all
  **differently**. Reverting the fix makes this and two sibling gates fail with
  `assert 'fft64.xi' != 'fft64.xi'`.
* **`test_peak_frequency_on_the_real_chip[64]` / `[32]`** — reads the peak off
  the same centred Hz axis the `.grc` plots. **Observed**: N=64 point 43 =
  **+5500.0 Hz**; N=32 point 27 = **+11000.0 Hz**.

Plus: a bin sweep (1, 5, 11, 23, 32, 47, 63), a two-tone case that must show
exactly two lines, a noise case that must NOT concentrate in one bin, the three
pinned chip contracts, the bin↔Hz map pinned against literals at both sizes
(including the negative half above `N/2`), the centred axis proved monotonic
and consistent with `bin_to_hz` for every bin, and static gates that the
shipped `.grc`s really do carry the Hz axis, the fftshift and the stimulus
scope.

**Mutations that must fail** (INV-4): no un-reversal, a wrong-width
un-reversal, a wrong scale, a wrong frame offset, an all-zero spectrum, a flat
full-scale spectrum, duplicated input rails, swapped input rails, an empty
rail, a one-sample-delayed rail, the old single-net namer rule (both labels
collide), a bin→Hz map that ignores the sample rate, and an axis read without
the fftshift (natural bin 11 against the centred axis reads −10500 Hz, not
+5500).

The `.grc`s are also covered by the repo-wide `test_examples_grc_valid.py` and
`test_examples_grc_instantiate.py` gates, which discover them by glob.

## Files

| file | what |
|---|---|
| `fft_spectrum.kyt` / `fft_spectrum_32.kyt` | the placed + routed designs — **open these** |
| `fft_spectrum.grc` / `fft_spectrum_32.grc` | the flowgraphs — open in GRC, press Run |
| `fft_spectrum_demo.py` | the headless demo, the stimulus, the un-reversal map, the bin↔Hz mapping (`bin_hz`, `bin_to_hz`, `fftshift_order`, `centred_spectrum`, `axis_hz`), and the chain builder |
| `build_kyt.py` | regenerates both `.kyt` files |
| `fft_spectrum.py` / `fft_spectrum_32.py` | the GRC-generated flowgraph scripts |
