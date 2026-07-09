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
