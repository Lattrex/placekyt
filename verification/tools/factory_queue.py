#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""factory_queue — the block-build work queue, backed by ``verification/manifest.json``.

The orchestrator (a session-driven loop, or a future daemon) uses this to pick the
next block to build, mark it in-progress, and record its outcome — so many builds can
be dispatched with minimal bookkeeping and a run can resume where it left off.

The manifest IS the queue (no side channel):
  * ``status: "planned"``     — READY (ascending tier = build order; Wave-1 first).
  * ``status: "in_progress"`` — CLAIMED (a builder is on it; skip unless resuming).
  * ``status: "done"``        — verified + committed.
  * ``status: "needs_human"`` — QUARANTINED (hit a substrate wall / repeated gate fail;
                                a human must look). NEW status this factory adds.

Commands:
    python verification/tools/factory_queue.py ready            # list READY blocks
    python verification/tools/factory_queue.py claim [--tier N] # claim the next READY
                                                                # (optionally lowest tier
                                                                # >= N), print its JSON
    python verification/tools/factory_queue.py set <Block> <status>
    python verification/tools/factory_queue.py status           # queue summary counts

``claim`` flips the picked block to ``in_progress`` and prints its full manifest entry
(so the orchestrator has grc_block/tier/params to build the dispatch prompt). Manifest
writes are the lock: in the session-driven model the single orchestrator serializes
claims; a future multi-worker daemon commits the flip as a git lock (first push wins).
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

MANIFEST = Path(__file__).resolve().parents[1] / "manifest.json"

READY = "planned"
CLAIMED = "in_progress"
DONE = "done"
QUARANTINED = "needs_human"
_TERMINAL = {DONE, "wont_map"}


def _load() -> dict:
    return json.loads(MANIFEST.read_text())


def _save(m: dict) -> None:
    MANIFEST.write_text(json.dumps(m, indent=2) + "\n")


def ready_blocks(m: dict) -> list[dict]:
    """READY blocks (status==planned), ascending tier then manifest order."""
    idx = {id(b): i for i, b in enumerate(m["blocks"])}
    return sorted((b for b in m["blocks"] if b.get("status") == READY),
                  key=lambda b: (b.get("tier", 99), idx[id(b)]))


def claim_next(m: dict, min_tier: int = 1) -> dict | None:
    """Flip the next READY block (tier >= min_tier) to CLAIMED; return its entry."""
    for b in ready_blocks(m):
        if b.get("tier", 99) >= min_tier:
            b["status"] = CLAIMED
            _save(m)
            return b
    return None


def set_status(m: dict, block: str, status: str) -> bool:
    for b in m["blocks"]:
        if b.get("kyttar_block") == block:
            b["status"] = status
            _save(m)
            return True
    return False


def summary(m: dict) -> dict:
    counts: dict[str, int] = {}
    for b in m["blocks"]:
        counts[b.get("status", "?")] = counts.get(b.get("status", "?"), 0) + 1
    return counts


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("ready")
    c = sub.add_parser("claim")
    c.add_argument("--tier", type=int, default=1)
    s = sub.add_parser("set")
    s.add_argument("block")
    s.add_argument("status")
    sub.add_parser("status")
    args = ap.parse_args(argv)

    m = _load()
    if args.cmd == "ready":
        for b in ready_blocks(m):
            print(f"  tier{b.get('tier','?')}  {b['kyttar_block']:32s} "
                  f"{b.get('grc_block','')}")
        return 0
    if args.cmd == "claim":
        b = claim_next(m, min_tier=args.tier)
        if b is None:
            sys.stderr.write("queue empty (no READY blocks)\n")
            return 1
        print(json.dumps(b, indent=2))
        return 0
    if args.cmd == "set":
        ok = set_status(m, args.block, args.status)
        if not ok:
            sys.stderr.write(f"no such block: {args.block}\n")
            return 1
        print(f"{args.block} -> {args.status}")
        return 0
    if args.cmd == "status":
        for k, v in sorted(summary(m).items()):
            print(f"  {k:14s} {v}")
        return 0
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
