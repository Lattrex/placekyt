#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""gen_paper_table — aggregate the autonomous block-build metrics for the paper.

Reads every ``verification/reports/factory/*.factory.json`` (written by the
orchestrator via ``factory_metrics.record``) and emits a paper-ready summary of what
it costs an AI agent to build + verify a Kyttar DSP block:

  * a per-block table (tokens, turns, walltime, interventions, attempts, outcome),
  * aggregate rows (per wave + overall): totals, means, medians, and the
    "fully-autonomous rate" (fraction built with zero human interventions).

Sits alongside ``gen_dashboard.py`` (which reports DSP CORRECTNESS from the other
report JSONs). This one reports WORKFLOW COST — the paper's AI-workflow thesis.

Usage:
    python verification/tools/gen_paper_table.py            # markdown to stdout
    python verification/tools/gen_paper_table.py --csv OUT  # also write a CSV
    python verification/tools/gen_paper_table.py --json     # machine-readable summary
"""
from __future__ import annotations

import argparse
import csv
import json
import statistics
import sys
from pathlib import Path

FACTORY_DIR = Path(__file__).resolve().parents[1] / "reports" / "factory"


def _load_all() -> list[dict]:
    if not FACTORY_DIR.exists():
        return []
    out = []
    for p in sorted(FACTORY_DIR.glob("*.factory.json")):
        try:
            out.append(json.loads(p.read_text()))
        except Exception:  # noqa: BLE001 — skip a malformed record, don't crash the table
            continue
    return out


def _tok_total(r: dict) -> int:
    t = r.get("tokens", {}) or {}
    return int(t.get("total", int(t.get("input", 0)) + int(t.get("output", 0))))


def _rows(records: list[dict]) -> list[dict]:
    rows = []
    for r in records:
        outcome = ("quarantined" if r.get("quarantined")
                   else "passed" if r.get("verify_passed") else "failed")
        rows.append({
            "block": r.get("block", "?"),
            "grc_block": r.get("grc_block", ""),
            "wave": r.get("wave", 0),
            "tier": r.get("tier", 0),
            "tokens": _tok_total(r),
            "turns": int(r.get("turns", 0)),
            "walltime_sec": float(r.get("walltime_sec", 0.0)),
            "attempts": int(r.get("attempts", 1)),
            "interventions": int(r.get("human_interventions", 0)),
            "outcome": outcome,
        })
    return rows


def _agg(rows: list[dict], label: str) -> dict:
    """One aggregate row over ``rows``."""
    n = len(rows)
    if n == 0:
        return {"group": label, "n": 0}
    toks = [r["tokens"] for r in rows]
    walls = [r["walltime_sec"] for r in rows]
    turns = [r["turns"] for r in rows]
    passed = [r for r in rows if r["outcome"] == "passed"]
    autonomous = [r for r in rows if r["interventions"] == 0]
    return {
        "group": label,
        "n": n,
        "passed": len(passed),
        "quarantined": len([r for r in rows if r["outcome"] == "quarantined"]),
        "tok_total": sum(toks),
        "tok_mean": round(statistics.mean(toks)) if toks else 0,
        "tok_median": round(statistics.median(toks)) if toks else 0,
        "walltime_total_sec": round(sum(walls)),
        "walltime_mean_sec": round(statistics.mean(walls)) if walls else 0,
        "turns_mean": round(statistics.mean(turns), 1) if turns else 0,
        "autonomous_rate": round(len(autonomous) / n, 3),
        "total_interventions": sum(r["interventions"] for r in rows),
    }


def _fmt_secs(s: float) -> str:
    s = int(s)
    if s < 60:
        return f"{s}s"
    if s < 3600:
        return f"{s // 60}m{s % 60:02d}s"
    return f"{s // 3600}h{(s % 3600) // 60:02d}m"


def _markdown(rows: list[dict], aggs: list[dict]) -> str:
    lines = ["# Kyttar block factory — build-cost metrics", ""]
    lines.append("Per-block cost of building + BER-0-verifying each block "
                 "autonomously (the paper's AI-workflow measurements).")
    lines.append("")
    lines.append("| Block | GRC block | Wave | Tokens | Turns | Walltime | Attempts | Human | Outcome |")
    lines.append("|-------|-----------|:----:|-------:|------:|---------:|:-------:|:-----:|---------|")
    for r in rows:
        lines.append(
            f"| {r['block']} | `{r['grc_block']}` | {r['wave']} | "
            f"{r['tokens']:,} | {r['turns']} | {_fmt_secs(r['walltime_sec'])} | "
            f"{r['attempts']} | {r['interventions']} | {r['outcome']} |")
    lines.append("")
    lines.append("## Aggregates")
    lines.append("")
    lines.append("| Group | N | Passed | Quar. | Tokens (total) | Tokens (mean) | Walltime (mean) | Turns (mean) | Autonomous | Interventions |")
    lines.append("|-------|--:|-------:|------:|---------------:|--------------:|----------------:|-------------:|-----------:|--------------:|")
    for a in aggs:
        if a["n"] == 0:
            continue
        lines.append(
            f"| {a['group']} | {a['n']} | {a['passed']} | {a['quarantined']} | "
            f"{a['tok_total']:,} | {a['tok_mean']:,} | "
            f"{_fmt_secs(a['walltime_mean_sec'])} | {a['turns_mean']} | "
            f"{a['autonomous_rate']*100:.0f}% | {a['total_interventions']} |")
    lines.append("")
    return "\n".join(lines)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--csv", metavar="OUT", help="also write a per-block CSV here")
    ap.add_argument("--json", action="store_true",
                    help="emit a machine-readable JSON summary instead of markdown")
    args = ap.parse_args(argv)

    records = _load_all()
    rows = _rows(records)
    rows.sort(key=lambda r: (r["wave"], r["block"]))

    # Aggregates: overall + per wave.
    waves = sorted({r["wave"] for r in rows})
    aggs = [_agg(rows, "ALL")] + [
        _agg([r for r in rows if r["wave"] == w], f"Wave {w}") for w in waves]

    if args.csv:
        with open(args.csv, "w", newline="") as f:
            w = csv.DictWriter(f, fieldnames=list(rows[0].keys()) if rows else
                               ["block", "grc_block", "wave", "tier", "tokens",
                                "turns", "walltime_sec", "attempts",
                                "interventions", "outcome"])
            w.writeheader()
            w.writerows(rows)
        sys.stderr.write(f"wrote {args.csv} ({len(rows)} blocks)\n")

    if args.json:
        print(json.dumps({"blocks": rows, "aggregates": aggs}, indent=2))
    else:
        print(_markdown(rows, aggs))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
