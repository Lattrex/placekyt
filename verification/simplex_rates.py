# SPDX-License-Identifier: GPL-3.0-or-later
"""Headless SIMPLEX sink-rate harness for the 7 shipped example designs.

Reproduces the GUI measurement a user makes by hand: open the shipped ``.kyt``,
set **Duplex schedule = sequential** and **Saturated (pipelined) drive**, Run, and
read each direction's **Settled rate** off the Stream Summary panel.

Sequential schedule = simplex: each chain's whole burst runs ALONE on the array
(no duplex contention), so each direction has one clean steady-state rate — the
peak per-chain number. (Interleaved schedule = full-duplex, where the two chains
time-slice the shared port; that number is contention-dependent and not reported
here by request.)

The reported number is ``settled_sps`` from ``TraceModel.stream_summary()`` — the
EXACT field the panel's "Settled rate" column shows.

Run:
    cd /home/system/placekyt
    QT_QPA_PLATFORM=offscreen .venv/bin/python verification/simplex_rates.py
"""
from __future__ import annotations

import math
import os
import socket
import sys
from pathlib import Path

import numpy as np

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

_ROOT = Path(__file__).resolve().parents[1]
for p in (str(_ROOT / "placekyt"), str(_ROOT / "runtime" / "python")):
    if p not in sys.path:
        sys.path.insert(0, p)

CHIP_YAML = str(_ROOT / "placekyt" / "resources" / "chips" / "kyttar_10x12.yaml")
FS = 32000.0
N = 400  # samples per direction — long enough for a settled steady state


# --- saturated stimulus (values don't affect the RATE, only count + type) ----
def _real(n):
    t = np.arange(n)
    return (0.7 * np.cos(2 * np.pi * 300.0 * t / FS)).astype(np.float32)


def _complex(n):
    t = np.arange(n)
    z = 0.7 * np.exp(1j * 2 * np.pi * 900.0 * t / FS)
    iq = np.empty(2 * n, dtype=np.float32)
    iq[0::2] = z.real
    iq[1::2] = z.imag
    return iq.astype(np.float32)


# 7 designs — the shipped .kyt each opens (hand-placed; open, don't re-import).
DESIGNS = [
    ("BPSK",   "bpsk_modem/bpsk_modem.kyt"),
    ("QPSK",   "qpsk_modem/qpsk_modem.kyt"),
    ("4FSK",   "fsk4_modem/fsk4_modem.kyt"),
    ("16-QAM", "qam16_modem/qam16_modem.kyt"),
    ("AM",     "am_transceiver/am_transceiver.kyt"),
    ("FM",     "fm_transceiver/fm_transceiver.kyt"),
    ("SSB",    "ssb_weaver/ssb_weaver.kyt"),
]

# Per-design REAL RX stimulus: a matched-filter RX passes a plain tone, but the
# timing-recovery + slicer chains (esp. 4FSK 4-PAM) only emit symbols on a valid
# modulated burst. Use each design's own canonical demo-stim burst so the RX chain
# actually recovers and the OUTPUT stream has a settled rate. Falls back to a tone.
_STIM_FILE = {
    "4FSK":   "fsk4_demo_stim.py",
    "QPSK":   "qpsk_demo_stim.py",
    "16-QAM": "qam16_demo_stim.py",
}


def _load_stim(fname):
    """Load a demo-stim module BY FILE PATH — importing via ``kyttar.__init__``
    pulls in gnuradio (absent in the placeKYT venv), so we load the leaf directly."""
    import importlib.util
    path = _ROOT / "gr-kyttar" / "python" / "kyttar" / fname
    spec = importlib.util.spec_from_file_location(fname[:-3], str(path))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _rx_burst(label, n):
    fname = _STIM_FILE.get(label)
    if not fname:
        return None
    try:
        m = _load_stim(fname)
        return np.asarray(_interleave(m.burst(n)), dtype=np.float32)
    except Exception as e:  # noqa: BLE001 — report, then fall back to the tone
        print(f"  [warn] {label} rx stim failed ({type(e).__name__}: {e}); using tone")
        return None


def _interleave(iq_complex_list):
    """[c0,c1,...] complex -> [I0,Q0,I1,Q1,...] float32."""
    z = np.asarray(iq_complex_list, dtype=np.complex64)
    out = np.empty(2 * len(z), dtype=np.float32)
    out[0::2] = z.real
    out[1::2] = z.imag
    return out


def _build(kyt_path):
    """Open the shipped .kyt (already placed+routed) and build it. Returns
    (build_result, stream_targets, reset_writes) — reused across both directions."""
    from PySide6.QtWidgets import QApplication
    QApplication.instance() or QApplication([])
    from engine.catalog import BlockCatalog
    from engine.port_config import stream_targets, batch_reset_writes
    from ui.controller import AppController

    cat = BlockCatalog.from_gr_kyttar()
    ctrl = AppController(catalog=cat)
    ctrl.open_project(str(kyt_path))          # GUI path — no re-import, no auto-P&R
    bres = ctrl.build()
    if not bres.ok:
        raise RuntimeError("build failed: " + "; ".join(str(e) for e in bres.errors))
    tgts = stream_targets(ctrl.project, ctrl.registry, ctrl.catalog, 0,
                          build_result=bres)
    return bres, tgts, batch_reset_writes(bres, 0)


def _stream_is_complex(tgts, sid):
    """A stream is complex-INPUT iff its landing takes two data regs (xi/xq)."""
    das = list(tgts[sid].get("data_addrs") or [])
    return len(das) >= 2


def _run_one_direction(bres, tgts, resets, sid, label):
    """Host a FRESH chip and drive ONE direction alone (sequential simplex, saturated)
    so its trace AND its performance_report reflect ONLY that direction. Returns
    (sink_sps, perf_report_dict)."""
    import simkyt
    from engine.sim_bridge import SimServer, send_message, recv_message
    from engine.trace_model import TraceModel

    cx = _stream_is_complex(tgts, sid)
    seg = _rx_burst(label, N) if sid == "rx" else None
    if seg is None:
        seg = _complex(N) if cx else _real(N)
    n_samp = (len(seg) // 2) if cx else len(seg)

    chip = simkyt.Chip.from_yaml(CHIP_YAML)
    chip.load_bitstream_physical(bres.chips[0].words)
    chip.enable_trace(8_000_000)
    srv = SimServer(chip, stream_targets=tgts, batch_reset_writes=resets)
    port = srv.start()
    try:
        c = socket.socket()
        c.connect(("127.0.0.1", port))
        try:
            send_message(c, {"op": "process_batch_duplex", "port": "x16_out",
                             "in_port": "x16_in", "schedule": "sequential",
                             "pipelined": True,
                             "streams": [{"stream_id": sid, "complex": cx,
                                          "raw": False, "n_samples": n_samp}]},
                         seg.astype(np.float32))
            recv_message(c)
        finally:
            c.close()
        perf = chip.performance_report()
        events = chip.get_trace()
    finally:
        srv.stop()

    tm = TraceModel()
    tm.ingest(0, events, 10)
    tag = int(tgts[sid]["out_tag"])
    cand = [r for r in tm.stream_summary()
            if r["direction"] == "out" and r["port"] == "x16_out"
            and r["settled_sps"] and int(r["tag"]) == tag]
    sps = max((r["settled_sps"] for r in cand), default=None)
    return sps, perf


def measure(label, relpath):
    """Per direction: {sink_sps, total_mw, active_mw, idle_mw, active_cells}."""
    bres, tgts, resets = _build(_ROOT / "examples" / relpath)
    out = {}
    for sid in ("rx", "tx"):
        if sid not in tgts:
            continue
        sps, perf = _run_one_direction(bres, tgts, resets, sid, label)
        out[sid] = {
            "sps": sps,
            "total_mw": perf.get("total_power_mw"),
            "active_mw": perf.get("average_power_mw"),
            "idle_mw": perf.get("idle_power_mw"),
            "active_cells": perf.get("active_cells"),
        }
    return out


def _fmt_rate(sps):
    if not sps:
        return "—"
    return f"{sps/1e6:.2f} MSa/s" if sps >= 1e6 else f"{sps/1e3:.0f} kSa/s"


def main():
    hdr = f"{'Design':8} {'RX rate':>11} {'RX pwr(mW)':>11} {'TX rate':>11} {'TX pwr(mW)':>11}"
    print(hdr)
    print("-" * len(hdr))
    table = []
    for label, relpath in DESIGNS:
        try:
            r = measure(label, relpath)
            rx, tx = r.get("rx", {}), r.get("tx", {})
            table.append((label, rx, tx))
            print(f"{label:8} "
                  f"{_fmt_rate(rx.get('sps')):>11} "
                  f"{(rx.get('total_mw') or 0):11.1f} "
                  f"{_fmt_rate(tx.get('sps')):>11} "
                  f"{(tx.get('total_mw') or 0):11.1f}")
        except Exception as e:  # noqa: BLE001
            print(f"{label:8}  ERROR: {type(e).__name__}: {e}")
    return table


if __name__ == "__main__":
    main()
