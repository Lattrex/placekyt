"""The GRC-server ORDER (start server vs import flowgraph) must NOT matter.

If the user turns the placeKYT GNURadio server ON before importing/routing a
flowgraph, the server captures an EMPTY stream_targets map ({}) at start-up.
When the design is then imported + auto-P&R'd, ``design_version`` bumps and the
next batch triggers ``_rebuild_if_dirty_threadsafe`` — which rebuilds the hosted
chip. THIS TEST guards that the rebuild also RE-RESOLVES stream_targets +
batch_reset_writes into the running server, so the batch injects at the routed
entries (25/…) instead of the entry=0/hop=30/out_tag=None single-stream fallback
(which emits 0 words — the "server-on-then-import → flat run" bug).
"""
from __future__ import annotations

import os
import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
QT = pytest.importorskip("PySide6.QtWidgets")
pytest.importorskip("simkyt")

from engine.catalog import BlockCatalog          # noqa: E402
from tests.conftest import EXAMPLES_DIR          # noqa: E402

GRC = EXAMPLES_DIR / "bpsk_modem.grc"
pytestmark = pytest.mark.skipif(not GRC.exists(), reason="modem .grc absent")


def test_rebuild_reresolves_stream_targets():
    from ui.controller import AppController
    from ui.sim_controller import SimController

    app = QT.QApplication.instance() or QT.QApplication([])
    ctrl = AppController(catalog=BlockCatalog.from_gr_kyttar())
    sim = SimController(ctrl)
    ctrl.import_grc(str(GRC))
    ctrl.auto_place(use_bus="always")
    ctrl.auto_pnr(use_bus="always")
    port = sim.start_gnuradio_server()
    assert port is not None
    try:
        srv = sim._gr_server
        assert set(srv._stream_targets) == {"rx", "tx"}   # sane start
        # Simulate the "server started before the design was routed" case: the
        # server holds an EMPTY map. Force the dirty check + rebuild.
        srv._stream_targets = {}
        srv._batch_reset_writes = []
        sim._hosted_design_version = -1               # force version mismatch
        chip, err = sim._rebuild_if_dirty_threadsafe()
        assert err is None and chip is not None
        # The rebuild MUST have re-resolved the per-stream targets + resets.
        assert set(srv._stream_targets) == {"rx", "tx"}, srv._stream_targets
        assert srv._stream_targets["rx"]["out_tag"] == 5
        assert srv._stream_targets["tx"]["out_tag"] == 10
        assert srv._stream_targets["rx"]["entry_addr"] == 25
        assert len(srv._batch_reset_writes) > 0
    finally:
        sim.stop_gnuradio_server()
