<!-- SPDX-License-Identifier: GPL-3.0-or-later -->

# Complex channel selector — freq-xlating front end on one array

A real multi-channel input (two in-channel tones at 8.6/9.4 kHz + interferers
at 4 and 14 kHz) is made complex, the 9 kHz channel is mixed to baseband and
selected, rotated, and its imag rail egresses:

```
x ─▶ FloatToComplex ─▶ FreqXlatingFIR(9 taps, −9 kHz) ─▶ ComplexLowPass(firdes, 0.9, 1.2 kHz)
                                                       ─▶ MultiplyConstComplex(0.6+0.35j) ─▶ ComplexToImag ─▶ out
```

## What is verified — and what "match" means here

The golden is the IDENTICAL stock-GNU-Radio chain (`float_to_complex`,
`freq_xlating_fir_filter_ccf`, `fir_filter_ccf` fed `firdes.low_pass` — the
demo ASSERTS the chip block designs the same firdes taps —
`multiply_const_cc`, `complex_to_imag`). The bound is **DERIVED** from the
per-block verified error reports, never tuned: FloatToComplex 0 + FXF 16 +
ComplexLowPass 32 + MultiplyConstComplex 13 + ComplexToImag 0 = **61 LSB**.
Measured: **25 LSB** worst over 320 samples.

`verification/tests/test_channel_selector_example.py` (5 tests): the derived
bound, BOTH I/Q rails wired on every complex edge (the importer `re`/`im`
rail-synthesis regression this example uncovered — converter-class Q rails
were silently never wired, all-zero output), interferer rejection is real,
shipped-`.kyt` parity, and a **mutation** (a wrong down-shift frequency blows
the bound).

`placekyt/tests/test_gr_client_loop_examples.py::test_channel_selector_real_gr_client`
runs the **genuine GR client** against the hosted `.kyt` — with
`pipelined=False`, because the **FreqXlatingFIR is SATURATION-BESPOKE**
(per-sample drive only; its mid-chain mixer has no functional serialize-LOCK).
The shipped `.grc` requests the paced drive; do not flip it to pipelined.

```
$ python examples/channel_selector/channel_selector_demo.py
   chip: 320/320 samples, worst |err| 25 LSB (bound 61)
RESULT: WITHIN DERIVED BOUNDS — placed chain matches stock GNU Radio
```

43/120 cells (abutment-first pack), 6 blocks. Whole-chain proof for FloatToComplex, FreqXlatingFIR,
ComplexLowPassFilter, MultiplyConstComplex, ComplexToImag.

**Known limitation (honest):** a Conjugate stage was planned and is absent —
a single-cell complex-in→complex-out block mis-delivers its rails under the
auto-router's abutment handoff (a known engine defect).
ConjugateBlock remains per-block-verified only.

## Run it

Two terminals, two commands — run both **from the repo root** (`placekyt/`).

**1. Host the chip** (terminal 1):

```bash
.venv/bin/python placekyt/main.py examples/channel_selector/channel_selector.kyt
```

Then **Simulation → Run as GNURadio Server** (port **58950**). Leave placeKYT running.

**2. Drive it** (terminal 2) — open the flowgraph in GNU Radio Companion and press
**▶ Run** (F6):

```bash
gnuradio-companion examples/channel_selector/channel_selector.grc
```

(Or, after pressing **Generate** in GRC once, run the generated top-block directly: `python3 examples/channel_selector/channel_selector.py`. That file is build output — it is not checked in, and GRC recreates it from the `.grc`.)

| File | What |
|------|------|
| `channel_selector.grc` | GRC-first source (kyttar markers, paced drive). |
| `channel_selector.kyt` | Auto-generated placed+routed project. |
| `build_kyt.py` | Regenerates the `.kyt`. |
| `channel_selector_demo.py` | Headless END-TO-END demo. |
