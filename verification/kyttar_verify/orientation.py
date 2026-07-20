# SPDX-License-Identifier: GPL-3.0-or-later
"""Orientation-invariance harness — a block MUST produce identical on-chip output
in ALL 8 D4 orientations (4 rotations x 2 mirrors).

A block is a rigid unit: rotating/mirroring it on the array must not change what it
computes — only where it sits and which way its ports face. A block whose output
changes (or vanishes) under some orientation is BROKEN: its per-cell faces, internal
handoffs, or feedback corridors do not transform with the block. This is a universal
correctness property; every block's regression asserts it at 100%.

The 8 D4 group elements as op-lists applied via ``Placement.transform``:
    identity, cw, cw+cw (180), ccw (=cw+cw+cw), mirror_v, mirror_v+cw,
    mirror_v+cw+cw, mirror_v+ccw.
(``mirror_h`` == ``mirror_v`` composed with a 180 rotation, so the 4 rotations x
{identity, mirror_v} enumerate all 8 without redundancy.)
"""
from __future__ import annotations

# The 8 distinct D4 orientations as transform op-lists (identity first).
D4_ORIENTATIONS: list[list[str]] = [
    [],                      # identity
    ["cw"],                  # 90
    ["cw", "cw"],            # 180
    ["cw", "cw", "cw"],      # 270 (== ccw)
    ["mirror_v"],            # flip
    ["mirror_v", "cw"],      # flip + 90
    ["mirror_v", "cw", "cw"],   # flip + 180 (== mirror_h)
    ["mirror_v", "cw", "cw", "cw"],  # flip + 270
]


def _label(orient: list[str]) -> str:
    return "identity" if not orient else "+".join(orient)


def check_orientation_invariance(run_dut, *, orientations=None, compare=None):
    """Run ``run_dut(orient=...)`` across all D4 orientations; return
    ``(ok, report)`` where ``report`` is a list of per-orientation dicts.

    Args:
        run_dut: a callable ``run_dut(orient: list[str]) -> result`` that builds +
            drives the block at the given orientation and returns whatever object
            the caller compares (a DUTResult, a list of words, an (i, q) tuple, …).
            Typically a lambda wrapping ``run_block_dut``/``_complex``/``_rate`` with
            the fixed block/stimulus and only ``orient`` varying.
        orientations: the op-lists to test (default: all 8 D4).
        compare: ``compare(identity_result, other_result) -> (ok: bool, detail: str)``.
            Defaults to :func:`compare_dut_results` (handles DUTResult / ComplexDUTResult
            / RateDUTResult / plain lists).

    Returns:
        ``(all_pass: bool, report: list[dict])``. Each report row:
        ``{"orient": <label>, "ok": bool, "detail": str}``. The identity row is
        always ok=True (it is the reference).
    """
    orientations = orientations if orientations is not None else D4_ORIENTATIONS
    compare = compare or compare_dut_results
    base = run_dut([])
    report = [{"orient": _label([]), "ok": True, "detail": "reference"}]
    all_pass = True
    for orient in orientations[1:]:
        try:
            res = run_dut(list(orient))
        except Exception as exc:  # noqa: BLE001 — a build/route crash is a FAIL, not an error
            report.append({"orient": _label(orient), "ok": False,
                           "detail": f"raised {type(exc).__name__}: {exc}"})
            all_pass = False
            continue
        ok, detail = compare(base, res)
        report.append({"orient": _label(orient), "ok": ok, "detail": detail})
        all_pass = all_pass and ok
    return all_pass, report


def compare_dut_results(base, other):
    """Default comparator: identical output across the DUT-runner result shapes.

    Handles the harness result types by duck-typing:
      * complex (``i_q15``/``q_q15``): both rails must match exactly;
      * rate/real (``outputs_q15``): the output word list must match;
      * a bare list: compared directly.
    A build/route failure (``ok is False``) on EITHER side is a mismatch (a block
    that won't even build in some orientation is broken)."""
    # ok flag (DUTResult-like)
    b_ok = getattr(base, "ok", True)
    o_ok = getattr(other, "ok", True)
    if not b_ok:
        return (False, f"REFERENCE (identity) failed: {getattr(base,'reason','?')}")
    if not o_ok:
        return (False, f"build/route failed: {getattr(other,'reason','?')}")
    # complex I/Q
    if hasattr(base, "i_q15") and hasattr(base, "q_q15"):
        bi = list(base.i_q15); bq = list(base.q_q15)
        oi = list(getattr(other, "i_q15", [])); oq = list(getattr(other, "q_q15", []))
        if bi == oi and bq == oq:
            return (True, "match")
        ni = sum(1 for k in range(min(len(bi), len(oi))) if bi[k] != oi[k])
        nq = sum(1 for k in range(min(len(bq), len(oq))) if bq[k] != oq[k])
        if all(x is None for x in oi) and all(x is None for x in oq):
            return (False, "NO OUTPUT (datapath died)")
        return (False, f"I {ni} mismatch, Q {nq} mismatch "
                       f"(len i {len(bi)}/{len(oi)}, q {len(bq)}/{len(oq)})")
    # real / rate (outputs_q15)
    if hasattr(base, "outputs_q15"):
        bo = list(base.outputs_q15); oo = list(getattr(other, "outputs_q15", []))
        if bo == oo:
            return (True, "match")
        if not oo:
            return (False, "NO OUTPUT (datapath died)")
        n = sum(1 for k in range(min(len(bo), len(oo))) if bo[k] != oo[k])
        return (False, f"{n} mismatch (len {len(bo)}/{len(oo)})")
    # bare comparable
    if base == other:
        return (True, "match")
    return (False, f"mismatch ({base!r} != {other!r})")


def format_report(block_label: str, report: list[dict]) -> str:
    """A compact human-readable orientation report for a block."""
    lines = [f"orientation invariance — {block_label}:"]
    for row in report:
        mark = "OK " if row["ok"] else "FAIL"
        lines.append(f"  [{mark}] {row['orient']:20} {row['detail']}")
    return "\n".join(lines)
