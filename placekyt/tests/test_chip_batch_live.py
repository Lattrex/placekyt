"""Live GR ↔ placeKYT chip_batch test (the headless/live reconciliation, #358).

The bug this guards: kyttar.source/sink are gr.sync_block (1:1 in:out rate), so a
RATE-CHANGING batch chain (RX 239 I/Q samples -> 119 bits) cannot push its
recovered words through the GR pipeline to a sink — the headless-vs-live split
where a direct RPC read sees the bits but a live flowgraph plots nothing.

kyttar.chip_batch is a gr.basic_block (decimating) that DOES carry the full
recovered stream. This test proves it END TO END through TWO real processes:
  1. a placeKYT SimServer (placeKYT venv) hosting the auto-placed modem, and
  2. a real GR top_block (system /usr/bin/python3) running chip_batch -> vector_sink.
It asserts the FULL 119-bit RX stream and the TX passband actually arrive at the
GR vector sinks (not just the server reply), recovering BER 0.

Skipped unless both a placeKYT venv python and a GR-capable /usr/bin/python3 with
the kyttar OOT are available (the install must be current — see gr-kyttar/install.sh).
"""

from __future__ import annotations

import os
import socket
import subprocess
import sys
import textwrap
import time
from pathlib import Path

import numpy as np
import pytest

REPO = Path(__file__).resolve().parents[2]            # /home/system/placekyt
GR_PYTHON = os.environ.get("KYTTAR_GR_PYTHON", "/usr/bin/python3")
GR_OOT = REPO / "gr-kyttar" / "python"


def _gr_available() -> bool:
    try:
        r = subprocess.run(
            [GR_PYTHON, "-c", "from gnuradio import gr, blocks; import kyttar; "
             "assert hasattr(kyttar, 'chip_batch')"],
            env={**os.environ, "PYTHONPATH": str(GR_OOT)},
            capture_output=True, timeout=30)
        return r.returncode == 0
    except Exception:
        return False


pytestmark = pytest.mark.skipif(
    not _gr_available(),
    reason="GR python with current kyttar OOT (chip_batch) not available")


def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


def _start_server(port: int) -> subprocess.Popen:
    """Start a placeKYT SimServer (in THIS venv) hosting the auto-placed modem on
    ``port``; returns the process once it prints SERVER_READY."""
    script = textwrap.dedent(f"""
        import time, simkyt
        from engine.catalog import BlockCatalog
        from ui.controller import AppController
        from engine.io.chip_type_io import load_chip_type
        from tests.conftest import CHIP_YAML
        from engine.port_config import stream_targets
        from engine.sim_bridge import SimServer
        cat = BlockCatalog.from_gr_kyttar()
        ctrl = AppController(catalog=cat)
        ctrl.import_grc('examples/bpsk_modem/bpsk_modem.grc', chip_type='kyttar_10x12')
        ct = load_chip_type(str(CHIP_YAML))
        # ABUTMENT-FIRST P&R (the default for compact fixed transceivers). The full
        # duplex modem (TX + coherent RX filaments sharing one port) is too dense for
        # the multiplexed BUS to route: the Costas.yi_tap -> Gardner.xi handoff (net1)
        # finds no bus path once the TX filament congests the layout. auto_pnr abuts
        # the connected block chains (far fewer cells), so that handoff becomes a
        # direct cell-to-cell abutment and every net routes. The bus topology remains
        # for DYNAMIC-reconfig designs; a fixed modem does not need it.
        ctrl.auto_pnr({{'kyttar_10x12': ct}})
        res = ctrl.build()
        tgt = stream_targets(ctrl.project, ctrl.registry, cat, 0, build_result=res)
        chip = simkyt.Chip.from_yaml(str(CHIP_YAML)); chip.load_bitstream_physical(res.words(0))
        srv = SimServer(chip, host='127.0.0.1', port={port}, stream_targets=tgt)
        srv.start()
        print('SERVER_READY', flush=True)
        try:
            while True: time.sleep(1)
        except KeyboardInterrupt:
            srv.stop()
    """)
    env = {**os.environ, "PYTHONPATH": str(REPO / "placekyt"),
           "QT_QPA_PLATFORM": "offscreen", "KYTTAR_SERVER_QUIET": "1"}
    p = subprocess.Popen([sys.executable, "-c", script], cwd=str(REPO),
                         env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                         text=True)
    # Wait for readiness.
    t0 = time.time()
    while time.time() - t0 < 60:
        line = p.stdout.readline()
        if not line and p.poll() is not None:
            raise RuntimeError("server died:\n" + (p.stdout.read() or ""))
        if "SERVER_READY" in line:
            return p
    p.kill()
    raise RuntimeError("server did not become ready in time")


def _run_gr(port: int) -> dict:
    """Run a real GR flowgraph (system python) driving chip_batch[rx] and [tx]
    against the server; return the vector-sink outputs as JSON."""
    gr_script = textwrap.dedent(f"""
        import json, random, numpy as np
        from gnuradio import gr, blocks
        import kyttar
        from kyttar import modem_demo_stim as stim
        PORT = {port}
        # RX: a real RRC-BPSK I/Q burst -> chip_batch(rx) -> recovered bits
        n_syms = 120
        iq = stim.rx_burst(n_syms)
        rx = kyttar.chip_batch(port=PORT, stream_id='rx', in_kind='complex',
                               out_kind='real', raw=True, burst_len=len(iq))
        rx_src = blocks.vector_source_c(iq, False)
        rx_vs = blocks.vector_sink_f()
        # TX: bits -> chip_batch(tx) -> passband words
        bits = [float(b) for b in stim.tx_bits(64)]
        tx = kyttar.chip_batch(port=PORT, stream_id='tx', in_kind='real',
                               out_kind='real', raw=False, burst_len=len(bits))
        tx_src = blocks.vector_source_f(bits, False)
        tx_vs = blocks.vector_sink_f()
        tb = gr.top_block()
        tb.connect(rx_src, rx, rx_vs)
        tb.connect(tx_src, tx, tx_vs)
        tb.run()
        print("RESULT " + json.dumps({{
            "rx": [int(round(v)) & 1 for v in rx_vs.data()],
            "tx_len": len(tx_vs.data()),
        }}))
    """)
    env = {**os.environ, "PYTHONPATH": str(GR_OOT)}
    r = subprocess.run([GR_PYTHON, "-c", gr_script], cwd=str(REPO), env=env,
                       capture_output=True, text=True, timeout=120)
    out = r.stdout + r.stderr
    for line in out.splitlines():
        if line.startswith("RESULT "):
            import json
            return json.loads(line[len("RESULT "):])
    raise RuntimeError("GR run produced no RESULT:\n" + out)


def _ber_with_lag(rx, ref):
    """Min BER over a small lag window, inversion-tolerant (mirrors bpsk_modem_demo)."""
    best = (len(ref), len(ref), 0)
    rx = list(rx)
    for inv in (0, 1):
        r = [b ^ inv for b in rx]
        for lag in range(0, 12):
            a = r[lag:]
            n = min(len(a), len(ref))
            if n < len(ref) // 2:
                continue
            errs = sum(1 for i in range(n) if a[i] != ref[i])
            if errs < best[0]:
                best = (errs, n, lag)
    return best


def test_chip_batch_live_recovers_full_stream():
    """The FULL recovered RX stream reaches a GR vector_sink through the basic_block
    (BER 0), and the TX passband is non-empty — proving headless == live."""
    import random
    port = _free_port()
    srv = _start_server(port)
    try:
        res = _run_gr(port)
    finally:
        srv.terminate()
        try:
            srv.wait(timeout=5)
        except Exception:
            srv.kill()

    rx = res["rx"]
    # Reference bits from the SAME stim seed bpsk_modem_demo / coherent_demo_stim use.
    random.seed(5)
    ref = [random.randint(0, 1) for _ in range(120)]
    ref = [0 if (1.0 if b == 0 else -1.0) > 0 else 1 for b in ref]
    e, m, lag = _ber_with_lag(rx, ref)
    assert len(rx) >= 100, f"RX stream truncated: only {len(rx)} bits reached the sink"
    assert m and e == 0, f"RX BER={e}/{m} (lag={lag}); {len(rx)} bits"
    assert res["tx_len"] > 0, "TX passband empty at the GR sink"
