"""BPSK transceiver PASSBAND LOOPBACK end-to-end, LIVE through real GR blocks.

This is the gate that proves the bpsk_loopback.grc recipe works LIVE end to end:
ONE placeKYT-hosted chip carries BOTH a TX chain and an RX chain (the
engine.bpsk_modem_demo duplex — the same blocks as examples/bpsk_modem.grc).
GRC closes the loop with STOCK blocks:

    bits -> chip_batch[tx] -> REAL passband (f = fs/8 = 0.125 cyc/sample)
         -> float_to_complex -> * sig_source(freq=-CARRIER, amp=2.0, phase=0)
         -> skiphead(1) -> keep_one_in_n(2)            (== bb[1:][::2])
         -> chip_batch[rx] -> recovered bits

and the recovered bits == the TX bits at BER 0 (lag-aligned, inversion-tolerant).

THE PROVEN DOWNCONVERT (verified BER 0 on the real chip, reproduced here):
    bb[n] = 2.0 * pb[n] * exp(-j*2*pi*0.125*n)   # image-reject gain 2.0
    bb = bb[1:]      # drop 1 (delay-1; the upconvert NCO increments before emit)
    bb = bb[::2]     # decimate sps 4 -> RX sps 2
GR realization (EXACT, deterministic — see proto_gr_loopback.py):
    sig_source_c(fs, GR_COS_WAVE, freq=-CARRIER, amplitude=2.0, phase=0.0)
      => 2*exp(-j*2*pi*0.125*n);   skiphead(1) == bb[1:];  keep_one_in_n(2) == [::2]
    (use SKIPHEAD, not blocks.delay: delay injects a leading zero that perturbs the
     Costas lock transient and makes BER nondeterministic — skiphead is exact.)

TWO-PROCESS harness (mirrors test_chip_batch_live / test_live_duplex_stream_id):
  1. a placeKYT SimServer (THIS venv) hosting the EXPLICIT-placement duplex
     (engine.bpsk_modem_demo.build_modem — the value-exact TX placement; see the
     module NOTE below on why NOT the GRC auto-import), with stream_targets; and
  2. a real GR top_block (system /usr/bin/python3) driving the loopback above.

NOTE — host placement: the host is engine.bpsk_modem_demo.build_modem()'s EXPLICIT
floorplan, NOT ctrl.import_grc(bpsk_modem.grc)+auto_place. The auto-placed GRC
import currently produces a BROKEN TX passband (it correlates ~0.08 with the ideal
TX passband for the same bits, vs +1.0000 for the explicit placement), so the
loopback cannot close on it. The explicit placement IS the same design/blocks and
is the value-exact, BER-0 reference (test_live_duplex_stream_id). The auto-P&R TX
breakage is a separate, REPORTED issue (the duplex import test only checks TX
non-emptiness, never value-correctness).

Skipped unless a GR-capable /usr/bin/python3 with the kyttar OOT (chip_batch) and
the chip yaml are available.

Run:
    QT_QPA_PLATFORM=offscreen \
      placekyt/.venv/bin/python -m pytest \
        placekyt/tests/test_bpsk_loopback_e2e.py -x -q -s
"""

from __future__ import annotations

import json
import math
import os
import socket
import subprocess
import sys
import textwrap
import time
from pathlib import Path

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from tests.conftest import CHIP_YAML as CT_PATH  # noqa: E402

REPO = Path(__file__).resolve().parents[2]          # /home/system/placekyt
GR_PYTHON = os.environ.get("KYTTAR_GR_PYTHON", "/usr/bin/python3")
GR_OOT = REPO / "gr-kyttar" / "python"

# The locked downconvert recipe (deterministic BER 0; see proto_gr_loopback.py).
FS = 32000.0
CARRIER = 4000.0          # f = CARRIER/FS = 0.125 cycles/sample = fs/8
LO_FREQ = -CARRIER        # sig_source freq -> exp(-j*2*pi*0.125*n)
LO_AMP = 2.0              # image-reject gain
LO_PHASE = 0.0            # exact (skiphead absorbs the delay-1; no phase fixup)
N_BITS = 64


def _gr_available() -> bool:
    try:
        r = subprocess.run(
            [GR_PYTHON, "-c",
             "from gnuradio import gr, blocks, analog; import kyttar; "
             "assert hasattr(kyttar, 'chip_batch')"],
            env={**os.environ, "PYTHONPATH": str(GR_OOT)},
            capture_output=True, timeout=30)
        return r.returncode == 0
    except Exception:
        return False


pytestmark = pytest.mark.skipif(
    not (CT_PATH.exists() and _gr_available()),
    reason="chip yaml or GR python with current kyttar OOT (chip_batch) absent")


def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


def _start_server(port: int) -> subprocess.Popen:
    """Start a placeKYT SimServer (THIS venv) hosting the EXPLICIT-placement duplex
    modem (build_modem) with stream_targets; return it once it prints SERVER_READY."""
    script = textwrap.dedent(f"""
        import os, time
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        import simkyt
        from PySide6.QtWidgets import QApplication
        QApplication.instance() or QApplication([])
        from engine import bpsk_modem_demo as M
        from engine.sim_bridge import SimServer
        from tests.conftest import CHIP_YAML as CT
        built = M.build_modem()
        rx, tx = built["rx"], built["tx"]
        targets = {{
            "rx": {{"entry_addr": int(rx["entry"]), "hop_count": int(rx["hop"]),
                   "data_addrs": [0, 1], "in_port": "x16_in", "out_tag": M.RX_TAG}},
            "tx": {{"entry_addr": int(tx["entry"]), "hop_count": int(tx["hop"]),
                   "data_addrs": [int(tx["da"])], "in_port": "x16_in",
                   "out_tag": M.TX_TAG}},
        }}
        chip = simkyt.Chip.from_yaml(str(CT))
        chip.load_bitstream_physical(built["bres"].words(0))
        srv = SimServer(chip, host="127.0.0.1", port={port}, stream_targets=targets)
        srv.start()
        print("SERVER_READY", flush=True)
        try:
            while True: time.sleep(1)
        except KeyboardInterrupt:
            srv.stop()
    """)
    env = {**os.environ, "PYTHONPATH": str(REPO / "placekyt"),
           "QT_QPA_PLATFORM": "offscreen", "KYTTAR_SERVER_QUIET": "1"}
    p = subprocess.Popen([sys.executable, "-c", script], cwd=str(REPO), env=env,
                         stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    t0 = time.time()
    while time.time() - t0 < 120:
        line = p.stdout.readline()
        if not line and p.poll() is not None:
            raise RuntimeError("server died:\n" + (p.stdout.read() or ""))
        if "SERVER_READY" in line:
            return p
    p.kill()
    raise RuntimeError("server did not become ready in time")


def _run_gr(port: int) -> dict:
    """Run the REAL GR loopback flowgraph (system python) and return TX bits,
    recovered bits, and counts as JSON. This is the SAME chain as bpsk_loopback.grc."""
    gr_script = textwrap.dedent(f"""
        import json, random, math
        from gnuradio import gr, blocks, analog
        import kyttar
        PORT = {port}; FS = {FS}; N_BITS = {N_BITS}
        random.seed(7)                       # == modem_demo_stim.tx_bits(N_BITS)
        bits = [float(random.randint(0, 1)) for _ in range(N_BITS)]
        ref = [0 if b == 0 else 1 for b in bits]    # mapper: 0->+1, 1->-1
        # Pad the TX source so it stays alive PAST the tx dispatch: the rate-
        # EXPANDING tx (64 bits -> 256 passband words) must keep getting general_work
        # calls to drain all 256 to the downstream; if the source ends at 64 the
        # scheduler stops it and the passband truncates to 64 (the bits length).
        pad = bits + [0.0] * 512
        tb = gr.top_block()
        src = blocks.vector_source_f(pad, False)
        tx = kyttar.chip_batch(port=PORT, stream_id='tx', in_kind='real',
                               out_kind='real', raw=False, burst_len=N_BITS)
        f2c = blocks.float_to_complex()
        lo = analog.sig_source_c(FS, analog.GR_COS_WAVE, {LO_FREQ}, {LO_AMP},
                                 0, {LO_PHASE})
        mix = blocks.multiply_cc()
        skip = blocks.skiphead(gr.sizeof_gr_complex, 1)        # == bb[1:]
        keep = blocks.keep_one_in_n(gr.sizeof_gr_complex, 2)   # == [::2]
        rx = kyttar.chip_batch(port=PORT, stream_id='rx', in_kind='complex',
                               out_kind='real', raw=True, burst_len=127)
        rx_vs = blocks.vector_sink_f()
        tx_vs = blocks.vector_sink_f()
        bb_vs = blocks.vector_sink_c()
        tb.connect(src, tx, f2c)
        tb.connect(tx, tx_vs)
        tb.connect(f2c, (mix, 0)); tb.connect(lo, (mix, 1))
        tb.connect(mix, skip, keep, rx, rx_vs)
        tb.connect(keep, bb_vs)
        tb.start(); tb.wait()
        rx_bits = [int(round(v)) & 1 for v in rx_vs.data()]
        print("RESULT " + json.dumps({{
            "tx_bits": [int(b) for b in bits],
            "ref": ref,
            "rx": rx_bits,
            "tx_pb_len": len(tx_vs.data()),
            "bb_len": len(bb_vs.data()),
        }}))
    """)
    env = {**os.environ, "PYTHONPATH": str(GR_OOT)}
    r = subprocess.run([GR_PYTHON, "-c", gr_script], cwd=str(REPO), env=env,
                       capture_output=True, text=True, timeout=240)
    out = r.stdout + r.stderr
    for line in out.splitlines():
        if line.startswith("RESULT "):
            return json.loads(line[len("RESULT "):])
    raise RuntimeError("GR run produced no RESULT:\n" + out[-3000:])


def _ber_with_lag(rx, ref, max_lag=16):
    """Min BER over a small lag window, inversion-tolerant (BPSK 180° ambiguity)."""
    best = (len(ref), 0, 0)
    rx = list(rx)
    for inv in (0, 1):
        r = [b ^ inv for b in rx]
        for lag in range(0, max_lag):
            a = r[lag:]
            n = min(len(a), len(ref))
            if n < len(ref) // 2:
                continue
            errs = sum(1 for i in range(n) if a[i] != ref[i])
            if errs < best[0]:
                best = (errs, n, lag)
    return best


def test_bpsk_passband_loopback_ber0():
    """bits -> chip[tx] -> passband -> downconvert -> chip[rx] -> bits, BER 0, LIVE."""
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

    tx_bits, ref, rx = res["tx_bits"], res["ref"], res["rx"]
    e, m, lag = _ber_with_lag(rx, ref)

    print("\n[loopback] sig_source: freq=%g Hz, amp=%g, phase=%g rad"
          % (LO_FREQ, LO_AMP, LO_PHASE))
    print(f"[loopback] TX passband samples = {res['tx_pb_len']} (expect 256), "
          f"downconverted bb = {res['bb_len']}, recovered bits = {len(rx)}")
    print(f"[loopback] BER = {e}/{m}  (lag={lag})")
    print(f"[loopback] TX bits      : {tx_bits[:32]}")
    print(f"[loopback] recovered@lag: "
          f"{[b ^ (1 if e and rx[lag:lag+1]!=ref[:1] else 0) for b in rx[lag:lag+32]]}")

    assert res["tx_pb_len"] >= 256, \
        f"TX passband truncated: {res['tx_pb_len']} samples (expect full 256)"
    assert len(rx) >= N_BITS, f"RX stream truncated: only {len(rx)} bits"
    assert m and e == 0, (
        f"passband loopback BER={e}/{m} (lag={lag}); recovered {len(rx)} bits — "
        f"sig_source(freq={LO_FREQ}, amp={LO_AMP}, phase={LO_PHASE})")


if __name__ == "__main__":
    port = _free_port()
    srv = _start_server(port)
    try:
        res = _run_gr(port)
    finally:
        srv.terminate()
    e, m, lag = _ber_with_lag(res["rx"], res["ref"])
    print(f"BER={e}/{m} lag={lag} tx_pb={res['tx_pb_len']} bb={res['bb_len']} "
          f"nrx={len(res['rx'])}")
    print("PASS" if (m and e == 0) else "FAIL")
