# SPDX-License-Identifier: GPL-3.0-or-later
"""Headless robust-RX demo — coarse frequency recovery, with its negative
control, END TO END on one array.

One raised-cosine 2-sps BPSK burst carrying a 0.18-cycles/sample carrier
offset (far beyond a Costas loop's pull-in) drives TWO placed receiver
chains duplexed on the shared ports:

  'rx'  : FLLBandEdge(sps 2, rolloff 0.35, fs 17, bw 0.1)
          -> ComplexCostasLoop(0.05, order 2) -> BPSKSlicer   => BER 0
  'ctl' : ComplexCostasLoop(0.05, order 2) -> BPSKSlicer      => BER ~0.2
          (the coherent chain's carrier-recovery core WITHOUT the FLL —
          the on-chip negative control: the same stimulus, the old chain
          provably fails)

The chain topology, parameters, and operating point are the FLL block's own
end-to-end chain gate verbatim (verification/tests/test_fll_band_edge.py
tier 5: FLL->Costas BER 0 at foff=0.18 where chip-Costas-only measures BER
~0.17; GR's own fll_band_edge_cc->costas chain proves the same competence at
GR's operating point foff=0.05 — INV-26 goldens live in that gate).

Pipeline: import robust_rx.grc -> generic auto place-and-route -> build ->
drive both streams interleaved per-sample on real simKYT -> demux the shared
output port by out_tag -> slice-and-compare BER against the transmitted bits.

Run::

    PYTHONPATH=runtime/python:placekyt QT_QPA_PLATFORM=offscreen \
        .venv/bin/python examples/robust_rx/robust_rx_demo.py
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
for _p in (str(ROOT / "runtime" / "python"), str(ROOT / "placekyt"),
           str(HERE)):
    if _p not in sys.path:
        sys.path.insert(0, _p)


def _load_stim():
    """The repo stim module (the same one the shipped .grc imports), loaded by
    FILE so this venv never touches the kyttar package __init__ (which imports
    gnuradio — present only in the GR interpreter)."""
    import importlib.util
    p = ROOT / "gr-kyttar" / "python" / "kyttar" / "robust_demo_stim.py"
    spec = importlib.util.spec_from_file_location("robust_demo_stim", p)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


stim = _load_stim()

CHIP_YAML = str(ROOT / "placekyt" / "resources" / "chips" / "kyttar_10x12.yaml")
GRC_PATH = HERE / "robust_rx.grc"
KYT_PATH = HERE / "robust_rx.kyt"
GR_PYTHON = os.environ.get("KYTTAR_GR_PYTHON", "/usr/bin/python3")

# The chain gate's decision protocol (test_fll_band_edge._chain_ber): trim the
# acquisition transient (the chip chain settles well before symbol 150), then
# take the best BER over the 2-sps timing phase, a small lag window (the fixed
# chain delay), and the BPSK 180-degree polarity ambiguity (physics — a Costas
# locks to either polarity). The negative control proves this bounded search
# cannot rescue an unlocked chain.
SKIP_SYM = 150
# The failure threshold for the negative control (the chain gate's bound; the
# measured Costas-only BER at foff=0.18 is ~0.17-0.2).
CTL_FAIL_BER = 0.05


def _wr(h, d):
    return (0x6 << 12) | ((h & 0x1F) << 5) | (d & 0x1F)


def _jp(h, e):
    return (0x7 << 12) | ((h & 0x1F) << 5) | (e & 0x1F)


def _q15(f):
    return max(-32768, min(32767, int(round(f * 32768.0)))) & 0xFFFF


def _s16(v):
    v &= 0xFFFF
    return v - 0x10000 if v & 0x8000 else v


def import_and_pnr():
    """import the .grc, generic auto place-and-route, build. Also asserts the
    FLL router hazard is absent: NO routed corridor transits a chip-port cell
    (the documented 8-wide-ring failure mode — route 'ok', chip dead)."""
    from engine.build import BuildEngine
    from engine.catalog import BlockCatalog
    from engine.grc_import import import_grc
    from engine.io.chip_type_io import load_chip_type
    from ui.controller import AppController

    cat = BlockCatalog.from_gr_kyttar()
    res = import_grc(str(GRC_PATH), cat)
    if not res.ok:
        raise RuntimeError(f"GRC import failed: unknown blocks {res.unknown}")
    ct = load_chip_type(CHIP_YAML)
    ctrl = AppController(catalog=cat)
    ctrl.project = res.project
    rep = ctrl.auto_pnr({res.project.chip_type: ct})
    if not rep.ok:
        raise RuntimeError(f"auto_pnr failed: {rep.reason}")
    port_cells = {(p.cell_x, p.cell_y) for p in ct.ports}
    n_routed = 0
    for c in res.project.connections:
        pts = c.route if isinstance(c.route, list) else None
        if pts and len(pts) > 2:
            n_routed += 1
            hit = [(p.x, p.y) for p in pts[1:-1] if (p.x, p.y) in port_cells]
            if hit:
                raise RuntimeError(
                    f"corridor {c.name} transits port cell(s) {hit} — the "
                    f"FLL ring port-pinch hazard (injections would be "
                    f"swallowed)")
    if n_routed == 0:      # the check must never be vacuous
        raise RuntimeError("no routed corridors found — port-transit check "
                           "saw nothing")
    bres = BuildEngine(cat, CHIP_YAML).build(res.project,
                                             {res.project.chip_type: ct})
    if not bres.ok:
        raise RuntimeError(
            "build failed: " + "; ".join(str(e) for e in bres.errors[:5]))
    return res.project, bres, cat, ctrl


def run_streams(project, bres, cat, ctrl=None, x=None):
    """Drive both streams per-sample interleaved on real simKYT; return
    {'rx': [bit words], 'ctl': [bit words]} demuxed by out_tag."""
    import simkyt
    from engine.port_config import stream_targets

    if ctrl is None:
        from ui.controller import AppController
        ctrl = AppController(catalog=cat)
        ctrl.project = project
    cfgs = stream_targets(project, ctrl.registry, cat, 0, build_result=bres)
    missing = {"rx", "ctl"} - set(cfgs)
    if missing:
        raise RuntimeError(f"streams unresolved: {missing}")
    if x is None:
        x = stim.rx_burst()

    chip = simkyt.Chip.from_yaml(CHIP_YAML)
    chip.load_bitstream_physical(bres.words(0))
    by_tag = {int(cfg["out_tag"]): sid for sid, cfg in cfgs.items()
              if cfg.get("out_tag") is not None}
    out = {"rx": [], "ctl": []}

    def drain():
        got = chip.read_port_words_timed("x16_out")
        for v, d, _t in got:
            sid = by_tag.get(int(d))
            if sid in out:
                out[sid].append(_s16(int(v)))
        return bool(got)

    for c in x:
        for sid in ("rx", "ctl"):
            cfg = cfgs[sid]
            h = int(cfg["hop_count"])
            da = [int(a) for a in cfg["data_addrs"]]
            chip.queue_words_physical("x16_in", [
                _wr(h, da[0]), _q15(c.real), _wr(h, da[1]), _q15(c.imag),
                _jp(h, int(cfg["entry_addr"]))])
            idle = 0
            for _ in range(120000):
                chip.run(max_events=256)
                idle = 0 if drain() else idle + 1
                if idle > 40:
                    break
    idle = 0
    for _ in range(120000):
        chip.run(max_events=256)
        idle = 0 if drain() else idle + 1
        if idle > 400:
            break
    return out


def chain_ber(bit_words, bits, sps=2, skip_sym=SKIP_SYM, max_lag=12):
    """Best BER of the sliced 0/1 words against the TX bits over timing phase
    / small lag / polarity (see SKIP_SYM note)."""
    import numpy as np
    y = np.array([v & 1 for v in bit_words], dtype=int)
    bits = np.asarray(bits, dtype=int)
    best = 1.0
    for ph in range(sps):
        d = y[ph::sps]
        for lag in range(0, max_lag):
            n = min(len(d) - lag, len(bits))
            if n <= skip_sym + 50:
                continue
            dec = d[lag:lag + n]
            tx = bits[:n]
            for pol in (0, 1):
                dd = dec if pol == 0 else 1 - dec
                best = min(best, float(
                    (dd[skip_sym:n] != tx[skip_sym:n]).mean()))
    return best


def main():
    print("1. import robust_rx.grc -> generic auto place-and-route -> build ...")
    project, bres, cat, ctrl = import_and_pnr()
    used = sum(c.cell_count for c in bres.chips.values())
    print(f"   build OK — {used}/120 cells, {len(project.blocks)} placed "
          f"blocks, 2 streams")
    print("2. drive 'rx' (FLL->Costas->slicer) + 'ctl' (Costas->slicer) with "
          f"the SAME foff={stim.FOFF} burst ...")
    out = run_streams(project, bres, cat, ctrl)
    bits = stim.tx_bits()
    n_want = stim.n_rx_bits()
    ber_rx = chain_ber(out["rx"], bits)
    ber_ctl = chain_ber(out["ctl"], bits)
    print(f"   'rx'  recovered {len(out['rx'])}/{n_want} bit words, "
          f"BER {ber_rx}")
    print(f"   'ctl' recovered {len(out['ctl'])}/{n_want} bit words, "
          f"BER {ber_ctl}  (the negative control MUST fail)")
    ok = (len(out["rx"]) >= n_want - 4 and len(out["ctl"]) >= n_want - 4
          and ber_rx == 0.0 and ber_ctl > CTL_FAIL_BER)
    print("RESULT:", "LOCKED — FLL chain BER 0 at foff=0.18; Costas-only "
          "chain fails (negative control)" if ok else "FAILED")
    if not ok:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
