<!-- SPDX-License-Identifier: GPL-3.0-or-later -->

# 2P2S gain demo — two parallel daisy-chains across 4 chips

The first **multi-chip** example: a trivial gain payload spread across the 2P2S dev
board's **two parallel daisy-chains** of two chips each. It proves the plumbing —
multiplexing through ports AND across chips in parallel — with a payload simple
enough that any deviation is a routing/plumbing bug, not DSP.

```
  chain A:  x16_in ─▶ [chip0 gain 0.5] ─▶x16_out ──wire──▶x16_in[chip1 gain 0.5] ─▶ x16_out
  chain B:  x16_in ─▶ [chip2 gain 0.5] ─▶x16_out ──wire──▶x16_in[chip3 gain 0.5] ─▶ x16_out
```

Two 0.5× gains in series per chain → **0.25×** at each chain's output. Both chains
run at once with different stimulus and recover independently (no crosstalk). The
FPGA on the real board selects the chain and merges the two tails; in placeKYT the
`MultiChipSimServer` does the equivalent.

## Files

| File | What it is |
|------|------------|
| `gain_2p2s.kyt` | The 4-chip design — **open this in placeKYT** (hand-placed, gain@(0,0) per chip). |
| `gain_2p2s.grc` | The GNU Radio flowgraph — two chains, each a `kyttar_source`(chip_id)→`kyttar_sink`(stream_id). Open in `gnuradio-companion`. |
| `gen_grc.py` | Regenerates `gain_2p2s.grc`. |

## Board

The design targets `placekyt/resources/boards/dev2p2s.kdb` — 4 chips, chain A
(chip0→chip1) + chain B (chip2→chip3), each chain's head `x16_in` and tail
`x16_out` exposed to the FPGA.

## Run it (two terminals, from the repo root)

**1. Host the chips** (terminal 1) — launch placeKYT, open the 4-chip design:

```bash
.venv/bin/python placekyt/main.py
```

In placeKYT: **File → Open** → `examples/gain_2p2s/gain_2p2s.kyt`, then
**Simulation → Run as GNURadio Server**. Because the project has 4 chips, placeKYT
hosts the **multi-chip** server (the status bar shows `… (multi-chip)`). Note the
printed **port**, and the resolved per-stream landing on the server console
(entry/hop/data_addr — for this at-landing design: entry 28, hop 30, addr 0).

**2. Drive it** (terminal 2) — open the flowgraph, set `server_port` to the printed
port, and press **▶ Run**:

```bash
gnuradio-companion examples/gain_2p2s/gain_2p2s.grc
```

Each chain's time sink shows its input sine (top) vs the recovered **0.25×** output
(bottom). Chain A and chain B carry different stimulus and recover independently.

> **First-time GR setup:** the multi-chip path added a `process_batch_multichip`
> op to the Kyttar Source/Sink OOT. Install the current OOT so `gnuradio-companion`
> sees it: `cd gr-kyttar && ./install.sh` (needs sudo for the system dirs).

## How the addressing works

Each `kyttar_source` carries a **`chip_id`** (which chain's head it feeds) plus the
head's resolved landing (`entry_addr`/`hop_count`/`data_addrs`) and the chain tail
(`out_chip`). The two sources rendezvous and dispatch **one**
`process_batch_multichip` RPC; the server drives each chain's head (routed-aware),
relays across the inter-chip wire, and returns each chain's recovered words, demuxed
by chain to its sink. `chip_id` is placement-derived — the GRC blocks otherwise look
exactly like the single-chip demo.

## Status — WORKING (placeKYT side)

Verified end to end headlessly: open `gain_2p2s.kyt` → host the multi-chip server →
both chains driven by `chip_id`, each recovers its own input at 0.25×, no crosstalk
(`placekyt/tests/test_multichip_sim_server.py`, `test_2p2s_routed_tap.py`,
`test_2p2s_plumbing.py`). The GR OOT client (`process_batch_multichip`) + this `.grc`
are the front-end; run `gr-kyttar/install.sh` then drive from `gnuradio-companion`.
