# SPDX-License-Identifier: GPL-3.0-or-later
"""Live GR ↔ placeKYT verification of the GRC-settable duplex SCHEDULE switch.

The knob under test: ``kyttar.source``'s **Duplex schedule** dropdown
(interleaved / sequential). It is a GRC VARIABLE the user flips before Run — NOT
an env var — that selects how the two duplex streams (TX + RX sharing one x16_in
port) are driven on the chip:

  * interleaved (full-duplex): TX + RX round-robin sample-by-sample; the chains
    contend for the shared input port and throttle each other — the honest
    steady-state full-duplex rate.
  * sequential (simplex): each direction's WHOLE burst runs alone; each chain is
    measured at its own compute-bound ceiling.

This guards the exact end-to-end carrier the GUI uses:

    .grc dropdown  ->  kyttar.source(schedule=...)  ->  DuplexRendezvous.submit
                   ->  _dispatch_all header "schedule"  ->  SimServer
                       _process_batch_duplex sequential/interleaved branch

driven through TWO real processes:
  1. a placeKYT SimServer (placeKYT venv) hosting the SHIPPED full-duplex
     qam16_modem.kyt (opened read-only, exactly as a user opens it), and
  2. a real GR top_block (system /usr/bin/python3) with two kyttar.source/sink
     pairs (rx + tx), the RX source carrying schedule=<mode>.

It asserts, for BOTH modes, that (a) the RX stream recovers BER 0 (correctness is
schedule-independent) and (b) the SERVER actually ran the requested schedule — the
server prints ``[placeKYT duplex] SEQUENTIAL|INTERLEAVED`` only when the dropdown
value reached it through the rendezvous. A prior env-var attempt silently no-op'd
because the running server never saw a var set in another shell; this proves the
GRC value arrives.

Skipped unless a GR-capable /usr/bin/python3 with the CURRENT kyttar OOT is
available (schedule= reaches the installed source only after gr-kyttar/install.sh,
OR via PYTHONPATH to the repo OOT, which this test sets).
"""

from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import textwrap
import time
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]            # /home/system/placekyt
GR_PYTHON = os.environ.get("KYTTAR_GR_PYTHON", "/usr/bin/python3")
GR_OOT = REPO / "gr-kyttar" / "python"
_KYT = REPO / "examples" / "qam16_modem" / "qam16_modem.kyt"


def _gr_has_schedule() -> bool:
    """GR python importable AND the source carries the schedule= param (repo OOT)."""
    try:
        r = subprocess.run(
            [GR_PYTHON, "-c",
             "import inspect; from gnuradio import gr, blocks; import kyttar; "
             "assert 'schedule' in inspect.signature(kyttar.source.__init__).parameters"],
            env={**os.environ, "PYTHONPATH": str(GR_OOT)},
            capture_output=True, timeout=30)
        return r.returncode == 0
    except Exception:
        return False


pytestmark = pytest.mark.skipif(
    not (_KYT.exists() and _gr_has_schedule()),
    reason="shipped .kyt or GR python with schedule-aware kyttar OOT unavailable")


def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


def _start_server(port: int) -> subprocess.Popen:
    """placeKYT SimServer hosting the SHIPPED full-duplex qam16 .kyt (opened
    read-only). NOT quiet — we read its ``[placeKYT duplex] <SCHEDULE>`` line as
    proof the dropdown value reached the server. Returns once it prints
    SERVER_READY."""
    script = textwrap.dedent(f"""
        import time, simkyt
        from engine.catalog import BlockCatalog
        from ui.controller import AppController
        from engine.io.chip_type_io import load_chip_type
        from engine.build import BuildEngine
        from tests.conftest import CHIP_YAML
        from engine.port_config import stream_targets
        from engine.sim_bridge import SimServer
        cat = BlockCatalog.from_gr_kyttar()
        ct = load_chip_type(str(CHIP_YAML))
        ctrl = AppController(catalog=cat)
        ctrl.open_project({str(_KYT)!r})          # SHIPPED .kyt, read-only
        bres = BuildEngine(cat, str(CHIP_YAML)).build(
            ctrl.project, {{'kyttar_10x12': ct}})
        assert bres.ok, [str(e) for e in bres.errors]
        tgt = stream_targets(ctrl.project, ctrl.registry, cat, 0, build_result=bres)
        assert 'rx' in tgt and 'tx' in tgt, sorted(tgt)
        chip = simkyt.Chip.from_yaml(str(CHIP_YAML))
        chip.load_bitstream_physical(bres.words(0))
        srv = SimServer(chip, host='127.0.0.1', port={port}, stream_targets=tgt)
        srv.start()
        print('SERVER_READY', flush=True)
        try:
            while True: time.sleep(1)
        except KeyboardInterrupt:
            srv.stop()
    """)
    env = {**os.environ, "PYTHONPATH": str(REPO / "placekyt"),
           "QT_QPA_PLATFORM": "offscreen"}
    env.pop("KYTTAR_SERVER_QUIET", None)   # we WANT the duplex schedule line
    p = subprocess.Popen([sys.executable, "-c", script], cwd=str(REPO),
                         env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                         text=True)
    t0 = time.time()
    while time.time() - t0 < 90:
        line = p.stdout.readline()
        if not line and p.poll() is not None:
            raise RuntimeError("server died:\n" + (p.stdout.read() or ""))
        if "SERVER_READY" in line:
            return p
    p.kill()
    raise RuntimeError("server did not become ready in time")


def _run_gr(port: int, schedule: str) -> dict:
    """Two kyttar.source/sink pairs (rx + tx) against the server, the RX source
    carrying schedule=<mode>. Returns the recovered RX symbols + TX word count."""
    gr_script = textwrap.dedent(f"""
        import json, numpy as np, math
        from gnuradio import gr, blocks
        import kyttar

        # --- SAME 16-QAM stimulus as test_qam16_modem_ber (seed 5) ---------------
        _NORM = 1.0/math.sqrt(10.0)
        _LEVELS = [(+1,-1),(-1,-1),(+3,-3),(-3,-3),(-3,-1),(+3,-1),(-1,-3),(+1,-3),
                   (-3,+3),(+3,+3),(-1,+1),(+1,+1),(+1,+3),(-1,+3),(+3,+1),(-3,+1)]
        _POINTS = [(i*_NORM, q*_NORM) for (i,q) in _LEVELS]
        def _rrc(beta, sps, span):
            n = span*sps; taps=[]
            for i in range(n+1):
                t=(i-n/2)/sps
                if abs(t)<1e-8: v=1-beta+4*beta/math.pi
                elif abs(abs(4*beta*t)-1.0)<1e-8:
                    v=(beta/math.sqrt(2))*((1+2/math.pi)*math.sin(math.pi/(4*beta))
                       +(1-2/math.pi)*math.cos(math.pi/(4*beta)))
                else:
                    v=(math.sin(math.pi*t*(1-beta))
                       +4*beta*t*math.cos(math.pi*t*(1+beta)))/(
                        math.pi*t*(1-(4*beta*t)**2))
                taps.append(v)
            e=math.sqrt(sum(x*x for x in taps)); return np.array([x/e for x in taps])
        n_syms = 400
        rng = np.random.RandomState(5)
        tx_syms = rng.randint(0,16,n_syms)
        base = np.array([complex(*_POINTS[s]) for s in tx_syms], dtype=np.complex128)
        up = np.zeros(n_syms*2, dtype=np.complex128); up[::2] = base
        shaped = np.convolve(up, _rrc(0.35,2,8))
        iq = (shaped/(np.max(np.abs(shaped))+1e-12)*0.9).astype(np.complex64)

        PORT = {port}
        SCHED = {schedule!r}
        # RX: complex I/Q burst -> chip (stream 'rx') -> recovered 4-bit symbols.
        rx_src = kyttar.source(device_id='kyttar_0', port_name='x16_in',
                               server_host='127.0.0.1', server_port=PORT,
                               complex_in=True, burst_len=len(iq),
                               stream_id='rx', schedule=SCHED, pipelined=True)
        rx_snk = kyttar.sink(device_id='kyttar_0', server_port=PORT,
                             stream_id='rx', in_type=True)   # complex marker chain
        rx_vsrc = blocks.vector_source_c(iq.tolist(), False)
        rx_vsnk = blocks.vector_sink_f()
        # TX: bits -> chip (stream 'tx') -> passband words.
        tx_bits = [float(b) for b in rng.randint(0,2,800)]
        tx_src = kyttar.source(device_id='kyttar_0', port_name='x16_in',
                               server_host='127.0.0.1', server_port=PORT,
                               complex_in=False, burst_len=len(tx_bits),
                               stream_id='tx', schedule=SCHED, pipelined=True)
        tx_snk = kyttar.sink(device_id='kyttar_0', server_port=PORT,
                             stream_id='tx')
        tx_vsrc = blocks.vector_source_f(tx_bits, False)
        tx_vsnk = blocks.vector_sink_f()

        tb = gr.top_block()
        tb.connect(rx_vsrc, rx_src, rx_snk, rx_vsnk)
        tb.connect(tx_vsrc, tx_src, tx_snk, tx_vsnk)
        tb.run()
        print("RESULT " + json.dumps({{
            "rx": [int(round(v)) & 0xF for v in rx_vsnk.data()],
            "tx_syms": [int(s) for s in tx_syms],
            "tx_len": len(tx_vsnk.data()),
        }}))
    """)
    env = {**os.environ, "PYTHONPATH": str(GR_OOT)}
    r = subprocess.run([GR_PYTHON, "-c", gr_script], cwd=str(REPO), env=env,
                       capture_output=True, text=True, timeout=180)
    out = r.stdout + r.stderr
    for line in out.splitlines():
        if line.startswith("RESULT "):
            return json.loads(line[len("RESULT "):])
    raise RuntimeError("GR run produced no RESULT:\n" + out)


# --- 16-QAM BER (rotation + lag tolerant), mirrors test_qam16_modem_ber ---------
import math  # noqa: E402


def _rot_sym(sym, r):
    _NORM = 1.0 / math.sqrt(10.0)
    _LEVELS = [(+1, -1), (-1, -1), (+3, -3), (-3, -3), (-3, -1), (+3, -1),
               (-1, -3), (+1, -3), (-3, +3), (+3, +3), (-1, +1), (+1, +1),
               (+1, +3), (-1, +3), (+3, +1), (-3, +1)]
    pts = [(i * _NORM, q * _NORM) for (i, q) in _LEVELS]
    i, q = pts[sym]
    for _ in range(r):
        i, q = -q, i
    return min(range(16), key=lambda j: (i - pts[j][0]) ** 2 + (q - pts[j][1]) ** 2)


def _qam16_ber(rx, tx, max_lag=25, guard=60):
    best = (1.0, 0, 0)
    for r in range(4):
        for lag in range(0, max_lag + 1):
            a = [_rot_sym(x, r) for x in rx[lag:]]
            m = min(len(a), len(tx))
            if m - guard < 80:
                continue
            err = sum(1 for k in range(guard, m) if a[k] != tx[k])
            if (err / (m - guard)) < best[0]:
                best = (err / (m - guard), r, lag)
    return best


def _drain_server(p: subprocess.Popen, want: str, timeout=8.0) -> str:
    """Read the server subprocess stdout until a ``[placeKYT duplex] <SCHED>`` line
    appears; return that line. Raises if none within ``timeout``."""
    import select
    t0 = time.time()
    while time.time() - t0 < timeout:
        rlist, _, _ = select.select([p.stdout], [], [], 0.5)
        if not rlist:
            continue
        line = p.stdout.readline()
        if not line:
            break
        if "[placeKYT duplex]" in line:
            return line.strip()
    raise RuntimeError(f"no '[placeKYT duplex] {want}' line from server")


@pytest.mark.parametrize("schedule,keyword",
                         [("interleaved", "INTERLEAVED"),
                          ("sequential", "SEQUENTIAL")])
def test_grc_schedule_reaches_server_and_recovers(schedule, keyword):
    """The GRC ``schedule`` dropdown value travels source -> rendezvous -> server:
    the server runs the requested schedule (its ``[placeKYT duplex] <SCHED>`` line)
    AND the RX stream still recovers BER 0 (correctness is schedule-independent)."""
    port = _free_port()
    srv = _start_server(port)
    try:
        res = _run_gr(port, schedule)
        srv_line = _drain_server(srv, keyword)
    finally:
        srv.terminate()
        try:
            srv.wait(timeout=5)
        except Exception:
            srv.kill()

    # 1) The dropdown value reached the server (this is the whole point — a value
    #    set in the GR flowgraph selected the server-side schedule branch).
    assert keyword in srv_line, (
        f"server ran wrong schedule for dropdown={schedule!r}: {srv_line!r}")

    # 2) BER 0 regardless of schedule (same design, only stimulus ORDER differs).
    rx, tx = res["rx"], res["tx_syms"]
    assert len(rx) >= len(tx) - 20, f"RX truncated: {len(rx)} of {len(tx)}"
    (ber, rot, lag) = _qam16_ber(rx, tx)
    assert ber == 0.0, f"schedule={schedule}: BER {ber:.4f} (rot={rot}, lag={lag})"
    assert res["tx_len"] > 0, "TX passband empty at the GR sink"
