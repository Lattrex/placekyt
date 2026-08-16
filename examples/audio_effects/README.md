<!-- SPDX-License-Identifier: GPL-3.0-or-later -->

# Audio effects rack — three placed dataflow JOINS

Three placed effects, each a **join** — two independent arms recombining into
a multi-input block, the topology this repo's engine gained single-fire
support for in this example's build-out:

```
echo:    x ──┬──────────────────────────▶ Add ─▶ Gain(0.5) ─▶ IIRBiquad(butter 2, 0.15) ─▶ KeepOneInN(2) ─▶ out
             └▶ Delay(8) ─▶ Gain(0.5) ──▶

tremolo: x ──┬──────────────────────────────────────▶ Multiply ─▶ out
             └▶ NCO(250 Hz, 0.45) ─▶ C2R ─▶ AddConst(0.5) ─▶      (gain swings 0.05..0.95)

comb:    x ──┬──────────────────────────▶ Subtract ─▶ out
             └▶ Delay(5) ─▶ Gain(0.3) ──▶
```

## The join machinery

A join's arms each hand off as WRITE(operand)+JUMP, so the combiner used to
fire once **per arm** (double-firing / starving). Now:

- Add/Subtract/Multiply declare a data-only **`sink` entry** (`HALT`);
- the importer **elects the deepest arm as THE trigger**
  (`grc_import._elect_join_triggers`) and points every other arm's JUMP at
  `sink` via the existing `Connection.entry_override`;
- the live bridge injects **every landing** of a fan-out stream per sample,
  data-only arms first, trigger last (`port_config` landings +
  `sim_bridge._drive_one`).

Joins are **per-sample-paced** designs (the shipped `.grc`s request
`pipelined=False`). This is a REAL substrate constraint, not a preference:
the two arms have UNEQUAL depth and reconverge on one combiner — a
cross-block instance of the reconvergent fan-in hazard. Driven SATURATED
(whole burst back-to-back) the counting join combines MISALIGNED samples:
measured on the shipped echo `.kyt`, the output count stays correct
(200/200) but **every value is wrong**. The per-block serialize-LOCK is
block-internal; no cross-block fork→join lock exists yet.
`test_saturated_join_skew_KNOWN_LIMIT` pins this as an executable guard —
it FAILS the day the substrate gains cross-block serialization, which is the
signal to flip these to Full-speed.

## What is verified

Goldens are the IDENTICAL stock-GNU-Radio chains; bounds are **DERIVED** from
the per-block verified error reports (never tuned):

| Effect | Derived bound | Measured |
|--------|--------------:|---------:|
| echo (Delay+Gain+Add+IIRBiquad+KeepOneInN) | 25 LSB | 11 LSB |
| tremolo (NCO+C2R+AddConst+Multiply) | 16 LSB | 2 LSB |
| comb (Delay+Gain+Subtract) | 4 LSB | 2 LSB |

`verification/tests/test_audio_effects_example.py` (8 tests): all three
bounds, election applied on import, shipped-`.kyt` parity, and a **mutation**
— stripping the election double-fires the combiner and the gate catches it.
`placekyt/tests/test_gr_client_loop_examples.py::test_effect_echo_real_gr_client_join_fanout`
runs the **genuine GR client** duplex loop against the hosted echo `.kyt`,
pinning the multi-landing injection path.

Whole-chain proof for Delay, Gain, Add, Subtract, Multiply, IIRBiquad, NCO,
AddConst, KeepOneInN (+ ComplexToReal mid-chain).

**Engine limits found while building this — BOTH SINCE LIFTED** (the fan-out
work shipped StreamSplitterBlock + replicated-WRITE multi-handoff +
auto-splice; `test_fanout_chains.py` proves 3-arm block and port fan-outs):
- ~~a port supports ~2 fan-out arms~~ — ≥3 arms now auto-splice a splitter;
- ~~a BLOCK's output cell cannot fan out~~ — splitter / packet forms shipped.

**Routing:** the shortest-path auto-router lays every
block-to-block hand-off as a single abutted broker (route audit: 0 excess
cells, 0 loop-backs on all three `.kyt`s — they previously staircased up and
over every gap). The echo display sink LOOPS the genuine
one-batch result (`server_repeat=True`): GNU Radio strands the tail of a
finite stream, so a scope sized to exactly the delivered burst never paints
on its own (pixel-proven); tremolo/comb deliver well past their
256-sample scopes and paint directly.

```
$ python examples/audio_effects/audio_effects_demo.py
   echo: 20/120 cells, 200/200 samples, worst |err| 11 LSB (bound 25) -> OK
   tremolo: 24/120 cells, 400/400 samples, worst |err| 2 LSB (bound 16) -> OK
   comb: 14/120 cells, 400/400 samples, worst |err| 2 LSB (bound 4) -> OK
(cell counts vary a little per auto-P&R run; the abutment-first pack keeps
every adjacent hand-off route-free)
RESULT: WITHIN DERIVED BOUNDS — all three placed effects match stock GNU Radio
```

## Run it

Two terminals, two commands — run both **from the repo root** (`placekyt/`). Each
effect is its own design; the echo is shown, swap in `effect_tremolo` or
`effect_comb` the same way.

**1. Host the chip** (terminal 1):

```bash
.venv/bin/python placekyt/main.py examples/audio_effects/effect_echo.kyt
```

Then **Simulation → Run as GNURadio Server** (port **58950**). Leave placeKYT running.

**2. Drive it** (terminal 2) — open the flowgraph in GNU Radio Companion and press
**▶ Run** (F6):

```bash
gnuradio-companion examples/audio_effects/effect_echo.grc
```

(Or run the generated top-block directly: `python3 examples/audio_effects/effect_echo.py`.)

| File | What |
|------|------|
| `effect_echo.grc` / `.kyt` | Echo + biquad + decimation. |
| `effect_tremolo.grc` / `.kyt` | NCO tremolo. |
| `effect_comb.grc` / `.kyt` | Feedforward comb. |
| `build_kyt.py` | Regenerates all three `.kyt`s. |
| `audio_effects_demo.py` | Headless END-TO-END demo (all three). |
