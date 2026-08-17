#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""factory_dispatch — print the exact, ready-to-paste builder prompt for a block.

So anyone can run the block factory on their own block without hand-writing a prompt:

    python verification/tools/factory_dispatch.py ComplexCostasLoopBlock

prints the filled-in dispatch prompt (block name + GNU Radio counterpart + params, pulled
from ``verification/manifest.json``) that you hand to an AI agent. The agent then follows
``AGENTS.md`` to author + verify + commit the block, and you record its cost with
``factory_metrics.py`` (see ``verification/FACTORY.md``).

    # pick from the queue instead of naming a block:
    python verification/tools/factory_dispatch.py --next        # lowest-tier ready block
    python verification/tools/factory_dispatch.py --claim       # ... and mark it in_progress

The prompt is the single source of the methodology — edit the TEMPLATE here (or in
FACTORY.md, keep them in sync) and every dispatch reflects it.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

MANIFEST = Path(__file__).resolve().parents[1] / "manifest.json"

TEMPLATE = """\
Follow AGENTS.md §3 to build and verify the Kyttar block **{block}** ({counterpart_clause}\
{params_clause}). Work in the repo at {repo} on the current git branch. {poc_clause}

Manifest spec for this block: {manifest_notes}
{no_gr_clause}\

Read the KB invariants FIRST — verification/KNOWLEDGE_BASE/invariants.md (esp. INV-4 the \
failing-mutation gate, INV-13 coefficient headroom, INV-22 GRC binding, INV-25 poc≠\
verified), layout_rules.md (fold rules, ≤8-across), lessons_log.md (prior gotchas) — \
then BLOCK_AUTHORING_GUIDE.md + PROGRAMMING_GUIDE.md. Study a shipped `done` block of a \
similar shape and mirror it.

Do the FULL AGENTS.md loop:
1. Author/finalize the block (cell_count, interface, build_cell_programs, \
process_reference). Mirror the GRC parameter names VERBATIM.
2. Write verification/tests/test_<block>.py from the test_gain.py template: edge + random \
(≥3 seeds) + a full PARAMETER SWEEP + **mutation tests proven to FAIL** (INV-4). Compare \
the DUT (built+simulated on simKYT) vs its reference (the GNU Radio counterpart above, or \
the Python golden if there is none) within the DERIVED Q15 tolerance. Fix the BLOCK, \
never the gate; never loosen a tolerance to pass.
3. Complete GRC binding (INV-22): gr-kyttar/grc/<id>.block.yml exposing EVERY param + \
every (param-dependent) port, the Python shim if needed, shipped by install.sh.
4. Confirm layout is folded/legal + orientation-invariant (INV-8/9/14/23/25) — run the \
placement/orientation tests for it.
5. Append a dated lessons_log.md entry.
6. Set the manifest status to "done" \
(verification/tools/factory_queue.py set {block} done).
7. Regenerate + --check the dashboard (verification/tools/gen_dashboard.py, then --check).
8. Commit to the CURRENT branch (block source + test + report JSON + manifest + STATUS.md \
+ KB entry, one commit, SPDX header on new files). Do NOT switch branches.

DEFINITION OF DONE = every box in AGENTS.md §4 (green test, mutation gate FAILS, coverage, \
report JSON verification/reports/{block}.json, verbatim GR param names, complete GRC \
binding, folded layout, orientation-invariant, legal footprint).

STOP + QUARANTINE (don't grind forever) if you hit a documented SUBSTRATE WALL — a Q15 \
dynamic-range limit needing external RAM, a fold that can't stay ≤8 across, or a harness \
gap — OR the equivalence/mutation gate still fails after 2 real attempts. Then: leave the \
manifest in_progress (or `factory_queue.py set {block} needs_human`), write a lessons_log \
entry naming the EXACT wall, and END reporting "QUARANTINE {block}: <reason>". Do not fake \
a pass.

Return a one-line status: `DONE {block} commit <sha>` or `QUARANTINE {block}: <reason>`. \
Also report: how many build/verify ATTEMPTS it took and whether anything needed a human.\
"""


def _load():
    return json.loads(MANIFEST.read_text())


def _entry(m, block):
    for b in m["blocks"]:
        if b.get("kyttar_block") == block:
            return b
    return None


def _next_ready(m):
    idx = {id(b): i for i, b in enumerate(m["blocks"])}
    ready = sorted((b for b in m["blocks"] if b.get("status") == "planned"),
                   key=lambda b: (b.get("tier", 99), idx[id(b)]))
    return ready[0] if ready else None


def render(entry) -> str:
    params = entry.get("params") or {}
    params_clause = f", params **{json.dumps(params)}**" if params else ""
    poc_clause = (
        "This block is a PoC (`poc: true`): code for it ALREADY EXISTS but was NEVER "
        "verified against GNU Radio (INV-25) — so your job is to FINALIZE + VERIFY the "
        "existing code across its full parameter range, and EXPECT to find and fix real "
        "bugs, not to trust it because an example uses it."
        if entry.get("poc") else
        "This is a new block (no existing code / not yet verified)."
    )
    grc = (entry.get("grc_block") or "").strip()
    if grc:
        counterpart_clause = f"GNU Radio counterpart **{grc}**"
        no_gr_clause = ""
    else:
        # A composite/waveform block with no single GR factory (e.g. Varicode, Morse):
        # verify against a Python golden model of the published spec, NOT a GR block.
        counterpart_clause = "NO single GNU Radio counterpart"
        no_gr_clause = (
            "IMPORTANT: this block has NO stock GNU Radio equivalent (grc_block is empty). "
            "You CANNOT gate it against a GR block. Instead build a Python GOLDEN model of "
            "the published spec named in the manifest notes (cite the source), verify the "
            "DUT BIT-EXACT / within a derived tolerance against that golden, AND round-trip "
            "against its inverse block if one exists. The mutation gate (INV-4) still "
            "applies against the golden. Do NOT invent semantics — follow the cited spec.\n\n"
        )
    return TEMPLATE.format(
        block=entry["kyttar_block"], counterpart_clause=counterpart_clause,
        params_clause=params_clause, poc_clause=poc_clause, no_gr_clause=no_gr_clause,
        manifest_notes=(entry.get("notes") or "(none)"),
        repo=str(MANIFEST.resolve().parents[1]))


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("block", nargs="?", help="KyttarBlock name")
    ap.add_argument("--next", action="store_true",
                    help="use the lowest-tier ready (planned) block")
    ap.add_argument("--claim", action="store_true",
                    help="with --next: also mark it in_progress")
    args = ap.parse_args(argv)

    m = _load()
    if args.next or not args.block:
        entry = _next_ready(m)
        if entry is None:
            sys.stderr.write("queue empty (no planned blocks)\n")
            return 1
        if args.claim:
            entry["status"] = "in_progress"
            MANIFEST.write_text(json.dumps(m, indent=2) + "\n")
            sys.stderr.write(f"claimed {entry['kyttar_block']} (-> in_progress)\n")
    else:
        entry = _entry(m, args.block)
        if entry is None:
            sys.stderr.write(f"no such block in manifest: {args.block}\n")
            return 1

    print(render(entry))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
