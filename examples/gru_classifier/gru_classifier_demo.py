# SPDX-License-Identifier: GPL-3.0-or-later
"""GRU modulation-classifier demo — WHAT IS AND IS NOT VERIFIED.

Read this before reading the output. The feature front end and the trained model
are verified; the assembled chain does **not** place and route as one chip, so
there is no end-to-end on-chip run to show yet. This script therefore prints:

1. the shipped stimulus and its two load-bearing properties;
2. the feature front end (the BLOCKS' own bit-exact references) against
   ``ml/features.py`` — ZCR bit-exact, RMS inside its derived bound;
3. the classifier verdict from the bit-exact GRU golden;
4. the placement wall, measured live: the join->GRU tail routes on a real chip,
   the full chain is always exactly one net short.

It deliberately does NOT claim an on-chip classification. See the README's
"Status" section and the ``gru_classifier example`` entry in
``verification/KNOWLEDGE_BASE/lessons_log.md``.

Run::

    PYTHONPATH=runtime/python:placekyt QT_QPA_PLATFORM=offscreen \
        .venv/bin/python examples/gru_classifier/gru_classifier_demo.py
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
                            route_report, segment_votes, zcr_feature_words)
from gru_stimulus import make_stimulus, peak_magnitude, to_q15  # noqa: E402


def _q15(x):
    return np.clip(np.round(np.asarray(x, dtype=np.float64) * 32768.0),
                   -32768, 32767).astype(np.int64)


def main() -> int:
    iq, truth = make_stimulus()
    print("1. stimulus")
    print(f"   {len(iq)} complex samples, {len(truth)} feature windows, "
          f"{len(CLASSES)} class segments")
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

    print("\n3. classifier verdict (bit-exact GRU golden on those features)")
    cls = np.asarray(golden_classes(iq))
    votes = segment_votes([int(c) for c in cls])
    print("   segment   truth       vote        per-step accuracy")
    ok = True
    for ci, name in enumerate(CLASSES):
        seg = cls[ci * SEGMENT_STEPS + 30:(ci + 1) * SEGMENT_STEPS]
        acc = float(np.mean(seg == ci)) if seg.size else float("nan")
        v = votes[ci]
        ok = ok and v == ci
        print(f"   {name:8s}  {ci} {name:6s}  {v} {CLASSES[v]:6s}  "
              f"{acc:6.3f}   {'OK' if v == ci else 'MISS'}")
    print(f"   all four segments correct: {ok}")

    print("\n4. on-chip placement (measured live)")
    cells = sum(_cell_count(nm, cls_, p) for nm, cls_, p in BLOCK_SPECS)
    print(f"   the chain is {cells} block cells on a 120-cell array "
          f"(GRUCellBlock alone is 51, in an 8x7 fold)")
    for label, bad, used in route_report():
        if bad:
            print(f"   {label}: DOES NOT ROUTE — failing nets: {bad}")
        else:
            print(f"   {label}: routes, {used}/120 cells")

    print("\nSTATUS: the feature front end and the model are verified; the "
          "whole chain does NOT yet route as one chip, so this example has "
          "NOT been run end to end on a placed + routed array.")
    return 0


def _cell_count(name, cls_name, params):
    from gr_kyttar.placement import blocks as B
    return getattr(B, cls_name)(name, **params).cell_count


if __name__ == "__main__":
    raise SystemExit(main())
