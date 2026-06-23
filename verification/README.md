<!-- SPDX-License-Identifier: GPL-3.0-or-later -->

# Block verification framework

Verifies that a Kyttar DSP block is a **drop-in equivalent** of its GNU Radio
Companion counterpart: the same stimulus is run through the GNU Radio block (the
golden reference / predictor) and the Kyttar block (built and run on simKYT, the
DUT), and the outputs are compared within a quantization-aware tolerance.

This is IP-level verification: the reference behavior is fully specified by GNU
Radio, so verifying a block is a matter of execution — build it, run the harness,
fix until it matches across edge, random, and parameter-sweep stimulus.

## Layout

| Path | What |
|------|------|
| `kyttar_verify/dut_runner.py` | Build one block x16_in→block→x16_out and run a stimulus on simKYT. |
| `kyttar_verify/gnuradio_ref.py` | Run a GNU Radio flowgraph as the golden reference (system-Python subprocess). |
| `kyttar_verify/compare.py` | `compare_against_grc` — alignment + derived Q15 tolerance + per-class metrics. |
| `tests/test_gain.py` | The gold-standard template: proves the harness on GainBlock + mutation tests. |
| `KNOWLEDGE_BASE/invariants.md` | Substrate invariants every block author/agent should read first. |
| `KNOWLEDGE_BASE/lessons_log.md` | Per-block lessons, appended as blocks are verified. |

## Requirements

- The placeKYT runtime installed (`pip install -e runtime/python` from the repo
  root) — provides `gr_kyttar` and the `simkyt` simulator.
- A Python interpreter with **GNU Radio** (the golden reference). By default the
  harness uses `/usr/bin/python3`; override with `KYTTAR_GR_PYTHON=/path/to/python`.
  GNU Radio runs in a separate subprocess so its NumPy never clashes with the
  verification environment's.

## Run

```bash
KYTTAR_GR_PYTHON=/usr/bin/python3 QT_QPA_PLATFORM=offscreen \
  python -m pytest verification/tests/ -q
```

## The acceptance bar

A block is **verified** when its test suite is green AND includes mutation tests
that prove the gate FAILS on a corrupted DUT (inverted output, wrong parameter,
latency offset, empty output). A gate that has never been shown to fail certifies
nothing — see `KNOWLEDGE_BASE/invariants.md` INV-4.

## Authoring a new block test

Use `tests/test_gain.py` as the template. The per-block work is thin: define the
stimulus, the GNU Radio reference flowgraph fragment, the block params, the block's
group delay and op count, then call `compare_against_grc`. The engine handles
alignment, tolerance, metrics, and diagnostics.
