"""The GNURadio server must run the CURRENT design, never a stale build.

Regression for the bug the user found: with the GNURadio server running, deleting
the Costas->Gardner route in the GUI and re-running the GRC flowgraph still produced
output — because the server hosted the chip built ONCE at "Run as Server" and never
rebuilt it when routes changed. A `process_batch` must reflect edits made since the
server started: rebuild-if-dirty before serving, and ERROR (not silently run a stale
chip) if the edited design fails to build/route.

This drives the server's ``on_before_batch`` hook (SimController
``_rebuild_if_dirty_threadsafe``) directly — no socket needed — across the three
cases: clean (fast path), edited-and-routable (fresh chip), edited-and-broken
(error, not a stale run).
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication  # noqa: E402

from engine.catalog import BlockCatalog  # noqa: E402
from engine.io.chip_type_io import load_chip_type  # noqa: E402
from ui.controller import AppController  # noqa: E402
from ui.sim_controller import SimController  # noqa: E402
from model.connection import BlockEndpoint, ChipPortEndpoint  # noqa: E402

from tests.conftest import CHIP_YAML as CT_PATH  # noqa: E402
pytestmark = pytest.mark.skipif(not CT_PATH.exists(), reason="chip yaml absent")


@pytest.fixture(scope="module")
def qapp():
    return QApplication.instance() or QApplication([])


def _build_rx(catalog):
    """Place MF->Costas->Gardner->Slicer, route all forward nets (mirror the
    production RX recipe). Returns the controller with a routed, buildable RX."""
    ctrl = AppController(catalog=catalog)
    ctrl.new_project("rx", "kyttar_10x12")
    lib = "lattrex.official"
    mf = ctrl.place_block("ComplexRRCMatchedFilterBlock", 0, 0, 0, library=lib)
    cos = ctrl.place_block("ComplexCostasLoopBlock", 0, 0, 0, library=lib)
    gar = ctrl.place_block("GardnerTimingRecovery", 0, 0, 0, library=lib)
    sli = ctrl.place_block("BPSKSlicerBlock", 0, 0, 0, library=lib)
    ctrl.add_route(ChipPortEndpoint(chip=0, port="x16_in"),
                   BlockEndpoint(block=mf, port="xi"), [])
    ctrl.add_route(ChipPortEndpoint(chip=0, port="x16_in"),
                   BlockEndpoint(block=mf, port="xq"), [])
    ctrl.add_route(BlockEndpoint(block=mf, port="yi"),
                   BlockEndpoint(block=cos, port="xi"), [])
    ctrl.add_route(BlockEndpoint(block=mf, port="yq"),
                   BlockEndpoint(block=cos, port="xq"), [])
    n_cg = ctrl.add_route(BlockEndpoint(block=cos, port="yi_tap"),
                          BlockEndpoint(block=gar, port="xi"), [])
    ctrl.add_route(BlockEndpoint(block=gar, port="out"),
                   BlockEndpoint(block=sli, port="llr"), [])
    ctrl.add_route(BlockEndpoint(block=sli, port="out"),
                   ChipPortEndpoint(chip=0, port="x16_out"), [])
    ctrl.auto_place(0)
    ct = load_chip_type(str(CT_PATH))
    rep = ctrl.auto_route_all({"kyttar_10x12": ct}, auto_orient=False,
                              use_bus="always")
    assert rep.ok, [(r.name, r.reason) for r in rep.failed]
    # the Costas->Gardner connection name (for the deletion experiment)
    cg = next((c.name for c in ctrl.project.connections
               if getattr(c.source, "block", None) == cos
               and getattr(c.target, "block", None) == gar), None)
    return ctrl, cg


def _host(qapp, catalog):
    ctrl, cg = _build_rx(catalog)
    sim = SimController(ctrl)
    port = sim.start_gnuradio_server()
    assert port, "server failed to start"
    return ctrl, sim, cg


def test_clean_design_is_fast_path(qapp):
    """No edit since build -> the pre-batch hook keeps the current chip (None)."""
    cat = BlockCatalog.from_gr_kyttar()
    ctrl, sim, _cg = _host(qapp, cat)
    try:
        chip, err = sim._rebuild_if_dirty_threadsafe()
        assert err is None
        assert chip is None, "unedited design should keep the hosted chip"
    finally:
        sim.stop_gnuradio_server()


def test_edit_then_batch_rebuilds(qapp):
    """A placement edit (move a block) marks the design dirty -> the next batch
    rebuilds and re-hosts a FRESH chip (not the stale one)."""
    cat = BlockCatalog.from_gr_kyttar()
    ctrl, sim, _cg = _host(qapp, cat)
    try:
        assert not ctrl.project.build_dirty       # build() at server-start cleared it
        # An edit through a command sets build_dirty.
        ctrl.project.mark_dirty()
        assert ctrl.project.build_dirty
        chip, err = sim._rebuild_if_dirty_threadsafe()
        assert err is None
        assert chip is not None, "edited+routable design must re-host a fresh chip"
        assert not ctrl.project.build_dirty, "rebuild should clear build_dirty"
    finally:
        sim.stop_gnuradio_server()


def test_deleting_route_does_not_silently_run_stale(qapp):
    """THE bug: with the server running, deleting the Costas->Gardner route must
    NOT let the next batch silently run the old (stale) build. After the delete
    the design no longer routes that net, so the pre-batch rebuild must EITHER
    re-host a chip whose Costas no longer feeds Gardner OR return an error — it
    must NOT return (None, None) (the stale-chip fast path)."""
    cat = BlockCatalog.from_gr_kyttar()
    ctrl, sim, cg = _host(qapp, cat)
    assert cg, "Costas->Gardner connection not found"
    try:
        ctrl.delete_route(cg)                      # break the physical route
        assert ctrl.project.build_dirty, "delete must mark the design dirty"
        chip, err = sim._rebuild_if_dirty_threadsafe()
        # Not the stale fast path: either a fresh re-host or a surfaced error.
        assert not (chip is None and err is None), \
            "deleting a route must invalidate the hosted chip, not run it stale"
    finally:
        sim.stop_gnuradio_server()


def test_reroute_then_gui_refresh_still_rebuilds(qapp):
    """THE stale-run / phantom-cells root cause (the user's live report).

    After a reroute the GUI fires its own post-edit refresh
    (``_on_model_changed`` -> ``_sync_resolved_faces`` -> ``cached_build()``),
    which rebuilds for the inspector/canvas and CLEARS ``build_dirty``. If the
    server's pre-batch check keyed on ``build_dirty`` it would then read False and
    skip — running the STALE chip with phantom cells from the old route, with NO
    rebuild log (exactly what the user saw). The fix keys on the monotonic
    ``design_version`` (bumped on every edit, never cleared by a build), so the
    GRC Run rebuilds even though the GUI already consumed ``build_dirty``."""
    from model.connection import BlockEndpoint  # noqa: F811
    from commands import SetConnectionRouteCommand

    cat = BlockCatalog.from_gr_kyttar()
    ctrl, sim, cg = _host(qapp, cat)
    assert cg, "Costas->Gardner connection not found"
    try:
        # Reroute the Costas->Gardner net along a different path (delete + re-add).
        conn = ctrl.project.connection(cg)
        new_route = [(p.x, p.y) for p in (conn.route or [])]
        ctrl.commands.execute(SetConnectionRouteCommand(ctrl.project, cg, None))
        ctrl.commands.execute(SetConnectionRouteCommand(ctrl.project, cg, new_route))
        # Simulate the GUI's post-edit refresh that consumes build_dirty.
        ctrl.cached_build()
        assert not ctrl.project.build_dirty, \
            "precondition: GUI refresh clears build_dirty (this is what broke it)"
        # Despite build_dirty being False, the server MUST still rebuild because the
        # design_version advanced past the hosted version.
        chip, err = sim._rebuild_if_dirty_threadsafe()
        assert not (chip is None and err is None), (
            "after a reroute + GUI refresh the server still ran the STALE chip "
            "(design_version not consulted) — the phantom-cells bug")
        assert err is None, f"reroute should rebuild cleanly, got: {err}"
        assert chip is not None
        # And a second batch with no further edits takes the fast path.
        chip2, err2 = sim._rebuild_if_dirty_threadsafe()
        assert chip2 is None and err2 is None, \
            "no edit since rebuild -> fast path (no redundant rebuild)"
    finally:
        sim.stop_gnuradio_server()


if __name__ == "__main__":
    import sys
    app = QApplication.instance() or QApplication([])
    test_clean_design_is_fast_path(app)
    print("[1] clean fast-path: PASS")
    test_edit_then_batch_rebuilds(app)
    print("[2] edit->rebuild: PASS")
    test_deleting_route_does_not_silently_run_stale(app)
    print("[3] delete-route not stale: PASS")
    test_reroute_then_gui_refresh_still_rebuilds(app)
    print("[4] reroute + GUI refresh still rebuilds: PASS")
