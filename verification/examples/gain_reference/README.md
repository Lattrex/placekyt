<!-- SPDX-License-Identifier: GPL-3.0-or-later -->

# The reference example: verifying a Kyttar block against GNU Radio

This is the **first thing to read** if you want to build and verify your own
Kyttar DSP block. It takes the simplest possible block — a gain (multiply by a
constant) — and shows the entire verification workflow end to end, heavily
commented so you (or an AI agent) can dissect exactly how it works.

The file [`verify_gain_reference.py`](verify_gain_reference.py) is the lesson.
Read it top to bottom — the module docstring explains the four concepts (Q15
fixed-point, stimulus, the DUT, the comparison), and every step is annotated.

## The idea in one sentence

A Kyttar block is *verified* when it produces the same output as its GNU Radio
equivalent for the same input, within fixed-point quantization noise — proving it
is a **drop-in replacement**. GNU Radio is the known-correct reference; the
Kyttar block running on the simKYT simulator is the device under test.

## The workflow

```
   stimulus (Q15 samples)
        │
        ├─────────────► GNU Radio block ──► reference output  (the "right answer")
        │                (golden)
        │
        └─────────────► Kyttar block ─────► DUT output         (what we built)
                         (built + run
                          on simKYT)
                                │
                                ▼
                   compare_against_grc  ──► PASS / FAIL + diagnostics
              (align by delay · model Q15 saturation ·
               check max error ≤ derived tolerance)
```

## Run it

```bash
# from the repo root, with the verification env active and GNU Radio available:
KYTTAR_GR_PYTHON=/usr/bin/python3 QT_QPA_PLATFORM=offscreen \
  python verification/examples/gain_reference/verify_gain_reference.py

# or as a test:
KYTTAR_GR_PYTHON=/usr/bin/python3 QT_QPA_PLATFORM=offscreen \
  python -m pytest verification/examples/gain_reference/ -q
```

You should see the real block pass with a ~1-LSB error (correct Q15 rounding of
a single multiply, well within the derived 2-LSB tolerance), and the negative
tests confirm the gate catches an inverted output and a wrong gain.

## The one rule to internalize

**A test that has never failed proves nothing.** This example includes negative
tests that deliberately break the output and assert the comparison *fails*. If
the gate cannot catch a broken block, a "pass" on the real block is meaningless.
Every block you verify must include these. See `../../KNOWLEDGE_BASE/invariants.md`
(INV-4).

## Where to go next

- [`../../KNOWLEDGE_BASE/invariants.md`](../../KNOWLEDGE_BASE/invariants.md) — the
  substrate gotchas (e.g. the placement-dependent hop count) that will bite you.
- [`../../README.md`](../../README.md) — the framework reference.
- [`../../../BLOCK_AUTHORING_GUIDE.md`](../../../BLOCK_AUTHORING_GUIDE.md) — writing
  the block itself (this example verifies a block; that guide builds one).
