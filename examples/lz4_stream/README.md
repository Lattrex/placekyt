<!-- SPDX-License-Identifier: GPL-3.0-or-later -->

# LZ4 stream — variable-rate compression on one array

**LZ4 compression as a live stream on the Kyttar array**, with the property no
fixed-rate DSP pipeline can have: **the output rate is a function of the
data**. A 1 KB payload that switches character mid-stream — 512 highly
repetitive bytes (`"KYTTAR LZ4 STREAM! "` repeated), then 512 random bytes —
goes through the SRAM-panel-backed `LZ4EncoderBlock`; the compressed stream
comes back to GNU Radio, is displayed against its worst-case buffer, and is
re-injected into the on-chip `LZ4DecoderBlock`, which recovers the payload
**byte-exactly**. The repetitive half compresses ~16:1, the random half is
~1:1 (the format's +0.5 % bound), the whole stream lands at **540 bytes for
1024 in (52.7 %)**.

```
raw : payload + EOB sentinel ─▶ LZ4EncoderBlock ─▶ compressed bytes (tag 'raw')
                                     │  15 cells + panel (input window [0,32768) + hash table)
cmp : compressed bytes ────────▶ LZ4DecoderBlock ─▶ recovered payload (tag 'cmp')
                                     │  8 cells + panel history (same array, same panel)
```

This is a **two-panel-client design — the design limit**: both blocks are
SRAM-panel-backed and share the chip's single `x1_out`/`x1_in` port pair. Two
things make that work:

- **The compressed hand-off goes through the client, not an on-chip net.**
  The panel word protocol is single-outstanding per *word*, so two
  controllers bursting at once would interleave register writes at the port
  merge and corrupt each other's transactions. The per-sample paced server
  (the panel contract) makes the client hand-off temporally exclusive **by
  construction**: the whole encode runs to quiescence inside the sentinel
  injection's settle, so the first compressed byte cannot reach the decoder
  until the encoder is idle.
- **The decoder keeps its proven `addr_base = 0`.** The embedded
  SRAM-controller's `addr_base` offsets only the *lookup* path (the write
  counter always starts at 0), so a read-write client cannot be relocated to
  a based region. The decoder instead reuses panel addresses `[0, len)`
  *sequentially* after the encoder: every decoder read is of an address the
  decoder itself wrote earlier in the same batch (LZ4's append-before-fetch
  invariant). The gate proves this is not aliasing by decoding a stream that
  *disagrees* with what the encoder left behind.

27 block cells on the 10×12 array (77/120 used including routing corridors
and the panel machinery). The example is **hand-placed** (open the shipped
`.kyt` — don't re-import): both folds are pure translations of the blocks'
proven layouts (encoder controller at (8,7), decoder controller at (6,10)),
the two controllers' to-panel corridors merge same-direction into the port
exit, and the shared `x1_in`/`x16_in` corridors fork at four
`CrossoverBlock`s — the only cell class two corridors may share.
`build_kyt.py` reproduces the placement and re-verifies the full round trip
on-chip before saving.

Both kyttar sources set **Output words = Raw** (bytes are integer words, and
the raw convention is symmetric — no q15 scaling on either side), which is
also what lets the compressed floats loop from `raw_sink` straight into
`cmp_src`. The `256` at the end of the payload vector is the encoder's
out-of-band **end-of-block sentinel**, not a byte.

## What is verified

`verification/tests/test_lz4_stream_example.py` (9 tests) — on the built
bitstream on real simKYT, and through the real hosted server:

- The encoder output is **model-exact** (540 bytes; the model itself is gated
  against the independent reference C decoder in the block's own suite) and
  the **full 1 KB round trip is byte-exact**, on both the rebuilt and the
  **shipped `.kyt`**.
- The **ratio gate**: the mixed payload compresses well under the
  all-literals floor; per-half, repetitive → 31 bytes (6.1 %), random →
  515 bytes (100.6 %). Proven non-vacuous by the **dead-hash-insert mutant**
  (INV-4), whose all-literals output *still round-trips* — only the ratio
  gate fires.
- The **panel-aliasing gate**: after encoding the payload, the decoder
  recovers the compressed form of the *reversed* payload — its reads come
  from its own writes, never encoder leftovers.
- The pass-2 **emission timeline is data-dependent** (sparse early tokens,
  literal flood at the tail), measured from the on-chip word timestamps.
- A one-byte corruption of the compressed stream breaks the round trip
  (the decode half of the gate is not satisfied by length alone).
- Every settle `stop_reason` is `QueueEmpty` (INV-56); the flags are
  asserted on the **generated Python** (INV-42): raw words ×2, port 58950
  ×4, `server_repeat` ×2, the worst-case scope buffer (1044), the literal
  window/hash params, and the exact payload vector.
- The full **user path**: the shipped `.kyt` hosted on port 58950, the
  shipped `.grc` GRC-generated and run under the real GNU Radio interpreter,
  both sinks byte-exact with clean `server_repeat` repetition.

Not verified: the literal Qt window (the data path is gate-covered; the
scopes follow the proven display recipe — full-size buffer +
`server_repeat=True` looping the genuine batch).

```
$ python examples/lz4_stream/lz4_stream_demo.py
   encoder: 540 compressed bytes for 1024 in (52.7%) — model-exact: True
   per-half (model): repetitive 512 -> 31 bytes (6.1%), random 512 -> 515 bytes (100.6%)
   pass-2 emission per time-eighth: [25, 0, 0, 0, 0, 0, 117, 398]   <- the output rate follows the DATA
   decoder: 1024 bytes recovered — round trip byte-exact: True
RESULT: EXACT — full 1 KB round trip through both panel clients on one array
```

## Run it

Two terminals, two commands — run both **from the repo root** (`placekyt/`).

**1. Host the chip** (terminal 1):

```bash
.venv/bin/python placekyt/main.py examples/lz4_stream/lz4_stream.kyt
```

Then **Simulation → Run as GNURadio Server** (port **58950**). Leave placeKYT
running.

**2. Drive it** (terminal 2) — open the flowgraph in GNU Radio Companion and
press **▶ Run** (F6):

```bash
gnuradio-companion examples/lz4_stream/lz4_stream.grc
```

Two scopes:

- **encoder output — variable rate**: the compressed stream against its
  **worst-case buffer** (1044 samples = 1024 + 1024/255 + 16, the LZ4
  incompressible bound). Only ~540 slots per batch carry data — the
  token-dense head is the repetitive half compressing, the long high-entropy
  tail is the random half passing through ~1:1. The fill fraction *is* the
  compression ratio.
- **recovered payload**: the byte-exact 1 KB round trip — the repeating text
  pattern, then the random half.

To change the payload, edit the `payload_src` vector (keep the trailing 256
sentinel) and set the `cmp_len` variable to the new compressed length (the
encoder-output scope shows it), then re-run `build_kyt.py` and re-host.

| File | What |
|------|------|
| `lz4_stream.grc` | GRC-first source (kyttar markers; the logical app). |
| `lz4_stream.kyt` | Hand-placed, on-chip-verified project — open this. |
| `build_kyt.py` | Regenerates + re-verifies the `.kyt` from the `.grc`. |
| `lz4_stream_demo.py` | Headless END-TO-END demo (full 1 KB round trip). |
