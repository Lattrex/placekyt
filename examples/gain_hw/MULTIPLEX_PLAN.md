<!-- SPDX-License-Identifier: GPL-3.0-or-later -->

# Two-gain multiplexed demo — plan + tagging spec

Goal: **two gain blocks on ONE chip, sharing ONE input port (x16_in) and ONE output
port (x16_out)**, each with its own GRC source/sink pair and its own live slider. This
proves the multiplexed-shared-port model (like the AM/SSB transceiver examples) on real
hardware, and pins down the tagging strategy for the loop-topology router work.

## Tagging spec (from the EXISTING sim scheme — not new)

Multiplexed streams already work in sim (SSB weaver: `tx`/`rx` share the ports). We reuse
that scheme verbatim. Two levels of addressing:

### Level 1 — intra-chip (EXISTS, works today)
Per `engine/port_config.stream_targets()`, each stream_id resolves to:
`{entry_addr, hop_count, data_addrs, out_tag}`.

- **Input:** the **WRITE/DATA is the tagged data stream** — `(hop, dest)` on the WRITE
  routes the sample to a specific cell/register. The **JUMP only triggers** the
  computation (it is NOT the data tag). Complex I/Q = two WRITE/DATA streams to two
  dest addresses in the SAME cell (I→addr0, Q→addr1), ONE JUMP. (Our gain is real =
  one WRITE/DATA per sample.)
- **Output:** each block's output WRITE carries a **distinct (hop, dest)** — both are
  valid demux tags. The host/sink demuxes recovered words by that tag (`out_tag`).

Proven values (SSB weaver, `stream_targets(..., build_result=)`):
`tx: entry=18 hop=15 addrs=[0,1] out_tag=10` · `rx: entry=23 hop=30 addrs=[1,2] out_tag=5`.

For this demo, the two GainBlocks at cells (4,1) and (4,2) each get a DISTINCT
(entry,hop,dest) in and a DISTINCT (hop,dest)/out_tag out — set once the GRC source/sink
blocks carry distinct `stream_id`s and the importer stamps them onto the input/output
connections (the `.kyt` alone has stream_id=None, so `stream_targets` is empty until GRC
drives it).

### Level 2 — chip-group tag (NEW — but STUBBED to 0 here)
With many parallel-x-series chips (Rev-A 2P2S = 4 chips) the FPGA also needs a
**chip-group tag** to pick which parallel chain a stream feeds, ABOVE the intra-chip
(hop,dest). We have NOT built this. On this single-logical-chip demo board there's only
one thing going on, so **chip_group is hardcoded 0** in the wire format now (so the format
is final and a real group tag prepends cleanly later, no rework).

## Build items (this session)

1. **FPGA `fake_kyttar_gain` → two gain cells.** Model TWO independent gain cells, each
   matched by its distinct input `(hop, dest)`, each with its own coefficient (own coeff
   WRITE), each emitting output with its distinct `(hop, dest)` tag. (Currently: one cell,
   coeff at dest=28.) Reset/held-ack sequencing preserved. Re-synth + flash.
2. **Server HW fast-path → multi-stream.** `sim_bridge` process_batch fast-path currently
   gates on `out_tag is None` (single stream only); extend to batch a TAGGED stream:
   inject at the stream's (entry,hop,dest), demux output by out_tag. (Sim multi-stream
   path already exists — mirror it in the batched HW path.)
3. **`HwChip.stream_samples` → tag params.** Accept per-stream (hop, dest, entry, out_tag),
   return tagged output the server demuxes.
4. **GRC `gain_hw.grc` → two source/sink pairs + two sliders.** Each pair a distinct
   stream_id; each slider drives its block's gain param → live coefficient WRITE.
5. **Live coeff writes.** slider → `set_grc_params` → coefficient WRITE to that block's
   cell (dest per block). Already proven for one block; extend to two.

## Why the hand-built gain_hw.kyt produces 0 output (ROOT CAUSE, 2026-07-13)

Two SEPARATE problems, found by tracing the sim (not guessing):

1. **Missing stream tags (FIXED).** The `.kyt` was hand-built (blocks dropped into the
   array, not imported from a GRC flowgraph), so its connections had `stream_id=None`
   / `out_tag=None` — the GRC importer is the only thing that used to set them. Now the
   **inspector lets you edit Stream ID / Out tag on a selected connection** (commit
   5553922). Setting a/b + 0/1 makes `stream_targets` resolve two streams.

2. **Shared input corridor fan-out conflict (ROUTER WORK, not fixed here).** Even with
   tags, the design emits 0 words because the two input routes SHARE cells that can't
   forward to both blocks:
       x16_in→gain  : (0,0)(1,0)(2,0)(3,0)(3,1)(4,1)
       x16_in→gain_1: (0,0)(1,0)(2,0)(3,0)(3,1)(3,2)(4,2)
   Both ride (0,0)→(3,0)→(3,1), then diverge. Cell (3,1) has ONE fwd_face but must
   forward to BOTH (4,1) and (3,2). So the build lands the injection at (3,0) — the
   divert point, NOT the gain cell — and the word never reaches the gain to trigger it.
   A shared input corridor fanning to >1 block needs a **fan-out BROKER** at the
   divergence cell (like `_apply_brokers` does for auto-P&R). Hand-drawn routes don't
   create one. THIS is the router work — and exactly why the loop/ring topology helps
   (tap off a backbone instead of fanning a shared corridor).

The proven-live two-gain HARDWARE path (fake_kyttar_gain2 + HwChip + server multi-stream
fast-path + live coeffs) works when each stream is injected with DISTINCT input tags
(sample dest 1 vs 2, jump entry 1 vs 2). The router must produce those distinct input
tags (or a fan-out broker) for the auto-flow to match the hardware.

## What is NOT this session (router agent)
The fan-out broker for a shared input corridor; loop/ring topology routing; the
snake-pattern block-fitting rule; ports-on-same-side constraint.
