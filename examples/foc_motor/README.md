<!-- SPDX-License-Identifier: GPL-3.0-or-later -->

# FOC motor control — the current loop on Kyttar

Field-oriented control is how you make a permanent-magnet synchronous motor
(PMSM) produce smooth torque: measure the phase currents, rotate them into the
rotor's own reference frame so the torque-producing and flux-producing
components separate, regulate each with its own PI controller, rotate the
voltage command back out, and modulate it onto the three inverter legs.

The **shipped `.kyt`** puts the **command half** of that loop on one array:

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

`foc_motor.grc` is the **whole** loop — measurement half, command half, the
host-side error former and a motor to close it. See **The full loop** below;
it runs closed across two arrays at a measured **32.17 kHz**.

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
| **sustained inter-iteration interval** | **17,925.1 ns** |
| **sustained control-loop rate** | **55.8 kHz** |
| cells | 87 / 120 (45 active in one iteration) |
| instructions per iteration | 634 |

The chain **streams**: consecutive control iterations, each with different
inputs, every duty word bit-exact against the host golden and every simulator
run settling `QueueEmpty`. The sustained figure is the mean interval between
completed duty packets over six consecutive iterations; the spread across them
is under 65 ns.

55.8 kHz sits **above the 10–40 kHz band** typical of industrial FOC current
loops, with the whole rotation, both PI controllers and the space-vector
modulator on the fabric.

**These are simulated times.** They come from simKYT's timing model for this
placement and routing. They are not silicon-certified, and a real device will
differ.

### Fill versus steady state

The sustained interval (17,925 ns) is within 0.4% of the first packet's latency
(17,861 ns). The two being equal is the finding: **this chain re-arms, it does
not pipeline.** Each rendezvous bars its arms until the current group has
cleared the block, so only one control iteration is ever in flight, and the
steady-state period is therefore the whole-chain traversal time rather than the
slowest single stage. There is no pipeline-fill discount to collect.

For a host-paced controller that is the right shape: an FOC loop is one sample
set per control period by construction, which is exactly how the example drives
it. It also means the loop rate is bounded by chain *depth*, so shortening the
chain — not widening it — is what buys rate here.

Two drive patterns still do not stream, both pinned as guard tests:

* an **arm-saturated** drive — the arm words enqueued back-to-back through
  `queue_words_physical`, the INV-19 saturated path — wedges and emits nothing;
* driving the arms in **reverse order** wedges.

Both are the same cause, and it is topological rather than arithmetic: the three
arm corridors share cells, so a word held at a barred face blocks the segment a
later arm must transit (INV-70). Saturated therefore does **not** equal
per-sample at chain level, and the sustained figure above is the per-sample one.

The per-stage trade is visible one level down. `PIControllerBlock` alone, driven
fully saturated:

| `pipeline_lock` | steady-state | per sample |
|---|---|---|
| `True` (the INV-19 serialize-LOCK) | 1,053,552 samples/s | 949.2 ns |
| `False` | 1,119,608 samples/s | 893.2 ns |

The lock costs about **6% of throughput**. The example keeps it on: the PI
integrator is a feedback loop, and correctness under saturation is not
tradeable for rate. Its single-shot latency is 1,338.5 ns against a 949.2 ns
steady-state period — a single stage *does* pipeline, which is precisely the
overlap the whole chain's per-block arm bars give up.

## The full loop

`foc_motor.grc` is the **whole** loop as a flowgraph — the measurement half,
the host-side error former, the command half, and a PMSM plant that closes it.

**Where the loop closes.** GNU Radio's stream scheduler forbids cycles
outright: a stream ring is refused at `tb.start()` with `flow graph has
loops!`, and no buffer sizing or priming makes one legal. A control loop *is*
a cycle, so the ring is closed **inside one block**. `foc_host` holds every
piece of state that has to persist from one control period to the next — the
motor's two stationary-frame currents and its rotor angle, and the two PI
integrators — and advances a whole period per sample. Its feedback is an
internal assignment rather than a wire, so it takes **no stream input at
all**, and what the scheduler sees is a tree rather than a ring:

```
                       ┌─ ia, ib, θ ─> Clarke ─> CordicRotate(sign=−1, θ) ─> (i_d, i_q)
                       │                            (forward Park)
   foc_host ───────────┤
   ┌──────────────┐    │                ┌─> PI(d) ─┐
   │ PMSM plant   │    └─ e_d, e_q, θ ─>┤          ├─> CordicRotate(sign=+1, θ) ─> SVPWM ─> a,b,c
   │ e = ref − i  │                     └─> PI(q) ─┘
   │ feedback ↺   │
   └──────────────┘   the loop closes HERE, as an assignment, not a wire
```

Every word `foc_host` emits is the **live** value of that wire in the loop it
is running right now, computed this period from the previous period's duties —
this is a genuinely closed loop, not a replay of a canned recording. The array
recomputes both halves from those words, and the scopes show the array's
result over the host's own, so the two are compared on screen rather than the
array's merely being displayed.

The flowgraph is a **logical** description: it is what you place, not a
placement. The whole loop does not route on one array (see above), so the
natural split is **measurement half on one die, command half on the other**.
The two are joined only by (i_d, i_q) out and (e_d, e_q) back, plus θ, so the
crossing is narrow — and a chip crossing costs about **40 ns**, negligible
against a ~31 µs loop period.

### What the flowgraph is careful about

* **Every ingress arm has its own `kyttar_source` with a distinct stream id,
  and its own relay block.** A net fanned straight off the input port lands
  every word on the port cell — hence on one face — which the rendezvous LOCK
  bars, and the chain then builds, routes and emits nothing (INV-71).
* **θ is delivered as two independent arms**, `th_park` and `th_ipark`, one per
  rotation. It feeds both, and on-chip fan-out to two rendezvous arms is the
  hard part; two deliveries sidestep it.
* **SVPWM emits three words per sample** on one stream, so its sink is set to
  q15 output words and the packet is split with a Deinterleave at 3.
* **`server_port` is 58950 everywhere.** A `0` there makes `kyttar_source`
  silently no-op — the flowgraph runs and does nothing, with no error.

### The full-loop rate

Measured with the loop actually closed across two arrays
(`foc_loop_twochip.py`), reading each output word's capture time:

| | measured |
|---|---|
| measurement half, sustained | **13,142.7 ns** (76.09 kHz alone) |
| command half, sustained | **17,940.5 ns** (55.74 kHz alone) |
| **the full loop, per sample** | **31,083.2 ns** |
| **full-loop control rate** | **32.17 kHz** |
| cells | 75 + 87 of 120 each |

The two halves are **strictly serial within a control period** — sample *k*'s
duties cannot be computed until sample *k*'s currents have been measured and
rotated — so the loop costs their **sum**, not the larger of them. That is why
a 55.8 kHz command half and a 76.1 kHz measurement half give a ~32 kHz loop.

Every iteration is bit-exact against the host golden on **both** halves, and
every simulator run settles `QueueEmpty`. **These are simulated times**, from
simKYT's timing model — not silicon-certified.

32.17 kHz sits inside the 10–40 kHz band typical of industrial FOC current
loops, with the entire loop — both rotations, both PI controllers and the
space-vector modulator — on the fabric.

### Does the controller actually control?

The rate says the loop is fast; it does not say the loop *regulates*. That is
checked separately, in pure host simulation, with every on-chip stage computed
by the blocks' own pinned integer models and the motor integrated by forward
Euler:

* **i_q converges to its reference** — within 2% by step 432, and it starts
  from zero, so it genuinely had to get there;
* **i_d holds at zero** (within 0.0005), which is what a surface PMSM wants:
  the magnets already supply the rotor flux, so d-axis current is pure loss;
* it does this **against the back-EMF**, 7 V of it on a 24 V bus at 200 rad/s
  electrical — rejecting that disturbance is what a current loop is *for*.

The plant is deliberately a current-loop model and not a drivetrain: `omega_e`
is held constant, because a current loop closes two to three orders of
magnitude faster than any real machine's mechanical pole. The inverter is
ideal — no dead time, no device drop, no switching ripple. Those change a real
drive's current ripple; they do not change whether the regulator regulates.

**One trap worth naming**, because it is silent and it bites anyone closing
this loop: `PIControllerBlock.process_reference_q15` is a **batch** function
whose 32-bit accumulator is local to the call. Fed a whole error sequence it is
exactly the chip. Fed one sample per call — the only thing a closed loop *can*
do — the integrator resets every step, the integral action silently ceases to
exist, the loop settles short of its reference, and changing `ki` changes
nothing at all. `foc_loop_model.StatefulPI` carries the accumulator across
calls and is gated bit-identical to the block's batch model.

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
| `foc_motor.kyt` | the shipped placed + routed chip (command half) — open this directly |
| `foc_motor.grc` | **the FULL loop as a flowgraph** — every block, every connection |
| `foc_motor_demo.py` | placement, build, drive, and the command-half rate measurement |
| `foc_loop_model.py` | the PMSM plant and the whole-loop host golden |
| `foc_loop_twochip.py` | the full loop closed across TWO arrays, with its rate |
| `build_kyt.py` | regenerate the `.kyt`; reverifies on chip before saving |
| `build_grc.py` | regenerate `foc_motor.grc` |
| `verification/tests/test_foc_motor_example.py` | the command-half gate suite |
| `verification/tests/test_foc_motor_grc.py` | the full-loop / flowgraph gate suite |

## Run it

### The flowgraph

Two terminals, two commands — run both **from the repo root** (`placekyt/`).

**1. Host the chip** (terminal 1):

```bash
.venv/bin/python placekyt/main.py examples/foc_motor/foc_motor.kyt
```

Then **Simulation → Run as GNURadio Server** (port **58950**). Leave placeKYT
running.

**2. Drive it** (terminal 2) — open the flowgraph in GNU Radio Companion and
press **▶ Run** (F6):

```bash
gnuradio-companion examples/foc_motor/foc_motor.grc
```

Three scopes:

- **measured rotor-frame currents i_d, i_q** — the demo plot. `i_q` climbs to
  the torque reference while `i_d` is held at zero. This is the loop
  regulating.
- **inverter duty cycles a, b, c** — the min-max injected set the inverter legs
  actually switch on.
- **current errors e_d, e_q** — both converge to zero as the loop settles.

The shipped `.kyt` holds the **command half**. The flowgraph describes the
whole loop, so placing all of it is a two-die job — that is the exercise the
file exists for. To regenerate the flowgraph:

```bash
.venv/bin/python examples/foc_motor/build_grc.py
```

### The full loop across two arrays, headless

```bash
QT_QPA_PLATFORM=offscreen \
  .venv/bin/python examples/foc_motor/foc_loop_twochip.py 12
```

Builds both halves, closes the loop around the plant, and prints each
iteration's measured currents and duties against the host golden, then the two
halves' sustained intervals and the full-loop rate.

### The command half alone

```bash
QT_QPA_PLATFORM=offscreen .venv/bin/python examples/foc_motor/foc_motor_demo.py
```

You will see each iteration's three duty words against the golden they are
compared to, the first-packet latency, the duty-word cadence, and the sustained
inter-iteration interval and loop rate in kHz.

To regenerate the `.kyt` (it is only written if the chip run is exact):

```bash
QT_QPA_PLATFORM=offscreen .venv/bin/python examples/foc_motor/build_kyt.py
```

The gates:

```bash
QT_QPA_PLATFORM=offscreen .venv/bin/python -m pytest \
  verification/tests/test_foc_motor_example.py \
  verification/tests/test_foc_motor_grc.py -q
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

**The `.kyt` measures the controller, the `.grc` closes the loop.** In the
shipped `.kyt` the errors and `θ` are supplied by the host, so what it measures
is the command half in isolation. The flowgraph adds the measurement half and a
host block holding the PMSM plant, the error former and the feedback path, and
`foc_loop_twochip.py` runs the whole thing closed across two arrays — see **The full loop** below.
