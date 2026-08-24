#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Render the build-cost figure from the factory records.

Two stacked panels sharing a build-order x axis:

  top     output tokens per block, marker-coded by outcome
          (filled = verified first attempt, hollow = needed a retry,
           X = quarantined, square = needs_human)
  bottom  wall-clock minutes per block, bar-coded by wave

Every value comes from ``verification/reports/factory/*.factory.json``; nothing
is hand-entered. Blocks are ordered by wave, then by the order they were built
within the wave (records carry no timestamp, so name order is the stable
within-wave key).

ZERO-COST ROWS ARE EXCLUDED from the plot and counted separately: a joint build
records the pair's whole cost on one block and a zero-cost cross-reference on
its sibling, so plotting the sibling would draw a phantom free block.

Usage::

    python verification/tools/gen_paper_figure.py [--out FILE] [--waves 1,2,3]
"""
from __future__ import annotations

import argparse
import json
import statistics
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[2]
_RECORDS = _ROOT / "verification" / "reports" / "factory"

# Wave labels as they appear in the legend. Keep these in sync with the
# campaign notes; the figure is unreadable without them.
WAVE_LABELS = {
    1: "bit and sample primitives",
    2: "carrier and timing sync",
    3: "transcendental, equalization",
    4: "coding, framing, amateur modes",
    5: "loops, AGC, resampling, FEC",
    6: "example assemblies",
    7: "transforms, inference, chirp",
}

# Colour-blind-safe sequence (blue / cyan / green / olive / red / purple / orange).
WAVE_COLOURS = {
    1: "#3B6FA0", 2: "#5BC8F5", 3: "#2E8B57", 4: "#C7B446",
    5: "#F2607A", 6: "#8A6FBF", 7: "#E8833A",
}


def _tok_total(r: dict) -> int:
    """Token total, matching gen_paper_table.py exactly."""
    t = r.get("tokens", {}) or {}
    return int(t.get("total", int(t.get("input", 0)) + int(t.get("output", 0))))


def _outcome(r: dict) -> str:
    """Outcome, derived the SAME way gen_paper_table.py derives it.

    The record schema is authoritative: ``quarantined`` and ``verify_passed``
    are the fields; there is no ``outcome`` key to read. A block that neither
    verified nor quarantined is an honest stop (needs_human) when its record
    says so, else a failure.
    """
    if r.get("quarantined"):
        return "quarantined"
    if r.get("verify_passed"):
        return "passed"
    return "needs_human" if r.get("needs_human") or not r.get("verify_passed") else "failed"


def _walltime_min(r: dict) -> float:
    return float(r.get("walltime_sec", 0.0)) / 60.0


def load_records(waves: set[int] | None = None) -> list[dict]:
    """Read every factory record, drop zero-cost cross-references, sort.

    Sort key is (wave, started_utc) so build order is real chronology where the
    record carries it, falling back to name order for the few that do not.
    """
    out = []
    for path in sorted(_RECORDS.glob("*.factory.json")):
        try:
            rec = json.loads(path.read_text())
        except Exception:  # noqa: BLE001 — a malformed record must not kill the figure
            continue
        if waves and rec.get("wave") not in waves:
            continue
        if not _tok_total(rec):
            continue  # joint-build cross-reference: cost lives on the sibling
        out.append(rec)
    return sorted(out, key=lambda r: (r.get("wave", 0),
                                      r.get("started_utc") or "zzz",
                                      r.get("block", "")))


def _marker(rec: dict) -> tuple[str, bool]:
    """(matplotlib marker, filled) for a record's outcome and attempt count."""
    outcome = _outcome(rec)
    if outcome == "quarantined":
        return "X", True
    if outcome == "needs_human":
        return "s", False
    return "o", rec.get("attempts", 1) <= 1


def render(records: list[dict], out_path: Path) -> None:
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    from matplotlib.lines import Line2D

    fig, (ax_tok, ax_wall) = plt.subplots(
        2, 1, figsize=(11, 6.5), sharex=True,
        gridspec_kw={"height_ratios": [2.1, 1.0], "hspace": 0.08})

    xs = range(1, len(records) + 1)
    tokens_k = [_tok_total(r) / 1000.0 for r in records]
    median_k = statistics.median(tokens_k)

    # --- top panel: cost per block -----------------------------------------
    for x, rec, tk in zip(xs, records, tokens_k):
        marker, filled = _marker(rec)
        colour = WAVE_COLOURS.get(rec.get("wave", 0), "#666666")
        ax_tok.plot(x, tk, marker=marker, markersize=7,
                    markerfacecolor=colour if filled else "none",
                    markeredgecolor=colour, markeredgewidth=1.6,
                    linestyle="none", zorder=3)

    ax_tok.axhline(median_k, color="#555555", linestyle="--", linewidth=1.0, zorder=1)
    ax_tok.annotate(f"median\n{median_k * 1000:,.0f}",
                    xy=(len(records) + 0.6, median_k), xytext=(4, 0),
                    textcoords="offset points", va="center", fontsize=8,
                    color="#555555")
    ax_tok.set_ylabel("Output tokens per block\n(thousands)", fontsize=9)
    ax_tok.grid(axis="y", color="#DDDDDD", linewidth=0.6)
    ax_tok.set_axisbelow(True)

    # Wave boundaries, so the reader can see difficulty increasing.
    prev = None
    for x, rec in zip(xs, records):
        w = rec.get("wave")
        if prev is not None and w != prev:
            for ax in (ax_tok, ax_wall):
                ax.axvline(x - 0.5, color="#999999", linestyle=":", linewidth=0.8, zorder=0)
        prev = w

    legend_outcome = [
        Line2D([], [], marker="o", linestyle="none", color="#3B6FA0",
               markerfacecolor="#3B6FA0", markersize=7, label="verified, first attempt"),
        Line2D([], [], marker="o", linestyle="none", color="#3B6FA0",
               markerfacecolor="none", markeredgewidth=1.6, markersize=7,
               label="verified, needed a retry"),
        Line2D([], [], marker="X", linestyle="none", color="#222222",
               markerfacecolor="#222222", markersize=7, label="quarantined"),
        Line2D([], [], marker="s", linestyle="none", color="#222222",
               markerfacecolor="none", markeredgewidth=1.6, markersize=7,
               label="needs human (honest stop)"),
    ]
    ax_tok.legend(handles=legend_outcome, loc="upper left", fontsize=8,
                  frameon=False, handletextpad=0.4)

    # --- bottom panel: wall clock ------------------------------------------
    for x, rec in zip(xs, records):
        mins = _walltime_min(rec)
        ax_wall.bar(x, mins, width=0.55,
                    color=WAVE_COLOURS.get(rec.get("wave", 0), "#666666"), zorder=3)
    ax_wall.set_ylabel("Wall clock\n(minutes)", fontsize=9)
    ax_wall.set_xlabel("Block build order", fontsize=9)
    ax_wall.grid(axis="y", color="#DDDDDD", linewidth=0.6)
    ax_wall.set_axisbelow(True)

    waves_present = sorted({r.get("wave", 0) for r in records})
    legend_waves = [
        Line2D([], [], color=WAVE_COLOURS.get(w, "#666666"), linewidth=5,
               label=f"wave {w}  {WAVE_LABELS.get(w, '')}")
        for w in waves_present
    ]
    ax_wall.legend(handles=legend_waves, loc="upper left", fontsize=7.5,
                   frameon=False, ncol=2, handletextpad=0.5)

    for ax in (ax_tok, ax_wall):
        for side in ("top", "right"):
            ax.spines[side].set_visible(False)

    ax_tok.set_xlim(0.2, len(records) + 1.4)
    fig.savefig(out_path, dpi=200, bbox_inches="tight")
    print(f"wrote {out_path}")


def summarize(records: list[dict]) -> None:
    """Print the caption-ready numbers so the prose can cite the same source."""
    tokens = [_tok_total(r) for r in records]
    first = [r for r in records if r.get("attempts", 1) <= 1]
    quar = [r for r in records if _outcome(r) == "quarantined"]
    human = [r for r in records if _outcome(r) == "needs_human"]
    print(f"costed builds        : {len(records)}")
    print(f"total output tokens  : {sum(tokens):,}")
    print(f"median / mean tokens : {statistics.median(tokens):,.0f} / "
          f"{statistics.mean(tokens):,.0f}")
    print(f"first-attempt        : {len(first)}/{len(records)} "
          f"({100 * len(first) / len(records):.0f}%)")
    print(f"quarantined          : {len(quar)}")
    print(f"needs_human          : {len(human)}"
          + (f"  ({', '.join(r['block'] for r in human)})" if human else ""))
    by_wave: dict[int, list[int]] = {}
    for r in records:
        by_wave.setdefault(r.get("wave", 0), []).append(_tok_total(r))
    print("\nper wave:")
    for w in sorted(by_wave):
        v = by_wave[w]
        print(f"  wave {w}: n={len(v):2d}  median={statistics.median(v):>9,.0f}  "
              f"total={sum(v):>10,}")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--out", default=str(_ROOT / "verification" / "reports" /
                                         "build_cost_figure.png"))
    ap.add_argument("--waves", help="comma-separated wave filter, e.g. 1,2,3")
    args = ap.parse_args()

    waves = {int(w) for w in args.waves.split(",")} if args.waves else None
    records = load_records(waves)
    if not records:
        raise SystemExit("no costed records matched")
    summarize(records)
    render(records, Path(args.out))


if __name__ == "__main__":
    main()
