# SPDX-License-Identifier: GPL-3.0-or-later
"""ABUTMENT connections must render as a SOLID link on the canvas, not vanish.

An abutment net (source output cell directly abutting the target input cell) is a
REAL routed connection with no corridor waypoints — the build synthesises the @1
handoff. Before this gate the canvas drew nothing for it (it is is_routed=True so no
fly line, but conn.route is the ABUTMENT sentinel string, not a waypoint list, so
the routed-line path had nothing to draw) — the link looked like it was "not wired".

This test loads converter_flavors.kyt (whose 4 inter-block links are abutments) and
asserts EVERY connection renders as a ConnectionItem and none of the abutment nets
are missing or drawn as a (dashed, unrouted) fly line.

Run::

    cd /home/system/placekyt
    QT_QPA_PLATFORM=offscreen .venv/bin/python -m pytest \
        verification/tests/test_abutment_render.py -q
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

_ROOT = Path(__file__).resolve().parents[2]
for p in (str(_ROOT / "placekyt"), str(_ROOT / "runtime" / "python")):
    if p not in sys.path:
        sys.path.insert(0, p)

KYT = _ROOT / "verification" / "tests" / "data" / "converter_flavors.kyt"
CHIP_YAML = str(_ROOT / "placekyt" / "resources" / "chips" / "kyttar_10x12.yaml")


def _render():
    from PySide6.QtWidgets import QApplication
    QApplication.instance() or QApplication([])
    from engine.catalog import BlockCatalog
    from engine.io.chip_type_io import load_chip_type
    from engine.io.project_io import load_project
    from ui.canvas.chip_canvas import ChipCanvas

    cat = BlockCatalog.from_gr_kyttar()
    proj = load_project(str(KYT))
    ct = load_chip_type(CHIP_YAML)

    def prov(bt, lib, params=None):
        out = {}
        try:
            pm = cat.port_map(bt, params, library=lib)
            for pp in pm.ports:
                out[pp.name] = (pp.cell_id, pp.direction)
        except Exception:  # noqa: BLE001
            pass
        return out

    cv = ChipCanvas()
    cv.port_cell_provider = prov
    cv.set_project(proj, {"kyttar_10x12": ct})
    return cv, proj


def test_abutment_nets_render_solid_not_missing():
    from ui.canvas.connection_item import ConnectionItem
    cv, proj = _render()

    items = [it for it in cv._scene.items() if isinstance(it, ConnectionItem)]
    by_name = {it.connection_name: it for it in items if it.connection_name}

    abut = [c.name for c in proj.connections if c.route == "abutment"]
    assert abut, "fixture must contain abutment nets"

    for name in abut:
        assert name in by_name, f"abutment net {name!r} drew NO connection item"
        assert not by_name[name].is_fly, (
            f"abutment net {name!r} drew a fly line (dashed/unrouted) — it is a "
            "real routed link and must render solid")

    # Every connection in the project drew exactly one ConnectionItem.
    assert set(by_name) >= {c.name for c in proj.connections}, (
        "some connections did not render at all")
