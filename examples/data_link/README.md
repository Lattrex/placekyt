<!-- SPDX-License-Identifier: GPL-3.0-or-later -->

# Scrambled data link — an 11-block byte loopback, END-TO-END on one array

**What this example is for:** it exercises, in ONE placed chain, the byte /
FEC / digital-domain blocks that no modem example touches — the bit
manglers (Unpack/Not/AndConst/MapBB/Pack), both LFSR scrambler roles, the
differential codec pair, and the char↔float casts. Payload bytes go in, ride
the 11-stage chain, and come back out **byte-exact** — verified against the
**identical stock GNU Radio flowgraph** running under the real GR
interpreter (the strongest equivalence claim for the composition), with the
loopback identity asserted on top. Watch the GRC window: the "recovered
bytes" stem plot prints the payload's byte values (the sink emits q15/32768
floats, so a ×32768 rescale feeds the scope; the sink LOOPS the genuine
one-batch result for display — `server_repeat=True` — because GNU Radio
strands the tail of a finite stream and an exact-size buffer never paints;
pixel-proven).

```
bytes ─▶ UnpackKBits(8) ─▶ Not ─▶ AndConst(1) ─▶ MapBB([1,0]) ─▶ LFSRScrambler
                                                                     │ (0x8A/0x7F/7)
bytes ◀─ PackKBits(8) ◀─ FloatToChar(128) ◀─ CharToFloat(128) ◀─ LFSRScrambler ◀─ DiffDecoder(2) ◀─ DiffEncoder(2)
```

Stage logic: `Not` complements the full byte; `AndConst(1)` extracts the
(complemented) LSB; `MapBB([1,0])` re-inverts — a meaningful bit-mangling
triple that cancels. The additive LFSR scrambler is **self-inverse in sync**,
so the second instance descrambles. `DiffEncoder`∘`DiffDecoder` cancels. The
`CharToFloat`/`FloatToChar` pair uses scale **128** — a documented Q15 hardware
limit (`char_to_float` must map int8 into [-1,1), so scale ≥ 128; GR mirrors
the same scale, and the pair is identity on bits).

This is the whole-chain proof for 8 blocks that previously had only per-block
gates: UnpackKBits, Not, AndConst, MapBB, LFSRScrambler, DiffDecoder,
CharToFloat, FloatToChar.

## What is verified

`verification/tests/test_data_link_example.py` (6 tests) + `data_link_demo.py`,
on the built bitstream on real simKYT via the real pipeline (import → generic
auto-P&R → build):

- The demo payload AND all 256 byte values: placed chain == stock-GR chain ==
  the payload, exactly.
- Shipped-`.kyt` parity.
- **SATURATED (Full-speed) parity**: the shipped `.grc` runs `pipelined:
  'yes'`, so the whole payload queued back-to-back with NO inter-byte
  quiescence must recover the same bytes — proven on the shipped `.kyt`
  (`test_shipped_kyt_saturated_matches_per_sample`). This test is also the
  regression pin for the router's DEADLOCK-CYCLE guard: a routing where a
  block's output corridor threads through its own input-delivery broker
  hard-deadlocks under exactly this drive (sim `stop_reason='Deadlock'`,
  zero output) while passing every per-sample gate.
- **Mutation**: a descrambler seed out of sync breaks the match (the
  loopback identity alone would be blind to matched-pair corruptions — which
  is exactly why the GR golden is primary).

```
$ python examples/data_link/data_link_demo.py "ANY PAYLOAD"
   chip: 11 bytes, GR golden: 11, mismatches vs GR: 0, loopback identity: True
RESULT: EXACT — placed chain == stock GNU Radio == the payload
```

14/120 cells — the whole 11-block chain is a single ABUTTED column
(placement is abutment-first; only the port ingress and the
egress corridor are routed at all). Not verified: the literal Qt window (the data path is
gate-covered; see the ham examples' READMEs for the server/client gates that
cover the GRC run mechanics generally).

## Run it

Two terminals, two commands — run both **from the repo root** (`placekyt/`).

**1. Host the chip** (terminal 1):

```bash
.venv/bin/python placekyt/main.py examples/data_link/data_link.kyt
```

Then **Simulation → Run as GNURadio Server** (port **58950**). Leave placeKYT running.

**2. Drive it** (terminal 2) — open the flowgraph in GNU Radio Companion and press
**▶ Run** (F6):

```bash
gnuradio-companion examples/data_link/data_link.grc
```

(Or run the generated top-block directly: `python3 examples/data_link/data_link.py`.)

| File | What |
|------|------|
| `data_link.grc` | GRC-first source (kyttar markers; uchar/float casts spliced on import). |
| `data_link.kyt` | Auto-generated placed+routed project. |
| `build_kyt.py` | Regenerates the `.kyt`. |
| `data_link_demo.py` | Headless END-TO-END demo — pass any payload text. |
