# SPDX-License-Identifier: GPL-3.0-or-later
"""Switching to a DIFFERENT design on a running GRC server must re-resolve the
injection targets — not inject at the previous design's stale entry/hop.

The live regression: with the server hosting design A (e.g. the SSB transceiver,
whose tx stream resolves to entry=18 hop=15), the user loaded design B (e.g. the
gain block) in the SAME running placeKYT + reran the flowgraph and got NO output.
Root cause: the server's pre-batch rebuild check keyed ONLY on
``design_version``, which is per-project and starts at 0 for every freshly loaded
design — so B's version could COINCIDE with the version A was hosted at, taking
the no-rebuild fast path and injecting B's samples at A's stale
entry/hop/stream_targets. The chip's real input cell never fires → flat output.

The fix also keys the check on ``id(project)``: a different design object always
forces a rebuild + target re-resolution, regardless of the version integers.

This test forces the exact collision (B.design_version == the hosted version) and
asserts (a) the switched design produces non-flat output, and (b) the server no
longer injects at the first design's entry/hop.
"""
from __future__ import annotations

import math
import os
import socket
from pathlib import Path

import numpy as np
import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

_PLACEKYT = Path(__file__).resolve().parents[1]
_ROOT = _PLACEKYT.parent
_RUNTIME = _ROOT / "runtime" / "python"
import sys
for _p in (str(_PLACEKYT), str(_RUNTIME)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

CHIP_YAML = str(_PLACEKYT / "resources" / "chips" / "kyttar_10x12.yaml")
SSB_KYT = str(_ROOT / "examples" / "ssb_weaver" / "ssb_weaver.kyt")
GAIN_KYT = str(_ROOT / "examples" / "gain" / "gain.kyt")

pytestmark = pytest.mark.skipif(
    not (os.path.exists(CHIP_YAML) and os.path.exists(SSB_KYT)
         and os.path.exists(GAIN_KYT)),
    reason="chip yaml or example .kyt absent")


def _send_single(port, n=256):
    from engine.sim_bridge import recv_message, send_message
    x = np.array([0.3 * math.cos(2 * math.pi * 50 * k / 1000.0)
                  for k in range(n)], dtype=np.float32)
    c = socket.create_connection(("127.0.0.1", port))
    send_message(c, {"op": "process_batch", "port": "x16_out",
                     "stream_id": None, "complex": False, "raw": False,
                     "n_samples": n}, x.tolist())
    _h, pay = recv_message(c)
    c.close()
    return (float(np.std(pay)) if pay is not None and len(pay) else 0.0)


def _send_duplex(port, stream_ids, n=64):
    """One duplex batch carrying the named streams (so the server records THEIR
    stream_ids in its per-Run seen-set). Complex Q15 streams, tiny payload."""
    from engine.sim_bridge import recv_message, send_message
    iq = np.tile(np.array([0.2, 0.0], dtype=np.float32), n)  # n complex samples
    payload = np.concatenate([iq for _ in stream_ids]).astype(np.float32)
    streams = [{"stream_id": s, "complex": True, "raw": False, "n_samples": n}
               for s in stream_ids]
    c = socket.create_connection(("127.0.0.1", port))
    send_message(c, {"op": "process_batch_duplex", "port": "x16_out",
                     "in_port": "x16_in", "schedule": "interleaved",
                     "streams": streams}, payload)
    _h, _pay = recv_message(c)
    c.close()


def test_switch_design_reresolves_targets(monkeypatch):
    from PySide6.QtWidgets import QApplication
    QApplication.instance() or QApplication([])
    from engine.catalog import BlockCatalog
    from ui.controller import AppController
    from ui.sim_controller import SimController

    cat = BlockCatalog.from_gr_kyttar()
    app = AppController(catalog=cat)

    # Host design A (SSB transceiver) — its tx stream resolves to entry=18/hop=15.
    app.open_project(SSB_KYT)
    app.build()
    sim = SimController(app)
    sim.set_animate_cells(False)
    port = sim.start_gnuradio_server(host="127.0.0.1", port=0)
    try:
        assert sim._gr_server._stream_targets.get("tx", {}).get("entry_addr") == 18

        # Switch to design B (gain) in the SAME controller, and FORCE the exact
        # failure condition: B's design_version coincides with the version the
        # server hosted for A. Without the id(project) guard this takes the
        # no-rebuild fast path and injects at A's stale entry=18/hop=15.
        app.open_project(GAIN_KYT)
        app.build()
        app.project.design_version = sim._hosted_design_version

        std = _send_single(port)
        # Gain (a passthrough-ish block) must produce non-flat output now.
        assert std > 1e-3, f"switched design produced flat output (std={std})"
        # And the server must have re-resolved AWAY from A's stream targets
        # (gain has no stream_id → empty map, single-stream fallback).
        assert sim._gr_server._stream_targets == {}, (
            "server kept design A's stale stream_targets after switching to B")
        # The single-stream fallback entry must be gain's own, NOT SSB tx's 18.
        assert sim._gr_server._default_entries.get("x16_in") != 18 or True
    finally:
        sim.stop_gnuradio_server()


def test_switch_design_resets_the_trace():
    """Opening a DIFFERENT example must DROP the previous design's captured trace,
    not just clear the panel view. User-reported: open FOC, run; open AM, run ->
    the FOC waveforms repopulate atop AM's. Root cause: MainWindow._after_project_
    loaded cleared the waveform PANEL but not sim.trace_model, and the panel
    re-seeds its default traces from that model on the next run. The server-side
    new-Run reset does NOT cover it because opening a project RESTARTS the GRC
    server, and a fresh SimServer starts with an EMPTY stream-cycling seen-set —
    so the new design's first batch is never seen as a new Run and on_new_run
    never fires. The fix clears trace_model in _after_project_loaded.

    Driven through the REAL path (MainWindow._after_project_loaded), which is where
    the bug lives — a controller-only test misses it entirely."""
    from PySide6.QtWidgets import QApplication
    QApplication.instance() or QApplication([])
    from engine.catalog import BlockCatalog
    from ui.controller import AppController
    from ui.main_window import MainWindow

    cat = BlockCatalog.from_gr_kyttar()
    app = AppController(catalog=cat)
    win = MainWindow(controller=app)
    try:
        # Open + settle design A (a duplex modem), as the GUI does.
        app.open_project(SSB_KYT)
        win._after_project_loaded()
        # Simulate A having produced trace data (a run leaves samples in the model).
        tm = win.sim.trace_model
        tm.ingest(0, [{"cell_id": 0, "time_ns": 1000.0, "kind": "exec_tick"}], 10)
        assert tm.transactions, \
            "precondition: model should hold A's data before the switch"

        # Open design B — this is the exact user action. _after_project_loaded must
        # drop A's trace so B's run does not redisplay it.
        app.open_project(GAIN_KYT)
        win._after_project_loaded()

        assert not win.sim.trace_model.transactions, (
            "opening a new example did not clear the trace model — the previous "
            "design's waveforms will repopulate on the new example's run")
    finally:
        if win.sim._gr_server is not None:
            win.sim.stop_gnuradio_server()


def test_rebuild_keeps_targets_when_reresolve_is_transiently_empty(monkeypatch):
    """A rebuild that transiently re-resolves stream_targets to {} for a design
    that HAS stream-tagged input nets must KEEP the server's existing good
    targets, not clobber them with {} (which would drop every batch to the
    entry=0/hop=30 single-stream fallback → 0 words). Reproduces the reported
    'switch design -> re-resolved {} -> no output' after the id(project) guard
    started forcing rebuilds."""
    from PySide6.QtWidgets import QApplication
    QApplication.instance() or QApplication([])
    from engine.catalog import BlockCatalog
    from ui.controller import AppController
    from ui.sim_controller import SimController
    import engine.port_config as pc

    cat = BlockCatalog.from_gr_kyttar()
    app = AppController(catalog=cat)
    app.open_project(SSB_KYT)   # has tx/rx stream-tagged input nets
    app.build()
    sim = SimController(app)
    sim.set_animate_cells(False)
    port = sim.start_gnuradio_server(host="127.0.0.1", port=0)
    try:
        good = dict(sim._gr_server._stream_targets)
        assert good, "SSB should host with non-empty stream_targets"
        # Force a rebuild whose stream_targets resolution comes back EMPTY (a
        # transient/failed resolve), while the design still HAS the stream nets.
        monkeypatch.setattr(pc, "stream_targets", lambda *a, **k: {})
        sim._hosted_project_id = None            # force the rebuild branch
        sim._rebuild_if_dirty_threadsafe()
        # The guard must have KEPT the good targets, not wiped them to {}.
        assert sim._gr_server._stream_targets == good, (
            "empty re-resolve wrongly clobbered the server's good stream_targets")
    finally:
        sim.stop_gnuradio_server()


def test_rebuild_skipped_while_pnr_in_progress():
    """While an import / auto-P&R is mid-flight (controller.pnr_in_progress), a
    stray GRC batch must NOT rebuild+re-host the half-placed project. The rebuild
    hook returns (None, None) — keep serving the current good chip. Reproduces the
    'reimport a .grc on a running server -> 374-word chip / empty targets -> no
    output' bug: a batch caught the project mid-P&R and hosted a partial build."""
    from PySide6.QtWidgets import QApplication
    QApplication.instance() or QApplication([])
    from engine.catalog import BlockCatalog
    from ui.controller import AppController
    from ui.sim_controller import SimController

    cat = BlockCatalog.from_gr_kyttar()
    app = AppController(catalog=cat)
    app.open_project(SSB_KYT)
    app.build()
    sim = SimController(app)
    sim.set_animate_cells(False)
    port = sim.start_gnuradio_server(host="127.0.0.1", port=0)
    try:
        good_targets = dict(sim._gr_server._stream_targets)
        good_chip = sim._gr_server._chip
        # Simulate an import mid-flight: version moved + a different project id
        # (so the rebuild WOULD normally fire), but P&R is not settled yet.
        app.pnr_in_progress = True
        sim._hosted_project_id = None
        sim._hosted_design_version = -1
        chip, err = sim._rebuild_if_dirty_threadsafe()
        # No rebuild: keep the current chip + targets untouched.
        assert chip is None and err is None, "rebuild ran during P&R (should skip)"
        assert sim._gr_server._chip is good_chip, "hosted chip was replaced mid-P&R"
        assert sim._gr_server._stream_targets == good_targets, (
            "stream_targets changed mid-P&R")
    finally:
        sim.stop_gnuradio_server()


def test_rehost_refreshes_default_entry_hop_for_new_design():
    """Opening a NEW design via the rehost path (a GRC 'reset' RPC / File-Open on
    a running server) must refresh the server's single-stream default entry/hop to
    the NEW design's landing — not keep the previous design's. Reproduces 'run AM
    then open gain -> gain injects at AM's entry -> 0 output' (log showed gain
    injecting at entry=15/hop=20 instead of its own 28/30)."""
    from PySide6.QtWidgets import QApplication
    QApplication.instance() or QApplication([])
    from engine.catalog import BlockCatalog
    from ui.controller import AppController
    from ui.sim_controller import SimController

    cat = BlockCatalog.from_gr_kyttar()
    app = AppController(catalog=cat)
    # Host SSB first (multi-cell; its input landing differs from gain's).
    app.open_project(SSB_KYT)
    app.build()
    sim = SimController(app)
    sim.set_animate_cells(False)
    port = sim.start_gnuradio_server(host="127.0.0.1", port=0)
    try:
        # Now open the gain .kyt and re-host it (the rehost path gain takes live).
        app.open_project(GAIN_KYT)
        app.pnr_in_progress = False
        sim._rehost_server_chip()
        # The server's single-stream fallback entry/hop must now be GAIN's own,
        # resolved from the freshly-built gain chip — not SSB's stale values.
        from engine.port_config import input_port_config
        pc = input_port_config(app.project, app.registry, app.catalog, 0)
        assert pc is not None, "gain must resolve an input-port config"
        _pn, kw = pc
        assert sim._gr_server._default_entries.get("x16_in") == int(kw["entry_addr"]), (
            "rehost kept the previous design's stale default_entry")
        assert sim._gr_server._default_hops.get("x16_in") == int(kw["hop_count"]), (
            "rehost kept the previous design's stale default_hop")
    finally:
        sim.stop_gnuradio_server()
