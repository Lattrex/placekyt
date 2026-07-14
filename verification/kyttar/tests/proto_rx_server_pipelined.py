# Step 4 end-to-end: drive the shipped coherent BPSK RX .kyt through the REAL
# SimServer bridge with header pipelined:true (the flag kyttar.source now sets from
# the .grc "Full-speed (saturated)" param), over an actual socket, and confirm BER 0.
# This exercises the exact server path the GRC flowgraph uses at full speed.
import sys
from pathlib import Path

ROOT = Path("/home/system/placekyt")
for p in (ROOT / "placekyt", ROOT / "runtime" / "python", ROOT / "verification"):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

import math      # noqa: E402
import random    # noqa: E402
import struct    # noqa: E402
import socket    # noqa: E402
import json      # noqa: E402
import numpy as np  # noqa: E402
import simkyt    # noqa: E402
from engine.sim_bridge import SimServer  # noqa: E402

CHIP = str(ROOT / "placekyt" / "resources" / "chips" / "kyttar_10x12.yaml")
KYT = str(ROOT / "examples" / "coherent_bpsk_rx" / "coherent_bpsk_rx.kyt")
_HDR = struct.Struct(">I")


def _rrc(beta, sps, span):
    n = span * sps; taps = []
    for i in range(n + 1):
        t = (i - n / 2) / sps
        if abs(t) < 1e-8:
            v = 1 - beta + 4 * beta / math.pi
        elif abs(abs(4 * beta * t) - 1.0) < 1e-8:
            v = (beta / math.sqrt(2)) * ((1 + 2 / math.pi) * math.sin(math.pi / (4 * beta))
                                         + (1 - 2 / math.pi) * math.cos(math.pi / (4 * beta)))
        else:
            num = (math.sin(math.pi * t * (1 - beta)) + 4 * beta * t * math.cos(math.pi * t * (1 + beta)))
            den = math.pi * t * (1 - (4 * beta * t) ** 2)
            v = num / den
        taps.append(v)
    e = math.sqrt(sum(v * v for v in taps)); return [v / e for v in taps]


def _gen_iq(nsym, foff=0.008, toff=0.45, seed=5):
    random.seed(seed)
    bits = [random.randint(0, 1) for _ in range(nsym)]
    taps = _rrc(0.35, 2, 6)
    syms = [1.0 if b == 0 else -1.0 for b in bits]
    up = []
    for s in syms:
        up.append(s); up.extend([0.0])
    shaped = []
    for n in range(len(up)):
        acc = 0.0
        for k in range(len(taps)):
            if 0 <= n - k < len(up):
                acc += taps[k] * up[n - k]
        shaped.append(acc)
    out = []
    for n in range(len(shaped) - 1):
        i = n + int(math.floor(toff)); frac = toff - math.floor(toff)
        if 0 <= i < len(shaped) - 1:
            out.append(shaped[i] * (1 - frac) + shaped[i + 1] * frac)
        else:
            out.append(shaped[n])
    inter = np.empty(2 * len(out), dtype=np.float32)
    for n, s in enumerate(out):
        inter[2 * n] = s * math.cos(2 * math.pi * foff * n)
        inter[2 * n + 1] = s * math.sin(2 * math.pi * foff * n)
    return bits, inter


def _build_chip():
    from kyttar_verify.dut_runner import _engine
    (app, BlockCatalog, load_chip_type, BuildEngine, AppController, _CP, _BE) = _engine()
    cat = BlockCatalog.from_gr_kyttar()
    ct = load_chip_type(CHIP); ct_key = getattr(ct, "name", None) or "kyttar_10x12"
    ctrl = AppController(catalog=cat); ctrl.open_project(KYT)
    bres = BuildEngine(cat, CHIP).build(ctrl.project, {ct_key: ct})
    assert bres.ok, bres.errors
    land = bres.chips[0].input_landings["net5"]
    chip = simkyt.Chip.from_yaml(CHIP); chip.load_bitstream_physical(bres.words(0))
    return chip, int(land["entry"]), int(land["hop"]) & 0x1F


def _send(conn, header, payload):
    header = dict(header)
    arr = np.ascontiguousarray(payload, dtype="<f4")
    header["n"] = int(arr.size)
    hb = json.dumps(header).encode()
    conn.sendall(_HDR.pack(len(hb))); conn.sendall(hb); conn.sendall(arr.tobytes())


def _recv(conn):
    def _ex(n):
        b = b""
        while len(b) < n:
            c = conn.recv(n - len(b))
            if not c:
                raise RuntimeError("closed")
            b += c
        return b
    hlen = _HDR.unpack(_ex(4))[0]
    header = json.loads(_ex(hlen).decode())
    n = int(header.get("n", 0))
    out = np.frombuffer(_ex(n * 4), dtype="<f4") if n else np.array([], dtype="<f4")
    return header, out


def _ber(rx, tx):
    rx = [int(v) & 1 for v in rx]
    best = 1.0
    for lag in range(0, 24):
        a = rx[lag:]
        for cand in (a, [1 - x for x in a]):
            m = min(len(cand), len(tx))
            if m < 40:
                continue
            best = min(best, sum(1 for i in range(30, m) if cand[i] != tx[i]) / (m - 30))
    return best


def main():
    chip, entry, hop = _build_chip()
    print("hosted chip: entry=%d hop=%d" % (entry, hop), flush=True)
    srv = SimServer(chip, default_entries={"x16_in": entry},
                    default_hops={"x16_in": hop})
    port = srv.start()
    try:
        bits, inter = _gen_iq(120)
        for pipe in (False, True):
            conn = socket.create_connection(("127.0.0.1", port))
            _send(conn, {"op": "process_batch", "port": "x16_out", "in_port": "x16_in",
                         "complex": True, "data_addrs": [0, 1], "raw": True,
                         "pipelined": bool(pipe)}, inter)
            reply, out = _recv(conn); conn.close()
            assert reply.get("ok"), reply
            print("%-10s: pipelined_reply=%s bits=%d BER=%.4f" % (
                "PIPELINED" if pipe else "PER-SAMPLE",
                reply.get("pipelined", False), len(out), _ber(out, bits)), flush=True)
    finally:
        srv.stop()


if __name__ == "__main__":
    main()
