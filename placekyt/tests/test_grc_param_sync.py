"""GRC↔placeKYT parameter-sync: detection, the 3 preference modes, undoable resync.

Covers:
  * Detection: ``engine.grc_sync`` diffs recorded GRC params vs placed params,
    coercing GRC strings so representation doesn't cause false positives, and
    flags a RESIZE (FIR 7→40 taps grows cell_count).
  * Wire detection: the SimServer ``set_grc_params`` op + the additive
    ``grc_params`` field on a ``process_batch`` header both invoke
    ``on_grc_params`` (backward compatible — absent ⇒ no call).
  * The 3 preference modes branch correctly (notify = no auto-action; auto =
    re-place+re-route; reanchor = resize-in-place, routes untouched).
  * The resync is ONE undoable command (undo restores params + placement).
"""

from __future__ import annotations

import os
import socket

import numpy as np
import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from engine.catalog import BlockCatalog  # noqa: E402
from engine.grc_sync import GrcSyncState, compute_param_diff  # noqa: E402
from engine.sim_bridge import (SimServer, recv_message,  # noqa: E402
                               send_message)
from model.block import Block  # noqa: E402
from model.placement import PlacedCell, Placement  # noqa: E402
from model.enums import Face  # noqa: E402
from model.project import Project  # noqa: E402


# ----------------------------------------------------------------------------
# Detection (Qt-free)
# ----------------------------------------------------------------------------

@pytest.fixture(scope="module")
def catalog():
    return BlockCatalog.from_gr_kyttar()


def _gain_project(catalog, gain=0.5):
    """A one-block project with a placed GainBlock."""
    spec = catalog.get("GainBlock")
    proj = Project(chip_type="kyttar_10x12")
    blk = Block("gain0", "GainBlock", library=spec.library,
                params={"gain": gain, "gain_range": 15})
    blk.placement = Placement(chip=0, cells=[PlacedCell(0, 0, 0, Face.EAST)])
    proj.blocks.append(blk)
    return proj


def test_diff_detects_changed_param(catalog):
    proj = _gain_project(catalog, gain=0.5)
    # GRC says gain=0.9 — a drift from the placed 0.5.
    diff = compute_param_diff(proj, catalog, {"gain0": {"gain": 0.9}})
    assert "gain0" in diff
    cur, grc = diff["gain0"].changes["gain"]
    assert cur == 0.5 and grc == 0.9
    assert diff["gain0"].resizes is False  # gain doesn't change geometry


def test_diff_in_sync_when_equal(catalog):
    proj = _gain_project(catalog, gain=0.5)
    # GRC sends the SAME value — even as a STRING; coercion makes it equal.
    diff = compute_param_diff(proj, catalog, {"gain0": {"gain": "0.5"}})
    assert diff == {}


def test_none_default_param_does_not_falsely_drift(catalog):
    """An optional param whose default is None (e.g. PSKSymbolMapperBlock's
    ``symbol_table``) must NOT be flagged out-of-sync on a fresh import. The
    coercion used to stringify it (``str(None)`` -> ``'None'``), making the
    re-coerced GRC param ('None') differ from the real placed value (None) and
    falsely reporting drift. None defaults now literal-eval back to None."""
    spec = catalog.get("PSKSymbolMapperBlock")
    proj = Project(chip_type="kyttar_10x12")
    blk = Block("mapper0", "PSKSymbolMapperBlock", library=spec.library,
                params={"modulation": "bpsk", "symbol_table": None,
                        "dimension": 1})
    blk.placement = Placement(chip=0, cells=[PlacedCell(0, 0, 0, Face.EAST)])
    proj.blocks.append(blk)
    # The GRC baseline (as observe_many seeds it) carries the real placed params,
    # including symbol_table=None. The diff must come back EMPTY (in sync).
    diff = compute_param_diff(proj, catalog, {"mapper0": dict(blk.params)})
    assert diff == {}, f"None-default param falsely drifted: {diff}"


def test_diff_flags_resize_on_fir_taps(catalog):
    """A FIR going 7→40 coefficients grows its cell count → resizes=True."""
    spec = catalog.get("FIRFilterBlock")
    proj = Project(chip_type="kyttar_10x12")
    blk = Block("fir0", "FIRFilterBlock", library=spec.library,
                params={"coefficients": [1.0] * 7})
    blk.placement = Placement(chip=0, cells=[PlacedCell(0, 0, 0, Face.EAST)])
    proj.blocks.append(blk)
    diff = compute_param_diff(proj, catalog,
                              {"fir0": {"coefficients": [1.0] * 40}})
    assert "fir0" in diff
    assert diff["fir0"].resizes is True


def test_sync_state_observe_and_clear(catalog):
    proj = _gain_project(catalog, gain=0.5)
    st = GrcSyncState()
    assert st.in_sync
    st.observe("gain0", {"gain": 0.9})
    st.diff_against(proj, catalog)
    assert not st.in_sync
    st.clear()
    assert st.in_sync


# ----------------------------------------------------------------------------
# Wire detection (SimServer)
# ----------------------------------------------------------------------------

class _NullChip:
    def inject_data_physical(self, *a, **k): pass
    def inject_jump_physical(self, *a, **k): pass
    def run(self, **k): pass
    def read_port(self, port): return np.array([], dtype=np.float32)


def _client(port):
    c = socket.socket()
    c.connect(("127.0.0.1", port))
    return c


def test_set_grc_params_op_invokes_callback():
    seen = []
    srv = SimServer(_NullChip(), on_grc_params=lambda p: seen.append(p))
    port = srv.start()
    try:
        c = _client(port)
        send_message(c, {"op": "set_grc_params",
                         "params": {"fir0": {"coefficients": [1.0] * 40}}})
        reply, _ = recv_message(c)
        assert reply["ok"]
        c.close()
    finally:
        srv.stop()
    assert seen == [{"fir0": {"coefficients": [1.0] * 40}}]


def test_process_batch_grc_params_field_invokes_callback():
    """The additive ``grc_params`` field on a process_batch header is detected."""
    seen = []
    srv = SimServer(_NullChip(), on_grc_params=lambda p: seen.append(p))
    port = srv.start()
    try:
        c = _client(port)
        send_message(c, {"op": "process_batch", "port": "x16_out",
                         "in_port": "x16_in", "complex": False,
                         "grc_params": {"gain0": {"gain": 0.9}}},
                     np.array([0.1, 0.2], dtype="<f4"))
        reply, _ = recv_message(c)
        assert reply["ok"]
        c.close()
    finally:
        srv.stop()
    assert seen == [{"gain0": {"gain": 0.9}}]


def test_process_batch_without_grc_params_is_backward_compatible():
    """No ``grc_params`` field ⇒ the callback is never invoked (back-compat)."""
    seen = []
    srv = SimServer(_NullChip(), on_grc_params=lambda p: seen.append(p))
    port = srv.start()
    try:
        c = _client(port)
        send_message(c, {"op": "process_batch", "port": "x16_out",
                         "in_port": "x16_in", "complex": False},
                     np.array([0.1, 0.2], dtype="<f4"))
        reply, _ = recv_message(c)
        assert reply["ok"]
        c.close()
    finally:
        srv.stop()
    assert seen == []


# ----------------------------------------------------------------------------
# SENDING / advertising link (gr-kyttar _batch_session)
# ----------------------------------------------------------------------------
#
# The receiving side (above) is proven. These tests cover the missing SEND link:
# the GRC marker DSP blocks advertise their params into the shared per-device
# BatchSession (``register_params``), and the source's batch dispatch ships them
# to the SimServer (``grc_params`` header) → ``on_grc_params`` → a real diff.
#
# ``_batch_session`` lives in gr-kyttar and imports with only socket+numpy (no
# GNURadio, no placeKYT), so we load it by path and drive the wire directly —
# exactly what the source block does at dispatch, minus GNURadio.

import importlib.util  # noqa: E402
from pathlib import Path  # noqa: E402

_BS_PATH = (Path(__file__).resolve().parents[2]
            / "gr-kyttar" / "python" / "kyttar" / "_batch_session.py")


def _load_batch_session():
    spec = importlib.util.spec_from_file_location("kyttar_batch_session", _BS_PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_register_params_keys_match_importer_naming():
    """A marker advertises by TYPE; the session reconstructs the placeKYT block
    NAME the importer would assign — first instance = _default_name, repeats get
    the ``_2`` suffix — so the keys line up with the diff."""
    bs = _load_batch_session()
    sess = bs.BatchSession("kyttar_0")
    assert sess.register_params("GainBlock", {"gain": 0.9}) == "gain"
    assert sess.register_params("FIRFilterBlock", {"coefficients": [1.0] * 40}) \
        == "firfilter"
    # A second instance of the same type gets the importer's _unique suffix.
    assert sess.register_params("GainBlock", {"gain": 0.3}) == "gain_2"
    collected = sess.collected_params()
    assert collected == {"gain": {"gain": 0.9},
                         "firfilter": {"coefficients": [1.0] * 40},
                         "gain_2": {"gain": 0.3}}


def test_dispatch_sends_grc_params_and_server_diffs():
    """FULL send path: advertise → dispatch ships ``grc_params`` → SimServer
    ``on_grc_params`` fires → the grc_sync diff against a placed design is
    non-empty (the indicator's data)."""
    bs = _load_batch_session()
    sess = bs.BatchSession("kyttar_0")
    # GRC says gain=0.9 — a drift from a placed 0.5 (built below).
    sess.register_params("GainBlock", {"gain": 0.9})

    seen = []
    srv = SimServer(_NullChip(), on_grc_params=lambda p: seen.append(p))
    port = srv.start()
    try:
        # A real REAL-burst dispatch (complex=False) over the live socket — the
        # source's exact call shape, sans GNURadio. The _NullChip returns no
        # output, which dispatch tolerates.
        sess.dispatch("127.0.0.1", port, [0.1, 0.2, 0.3],
                      in_port="x16_in", out_port="x16_out",
                      complex=False, raw=False)
    finally:
        srv.stop()

    # The server received the advertised params alongside the batch.
    assert seen == [{"gain": {"gain": 0.9}}]

    # And those params, fed to the same diff the GUI uses, flag the drift. The
    # placed block is named "gain" — exactly what grc_import's _default_name
    # assigns the first GainBlock — which is the key the marker reconstructs.
    catalog = BlockCatalog.from_gr_kyttar()
    spec = catalog.get("GainBlock")
    proj = Project(chip_type="kyttar_10x12")
    blk = Block("gain", "GainBlock", library=spec.library,
                params={"gain": 0.5, "gain_range": 15})
    blk.placement = Placement(chip=0, cells=[PlacedCell(0, 0, 0, Face.EAST)])
    proj.blocks.append(blk)
    diff = compute_param_diff(proj, catalog, seen[0])
    assert "gain" in diff
    cur, grc = diff["gain"].changes["gain"]
    assert cur == 0.5 and grc == 0.9


def test_dispatch_without_advertised_params_omits_header():
    """No advertised params ⇒ no ``grc_params`` header ⇒ callback never fires
    (backward compatible with a host/run that has nothing to sync)."""
    bs = _load_batch_session()
    sess = bs.BatchSession("kyttar_0")  # nothing registered
    seen = []
    srv = SimServer(_NullChip(), on_grc_params=lambda p: seen.append(p))
    port = srv.start()
    try:
        sess.dispatch("127.0.0.1", port, [0.1, 0.2],
                      in_port="x16_in", out_port="x16_out",
                      complex=False, raw=False)
    finally:
        srv.stop()
    assert seen == []


def test_register_params_resets_on_new_run():
    """Across runs (a prior dispatch flipped ``done``) the first re-registration
    starts a fresh advertisement map, so per-type counters don't grow unbounded
    in a long-lived GR process."""
    bs = _load_batch_session()
    sess = bs.BatchSession("kyttar_0")
    sess.register_params("GainBlock", {"gain": 0.9})
    sess.done = True  # simulate a completed dispatch (end of run 1)
    # Run 2: the first registration clears the stale map and re-keys from scratch.
    assert sess.register_params("GainBlock", {"gain": 0.4}) == "gain"
    assert sess.collected_params() == {"gain": {"gain": 0.4}}


# ----------------------------------------------------------------------------
# Controller + the 3 preference modes + undoable resync
# ----------------------------------------------------------------------------

from tests.conftest import CHIP_YAML  # noqa: E402

pytestmark_ct = pytest.mark.skipif(
    not CHIP_YAML.exists(), reason="chip yaml absent")


@pytest.fixture(scope="module")
def qapp():
    from PySide6.QtWidgets import QApplication
    return QApplication.instance() or QApplication([])


def _routed_two_block_rx(catalog):
    """A tiny routed chain: input → Gain → output. Returns the controller."""
    from ui.controller import AppController
    from model.connection import BlockEndpoint, ChipPortEndpoint

    ctrl = AppController(catalog=catalog)
    ctrl.new_project("sync", "kyttar_10x12")
    lib = "lattrex.official"
    g = ctrl.place_block("GainBlock", 0, 0, 0, library=lib)
    ctrl.add_route(ChipPortEndpoint(chip=0, port="x16_in"),
                   BlockEndpoint(block=g, port="sample"), [])
    ctrl.add_route(BlockEndpoint(block=g, port="out"),
                   ChipPortEndpoint(chip=0, port="x16_out"), [])
    ctrl.auto_place(0)
    return ctrl, g


@pytestmark_ct
def test_observe_grc_params_marks_out_of_sync(qapp, catalog):
    ctrl, g = _routed_two_block_rx(catalog)
    assert not ctrl.grc_out_of_sync()
    diffs = ctrl.observe_grc_params({g: {"gain": 0.9}})
    assert g in diffs
    assert ctrl.grc_out_of_sync()


@pytestmark_ct
def test_notify_mode_does_not_change_design(qapp, catalog):
    """Notify-only: observing a drift flags it but does NOT mutate the design."""
    from engine import preferences

    ctrl, g = _routed_two_block_rx(catalog)
    before = ctrl.project.block(g).params["gain"]
    ctrl.observe_grc_params({g: {"gain": 0.9}})
    # The indicator path (notify) is a pure observation — no resync invoked.
    assert ctrl.project.block(g).params["gain"] == before
    assert ctrl.grc_out_of_sync()


@pytestmark_ct
def test_auto_mode_resyncs_params(qapp, catalog):
    """Auto mode: resync re-applies the GRC params and clears out-of-sync."""
    from engine import preferences

    ctrl, g = _routed_two_block_rx(catalog)
    ctrl.observe_grc_params({g: {"gain": 0.9}})
    affected, report = ctrl.resync_from_grc(mode=preferences.GRC_AUTO)
    assert g in affected
    assert ctrl.project.block(g).params["gain"] == 0.9
    assert not ctrl.grc_out_of_sync()


@pytestmark_ct
def test_reanchor_mode_resizes_in_place_keeping_anchor(qapp, catalog):
    """Re-anchor: a FIR 1→8 cells resize keeps the block's anchor (min corner)
    and does NOT reroute (report is None)."""
    from engine import preferences
    from ui.controller import AppController
    from model.connection import BlockEndpoint, ChipPortEndpoint

    ctrl = AppController(catalog=catalog)
    ctrl.new_project("sync", "kyttar_10x12")
    lib = "lattrex.official"
    fir = ctrl.place_block("FIRFilterBlock", 0, 2, 3, library=lib,
                           params={"coefficients": [1.0]})
    blk = ctrl.project.block(fir)
    anchor_before = (blk.placement.bounding_box()[0],
                     blk.placement.bounding_box()[1])
    n_before = len(blk.placement.cells)
    ctrl.observe_grc_params({fir: {"coefficients": [1.0] * 40}})
    affected, report = ctrl.resync_from_grc(mode=preferences.GRC_REANCHOR)
    assert fir in affected
    assert report is None, "re-anchor must NOT reroute"
    blk = ctrl.project.block(fir)
    n_after = len(blk.placement.cells)
    assert n_after > n_before, "FIR should have grown"
    anchor_after = (blk.placement.bounding_box()[0],
                    blk.placement.bounding_box()[1])
    assert anchor_after == anchor_before, "anchor (min corner) must be preserved"


@pytestmark_ct
def test_resync_is_one_undoable_command(qapp, catalog):
    """The resync is ONE undo step: undo restores both params and placement."""
    from engine import preferences

    ctrl, g = _routed_two_block_rx(catalog)
    gain_before = ctrl.project.block(g).params["gain"]
    cells_before = [(c.cell_id, c.x, c.y) for c in
                    ctrl.project.block(g).placement.cells]
    depth_before = ctrl.commands.undo_depth

    ctrl.observe_grc_params({g: {"gain": 0.9}})
    ctrl.resync_from_grc(mode=preferences.GRC_AUTO)
    assert ctrl.commands.undo_depth == depth_before + 1, "exactly ONE undo step"
    assert ctrl.project.block(g).params["gain"] == 0.9

    ctrl.undo()
    assert ctrl.project.block(g).params["gain"] == gain_before
    cells_after = [(c.cell_id, c.x, c.y) for c in
                   ctrl.project.block(g).placement.cells]
    assert cells_after == cells_before, "undo must restore placement"


@pytestmark_ct
def test_preferences_roundtrip(qapp):
    """The QSettings-backed preference persists + coerces invalid values."""
    from engine import preferences

    preferences.set_grc_param_change_mode(preferences.GRC_AUTO)
    assert preferences.grc_param_change_mode() == preferences.GRC_AUTO
    preferences.set_grc_param_change_mode("bogus")
    assert preferences.grc_param_change_mode() == preferences.GRC_NOTIFY
    preferences.set_grc_param_change_mode(preferences.GRC_NOTIFY)


if __name__ == "__main__":
    import sys
    sys.exit(pytest.main([__file__, "-q"]))
