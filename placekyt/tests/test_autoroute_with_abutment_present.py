# SPDX-License-Identifier: GPL-3.0
"""Auto-routing UNROUTED nets while an ABUTMENT connection exists must not crash.

An abutment net stores the SENTINEL STRING ``ABUTMENT_ROUTE`` ("abutment") as its route,
yet ``conn.is_routed`` is True (the build synthesises the @1 handoff). The routers'
occupied-cell collectors iterated ``conn.route`` for every ``is_routed`` net to reserve
its corridor cells — but iterating the sentinel STRING yields its characters, so
``(p.x, p.y) for p in conn.route`` raised ``'str' object has no attribute 'x'``. This
surfaced as the "Auto-route failed: 'str' object has no attribute 'x'" dialog after a
user transformed a block (which abuts a neighbour) and hit Route All.

Fix: the occupied-cell collectors (AutoRouter / CP-SAT router / bus DRC) skip a route
that is not a waypoint LIST — an abutment occupies no corridor cells.

Run:
    QT_QPA_PLATFORM=offscreen placekyt/.venv/bin/python -m pytest \
        placekyt/tests/test_autoroute_with_abutment_present.py -q
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication  # noqa: E402

from engine.catalog import BlockCatalog  # noqa: E402
from engine.io.chip_type_io import load_chip_type  # noqa: E402
from engine.autoroute import AutoRouter  # noqa: E402
from ui.controller import AppController  # noqa: E402
from model.connection import (BlockEndpoint, ABUTMENT_ROUTE,  # noqa: E402
                              Connection)

_CHIP = str(Path(__file__).resolve().parents[1] / "resources" / "chips"
            / "kyttar_10x12.yaml")


@pytest.fixture(scope="module")
def qapp():
    return QApplication.instance() or QApplication([])


@pytest.mark.skipif(not os.path.exists(_CHIP), reason="chip yaml absent")
def test_autoroute_does_not_crash_on_abutment_sentinel(qapp):
    key = "kyttar_10x12"
    cat = BlockCatalog.from_gr_kyttar()
    ct = load_chip_type(_CHIP)
    ctrl = AppController(catalog=cat)
    ctrl.new_project("abut_route", key)

    a = ctrl.place_block("GainBlock", 0, 2, 2, library="lattrex.official",
                         params={"gain": 1.0})
    b = ctrl.place_block("GainBlock", 0, 3, 2, library="lattrex.official",
                         params={"gain": 1.0})
    c = ctrl.place_block("GainBlock", 0, 6, 6, library="lattrex.official",
                         params={"gain": 1.0})
    # a.out -> b.sample as an ABUTMENT (route is the sentinel STRING, is_routed True).
    ctrl.add_route(BlockEndpoint(block=a, port="out"),
                   BlockEndpoint(block=b, port="sample"), [])
    for cn in ctrl.project.connections:
        if getattr(cn.source, "block", "") == a:
            cn.route = ABUTMENT_ROUTE
    assert ctrl.project.connection(next(
        cn.name for cn in ctrl.project.connections
        if getattr(cn.source, "block", "") == a)).is_routed
    # An UNROUTED net so the router runs its occupied-cell collector.
    ctrl.project.connections.append(Connection(
        "unrouted_net", source=BlockEndpoint(block=b, port="out"),
        target=BlockEndpoint(block=c, port="sample"), route=None))

    def _pc(bt, lib, params=None):
        pm = cat.port_map(bt, params=params, library=lib)
        return {p.name: (p.cell_id, p.direction) for p in pm.ports}

    # The bare AutoRouter (the path the "str.x" crash came from).
    rep = AutoRouter(ctrl.project, {key: ct}, _pc).route_all()
    assert rep is not None  # did not raise

    # The controller entry the GUI "Route All" uses must also not raise.
    ctrl.auto_route_all({key: ct}, auto_orient=True, use_bus="always")
