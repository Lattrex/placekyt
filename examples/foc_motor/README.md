<!-- SPDX-License-Identifier: GPL-3.0-or-later -->

# FOC motor control — the current loop on one Kyttar array

Field-oriented control is how you make a permanent-magnet synchronous motor
(PMSM) produce smooth torque: measure the phase currents, rotate them into the
rotor's own reference frame so the torque-producing and flux-producing
components separate, regulate each with its own PI controller, rotate the
voltage command back out, and modulate it onto the three inverter legs.

This example puts the **command half** of that loop on the array:

```
e_d ──> PI(d) ──┐
                 ├──> CordicRotate(sign=+1, θ) ──> SVPWM ──> duty a, b, c
e_q ──> PI(q) ──┘         (inverse Park)
θ ────────────────────────^
```

`e_d` and `e_q` are the d- and q-axis current errors, `θ` is the rotor
electrical angle. The output is the three inverter duty cycles, as one 3-word
Q15 packet in fixed order a, b, c.

**87 of the 120 cells** on a 10×12 array.

## What is not here, and why

The **measurement half** — the Clarke transform and the forward Park rotation,
which turn two sensed phase currents into (i_d, i_q) — is not in this build.
Both blocks ship and are chip-proven, and the sub-chain **routes, builds and
runs bit-exactly on its own**: measured at 75 cells (Clarke + `CordicRotate
(sign=-1)` + a relay per arm), three distinct arm hops, `QueueEmpty` on every
run, output `0x868, 0x870` matching the host golden exactly.

What does not fit is the *whole loop on one 10×12 array*, and the reason is
worth stating precisely because it is not the obvious one:

**the limit is routing, not cells.** The full chain is 55 block cells of 120 —
under half the array. But each face-locking rendezvous needs its arms delivered
on *distinct faces* by *corridors that do not share cells*, and the full loop
has three rendezvous (two CORDIC at N=3, one SVPWM at N=2) competing for the
same free space. Over roughly 2600 placements — random, structured and
hill-climbed — the best whole-chain result still left 2 of 13 nets unrouted,
and the unrouted nets were always rendezvous arms. Cost a design like this by
**arm count**, not cell count. (Recorded as INV-71.)

## The loop rate

Measured on the real placed + routed + built chip, driving the shipped `.kyt`
through the simulator and reading each output word's capture time with
`read_port_words_timed`:

| | measured |
|---|---|
| latency to the first duty word | **17,576.9 ns** |
| latency to the complete 3-word packet | **17,861.5 ns** |
| cadence of the three duty words | 142.29 ns apart |
| equivalent control-loop rate | **56.0 kHz** |
| cells | 87 / 120 (45 active in one iteration) |
| instructions per iteration | 634 |

56 kHz sits **above the 10–40 kHz band** typical of industrial FOC current
loops, with the whole rotation, both PI controllers and the space-vector
modulator on the fabric.

**These are simulated times.** They come from simKYT's timing model for this
placement and routing. They are not silicon-certified, and a real device will
differ.

### There is no steady-state throughput number here, and that is the finding

The brief asked for the sustained rate under full saturation, and the honest
answer is that **this chain has no steady state**. Measured, three ways:

* a **second** control iteration wedges — zero further words, every run
  reporting `Deadlock` *after* the group closes (a real wedge by INV-67, not
  the healthy mid-group hold);
* an **arm-saturated** drive — one iteration's three arm words enqueued
  back-to-back through `queue_words_physical`, the INV-19 saturated path —
  wedges and emits nothing at all;
* driving the arms in **reverse order** wedges.

So the chain sustains exactly **one control iteration**, driven arm-by-arm in
lock order, and that iteration is bit-exact. All three boundaries are pinned as
guard tests: if any of them ever moves, a test fails and says so.

The cause is structural and already documented per block. Both
`CordicRotateBlock` (N=3) and `SVPWMBlock` (N=2) carry a measured whole-burst
depth of **one**, because the serialize-LOCK release has to ride the single
cell abutting the rendezvous — there is no spare face for a release corridor of
its own. Chained, those walls compose: the deeper chain is still holding the
previous sample when the next group is admitted.

For a host-paced controller this is not a defect — an FOC loop is one sample
set per control period by construction, which is exactly how the example drives
it. It does mean the fill-versus-steady-state gap this measurement was meant to
expose cannot be measured on *this* chain: there is only fill.

What *can* be measured is the same trade one stage down. `PIControllerBlock`
alone, driven fully saturated:

| `pipeline_lock` | steady-state | per sample |
|---|---|---|
| `True` (the INV-19 serialize-LOCK) | 1,053,552 samples/s | 949.2 ns |
| `False` | 1,119,608 samples/s | 893.2 ns |

The lock costs about **6% of throughput**. The example keeps it on: the PI
integrator is a feedback loop, and correctness under saturation is not
tradeable for rate. Its single-shot latency is 1,338.5 ns against a 949.2 ns
steady-state period — the pipeline fill the deeper chain never gets to show.

## Where 16 bits is, and is not, enough

Q15 I/O is at or above industrial practice: 12–14-bit current ADCs, 12–16-bit
PWM comparators, and a 16-bit angle is 0.0055° — finer than typical encoder
resolution. The rotation itself is comfortable; `CordicRotateBlock` is
unity-gain to a measured worst case of 24.75 LSB over a 48k-case sweep.

The resolution risk concentrates in **one place: the PI integrator.** `Ki·e` per
step can fall well below one Q15 LSB, and in a 16-bit accumulator it then
vanishes silently — the loop simply never cancels a small steady-state error,
and nothing about the output looks wrong.

This is measured, not argued. At `Ki·e = 0.030 LSB/step`, over 1200 steps, the
shipped 32-bit accumulator drifts **0.97 LSB** against a double-precision
reference, while a 16-bit-only accumulator — built and run on the same chip —
integrates **exactly zero**. That is why the block carries a 32-bit hi/lo
register-pair integrator with the high half written first.

So: 16 bits is enough for the currents, the angle, the rotation and the duty
cycles. It is **not** enough for the integrator, and that one place is where
the extra precision has to go.

## Files

| file | what |
|---|---|
| `foc_motor.kyt` | the shipped placed + routed chip — open this directly |
| `foc_motor_demo.py` | placement, build, drive, and the loop-rate measurement |
| `build_kyt.py` | regenerate the `.kyt`; reverifies on chip before saving |
| `verification/tests/test_foc_motor_example.py` | the whole-chain gate suite |

## Running it

```bash
QT_QPA_PLATFORM=offscreen .venv/bin/python examples/foc_motor/foc_motor_demo.py
```

You will see the three duty words, the golden they are compared against, the
per-iteration latency, the duty-word cadence, and the equivalent loop rate in
kHz.

To regenerate the `.kyt` (it is only written if the chip run is exact):

```bash
QT_QPA_PLATFORM=offscreen .venv/bin/python examples/foc_motor/build_kyt.py
```

The gates:

```bash
QT_QPA_PLATFORM=offscreen \
  .venv/bin/python -m pytest verification/tests/test_foc_motor_example.py -q
```

## Notes on the construction

**Each ingress arm is driven through its own relay block.** This is
load-bearing, not tidiness. Nets fanned straight out of the chip input port
into a face-locking block's arms produce `input_landings` with *distinct
entries and data addresses* — the signature that normally means "distinct
arms" — while every word actually lands on the **port cell**, and so arrives on
**one face**, which the rendezvous LOCK bars. The chain then routes, builds,
and emits nothing, with every run reporting `QueueEmpty` because the work never
started. The gate asserts the three arms land on three distinct **hops**, which
is the property that actually distinguishes them.

**Placement is by hand-authored anchors, routed route-only.** `auto_pnr`
re-packs the placement compactly and herds the arm corridors together, which is
the INV-70 head-of-line wedge; the anchors here are one of the few sets that
both route and deliver.

**The plant is not modelled.** This example measures the controller, not a
closed loop against a motor: `e_d`, `e_q` and `θ` are supplied by the host. A
closed-loop settle test would need the measurement half of the chain, which —
per the section above — does not fit alongside the command half on one array.
