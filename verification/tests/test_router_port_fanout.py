# SPDX-License-Identifier: GPL-3.0-or-later
"""Regression: a chip INPUT-port fan-out to >=2 different blocks' input cells must
route as a COMMON BUS that FORKS at a shared cell BEYOND the port cell — the two
corridors must NOT diverge AT the port cell.

Root cause (user's words): a chip input PORT cell has ONE forward face. It cannot
steer two words in two different directions. If two port fan-out nets leave the port
cell in DIFFERENT directions (two private corridors), the port's single fwd_face can
only steer one stream; the other is LOST (in the QPSK full-duplex modem the RX chain
got data and the TX chain got NONE).

Ground truth (from the working QPSK full-duplex layouts): the WORKING topology
routes net6 (0,0)->(0,1)->(1,1) and net8 (0,0)->(0,1)->(0,2): both leave the port
cell (0,0) SOUTH (one shared direction), share the prefix (0,0)->(0,1), and FORK at
(0,1) — net6 brokers off there (EAST), net8 transits it onward (SOUTH). The BROKEN
topology leaves (0,0) in two directions (EAST vs SOUTH) and loses a stream.

This test builds that fan-out UNROUTED, auto-routes it over the bus, and asserts the
two port nets share a fork cell beyond the port cell (they share >=1 non-port cell AND
leave the port cell the SAME direction) — never diverge at the port.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

_PLACEKYT = Path(__file__).resolve().parents[2] / "placekyt"
for _p in (str(_PLACEKYT),):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from PySide6.QtWidgets import QApplication  # noqa: E402

from engine.catalog import BlockCatalog  # noqa: E402
from ui.controller import AppController  # noqa: E402

from tests.conftest import CHIP_YAML as CT_PATH  # noqa: E402

pytestmark = pytest.mark.skipif(not CT_PATH.exists(), reason="chip yaml absent")

PORT_CELL = (0, 0)  # x16_in on kyttar_10x12


@pytest.fixture(scope="module")
def qapp():
    return QApplication.instance() or QApplication([])


@pytest.fixture(scope="module")
def catalog(qapp):
    return BlockCatalog.from_gr_kyttar()


def _route_points(ctrl, name):
    conn = ctrl.project.connection(name)
    assert conn is not None, f"no connection {name}"
    return [(p.x, p.y) for p in (conn.route or [])]


def _first_hop_dir(route):
    """Direction (dx, dy) the route leaves the PORT cell (its first step)."""
    if len(route) < 2:
        return None
    (x0, y0), (x1, y1) = route[0], route[1]
    return (x1 - x0, y1 - y0)


def test_port_fanout_shares_fork_cell_not_diverge_at_port(qapp, catalog):
    """x16_in fans out to ComplexRRCMatchedFilter.xi AND PSKSymbolMapper.sample.
    Both port nets must leave the port cell the SAME direction and share a non-port
    fork cell (common bus prefix), never diverge at the port cell."""
    lib = "lattrex.official"
    ctrl = AppController(catalog=catalog)
    ctrl.new_project("fanout", "kyttar_10x12")

    # Place the matched filter and mapper hugging the port so the fan-out is forced
    # into the tight NW corner (the modem's exact geometry).
    ctrl.place_block("ComplexRRCMatchedFilterBlock", 0, 1, 1,
                     params={"beta": 0.35, "sps": 2, "span": 8,
                             "headroom_shift": 1, "decimation": 1}, library=lib)
    ctrl.place_block("PSKSymbolMapperBlock", 0, 0, 2,
                     params={"modulation": "qpsk", "dimension": 1}, library=lib)

    # x16_in -> matched filter (xi, iq sibling xq synthesised is a BLOCK net; here the
    # PORT feeds a complex block input, one logical net per rail is fine). Wire the two
    # DISTINCT sinks off the ONE input port.
    net_mf = ctrl.add_logical_connection(
        {"chip": 0, "port": "x16_in"},
        {"block": "complexrrcmatchedfilter", "port": "xi"})
    net_map = ctrl.add_logical_connection(
        {"chip": 0, "port": "x16_in"},
        {"block": "psksymbolmapper", "port": "sample"})

    ctrl.auto_route_all(auto_orient=False, use_bus="always")

    r_mf = _route_points(ctrl, net_mf)
    r_map = _route_points(ctrl, net_map)

    # Both nets must actually route past the port cell.
    assert len(r_mf) >= 2 and len(r_map) >= 2, (
        f"a port fan-out net did not route past the port cell: "
        f"mf={r_mf} map={r_map}")

    assert r_mf[0] == PORT_CELL and r_map[0] == PORT_CELL, (
        f"port nets must originate at the port cell {PORT_CELL}: "
        f"mf={r_mf} map={r_map}")

    d_mf = _first_hop_dir(r_mf)
    d_map = _first_hop_dir(r_map)
    assert d_mf == d_map and d_mf is not None, (
        "DIVERGE-AT-PORT bug: the two port fan-out nets leave the port cell "
        f"in DIFFERENT directions ({d_mf} vs {d_map}); the port's single fwd_face "
        f"can steer only ONE stream, the other is LOST. mf={r_mf} map={r_map}")

    # They must also SHARE a fork cell BEYOND the port cell (a common bus prefix, not
    # merely a common first direction that immediately splits).
    shared_beyond_port = (set(r_mf) & set(r_map)) - {PORT_CELL}
    assert shared_beyond_port, (
        "port fan-out nets share ONLY the port cell — no common fork cell beyond it. "
        f"mf={r_mf} map={r_map}")
