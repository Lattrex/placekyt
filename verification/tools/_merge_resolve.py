# SPDX-License-Identifier: GPL-3.0-or-later
"""One-shot conflict resolver for merging parallel factory-builder commits.

NOT a shipped tool — a merge aid the orchestrator runs between cherry-picks. Handles the
recurring, mechanical conflicts that arise when many single-cell blocks land on adjacent
lines of the same append-only / registration files:

  * lessons_log.md, invariants.md         -> keep BOTH sides (append entries), add a --- rule
  * test_placement_legality.py,
    test_orientation_invariance.py         -> keep BOTH list entries
  * _modmap.py, kyttar/__init__.py,
    dsp_markers.py                         -> keep BOTH registration lines
  * manifest.json                          -> take THEIRS (the builder's verified entry)
  * STATUS.md, README.md                   -> take THEIRS (regenerated afterwards anyway)

Then it FAILS LOUDLY if any conflict markers remain, and validates manifest.json as JSON
(INV-27). Usage: python verification/tools/_merge_resolve.py
"""
from __future__ import annotations

import json
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]

# Files whose conflicts we resolve by keeping BOTH sides verbatim (append/list/registration).
KEEP_BOTH = {
    "verification/KNOWLEDGE_BASE/lessons_log.md",
    "verification/KNOWLEDGE_BASE/invariants.md",
    "verification/tests/test_placement_legality.py",
    "verification/tests/test_orientation_invariance.py",
    "verification/tests/test_pipeline_saturation.py",
    "runtime/python/gr_kyttar/placement/blocks/_modmap.py",
    "runtime/python/gr_kyttar/placement/__init__.py",
    "gr-kyttar/python/kyttar/__init__.py",
    "gr-kyttar/python/kyttar/dsp_markers.py",
    "placekyt/engine/grc_import.py",
    "placekyt/engine/catalog.py",
    "gr-kyttar/grc/CMakeLists.txt",
    "gr-kyttar/python/kyttar/dsp_markers.py",
    "gr-kyttar/python/gr_kyttar/placement/blocks/_modmap.py",
}
# Files where we take the incoming (builder) side.
TAKE_THEIRS = {
    "verification/manifest.json",
    "verification/STATUS.md",
    "README.md",
    "verification/tools/gen_dashboard.py",
}

_CONFLICT = re.compile(
    r'<<<<<<< [^\n]*\n(.*?)\n=======\n(.*?)\n>>>>>>> [^\n]*\n', re.DOTALL)


def _unmerged() -> list[str]:
    out = subprocess.run(["git", "diff", "--name-only", "--diff-filter=U"],
                         cwd=ROOT, capture_output=True, text=True).stdout
    return [l for l in out.splitlines() if l.strip()]


def _resolve_keep_both(text: str) -> str:
    def repl(m):
        head, inc = m.group(1).rstrip("\n"), m.group(2).rstrip("\n")
        # For markdown logs put a rule between; for code lists a newline is enough.
        sep = "\n" if head.endswith(("---", ",", "[")) else "\n"
        return head + sep + inc + "\n"
    return _CONFLICT.sub(repl, text)


def _resolve_take_theirs(text: str) -> str:
    return _CONFLICT.sub(lambda m: m.group(2) + "\n", text)


def main() -> int:
    files = _unmerged()
    if not files:
        print("no unmerged files")
        return 0
    for rel in files:
        p = ROOT / rel
        s = p.read_text(encoding="utf-8")
        if rel in TAKE_THEIRS:
            s2 = _resolve_take_theirs(s)
        elif rel in KEEP_BOTH:
            s2 = _resolve_keep_both(s)
        else:
            print(f"!! UNHANDLED conflict file (resolve by hand): {rel}", file=sys.stderr)
            return 2
        if "<<<<<<<" in s2 or "\n=======\n" in s2 or ">>>>>>>" in s2:
            print(f"!! markers remain after resolve in {rel} (resolve by hand)",
                  file=sys.stderr)
            return 3
        p.write_text(s2, encoding="utf-8")
        subprocess.run(["git", "add", rel], cwd=ROOT, check=True)
        print(f"resolved {rel}")

    # INV-27: manifest must be valid JSON before we continue.
    mp = ROOT / "verification/manifest.json"
    try:
        json.loads(mp.read_text())
    except Exception as e:  # noqa: BLE001
        print(f"!! manifest.json invalid after resolve: {e}", file=sys.stderr)
        return 4
    print("manifest.json valid")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
