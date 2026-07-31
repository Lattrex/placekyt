<!-- SPDX-License-Identifier: GPL-3.0-or-later -->

# 2P2S gain demo — four multiplexed streams across 4 chips

The first **multi-chip** example: **four independent gain streams** — one per chip —
multiplexed across the 2P2S dev board's **two parallel daisy-chains** of two chips
each. It proves the plumbing (multiplexing through ports AND across chips in
parallel) with a payload simple enough that any deviation is a routing bug, not DSP.

```
  chain A (chip0 ─▶ chip1):   stream A ─▶ chip0's gain   ;   stream B ─▶ chip1's gain
  chain B (chip2 ─▶ chip3):   stream C ─▶ chip2's gain   ;   stream D ─▶ chip3's gain
```

**One stream per gain** — that's the multiplexing. Each stream taps ONE gain cell
(0.5×) and exits its chain's tail. Streams **A + B share chain A's head** (chip0's
`x16_in`) **and tail** (chip1's `x16_out`); C + D share chain B's. They ride the same
ports, distinguished purely by hop count (which gain they tap) and by tag (which
stream's words at the tail). Each stream's output is **0.5×** its input.

The inter-chip link is a **transparent wire** (cell (9,0).EAST ⟷ cell (0,0).WEST): a
word crosses carrying its hop and self-routes on the next chip, exactly like an
intra-chip hop — no different than routing on one chip, just more hops. A stream
targeting chip1's gain simply enters chip0's head with a bigger hop, transits chip0's
bus, and lands on chip1's gain. Its output likewise transits chip1 to the tail.

## Separation of concerns

- **GNU Radio = the LOGICAL app** (the software): streams, blocks, what connects to
  what. Each `kyttar_source`/`kyttar_sink` carries only its `stream_id`.
- **placeKYT = the PHYSICAL app**: placement, routing, bitstream. It owns WHICH chip /
  port / hop / tag each `stream_id` maps to — resolved from the placed+routed design.

So the GRC flowgraph looks the same as any single-chip demo; placeKYT does the
multi-chip mapping. The `stream_id` is the only contract between them.

## Files

| File | What it is |
|------|------------|
| `gain_2p2s.kyt` | The 4-chip design — **open this in placeKYT**. Gain-on-bus @(1,0) per chip; A/B on chain A (tags 5/10), C/D on chain B (tags 15/20). |
| `gain_2p2s.grc` | The GNU Radio flowgraph — **4 source/gain/sink triples**, one per stream (A/B/C/D). Open in `gnuradio-companion`. |
| `gen_grc.py` | Regenerates `gain_2p2s.grc`. |

## Board

Targets `placekyt/resources/boards/dev2p2s.kdb` — 4 chips, chain A (chip0→chip1) +
chain B (chip2→chip3), each chain's head `x16_in` and tail `x16_out` on the FPGA.

## Run it (two terminals, from the repo root)

> **First-time GR setup:** the multi-chip path added a `process_batch_multichip` op
> to the Kyttar OOT. Install the current OOT so `gnuradio-companion` sees it:
> `cd gr-kyttar && ./install.sh` (needs sudo for the system dirs).

**1. Host the chips** (terminal 1) — launch placeKYT, open the 4-chip design:

```bash
.venv/bin/python placekyt/main.py
```

In placeKYT: **File → Open** → `examples/gain_2p2s/gain_2p2s.kyt`, then **Simulation →
Run as GNURadio Server**. Because the project has 4 chips, placeKYT hosts the
**multi-chip** server (status bar shows `… (multi-chip)`). Note the printed **port**.

**2. Drive it** (terminal 2) — open the flowgraph, set `server_port`, press **▶ Run**:

```bash
gnuradio-companion examples/gain_2p2s/gain_2p2s.grc
```

Each of the four streams' time sinks shows its input sine (top) vs the recovered
**0.5×** output (bottom). The four streams carry different stimulus and recover
independently — two chains, four multiplexed streams, cross-chip transit.

## Status — WORKING (placeKYT side, verified end to end)

Open `gain_2p2s.kyt` → host the multi-chip server → drive all 4 streams (via the GR
rendezvous client, each carrying only its `stream_id`) → each demuxed by tag at its
chain tail, each **0.5×**, no crosstalk:

| stream | in | out (0.5×) | path |
|--------|----|-----------|------|
| A | 0.5, 0.25 | 0.25, 0.125 | chip0 gain → chain A tail |
| B | 0.6, 0.3 | 0.3, 0.15 | transit chip0 → chip1 gain → chain A tail |
| C | 0.7, 0.35 | 0.35, 0.175 | chip2 gain → chain B tail |
| D | 0.8, 0.4 | 0.4, 0.2 | transit chip2 → chip3 gain → chain B tail |

Gate: `placekyt/tests/test_multichip_sim_server.py`,
`placekyt/tests/test_2p2s_plumbing.py`, `placekyt/tests/test_2p2s_routed_tap.py`.
