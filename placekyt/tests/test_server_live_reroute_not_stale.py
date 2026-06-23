"""END-TO-END: a real-socket GRC batch through the SimController-hosted server
must run the CURRENT design after a reroute, even after the GUI's own post-edit
``cached_build()`` (which clears ``build_dirty``) has fired.

This is the live-path regression for the stale-run / phantom-cells bug the user
reported: with the GNURadio server running, rerouting a net in placeKYT and
re-running the GRC flowgraph produced garbage (triple the transitions) and
phantom cells, with NO ``[placeKYT server] rebuilt`` log — because the server's
pre-batch check keyed on ``build_dirty``, which the GUI's inspector/face refresh
(``_on_model_changed`` -> ``_sync_resolved_faces`` -> ``cached_build()``) had
already cleared. The fix keys the server on the monotonic ``design_version``.

Unlike ``test_coherent_rx_live_bridge`` (bare SimServer, no on_before_batch), this
hosts through ``SimController.start_gnuradio_server`` so the real ``on_before_batch``
hook runs — then drives a real socket via the same ``ChipProxy`` client the GRC
``placekyt_chip`` block uses. Both the pre-reroute and post-reroute batches must
recover BER 0.
"""

from __future__ import annotations

import importlib.util
import math
import os
import random
from pathlib import Path

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication  # noqa: E402

from engine.catalog import BlockCatalog  # noqa: E402
from engine.io.chip_type_io import load_chip_type  # noqa: E402
from model.connection import BlockEndpoint, ChipPortEndpoint  # noqa: E402
from ui.controller import AppController  # noqa: E402
from ui.sim_controller import SimController  # noqa: E402

from tests.conftest import CHIP_YAML as CT_PATH  # noqa: E402
from tests.conftest import GR_KYTTAR_PY  # noqa: E402
pytestmark = pytest.mark.skipif(not CT_PATH.exists(), reason="chip yaml absent")

_PSC = GR_KYTTAR_PY / "placekyt_sim_client.py"


def _chip_proxy_cls():
    spec = importlib.util.spec_from_file_location("psc_reroute", str(_PSC))
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m.ChipProxy


# --- self-contained BPSK RRC transmitter (mirrors the live-bridge test) --------

def _make_rrc(beta, sps, span):
    n = span * sps
    taps = []
    for i in range(n + 1):
        t = (i - n / 2) / sps
        if abs(t) < 1e-8:
            v = 1 - beta + 4 * beta / math.pi
        elif abs(abs(4 * beta * t) - 1.0) < 1e-8:
            v = (beta / math.sqrt(2)) * (
                (1 + 2 / math.pi) * math.sin(math.pi / (4 * beta))
                + (1 - 2 / math.pi) * math.cos(math.pi / (4 * beta)))
        else:
            num = (math.sin(math.pi * t * (1 - beta))
                   + 4 * beta * t * math.cos(math.pi * t * (1 + beta)))
            den = math.pi * t * (1 - (4 * beta * t) ** 2)
            v = num / den
        taps.append(v)
    e = math.sqrt(sum(v * v for v in taps))
    return [v / e for v in taps]


def _tx_signal(bits, sps=2, beta=0.35, span=6, timing_offset=0.0):
    syms = [1.0 if b == 0 else -1.0 for b in bits]
    taps = _make_rrc(beta, sps, span)
    up = []
    for s in syms:
        up.append(s)
        up.extend([0.0] * (sps - 1))
    shaped = []
    L = len(taps)
    for n in range(len(up)):
        acc = 0.0
        for k in range(L):
            if 0 <= n - k < len(up):
                acc += taps[k] * up[n - k]
        shaped.append(acc)
    out = []
    for n in range(len(shaped) - 1):
        i = n + int(math.floor(timing_offset))
        frac = timing_offset - math.floor(timing_offset)
        if 0 <= i < len(shaped) - 1:
            out.append(shaped[i] * (1 - frac) + shaped[i + 1] * frac)
        else:
            out.append(shaped[n])
    return out, syms


def _ber_with_lag(rx, tx, max_lag=20, min_overlap=40):
    best = (10 ** 9, 0)
    for lag in range(0, max_lag + 1):
        a, b = rx[lag:], tx[: len(rx) - lag]
        m = min(len(a), len(b))
        if m < min_overlap:
            continue
        e = sum(1 for i in range(m) if a[i] != b[i])
        e = min(e, m - e)
        if e < best[0]:
            best = (e, m)
    return best


@pytest.fixture(scope="module")
def qapp():
    return QApplication.instance() or QApplication([])


def _build_coherent_rx(catalog):
    """Single proven CoherentRXBlock with bit->x16_out (the live-bridge recipe)."""
    ctrl = AppController(catalog=catalog)
    ctrl.new_project("crx_reroute", "kyttar_10x12")
    nm = ctrl.place_block("CoherentRXBlock", 0, 0, 0, library="lattrex.official")
    bit_net = ctrl.add_route(BlockEndpoint(block=nm, port="bit"),
                             ChipPortEndpoint(chip=0, port="x16_out"), [])
    ctrl.auto_place(0)
    ct = load_chip_type(str(CT_PATH))
    rep = ctrl.auto_route_all({"kyttar_10x12": ct}, auto_orient=False,
                              use_bus="always")
    assert rep.ok, [(r.name, r.reason) for r in rep.failed]
    return ctrl, bit_net


def _run_batch(proxy):
    import numpy as np
    random.seed(5)
    bits = [random.randint(0, 1) for _ in range(120)]
    sig, syms = _tx_signal(bits, timing_offset=0.45)
    k = np.arange(len(sig))
    rot = np.exp(1j * 2 * np.pi * 0.008 * k)
    iq = (np.asarray(sig) * rot).astype(np.complex64)
    inter = np.empty(2 * len(sig), dtype=np.float32)
    inter[0::2] = iq.real
    inter[1::2] = iq.imag
    out = proxy.process_batch("x16_in", "x16_out", inter, raw=True)
    rx = [int(round(float(v))) & 1 for v in out]
    tx = [0 if s > 0 else 1 for s in syms]
    return _ber_with_lag(rx, tx)


def test_live_reroute_does_not_run_stale(qapp):
    """Host through SimController; batch (BER 0); reroute the bit->x16_out net +
    fire the GUI's cached_build() (clears build_dirty); batch AGAIN over a fresh
    socket — must STILL be BER 0 (the server rebuilt the current design)."""
    from commands import SetConnectionRouteCommand

    cat = BlockCatalog.from_gr_kyttar()
    ctrl, bit_net = _build_coherent_rx(cat)
    sim = SimController(ctrl)
    port = sim.start_gnuradio_server()
    assert port, "server failed to start"
    ChipProxy = _chip_proxy_cls()
    try:
        # First batch — the freshly hosted design.
        e1, m1 = _run_batch(ChipProxy("127.0.0.1", port, "x16_in", "x16_out"))
        assert m1 and e1 == 0, f"pre-reroute BER not 0: {e1}/{m1}"

        # Reroute the bit->x16_out net (delete + re-add same path), then fire the
        # GUI's own post-edit refresh which clears build_dirty.
        conn = ctrl.project.connection(bit_net)
        new_route = [(p.x, p.y) for p in (conn.route or [])]
        ctrl.commands.execute(SetConnectionRouteCommand(ctrl.project, bit_net, None))
        ctrl.commands.execute(
            SetConnectionRouteCommand(ctrl.project, bit_net, new_route))
        ctrl.cached_build()
        assert not ctrl.project.build_dirty, \
            "precondition: GUI refresh cleared build_dirty (what masked the bug)"

        # Second batch over a FRESH socket (GRC opens one per Run). The server must
        # have rebuilt from the current design — BER still 0, not stale garbage.
        e2, m2 = _run_batch(ChipProxy("127.0.0.1", port, "x16_in", "x16_out"))
        assert m2 and e2 == 0, \
            f"post-reroute BER not 0 (STALE run / phantom cells): {e2}/{m2}"
    finally:
        sim.stop_gnuradio_server()


if __name__ == "__main__":
    app = QApplication.instance() or QApplication([])
    test_live_reroute_does_not_run_stale(app)
    print("live reroute not stale (BER 0 both batches): PASS")
