# SPDX-License-Identifier: GPL-3.0-or-later
"""Dump the built face/memory of the port cell + input corridors for the orig modem."""
from __future__ import annotations
import os, sys
from pathlib import Path
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
_ROOT = Path(__file__).resolve().parents[3]
for p in (str(_ROOT / "placekyt"), str(_ROOT / "runtime" / "python")):
    if p not in sys.path:
        sys.path.insert(0, p)
from PySide6.QtWidgets import QApplication            # noqa: E402
QApplication.instance() or QApplication([])
from engine.io.project_io import load_project         # noqa: E402
from engine.build import BuildEngine                  # noqa: E402
from engine.catalog import BlockCatalog               # noqa: E402
from engine.io.chip_type_io import load_chip_type     # noqa: E402

_WRITE, _JUMP = 0x6000, 0x7000
CHIP = str(_ROOT / "placekyt" / "resources" / "chips" / "kyttar_10x12.yaml")
proj = load_project(str(_ROOT / "examples" / "qpsk_modem" / "qpsk_modem.orig.kyt"))
ct = load_chip_type(CHIP); key = getattr(ct, "name", None) or "kyttar_10x12"
cat = BlockCatalog.from_gr_kyttar()
bres = BuildEngine(cat, CHIP).build(proj, {key: ct})
cb = bres.chips.get(0)
print("build ok", bres.ok)


def _decode(w):
    op = w & 0xF000
    if op == _WRITE:
        return f"WRITE hop={(w>>5)&0x1F} dest={w&0x1F}"
    if op == _JUMP:
        return f"JUMP hop={(w>>5)&0x1F} entry={w&0x1F}"
    return None


# input corridor cells for both streams
for (x, y) in [(0, 0), (1, 0), (1, 1), (0, 1), (0, 2), (0, 3)]:
    c = cb.cells.get((x, y))
    if c is None:
        print(f"  ({x},{y}): <empty>")
        continue
    instrs = [(a, _decode(w)) for a, w in enumerate(c["memory"]) if _decode(w)]
    print(f"  ({x},{y}): face={c['face']} kind={c['kind']} block={c['block']}/{c['cell_id']}"
          f" entry={c['entry']}")
    for a, d in instrs:
        print(f"        [{a}] {d}")
