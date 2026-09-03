<!-- SPDX-License-Identifier: GPL-3.0-or-later -->

# TMR pipeline — a triple-redundant path and a single path, on one array

**Triple-modular redundancy on the Kyttar array**, demonstrated the only way it
can be: next to an ordinary non-redundant stream **on the same die, in the same
run**. A 256-byte ramp fans out to three identical workers on disjoint cells; a
majority voter recombines them into a 2-word `[value, status]` packet per
sample. A second, independent stream rides the same array through a single
`GainBlock`. Redundancy here is an **area** cost, not a **performance** cost:
the three arms compute concurrently, the voter only sequences three
already-computed words, and the customer picks the redundancy factor **per
path, at place-and-route time** — TMR for the safety-critical path, single-path
for the rest, same die, no different hardware.

```
                     ┌─▶ worker A ─▶ AddConst(0) ─▶ [west]  ┐
ramp ─▶ split ───────┼─▶ worker B ─▶ AddConst(f) ─▶ [north] ┼─▶ TMRVoter ─▶ [value, status]
(stream 'tmr')       └─▶ worker C ─▶ AddConst(0) ─▶ [south] ┘    packets on x16_out

ramp ─▶ GainBlock(0.5) ─▶ 0.5×ramp on x16_out        (stream 'solo', the SAME array)
```

- **The workers are `StreamSplitterBlock` identities** — exact, memoryless,
  delay 0 — so "the arms agree" is bit-exact and an injected fault differs by
  exactly the amount injected (a Q15 multiply would mangle byte values).
- **Fault injection is depth-neutral**: an `AddConstBlock` sits on **all
  three** arms with constants `0 / f / 0`. Toggling `f` changes path B's value
  by exactly one word LSB without changing any arm's pipeline depth — a
  detected fault can never be confused with a timing artifact. `f` is a GRC
  variable in signal units: `3.0517578125e-05` (= 1/32768, one word LSB) is
  the shipped fault; set it to `0.0` for the healthy run. (It is a float
  literal, not an expression — the placeKYT importer resolves a marker param
  through one variable lookup and keeps the block default for anything it
  cannot literal-coerce.)
- **The voter tells the arms apart by ARRIVAL FACE** (west/north/south — the
  only stream identity on a clockless array), rotating an arbiter lock
  a → b → c, and emits `[value, status]`: status `0` = all agree, `1`/`2`/`3`
  = that path disagreed (the value is still the correct majority), `7` = no
  majority (value = sentinel `65535`).

What you see, per the shipped fault `f` = 1 LSB on path B: **every packet is
`[ramp byte, 2]`** — the value rail is still the exact ramp (TMR corrects the
fault) and the status rail names path B on every sample. With `f = 0.0` every
packet is `[ramp byte, 0]`. The solo stream is 0.5× the ramp throughout.

## The chain

| GRC block id | placeKYT block | role |
|--------------|----------------|------|
| `kyttar_splitter` | StreamSplitterBlock | 3-way fan-out (1 cell) |
| `kyttar_splitter` ×3 | StreamSplitterBlock | the redundant identity workers |
| `kyttar_add_const` ×3 | AddConstBlock | depth-neutral fault injectors, `const` = `0 / f / 0` |
| `kyttar_tmr_voter` | TMRVoterBlock | face-rotation majority voter (4 cells) |
| `kyttar_gain` | GainBlock | the single-path stream (`gain=0.5`) |

12 block cells on the 10×12 array (50/120 used including routing corridors,
as measured at authoring — the demo prints the live count).
The example is **hand-placed** (open the shipped `.kyt` — don't re-import): the
voter's rendezvous cell needs its three arms delivered on three **distinct
faces**, so the three injectors sit west/north/south of it with the voter's own
fold running east. `build_kyt.py` reproduces the placement and re-verifies the
full ramp on-chip before saving.

Both kyttar sources set **Output words = Raw** explicitly: byte values and
status codes are integer words, not Q15 samples — and the raw convention is
symmetric, so the byte ramp feeds the sources directly (no q15/32768 scaling
on either side). The two streams share `x16_in`/`x16_out`, demuxed by
`stream_id` (`"tmr"` / `"solo"`); a `Deinterleave` block splits the packet
stream into one scope for values and one for status.

## What is verified

`verification/tests/test_tmr_pipeline_example.py` (9 tests) — on the built
bitstream on real simKYT, and through the real hosted server:

- **f=0**: 256 packets, every one `[ramp byte, 0]`; **f=1 LSB on path B**: 256
  packets, every one `[ramp byte, 2]` — values still the exact ramp. A 100-LSB
  fault is equally corrected (majority, not magnitude).
- The **solo stream** recovers 0.5× the ramp bit-exactly in the same runs.
- **Shipped-`.kyt` parity**, and the full **user path**: the shipped `.kyt`
  hosted on port 58950, the shipped `.grc` GRC-generated and run under the
  real GNU Radio interpreter, both sinks bit-exact with clean
  `server_repeat` repetition.
- Every per-sample settle `stop_reason` is `QueueEmpty` (nothing wedged).
- The flags are asserted on the **generated Python** (raw output words, port
  58950, looped display batches) — the `.grc` text alone can silently fall
  back to defaults.
- **Mutations**: moving the same fault to path A flips every status 2 → 1 and
  breaks the expectation (the gate sees WHICH arm faulted); the faulted build
  cannot satisfy the healthy golden.

Not verified: the literal Qt window (the data path is gate-covered; the scopes
follow the proven display recipe — full-size buffer + `server_repeat=True`
looping the genuine batch).

```
$ python examples/tmr_pipeline/tmr_pipeline_demo.py
   tmr: 256 packets; values == ramp: True; statuses: [2]
   solo: 256 words; == 0.5x ramp: True
RESULT: EXACT — voter packets and single-path stream both match the pinned goldens
```

## Run it

> **Open the `.kyt`, not the `.grc`.** The three-arm voter is a hand-placed
> rendezvous; importing `tmr_pipeline.grc` into placeKYT does **not** place and
> route it correctly. Always open the shipped `tmr_pipeline.kyt`, which is
> already placed and routed.

Two terminals, two commands — run both **from the repo root** (`placekyt/`).

**1. Host the chip** (terminal 1):

```bash
.venv/bin/python placekyt/main.py examples/tmr_pipeline/tmr_pipeline.kyt
```

Then **Simulation → Run as GNURadio Server** (port **58950**). Leave placeKYT running.

**2. Drive it** (terminal 2) — open the flowgraph in GNU Radio Companion and press
**▶ Run** (F6):

```bash
gnuradio-companion examples/tmr_pipeline/tmr_pipeline.grc
```

Three scopes: the voted values (the ramp), the vote status (a flat line at 2 —
or at 0 after setting `f` to `0.0`), and the solo stream (0.5× ramp). To
change `f` on the chip, edit the path-B `AddConstBlock`'s `const` in placeKYT
(or re-run `build_kyt.py` after changing `f` in the `.grc`) and re-host.

| File | What |
|------|------|
| `tmr_pipeline.grc` | GRC-first source (kyttar markers; the logical app). |
| `tmr_pipeline.kyt` | Hand-placed, routed, on-chip-verified project — open this. |
| `build_kyt.py` | Regenerates + re-verifies the `.kyt` from the `.grc`. |
| `tmr_pipeline_demo.py` | Headless END-TO-END demo (both streams, full ramp). |
| `tmr_pipeline.py` | GRC's generated flowgraph — regenerated output, do not edit. |
