<!-- SPDX-License-Identifier: GPL-3.0-or-later -->

# Complex math — two-stream Add/Sub/Multiply (the mixer demo)

**The wiring pattern to copy for any GRC design that combines two complex
streams.** Two analytic complex tones drive the three two-stream arithmetic
blocks placed on ONE chip:

```
tone A (10/256 cyc/sample) ──┬─▶ AddCC      ─▶ 'sum'  : a+b — the tones superpose (beat envelope)
tone B (17/256 cyc/sample) ──┼─▶ SubCC      ─▶ 'diff' : a−b — the same beat, B phase-flipped
        (0.45 each, Q15-snapped) └─▶ MultiplyCC ─▶ 'prod' : a×b — THE MIXER: one clean tone at 27/256
```

The multiply scope is the classic **up-conversion beat-note**: multiplying
analytic tones *adds* their frequencies, so the product is a single tone at
`f_a + f_b = 27/256` — asserted bin-sharp in the gate (the tones sit on exact
DFT bins of the 256-sample burst).

## The two-complex-stream client contract (why the flowgraph looks like this)

* **Each block gets its OWN source pair** — six ingress streams
  (`sum`/`b_add`, `diff`/`b_sub`, `prod`/`b_mul`). A complex ingress stream
  cannot fan out on-chip (the importer's auto-spliced fan-out relay is
  single-rail), so this is the same duplicated-ingress pattern fec_link uses.
* **Each landing cell pairs the two per-sample packets with its counting
  join, in any arrival order** (the gate drives both orders and gets
  identical bits).
* **The block's recovered stream rides its FIRST input's stream** — the
  deterministic out_tag-ownership rule (`engine.port_config.stream_targets`):
  name the sink after the block's first-port stream (`sum`, `diff`, `prod`).
  Without it, two ingress streams would both claim the chain's output tag and
  the duplex demux would depend on client thread order.
* **Complex egress**: each block's yi/yq rails leave on consecutive tags —
  the sink receives the interleaved I/Q stream; the `.grc`'s embedded
  `iq2c` blocks reassemble `gr_complex` for the scopes. The sources set
  `output_words: q15` (the outputs are Q15 *values*, not packed bits).

## What is verified

`verification/tests/test_complex_math_example.py` (7 tests) on real simKYT
via the real pipeline (import → generic auto-P&R → build), plus the shipped
artifacts:

- All three chip streams **bit-exact** vs each block's own
  `process_reference_q15` (never self-consistent-only), through the built
  design AND the shipped `.kyt`.
- **The mixer claim with teeth (INV-4)**: dominant product bin == 27/256 (not
  10, not 17), and a separable no-cross-term fake plus a conjugated-b
  correlator both MISMATCH the chip stream — the exactness gate genuinely
  sees the full complex product.
- **Live-GR cross-check**: `blocks.add_cc`/`sub_cc`/`multiply_cc` on the same
  Q15-snapped tones — add/sub EXACT, product within its derived 3-LSB floor.
- **Any arrival order**: a-first and b-first per-sample packet orders recover
  identical streams (the counting join contract).
- The deterministic out_tag ownership + per-stream (bi, bq) pair delivery
  (the engine contracts this example introduced) are asserted directly.

`verification/tests/test_examples_grc_userpath.py::test_complex_math_shipped_grc_user_path`
hosts the SHIPPED `.kyt` exactly as the GUI's *Run as GNURadio Server* and
runs the SHIPPED `.grc` GRC-generated under the real GNU Radio interpreter —
all three sinks bit-exact with clean `server_repeat` repetition.
`placekyt/tests/test_gr_client_loop_examples.py::test_complex_math_real_gr_client_six_streams`
drives the same verdicts through six genuine `kyttar.source` streams and
three `kyttar.sink`s via the DuplexRendezvous.

```
$ python examples/complex_math/complex_math_demo.py
   sum : 256/256 complex samples, bit-exact vs the block reference: True
   diff: 256/256 complex samples, bit-exact vs the block reference: True
   prod: 256/256 complex samples, bit-exact vs the block reference: True
   mixer: product dominant DFT bin 27/256 (f_a+f_b = 27/256) — CONFIRMED
RESULT: EXACT — all three chip streams bit-match their references; the mixer adds the tone frequencies
```

25/120 cells, 3 blocks. Not verified: the literal Qt windows (the recovered
data paths, including what each scope is fed, are gate-covered end to end).
Pacing: per-sample (`pipelined: 'no'` — the per-sample-paced join is the
documented two-stream client contract).

## Run it

Two terminals, two commands — run both **from the repo root** (`placekyt/`).

**1. Host the chip** (terminal 1):

```bash
.venv/bin/python placekyt/main.py examples/complex_math/complex_math.kyt
```

Then **Simulation → Run as GNURadio Server** (port **58950**). Leave placeKYT running.

**2. Drive it** (terminal 2) — open the flowgraph in GNU Radio Companion and press
**▶ Run** (F6):

```bash
gnuradio-companion examples/complex_math/complex_math.grc
```

(Or, after pressing **Generate** in GRC once, run the generated top-block directly: `python3 examples/complex_math/complex_math.py`. That file is build output — it is not checked in, and GRC recreates it from the `.grc`.)

| File | What |
|------|------|
| `complex_math.grc` | GRC-first source (six sources, three combiners, three complex sinks + scopes). |
| `complex_math.kyt` | Auto-generated placed+routed project. |
| `complex_math.py` | GRC-generated top block (+ the three `*_iq2c.py` epy display-glue modules). |
| `build_kyt.py` | Regenerates the `.kyt`. |
| `complex_math_demo.py` | Headless END-TO-END demo — all three streams + the mixer bin, one command. |
