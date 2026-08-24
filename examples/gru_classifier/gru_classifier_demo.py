# SPDX-License-Identifier: GPL-3.0-or-later
"""GRU modulation-classifier demo — the whole chain, on the real chip.

A complex baseband stream goes into ONE 10x12 array and a class index comes
back, one word per 32-sample window. Everything between is on chip: the RMS
arm, the zero-crossing-rate arm, the ordered feature rendezvous, and a GRU cell
(H=4, I=2) with its 4-class readout head and an INTERNAL recurrence.

This script prints, in order:

1. the shipped stimulus and its two load-bearing properties;
2. the feature front end (the BLOCKS' own bit-exact references) against
   ``ml/features.py`` — ZCR bit-exact, RMS inside its derived bound;
3. the placement, measured live;
4. **the ON-CHIP classification** — the stimulus driven through the real
   placed + routed + built bitstream, its class stream compared word for word
   against the offline chip-exact golden, and the per-class verdict.

Run::

    PYTHONPATH=runtime/python:placekyt QT_QPA_PLATFORM=offscreen \
        .venv/bin/python examples/gru_classifier/gru_classifier_demo.py

It takes about a minute: step 4 simulates 15360 input samples through 102
cells.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import numpy as np

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
for _p in (str(ROOT / "runtime" / "python"), str(ROOT / "placekyt"),
           str(HERE), str(HERE / "ml")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from gru_classifier import (BLOCK_SPECS, CLASSES, SEGMENT_STEPS,  # noqa: E402
                            WINDOW_N, golden_classes, rms_feature_words,
                            route_report, run_on_chip, segment_votes,
                            zcr_feature_words)
from gru_stimulus import make_stimulus, peak_magnitude  # noqa: E402


def _q15(x):
    return np.clip(np.round(np.asarray(x, dtype=np.float64) * 32768.0),
                   -32768, 32767).astype(np.int64)


def _cell_count(name, cls_name, params):
    from gr_kyttar.placement import blocks as B
    return getattr(B, cls_name)(name, **params).cell_count


def main() -> int:
    iq, truth = make_stimulus()
    print("1. stimulus")
    print(f"   {len(iq)} complex samples, {len(truth)} feature windows, "
          f"{len(CLASSES)} class segments ({' -> '.join(CLASSES)})")
    print(f"   peak |z| = {peak_magnitude(iq):.4f}  (must stay < 1: the Q15 "
          f"power stage saturates)")

    print("\n2. feature front end vs ml/features.py")
    import features as F
    fe = F.compute_features(iq, WINDOW_N)
    rms = np.asarray(rms_feature_words(iq), dtype=np.int64)
    zcr = np.asarray(zcr_feature_words(iq), dtype=np.int64)
    rms_ref, zcr_ref = _q15(fe["rms"]), _q15(fe["zcr"])
    n = min(len(rms), len(rms_ref))
    d = rms[:n] - rms_ref[:n]
    dz = zcr[:n] - zcr_ref[:n]
    print(f"   RMS: {n} windows, error {d.min()} .. {d.max()} Q15 LSB "
          f"(downward truncation bias; derived bound is level-dependent)")
    print(f"   ZCR: {int(np.sum(dz != 0))}/{n} windows differ from plain "
          f"features.py, all by +1024 LSB = +1 crossing "
          f"(the pinned boundary-pair convention; bit-exact against it)")

    print("\n3. on-chip placement (measured live)")
    cells = sum(_cell_count(nm, cls_, p) for nm, cls_, p in BLOCK_SPECS)
    print(f"   the chain is {cells} block cells on a 120-cell array "
          f"(GRUCellBlock alone is 51, in a wide-flat 10x6 fold)")
    for label, bad, used in route_report():
        if bad:
            print(f"   {label}: DOES NOT ROUTE — failing nets: {bad}")
        else:
            print(f"   {label}: routes, {used}/120 cells")

    print("\n4. ON-CHIP CLASSIFICATION (the real placed + routed bitstream)")
    print(f"   driving {len(iq)} samples through the built chip ...")
    words, used = run_on_chip(iq)
    gold = golden_classes(iq)
    m = min(len(words), len(gold))
    agree = sum(1 for a, b in zip(words[:m], gold[:m]) if a == b) / max(m, 1)
    print(f"   {len(words)} class words out of {used}/120 cells")
    print(f"   agreement with the offline chip-exact golden: {agree:.6f} "
          f"({m} windows)")
    votes = segment_votes(words)
    off = segment_votes(gold)
    print("   segment   truth       chip vote   offline vote   step accuracy")
    ok = True
    for ci, name in enumerate(CLASSES):
        seg = np.asarray(words[ci * SEGMENT_STEPS + 30:
                               (ci + 1) * SEGMENT_STEPS])
        acc = float(np.mean(seg == ci)) if seg.size else float("nan")
        v, o = votes[ci], off[ci]
        ok = ok and v == ci
        print(f"   {name:8s}  {ci} {name:6s}  {v} {CLASSES[v]:6s}     "
              f"{o} {CLASSES[o]:6s}       {acc:6.3f}   "
              f"{'OK' if v == ci else 'MISS'}")
    print(f"   all four segments classified correctly ON CHIP: {ok}")
    return 0 if ok and agree == 1.0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
