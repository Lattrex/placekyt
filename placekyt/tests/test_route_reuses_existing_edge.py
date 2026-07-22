# SPDX-License-Identifier: GPL-3.0
"""Drawing a wire between two blocks that a logical net ALREADY links must REROUTE
that net — never spawn a second, reversed, phantom net.

The FSK4 modem imports ``net4 = rrcpulseshaper.out -> frequencymodulator.x`` (the TX
RRC feeds the FM). After moving blocks around, the user re-draws the RRC↔FM edge, but
the drag DIRECTION happened to run FM→RRC. ``_on_route_completed`` resolved the FM side
to its OUTPUT (``yi``) and the RRC side to its INPUT (``sample``) and, finding no exact
``fm.yi -> rrc.sample`` match, created a NEW connection
``frequencymodulator_to_rrcpulseshaper`` (fm.yi→rrc.sample) — a stray, unrouted fly line
that "appeared after moving blocks around" and made two routes leave the FM.

Fix: before resolving generic ports, if the two dragged handles are blocks an existing
net links in EITHER direction, reroute THAT net (its real endpoints/direction) instead
of adding a duplicate.

Run:
    QT_QPA_PLATFORM=offscreen placekyt/.venv/bin/python -m pytest \
        placekyt/tests/test_route_reuses_existing_edge.py -q
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
from ui.main_window import MainWindow  # noqa: E402
from model.connection import BlockEndpoint  # noqa: E402

_CHIP = str(Path(__file__).resolve().parents[1] / "resources" / "chips"
            / "kyttar_10x12.yaml")
_LIB = "lattrex.official"


@pytest.fixture(scope="module")
def qapp():
    return QApplication.instance() or QApplication([])


def _link(project, a, b):
    """An existing block↔block net between blocks a and b (either direction)."""
    for c in project.connections:
        if isinstance(c.source, BlockEndpoint) and isinstance(c.target, BlockEndpoint):
            if {c.source.block, c.target.block} == {a, b}:
                return c
    return None


@pytest.mark.skipif(not os.path.exists(_CHIP), reason="chip yaml absent")
def test_redraw_existing_edge_reroutes_not_duplicates(qapp):
    key = "kyttar_10x12"
    cat = BlockCatalog.from_gr_kyttar()
    ct = load_chip_type(_CHIP)  # noqa: F841 (kept for parity / chip presence)

    w = MainWindow()
    ctrl = AppController(catalog=cat)
    ctrl.new_project("redraw", key)
    w.controller = ctrl

    # A feeds B (A.out -> B.sample), like rrc.out -> fm.x. Start B as a fly line so a
    # "redraw" is the natural user action.
    a = ctrl.place_block("GainBlock", 0, 2, 2, library=_LIB, params={"gain": 1.0})
    b = ctrl.place_block("GainBlock", 0, 4, 2, library=_LIB, params={"gain": 1.0})
    ctrl.add_route(BlockEndpoint(block=a, port="out"),
                   BlockEndpoint(block=b, port="sample"),
                   [(3, 2), (3, 2)])  # placeholder route
    net = _link(ctrl.project, a, b)
    assert net is not None
    assert net.source.block == a and net.target.block == b

    before = {c.name for c in ctrl.project.connections}

    # Drag in the WRONG direction (B -> A): must NOT spawn a reversed phantom.
    w._on_route_completed(b, a, [(3, 2), (4, 2)])
    # Drag in the RIGHT direction (A -> B): must also reuse, not duplicate.
    w._on_route_completed(a, b, [(3, 2), (4, 2)])

    after = {c.name for c in ctrl.project.connections}
    new = after - before
    assert not new, f"redraw spawned phantom net(s): {new}"

    # Exactly one net still links the pair, still A -> B, and it is now routed.
    net2 = _link(ctrl.project, a, b)
    assert net2 is not None and net2.name == net.name
    assert net2.source.block == a and net2.target.block == b
    assert isinstance(net2.route, list) and net2.route, "existing net left unrouted"
