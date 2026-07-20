# SPDX-License-Identifier: GPL-3.0-or-later
"""Oracle: does a chip-input-port fan-out to 2 blocks deliver BOTH streams?

Loads a .kyt, builds it, and prints the resolved input_landings for every
PORT->block input net. The common-bus topology (both nets share a fork cell)
must yield a DISTINCT, valid landing for the TX (mapper) net; the diverging
topology (nets share only the port cell) collapses/loses the TX landing.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

_ROOT = Path(__file__).resolve().parents[3]
_PLACEKYT = _ROOT / "placekyt"
for p in (str(_PLACEKYT), str(_ROOT / "runtime" / "python")):
    if p not in sys.path:
        sys.path.insert(0, p)

from PySide6.QtWidgets import QApplication              # noqa: E402
QApplication.instance() or QApplication([])
from engine.io.project_io import load_project           # noqa: E402
from engine.build import BuildEngine                     # noqa: E402
from engine.catalog import BlockCatalog                  # noqa: E402
from engine.io.chip_type_io import load_chip_type        # noqa: E402
from model.connection import ChipPortEndpoint, BlockEndpoint  # noqa: E402

CHIP_YAML = str(_PLACEKYT / "resources" / "chips" / "kyttar_10x12.yaml")


def report(kyt_path: str) -> None:
    proj = load_project(kyt_path)
    ct = load_chip_type(CHIP_YAML)
    ct_key = getattr(ct, "name", None) or "kyttar_10x12"
    cat = BlockCatalog.from_gr_kyttar()
    bres = BuildEngine(cat, CHIP_YAML).build(proj, {ct_key: ct})
    print(f"\n=== {Path(kyt_path).name} ===")
    print("build ok:", bres.ok, bres.errors[:3] if not bres.ok else "")
    cb = bres.chips.get(0)
    il = getattr(cb, "input_landings", None) if cb is not None else None
    if il is None:
        print("  (no input_landings)")
        return
    port_nets = []
    for conn in proj.connections:
        if (isinstance(conn.source, ChipPortEndpoint)
                and isinstance(conn.target, BlockEndpoint)):
            port_nets.append(conn)
    for conn in port_nets:
        land = il.get(conn.name)
        print(f"  {conn.name}: -> {conn.target.block}.{conn.target.port}"
              f"  landing={land}")


if __name__ == "__main__":
    base = _ROOT / "examples" / "qpsk_modem"
    for f in ("qpsk_modem.orig.kyt",):
        report(str(base / f))
