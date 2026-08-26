<!-- SPDX-License-Identifier: GPL-3.0-or-later -->

# CSS receiver — chirp spread spectrum, the whole receive spine on one array

**The story this demo tells:** a chirp-spread-spectrum symbol is a linear
up-chirp that has been *cyclically shifted*, and the shift **is** the symbol.
Multiply the incoming stream by the **conjugate** of the un-shifted reference
chirp — the *dechirp* — and the sweep cancels: what is left is a constant tone
whose frequency encodes the symbol. A 16-point FFT concentrates that tone into
**one bin**, and the winning bin index recovers the symbol. The whole of that
receive spine is placed and routed on **one placeKYT array**:

```
x16_in ─▶ ConjChirpMixer(n=16) ─▶ FFT16 ─▶ ComplexToMagSquared
       ─▶ Delay(1) ─▶ BinArgmax(16) ─▶ x16_out
```

One continuous burst carries the message **`KYTTAR CSS`** through that chain
**twice**:

* **segment A — +10 dB SNR:** every symbol decodes, the message reads back
  exactly (**SER 0**);
* **segment B — −10 dB SNR:** the same chain, the same stream, the same run —
  the decode collapses (**SER 0.625**).

Segment B is the **negative control, and it runs on the chip**. It is not a
host-side model of failure asserted beside a working chip; it is the same
placed silicon fed a worse signal in the same continuous burst. That is what
makes the SER number mean something.

## The two insights this example pins

**1. The decode map is `s = brev4(index)`.** FFT16 is a decimation-in-frequency
radix-2 FFT and emits its bins in **bit-reversed order** — slot *k* of each
16-sample frame carries bin `brev4(k)`. The block deliberately ships no reorder
buffer, so the consumer applies the map. The winning *index* is therefore not
the symbol until it is 4-bit-reversed. (Gated: decoding the raw index must
fail.)

**2. `Delay(1)` is load-bearing, not padding.** FFT16's streaming latency is
`N−1 = 15 ≡ −1 (mod 16)`. Without correction, BinArgmax's 16-sample frames
**straddle two FFT frames** and every symbol decodes wrong. Exactly **one**
extra real-rail sample of delay lands each argmax frame on exactly one FFT
frame. (Gated: the no-delay variant must disagree with the aligned one and must
fail to decode.)

Frame *f*'s index emerges during frame *f+1*, so the framed burst carries one
trailing **flush symbol** — without a symbol behind it, the last data symbol's
index never leaves the chip.

## Where the on-chip / host boundary sits

**This is an RX example. It does not claim a transmitter on the chip.**

* **ON-CHIP** — one placed + routed array, real corridors and hand-offs: the
  whole **receive spine** (dechirp, FFT16, magnitude, alignment delay,
  framewise argmax). **82 of the array's 120 cells** (60 block cells + the
  routing corridors).
* **HOST-SIDE** — numpy, in `gr-kyttar/python/kyttar/css_demo_stim.py`: the
  **transmitter** (`ChirpSymbolMapperBlock` + `ChirpGeneratorBlock`) and the
  channel (attenuation + AWGN, then Q15 quantization — a channel is numpy by
  nature). The stim module's TX output is asserted **bit-identical** to those
  two blocks' own chip-verified integer references, so the goldens are never
  self-consistent-only; but the transmitter is not placed here.

Why not both: the RX spine alone is 82/120 cells. The TX chain (mapper 1 +
generator 10 cells) would fit on paper, but a second stream feeding the shared
input port could not be routed alongside the placed spine (`no bus path from
source to the broker tap`), and an on-chip TX→RX loopback is not supported. The
RX spine is the demo worth shipping, so it ships alone and says so.

`ChirpSyncBlock` (the K-run preamble detector) is **not** in the shipped chain
either: its output is a lock flag, not the payload, and a second tagged egress
on one stream is not available — so shipping it would replace the decoded
symbols on screen with a sync flag. The burst still carries the 4-symbol
`s = 0` preamble, and the sync block's own on-chip behaviour is gated in
`verification/tests/test_css_rx_system.py`.

## What the GRC window shows

The window reads top to bottom as one argument. **Both segments succeeding at
what they are each supposed to do is the result** — and the layout is built so
that no one has to have read this file to know it.

| Row | What |
|-----|------|
| **Caption** | one line: *"BOTH PANELS ARE THE EXPECTED RESULT — A (+10 dB) LOCKS, SER 0.00 \| B (−10 dB) COLLAPSES, SER ~0.63 …"* |
| **Panel 1 — SEGMENT A · +10 dB · DECODE LOCKS ✓ EXPECTED** | segment A alone. Every **blue X** (decoded, off the chip) lands inside a **cyan circle** (transmitted reference) — 24 for 24. Draws in the left half of the sweep only. |
| **Panel 2 — SEGMENT B · −10 dB · NEGATIVE CONTROL … ✓ EXPECTED, THIS IS THE POINT** | segment B alone. **Red X marks miss their dark-red circles**, on purpose. Draws in the right half only. A clean overlay *here* would mean the control had stopped working. |
| **MEASURED SYMBOL ERROR RATE** | the two headline numbers, measured live on the symbols actually plotted above: **A reads 0.000000**, **B reads 0.625000**. |
| **RF burst** | the received chirp burst (I and Q). Each 16-sample frame is one cyclic-shifted up-chirp. |
| **Chip output — winning FFT bin** | the RAW argmax index straight off the chip (0..15), in FFT16's bit-reversed order. |

Each segment gets its **own panel with its own verdict in its own title**. One
axis carrying both a lock and a deliberate collapse cannot say which is which —
the −10 dB segment is *supposed* to scatter, and a shared axis makes that read
as breakage.

Display notes — worth knowing if you build a similar display: the chain is complex-input,
so the sink egresses **raw word floats** — the index plots directly with no
×32768 rescale. Every scope is sized to a real burst and the source repeats, and
the sink loops its genuine one-batch result (`server_repeat=True`), because GNU
Radio strands the tail of a finite stream and a scope sized ≥ its burst never
paints. And: **no trace may use line style 0 (NoPen).** A qtgui `time_sink`
channel set to markers-only draws *nothing* on any channel above channel 0 —
reproduced standalone with two sources of different amplitude, on a real X
display as well as offscreen. Both panels use a solid pen, and
the gate rejects style 0.

`test_css_transceiver_example.py` asserts all of it directly — segment A
matching at every plotted point, segment B missing across a derived SER band,
the two disjoint, the SER readouts agreeing with the traces beside them, every
frame identical across the run, and the `.grc`'s panel structure, titles,
wiring orientation and line styles, each with mutation
coverage proving the gate fails when it is violated.

## Run it

Two terminals, two commands — run both **from the repo root**.

**1. Host the chip** (terminal 1):

```bash
.venv/bin/python placekyt/main.py examples/css_transceiver/css_transceiver.kyt
```

Then **Simulation → Run as GNURadio Server** (port **58950**). Leave placeKYT
running.

**2. Drive it** (terminal 2) — open the flowgraph in GNU Radio Companion and
press **▶ Run** (F6):

```bash
gnuradio-companion examples/css_transceiver/css_transceiver.grc
```

> **First run on a fresh checkout:** the `.grc` imports
> `gnuradio.kyttar.css_demo_stim`, which GNU Radio resolves from the
> **installed** OOT — not the repo. Run `gr-kyttar/install.sh` once (it needs
> sudo) so the new stimulus module and ymls are in place, or GRC reports the
> import and every stim-derived value as unevaluatable.

Headless, the whole story in one command:

```bash
$ .venv/bin/python examples/css_transceiver/css_transceiver_demo.py
1. import css_transceiver.grc -> generic auto place-and-route -> build ...
   build OK — 82/120 cells, 5 placed blocks (the whole CSS receive spine on ONE chip)
2. drive the shipped 800-sample burst (+10 dB segment, then the -10 dB control) SATURATED on real simKYT ...
   recovered 50/50 index words; bit-exact vs the composed golden: True
3. segment A (+10 dB): 0 symbol errors / 24 -> SER 0.0000, message 'KYTTAR CSS'
   segment B (-10 dB, the on-chip negative control): 15 errors -> SER 0.6250, message '.[T]..:Dcs'
RESULT: EXACT — 'KYTTAR CSS' recovered at +10 dB (SER 0); the -10 dB control collapses (SER 0.62)
```

## What is verified

`verification/tests/test_css_transceiver_example.py` (14 tests) on real simKYT,
through the real pipeline:

- **The stim module's goldens are the BLOCKS' own references** — bits→symbols
  equals `ChirpSymbolMapperBlock.process_reference`, symbols→waveform equals
  `ChirpGeneratorBlock.process_reference_q15`, word for word.
- **Whole chain on the placed chip, bit-exact** vs the composed integer golden
  of the five RX blocks — driven **per-sample** AND fully **saturated** (the
  whole burst queued back to back, one continuous run).
- **Shipped-`.kyt` parity:** the file the user opens produces the same stream as
  a fresh import.
- **The decode:** SER **0** over the +10 dB segment with `KYTTAR CSS` recovered
  exactly, and a SER inside the derived band **[0.40, 0.75)** over the −10 dB
  on-chip control (measured: **0.625**, 15 errors of 24). The band is asserted
  as a range because the control is noise-driven, and both ends mean something:
  below **0.40** the control has started decoding and segment A's SER 0 proves
  nothing; at **0.75** and above the chain is emitting a *constant*, since the
  cheapest of the 16 stuck-at-k streams scores exactly 0.75 on this frame — a
  bare "SER is high" check would accept a dead chain as a healthy control.
- **Mutations (INV-4)** that must FAIL: decoding the raw index without `brev4`;
  dropping the `Delay(1)` alignment; a non-conjugated dechirp reference.
- **The real GR-client user path:** the shipped `.kyt` hosted exactly as the
  GUI's *Run as GNURadio Server* (port 58950), the shipped `.grc` GRC-generated
  and run under the real GNU Radio interpreter — the sink's recovered stream is
  the chip-proven golden, decodes to `KYTTAR CSS`, the control still fails, and
  the `server_repeat` loop is a clean repetition of the genuine batch.
- **The TRACES THE USER SEES**, tapped from the display block's six output
  channels in that same run: segment A's decoded trace equals its reference at
  **every** plotted point, segment B's misses across the derived band, the two
  never draw at the same position, the two SER readouts hold steady and equal
  the mismatches actually plotted beside them, and every scope frame across the
  run is identical (no drift).
- **The PLOT LAYOUT ITSELF**, read from the shipped `.grc`: two per-segment
  scopes rather than one four-trace axis, each fed only its own segment's pair,
  each title naming its segment, SNR and verdict with B's marked EXPECTED and
  CONTROL, the reference/decoded wiring orientation, no trace on NoPen, marker
  shapes distinct with the reference drawn wider, and a two-input SER readout
  wired to the two SER channels.
- Backed by **eight mutation tests** that replay every display defect — the
  phase drift, the A/B conflation, unequal channel counts, a drifting trace, a
  control that quietly starts decoding, all sixteen stuck-at constants, a
  readout that lies about its panel, and an unsettled readout — and prove this
  gate FAILS on each.

Run the user-path test **standalone** — it binds port 58950 and self-contends
with the other examples' user-path gates under concurrent load.

## Known limits

* **The `.grc`'s block params must be LITERALS.** The placeKYT importer
  evaluates block parameters *without* the flowgraph's `stim` module (that is a
  GNU Radio import, resolved only in the GR interpreter). Writing `n: stim.N`
  does **not** fail loudly — the importer silently falls back to the yml default
  (128), the chip is built for a 128-sample chirp while the host transmits
  16-sample chirps, and import/route/build all report OK while the chip emits a
  handful of garbage words. This example therefore carries a literal `n_css`,
  and `css_transceiver_demo._assert_chirp_len` turns any future drift into a
  loud failure. **Scope-sizing** variables may still be `stim.*` — GRC evaluates
  those, and the importer never reads them.
* **The geometry is PINNED, not auto-placed.** A generic auto-place of this
  design rotates the 44-cell FFT16 CCW and packs the chain into the top nine
  rows; that layout routes and builds "ok" and then does not work (measured: 6
  words out instead of 50, with values outside BinArgmax(16)'s legal 0..15
  range). Isolated further: the proven geometry with FFT16 alone rotated CCW
  does not even complete a run. This is a **composition-level** limitation, not
  a block defect — FFT16 passes `test_orientation_invariance.py` in all 8 D4
  orientations standalone. `build_kyt.py` therefore imports the `.grc` for its
  topology and pins the proven anchors before routing, the same hand-placed
  convention the FSK4 and 16-QAM modem examples use.
* Not verified: the literal Qt windows. The recovered data paths — including
  what each scope is fed — are gate-covered end to end through the real client
  stack; the display pattern (raw-word floats, burst-sized scopes,
  `server_repeat` looping) is the pixel-proven one from the other examples.

| File | What |
|------|------|
| `css_transceiver.grc` | GRC-first source — the flowgraph the user opens and runs. |
| `css_transceiver.kyt` | The placed + routed project (pinned geometry, 82/120 cells). |
| `css_decode_map.py` | The embedded display block: `s = brev4(index)`, the A/B split onto per-panel channels, and the live per-segment SER. |
| `build_kyt.py` | Regenerates the `.kyt` from the `.grc` (import → pin geometry → route → build). |
| `css_transceiver_demo.py` | Headless END-TO-END demo — the whole story in one command. |
| `../../gr-kyttar/python/kyttar/css_demo_stim.py` | The stimulus the `.grc` feeds from (TX + channel). |
