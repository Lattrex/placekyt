# SPDX-License-Identifier: GPL-3.0-or-later
"""Build-time refresh of placement-derived panel parameters (GUI-edit safety).

A panel chain's parameters are FUNCTIONS OF THE ROUTES: push-read descriptors
encode the return-corridor length + consumer register/entry; crossover track
hops encode corridor lengths; the RAW CW keyer's emit/done targets encode the
egress length + crossover entries. The GUI lets the user move blocks and redraw
routes, which used to leave those baked values silently stale (user-reported:
after moving the Varicode block the x1 return could not even be re-routed —
also fixed, by exposing the panel return port in the PortMap).

``engine.panel_pnr.refresh_panel_params`` (called by BuildEngine.build) now
re-derives every such parameter from the CURRENT routes. These gates corrupt
the parameters as a stale edit would leave them, rebuild, and prove the chain
still runs EXACT end to end — plus assert the refresh is a no-op on a fresh
auto-P&R'd project (idempotence).
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import numpy as np
import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

_ROOT = Path(__file__).resolve().parents[2]
for _p in (str(_ROOT / "runtime" / "python"), str(_ROOT / "placekyt"),
           str(_ROOT / "examples" / "psk31_transceiver"),
           str(_ROOT / "examples" / "cw_transceiver")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

CHIP_YAML = str(_ROOT / "placekyt" / "resources" / "chips" / "kyttar_10x12.yaml")


def _rebuild(project, cat):
    from engine.build import BuildEngine
    from engine.io.chip_type_io import load_chip_type

    ct = load_chip_type(CHIP_YAML)
    res = BuildEngine(cat, CHIP_YAML).build(project, {project.chip_type: ct})
    assert res.ok, [str(e) for e in res.errors[:3]]
    return res


def test_refresh_is_noop_on_fresh_pnr():
    """A freshly auto-P&R'd project's params already match its routes — the
    refresh must change NOTHING (idempotence; a diff here means the template
    and the route-derivation disagree)."""
    from engine.panel_pnr import refresh_panel_params
    from psk31_transceiver_demo import import_and_pnr

    project, bres, cat, ct = import_and_pnr()
    notes = refresh_panel_params(project, cat)
    assert notes == [], f"refresh changed a fresh project: {notes}"


def test_psk31_stale_params_rescued_by_build():
    """Corrupt the descriptors + crossover hops (a stale-edit state), rebuild:
    the build re-derives them from routes (with named warnings) and the chain
    runs SAMPLE-EXACT."""
    from psk31_transceiver_demo import import_and_pnr, run_duplex
    from psk31_tx_golden import golden_tx_q15

    project, bres, cat, ct = import_and_pnr()
    blk = project.block("varicodeencoder")
    xo = project.block("varicodeencoder_xover")
    blk.params["read_wr_desc"] = 0
    blk.params["read_jp_desc"] = 0
    xo.params["hop_a"] = 1
    xo.params["hop_b"] = 1
    bres2 = _rebuild(project, cat)
    notes = [str(w) for w in bres2.warnings if "panel_param_refreshed" in str(w)]
    assert len(notes) >= 4, f"expected refresh warnings, got {notes}"
    tx, rx = run_duplex(project, bres2, "CQ DE KYTTAR", "R 599 73")
    assert tx == golden_tx_q15("CQ DE KYTTAR", sps=8, amplitude=1.0), \
        "stale-param rebuild does not reproduce the golden"
    assert rx == "R 599 73", f"stale-param rebuild broke the RX ({rx!r})"


def test_cw_stale_raw_emit_params_rescued_by_build():
    """The CW keyer authors its own hops (RAW): corrupt its emit/done targets +
    descriptors, rebuild, and the keying must still be BIT-EXACT."""
    from cw_transceiver_demo import import_and_pnr, keyed_envelope, run_duplex

    project, bres, cat, ct = import_and_pnr()
    blk = project.block("cwkeyer")
    blk.params["emit_hop"] = 1
    blk.params["emit_entry"] = 1
    blk.params["done_entry"] = 0
    blk.params["read_wr_desc"] = 0
    blk.params["read_jp_desc"] = 0
    bres2 = _rebuild(project, cat)
    notes = [str(w) for w in bres2.warnings if "panel_param_refreshed" in str(w)]
    assert len(notes) >= 4, f"expected refresh warnings, got {notes}"
    tx, rx = run_duplex(project, bres2, "CQ CQ", "SOS")
    assert tx == keyed_envelope("CQ CQ"), \
        "stale-RAW-param rebuild does not reproduce the ITU-R golden"
    assert rx == "SOS", f"stale-RAW-param rebuild broke the RX ({rx!r})"


def test_panel_return_port_exposed_for_gui_anchoring():
    """The panel return input ('word'/'base') is a REAL PortMap port on its
    cell — what lets the GUI anchor the x1_in net and route it interactively
    (the user-reported floating fly line)."""
    from engine.catalog import BlockCatalog

    cat = BlockCatalog.from_gr_kyttar()
    pm_v = cat.port_map("VaricodeEncoderBlock", None)
    word = next((p for p in pm_v.ports
                 if p.name == "word" and p.direction == "in"), None)
    assert word is not None and word.cell_id == 1 and word.register is not None
    pm_c = cat.port_map("CWKeyerBlock", None)
    base = next((p for p in pm_c.ports
                 if p.name == "base" and p.direction == "in"), None)
    assert base is not None and base.cell_id == 1 and base.register is not None


def test_corridor_routes_end_on_cells():
    """Every template corridor route starts/ends ON its endpoint cells (the
    GUI-visible connection the user compared against the BPSK modem)."""
    from model.connection import BlockEndpoint, ChipPortEndpoint
    from psk31_transceiver_demo import import_and_pnr

    project, bres, cat, ct = import_and_pnr()
    blk = project.block("varicodeencoder")
    cells = {c.cell_id: (c.x, c.y) for c in blk.placement.cells}
    xo = project.block("varicodeencoder_xover")
    xo_pos = (xo.placement.cells[0].x, xo.placement.cells[0].y)
    routes = {c.name: [(p.x, p.y) for p in c.route]
              for c in project.connections if isinstance(c.route, list)}
    assert routes["in_to_xo"][-1] == xo_pos
    assert routes["xo_to_ctl"][0] == xo_pos
    assert routes["xo_to_ctl"][-1] == cells[0]          # the controller cell
    assert routes["varicodeencoder_panel_return"][-1] == cells[1]  # emit cell
    assert routes["xo_to_out"][0] == xo_pos
