<!-- SPDX-License-Identifier: GPL-3.0-or-later -->

# FEC protocol link — a channel burst dispersed, corrected, and CRC-proven

**The story this demo tells:** a 2-bit consecutive channel burst is a FATAL
error for a Hamming(7,4) codeword — the code corrects single errors only, and
a double error inside one codeword gets silently *mis*-corrected into wrong
data. The 4×3 **block interleaver** disperses those two consecutive channel
bits into **two different codewords**, one correctable error each, so the
**Hamming decoder** heals both — and the frame *proves* itself by a **CRC-16
match**: the CRC word computed **on the chip** over the transmitted message
equals the CRC recomputed over the recovered bytes. Remove the interleaver
and the very same burst breaks the message AND the CRC match (the gate builds
that control variant on-chip and proves it fails).

Three streams share one placeKYT array (each stream = one injection landing +
one tagged egress off the shared duplex ports — the GR client contract, one
`out_tag` per `stream_id`):

```
'tx'    : bytes ─▶ UnpackKBits(8) ─▶ HammingEncoder(4:7) ─▶ BlockInterleaver(4×3) ─▶ coded bits out
'txcrc' : the same bytes ─▶ Crc16(0x1021/0xFFFF, frame_len=12) ─▶ the TX CRC word out
                 │ host channel: prefix + coded bits XOR the 2-bit burst
'rx'    : channel bits ─▶ BlockInterleaver(4×3, deint) ─▶ HammingDecoder(7:4) ─▶ PackKBits(8) ─▶ bytes out
```

The GRC window shows: the chip's interleaved coded bits, the channel stream
with the burst, the recovered byte stream (6 alignment zeros, then
`KYTTAR FEC73`, then pad zeros — the sinks emit q15/32768 floats, so a
×32768 rescale feeds each scope, and every display sink loops its genuine
one-batch result, `server_repeat=True`, because GNU Radio strands the tail
of a finite stream), and the **CRC frame verdict** panel: `TX CRC (chip)`,
`RX CRC (recomputed)`, `FRAME OK = 1`.

## The burst arithmetic (derived, not assumed)

All of this is computed and asserted in
`gr-kyttar/python/kyttar/fec_demo_stim.py` (the module the `.grc` feeds its
sources from) and re-proven against the blocks' own references in the gate:

* The streaming interleaver (BlockInterleaverBlock) is 1:1 with a group
  delay of N = 12: output block *b* carries input block *b−1* read
  column-major, `sigma(i) = (i mod 4)·3 + (i div 4)`; its first 12 outputs
  are zeros.
* **Flush:** 12 message bytes → 96 bits → 168 coded bits = 14 whole 12-bit
  blocks; 6 zero pad bytes (+84 coded bits) push the message through BOTH
  interleaver stages (one block of delay each) and keep the stream
  block-aligned. The pads are a *dropped partial CRC frame*
  (`frame_len=12`), so the on-chip CRC covers exactly the message.
* **Codeword alignment:** the decoder frames 7-bit codewords from stream
  start, but the two interleaver stages put 12+12 = 24 zeros ahead of the
  data — 24 mod 7 ≠ 0 would misframe *every* codeword. The channel vector
  prepends 60 more zeros: 12+60+12 = 84 = 12 whole zero codewords, which
  decode to exactly 6 zero bytes. The recovered stream is therefore
  byte-aligned: 6 zeros, the message, 4 pad zeros.
* **The burst:** two consecutive interleaved positions *g*, *g+1* inside one
  column walk carry coded positions exactly **cols = 3 apart** — and two
  coded positions o, o+3 straddle a codeword boundary iff `o mod 7 ∈
  {4,5,6}`. The demo's burst sits at TX-egress offset 28 (channel vector
  offset 60+28 = 88): it corrupts coded bits 13 (p0 of codeword 1) and 16
  (d1 of codeword 2) — one error each, both corrected. **Without** the
  interleaver the same two channel bits hit coded bits 28, 29 = d3, d2 of
  ONE codeword: the syndrome then flips a *third* bit (p0), the decoded
  byte 2 comes out wrong (`T` → 148), and the CRC catches it.

## What is verified

`verification/tests/test_fec_link_example.py` (10 tests) on real simKYT via
the real pipeline (import → generic auto-P&R → build), plus the shipped
artifacts:

- The stim module's goldens are bit-identical to the verified blocks' own
  `process_reference` chain (never self-consistent-only).
- Whole chain, all three streams interleaved on the placed chip: TX coded
  bits **bit-exact**, the on-chip CRC word == `0x2954`, RX recovers the
  exact message **through the burst**, and chip-CRC == CRC(recovered).
- Shipped-`.kyt` parity, per-sample AND fully **saturated** (all three
  bursts queued back-to-back, packet-interleaved, one continuous run —
  exact; every block in the chains is individually saturation-proven).
- **Mutations (INV-4):** the no-interleaver control — same burst, same
  chains minus the interleaver pair, built and run on-chip — FAILS to
  recover and FAILS the CRC match; a mismatched 3×4 deinterleaver against
  the 4×3 interleaver also breaks recovery.
- **The real GR-client user path:** the shipped `.kyt` hosted exactly as the
  GUI's *Run as GNURadio Server* (port 58950), the shipped `.grc`
  GRC-generated and run under the real GNU Radio interpreter — all three
  sinks recover their goldens with clean `server_repeat` repetition, and the
  CRC verdict holds on the recovered stream itself.

```
$ python examples/fec_link/fec_link_demo.py
   TX coded bits: 252/252, bit-exact vs golden: True
   TX CRC word (chip): ['0x2954'] (golden 0x2954)
   RX recovered through the 2-bit burst: 'KYTTAR FEC73', byte-exact: True
   CRC verdict: chip 0x2954 vs host-recomputed 0x2954 — MATCH
RESULT: EXACT — burst dispersed, corrected, and CRC-verified
```

Not verified: the literal Qt windows (the recovered data paths, including
what each scope is fed, are gate-covered end to end; the display pattern —
×32768 rescale + `server_repeat` looping with scope size = burst length — is
the same pattern data_link uses; the data path is gate-covered, the window
itself is not pixel-checked).

Pacing note: the `.grc` runs per-sample (`pipelined: 'no'`). The shipped
`.kyt` (44/120 cells, every corridor shortest-path) is saturated-proven, but
a plain GUI auto-P&R of this design can accept a layout whose port→CRC
corridor circumnavigates the array, and on that layout the 1:14
rate-expanding TX chain deadlocks under saturated drive (the route-quality
ratchet's documented hazard, measured). Per-sample is exact on every layout;
`build_kyt.py` additionally nudges the CRC cell so the shipped layout is
shortest-path everywhere.

## Run it

Two terminals, two commands — run both **from the repo root** (`placekyt/`).

**1. Host the chip** (terminal 1):

```bash
.venv/bin/python placekyt/main.py examples/fec_link/fec_link.kyt
```

Then **Simulation → Run as GNURadio Server** (port **58950**). Leave placeKYT running.

**2. Drive it** (terminal 2) — open the flowgraph in GNU Radio Companion and press
**▶ Run** (F6):

```bash
gnuradio-companion examples/fec_link/fec_link.grc
```

(Or, after pressing **Generate** in GRC once, run the generated top-block directly: `python3 examples/fec_link/fec_link.py`. That file is build output — it is not checked in, and GRC recreates it from the `.grc`.)

| File | What |
|------|------|
| `fec_link.grc` | GRC-first source (kyttar markers; byte/short↔float casts spliced on import). |
| `fec_link.kyt` | Auto-generated placed+routed project (shortest-path, saturated-proven). |
| `fec_link.py` | GRC-generated top block (+ `fec_link_crc_check.py`, the CRC-verdict epy block). |
| `build_kyt.py` | Regenerates the `.kyt` (auto-P&R + the CRC corridor-quality nudge). |
| `fec_link_demo.py` | Headless END-TO-END demo — the whole story in one command. |
