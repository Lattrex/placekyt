"""Live GNURadio↔placeKYT bridge for the full coherent BPSK receiver (#227).

Drives the proven CoherentRXBlock — hosted on a placeKYT ``SimServer`` — through
the REAL GNURadio-side client (``ChipProxy``, the pure-socket client the
``placekyt_chip`` GR block uses), streaming a complex BPSK burst with carrier +
timing offset and reading back the recovered BITS. This is the "live coherent BPSK
RX (GNURadio → placeKYT)" path end to end (the runnable GRC companion is
the live-bridge example).

Uses ``process_batch`` (the proven model for a multi-cell DUT — per-sample live
streaming crawls; one RPC per burst is ~30× faster and gives the same result).
``raw=True`` returns the slicer's packed-bit words (Q15 scaling would crush the
LSB). The receiver recovers BER 0 over the offset.
"""

from __future__ import annotations

import math
import os
import random
from pathlib import Path

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication  # noqa: E402

from engine.build import BuildEngine  # noqa: E402
from engine.catalog import BlockCatalog  # noqa: E402
from engine.io.chip_type_io import load_chip_type  # noqa: E402
from model.connection import BlockEndpoint, ChipPortEndpoint  # noqa: E402
from ui.controller import AppController  # noqa: E402

from tests.conftest import CHIP_YAML as CT_PATH  # noqa: E402
from tests.conftest import GR_KYTTAR_PY  # noqa: E402
pytestmark = pytest.mark.skipif(not CT_PATH.exists(), reason="chip yaml absent")

# ChipProxy lives in the gr-kyttar tree; import it directly (it is a pure-socket
# client — no GNURadio needed since the lazy-gr decoupling).
_PSC = GR_KYTTAR_PY / "placekyt_sim_client.py"


def _chip_proxy_cls():
    import importlib.util
    spec = importlib.util.spec_from_file_location("psc_live", str(_PSC))
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m.ChipProxy


# --- self-contained BPSK RRC transmitter (mirrors the build test) -------------

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


@pytest.fixture(scope="module")
def catalog(qapp):
    return BlockCatalog.from_gr_kyttar()


@pytest.fixture(scope="module")
def chip_type():
    return load_chip_type(str(CT_PATH))


def test_chip_proxy_imports_without_gnuradio():
    """ROBUSTNESS: the pure-socket ChipProxy client imports with NO GNURadio
    (the lazy-gr decoupling) — so the live bridge + tests run headless."""
    assert _chip_proxy_cls() is not None


def test_live_bridge_coherent_rx_recovers_bits(qapp, catalog, chip_type):
    import socket  # noqa: F401  (ChipProxy opens its own connection)
    import time

    import simkyt
    import numpy as np
    from engine.sim_bridge import SimServer

    ChipProxy = _chip_proxy_cls()

    # Build the coherent receiver, recovered bits → x16_out.
    ctrl = AppController(catalog=catalog)
    ctrl.new_project("crx_live", "kyttar_10x12")
    nm = ctrl.place_block("CoherentRXBlock", 0, 0, 0, library="lattrex.official")
    ctrl.add_route(BlockEndpoint(block=nm, port="bit"),
                   ChipPortEndpoint(chip=0, port="x16_out"), [])
    res = BuildEngine(catalog, str(CT_PATH)).build(
        ctrl.project, {"kyttar_10x12": chip_type})
    assert res.ok, [str(e) for e in res.errors]
    entry, _in = catalog.resolved_io("CoherentRXBlock")

    chip = simkyt.Chip.from_yaml(str(CT_PATH))
    chip.load_bitstream_physical(res.words(0))
    chip.set_port_entry_address("x16_in", entry)
    srv = SimServer(chip, host="127.0.0.1", port=0,
                    default_entries={"x16_in": entry})
    p = srv.start()
    try:
        proxy = ChipProxy("127.0.0.1", p, "x16_in", "x16_out")
        random.seed(5)
        bits = [random.randint(0, 1) for _ in range(120)]
        sig, syms = _tx_signal(bits, timing_offset=0.45)
        k = np.arange(len(sig))
        rot = np.exp(1j * 2 * np.pi * 0.008 * k)            # carrier offset
        iq = (np.asarray(sig) * rot).astype(np.complex64)
        inter = np.empty(2 * len(sig), dtype=np.float32)
        inter[0::2] = iq.real
        inter[1::2] = iq.imag

        t0 = time.time()
        out = proxy.process_batch("x16_in", "x16_out", inter, raw=True)
        dt = time.time() - t0

        rx = [int(round(float(v))) & 1 for v in out]
        tx = [0 if s > 0 else 1 for s in syms]
        e, m = _ber_with_lag(rx, tx)
        assert m and e == 0, f"live-bridge CoherentRX BER not 0: {e}/{m}"
        assert dt < 5.0, f"bridge too slow: {dt:.2f}s"
    finally:
        srv.stop()
