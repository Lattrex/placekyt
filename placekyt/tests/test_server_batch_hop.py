# SPDX-License-Identifier: GPL-3.0-or-later
"""process_batch injects at the PLACEMENT-DEPENDENT hop, not a hardcoded 30.

INV-1: the WRITE/JUMP injection hop is 31 - distance from the input port to the
block's landing cell. process_batch hardcoded target_hop_cnt=30 (the 1-hop case),
so a block placed anywhere but 1 hop from the port silently produced NO output —
the JUMP landed at the wrong cell and the block never executed. The GUI then
showed the un-filtered passthrough (a clean sinusoid where a saturating FIR was
expected). This drives the REAL server-batch path the GUI uses and asserts a FIR
placed at a non-edge cell actually runs and saturates.
"""
import os
import socket

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import numpy as np
import pytest

# gr_kyttar resolves via the editable install (`pip install -e runtime/python`),
# the same way the GUI imports it — no sys.path hacks. This test exercises the
# REAL server-batch path the GUI uses.

from engine.io.chip_type_io import load_chip_type
from engine.build import BuildEngine
from engine.simulator import SimulationEngine
from engine.sim_bridge import SimServer, send_message, recv_message
from engine.port_config import input_port_config
from ui.controller import AppController
from model.connection import ChipPortEndpoint, BlockEndpoint

from tests.conftest import CHIP_YAML as CT_PATH

pytestmark = pytest.mark.skipif(not CT_PATH.exists(), reason="chip yaml absent")


def _host_fir(place_xy, taps=(0.9, 0.9)):
    """Build gain-free FIR@place_xy → x16_out, host on a SimServer, return
    (server, port, default_hops) — the exact path the GUI server-batch uses."""
    ct = load_chip_type(str(CT_PATH))
    ctrl = AppController()
    cat = ctrl.catalog
    ctrl.new_project("dut", "kyttar_10x12")
    px, py = place_xy
    bn = ctrl.place_block("FIRFilterBlock", 0, px, py,
                          library="lattrex.official",
                          params={"coefficients": list(taps)})
    bn = bn if isinstance(bn, str) else bn.name
    ctrl.add_logical_connection(ChipPortEndpoint(0, "x16_in"),
                                BlockEndpoint(bn, "sample"))
    ctrl.add_logical_connection(BlockEndpoint(bn, "out"),
                                ChipPortEndpoint(0, "x16_out"))
    ctrl.auto_route_all({"kyttar_10x12": ct})
    bres = BuildEngine(cat, ctrl.registry.paths()).build(
        ctrl.project, {"kyttar_10x12": ct})
    assert bres.ok, "FIR build failed"
    eng = SimulationEngine(str(CT_PATH))
    eng.load(bres.words(0), trace=False)
    cfg = input_port_config(ctrl.project, ctrl.registry, cat, chip_id=0)
    pn, kw = cfg
    eng.configure_input_port(pn, **kw)
    entries = {pn: int(kw["entry_addr"])}
    hops = {pn: int(kw["hop_count"])}
    srv = SimServer(eng.chip, host="127.0.0.1", port=0,
                    default_entries=entries, default_hops=hops)
    return srv, srv.start(), hops


def _run_overload(port, n=20, level=0.95):
    x = np.full(n, level, dtype=np.float32)
    c = socket.create_connection(("127.0.0.1", port))
    send_message(c, {"op": "process_batch", "port": "x16_out",
                     "in_port": "x16_in", "complex": False, "raw": False}, x)
    _, p = recv_message(c)
    c.close()
    return [] if p is None else [float(v) for v in p]


@pytest.mark.parametrize("place_xy", [(0, 0), (1, 1), (2, 2)])
def test_fir_runs_and_saturates_at_any_placement(place_xy):
    """A FIR placed at the port edge OR deeper must execute via the server path
    and SATURATE on overload — proving process_batch uses the right hop."""
    srv, port, hops = _host_fir(place_xy)
    try:
        out = _run_overload(port)
        assert out, f"FIR@{place_xy} produced NO output (wrong injection hop)"
        steady = out[4:]
        mx = max(abs(v) for v in steady)
        assert mx > 0.98, (
            f"FIR@{place_xy} did not saturate (max|out|={mx:.3f}); "
            f"hop={hops}")
    finally:
        srv.stop()


def test_deep_placement_hop_is_not_30():
    """Sanity: a non-edge placement resolves a hop OTHER than 30, so this test
    actually exercises the bug (a hardcoded-30 bridge fails the case above)."""
    srv, port, hops = _host_fir((2, 2))
    try:
        assert hops["x16_in"] != 30, "expected a non-30 hop for a deep placement"
    finally:
        srv.stop()
