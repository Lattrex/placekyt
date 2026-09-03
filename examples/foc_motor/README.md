<!-- SPDX-License-Identifier: GPL-3.0-or-later -->

# FOC motor control — the current loop on Kyttar

Field-oriented control is how you make a permanent-magnet synchronous motor
(PMSM) produce smooth torque: measure the phase currents, rotate them into the
rotor's own reference frame so the torque-producing and flux-producing
components separate, regulate each with its own PI controller, rotate the
voltage command back out, and modulate it onto the three inverter legs.

The **shipped `.kyt`** puts the **whole current loop** on one array — the
measurement half (Clarke + forward Park) and the command half (two PI
controllers + inverse Park + SVPWM), all six blocks on one 10×12 die:

```
ia, ib ─> Clarke ─> CordicRotate(sign=−1, θ) ─> (i_d, i_q)     [measurement half]
                       (forward Park)
e_d ──> PI(d) ──┐
                 ├──> CordicRotate(sign=+1, θ) ─> SVPWM ─> duty a, b, c   [command half]
e_q ──> PI(q) ──┘         (inverse Park)
θ ────────────────────────^          (fed to both rotations)
```

`ia, ib` are the two sensed phase currents; `e_d`/`e_q` are the d- and q-axis
current errors; `θ` is the rotor electrical angle. The output is the three
inverter duty cycles, as one 3-word Q15 packet in fixed order a, b, c.

**60 of the 120 cells** on a 10×12 array — the whole loop, measurement side
included, on a single die. (An earlier build shipped only the command half,
because the whole loop did not route on one array; the build fixes recorded in
INV-74 — fork-broker `@N` delivery, reshaped internal faces, and crossover
fan-out — closed that, so it now places, routes and streams bit-exact on one
array.)

`foc_motor.grc` is the same loop as a flowgraph you drive from GNU Radio, with a
host-side error former and a PMSM plant to close it. See **The full loop** below.

## The loop rate

Measured on the real placed + routed + built chip, driving the shipped `.kyt`
through the simulator and reading each output word's capture time with
`read_port_words_timed`:

| | measured |
|---|---|
| **sustained inter-iteration interval** | **29,004.9 ns** |
| **sustained control-loop rate** | **34.48 kHz** |
| latency to the first duty word (fill) | 109,654.0 ns |
| latency to the complete 3-word packet | 109,938.6 ns |
| cadence of the three duty words | 142.29 ns apart |
| cells | 60 / 120 (72 active in one iteration) |
| instructions per iteration | ~1,064 |

The chain **streams**: six consecutive control iterations, each with different
inputs, every duty word bit-exact against the host golden and every simulator
run settling `QueueEmpty`. The sustained figure is the mean interval between
completed duty packets over the six; the spread across them is under 75 ns.

34.48 kHz sits **inside the 10–40 kHz band** typical of industrial FOC current
loops, with the whole loop — both rotations, both PI controllers and the
space-vector modulator — on one fabric.

**These are simulated times.** They come from simKYT's timing model for this
placement and routing. They are not silicon-certified, and a real device will
differ.

### Fill versus steady state

The first duty packet lands at ~109,939 ns, but the sustained interval between
packets is ~29,005 ns — the fill latency is about **four times** the steady-state
period. That gap is the finding: **the full loop OVERLAPS successive iterations.**
The chain is deep enough (both rotations, both PI stages and the modulator in
series) that more than one control iteration is in flight at once, so the
steady-state period is well under the whole-chain traversal time — there is a
real pipeline-fill discount here, unlike the command half in isolation (where
the shorter chain re-arms and its latency equals its interval).

For a host-paced controller this still behaves as one sample set per control
period by construction, which is how the example drives it; the overlap simply
means the sustained rate is higher than the single-shot latency would suggest.

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
placement. The shipped `foc_motor.kyt` places the whole thing on one 10×12 array
(see **The loop rate** above for the measured on-chip result).

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
| `foc_motor.kyt` | the shipped placed + routed chip — **the whole loop on one array** — open this directly |
| `foc_motor.grc` | **the FULL loop as a flowgraph** — every block, every connection |
| `foc_motor_demo.py` | placement, build, drive, and the loop-rate measurement |
| `foc_loop_model.py` | the PMSM plant and the whole-loop host golden |
| `foc_loop_twochip.py` | an alternative two-die split of the same loop |
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

The shipped `.kyt` holds the **whole loop** (measurement + command half) on one
array. To regenerate the flowgraph:

```bash
.venv/bin/python examples/foc_motor/build_grc.py
```

### The loop headless (the shipped one-array design)

```bash
QT_QPA_PLATFORM=offscreen .venv/bin/python examples/foc_motor/foc_motor_demo.py
```

You will see each iteration's three duty words against the golden they are
compared to, the first-packet latency, the duty-word cadence, and the sustained
inter-iteration interval and loop rate in kHz.

### An alternative: the loop split across two arrays

```bash
QT_QPA_PLATFORM=offscreen \
  .venv/bin/python examples/foc_motor/foc_loop_twochip.py 12
```

`foc_loop_twochip.py` places the two halves on two separate dies (measurement on
one, command on the other) instead of one — a study in splitting a loop across a
carrier link. It builds both halves, closes the loop around the plant, and
prints each iteration's measured currents and duties against the host golden.

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

**The `.kyt` runs the loop on chip, the `.grc` closes it around a motor.** The
shipped `.kyt` computes the whole loop on one array — Clarke and forward Park to
get (i_d, i_q), then the two PI controllers, inverse Park and SVPWM to the
duties — with its inputs (the sensed currents, the errors and `θ`) supplied by
the host. The flowgraph feeds those inputs from a host block holding the PMSM
plant, the error former and the feedback path, so the loop actually closes.
