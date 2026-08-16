# SPDX-License-Identifier: GPL-3.0-or-later
"""Headless PSK31 FULL TRANSCEIVER demo — TX + RX duplex on ONE chip, sharing
ONE SRAM panel.

  TX (stream 'tx'): chars → VaricodeEncoder (SRAM-backed, table at addr_base
      1024) → DiffEncoder → BPSK mapper → hold ×8 → raised-cosine envelope
      → shaped baseband out (tagged 'tx').
  RX (stream 'rx'): symbol-rate soft samples → BPSKSlicer → DiffDecoder →
      VaricodeDecoder (SRAM-backed, reverse map at 1..955) → ASCII chars out
      (tagged 'rx').

The two Varicode tables SHARE the single panel: the encoder's embedded
SramController adds ``addr_base`` (1024) to every lookup key, so the regions
are disjoint, and EVERY panel read writes its OWN R3/R4 push-read descriptors
(the SramController read protocol) — the two clients' lookups interleave
safely. The duplex corridor layout (three-track TX crossover with a DATA
track_c for the RX egress, the RX tap/tail/return relays with transit-face
restore) is the ``engine/panel_pnr.py`` duplex template.

VERIFICATION (both asserted, never loosened):
  * TX: SAMPLE-EXACT vs the PSK31 golden (``psk31_tx_golden.golden_tx_q15``
    — the shared PSK31 golden, ``psk31_tx_golden.py`` here), while the RX
    stream runs interleaved.
  * RX: the decoded chars EXACTLY equal the transmitted text — driven with
    diff-encoded ±0.9 symbols of the golden Varicode bit stream (what a
    coherent demodulator hands the slicer).

PER-SAMPLE PACED (the panel contract; the GRC server enforces it for
panel-backed designs).

Run::

    PYTHONPATH=runtime/python:placekyt QT_QPA_PLATFORM=offscreen \
        .venv/bin/python examples/psk31_transceiver/psk31_transceiver_demo.py
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

CHIP_YAML = str(ROOT / "placekyt" / "resources" / "chips" / "kyttar_10x12.yaml")
GRC_PATH = HERE / "psk31_transceiver.grc"
KYT_PATH = HERE / "psk31_transceiver.kyt"
GR_PYTHON = os.environ.get("KYTTAR_GR_PYTHON", "/usr/bin/python3")

TX_TEXT = "CQ CQ DE KYTTAR"
RX_TEXT = "OK 599 TU 73"


def _wr(h, d):
    return (0x6 << 12) | ((h & 0x1F) << 5) | (d & 0x1F)


def _jp(h, e):
    return (0x7 << 12) | ((h & 0x1F) << 5) | (e & 0x1F)


def _q15(f):
    return max(-32768, min(32767, int(round(f * 32768.0)))) & 0xFFFF


def _s16(v):
    v &= 0xFFFF
    return v - 0x10000 if v & 0x8000 else v


def rx_symbols(text: str) -> list[float]:
    """The RX drive: the golden Varicode bits of ``text``, differentially
    ENCODED (mod-2 running sum) and mapped to ±0.9 soft symbols — exactly what
    a coherent PSK31 demodulator hands the slicer at symbol rate."""
    from gr_kyttar.placement.blocks.varicode_decoder_block import varicode_encode
    bits = [int(c) for c in ("00" + varicode_encode(text))]
    y, enc = 0, []
    for x in bits:
        y = (x + y) % 2
        enc.append(y)
    return [0.9 if v else -0.9 for v in enc]


def import_and_pnr():
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
    bres = BuildEngine(cat, CHIP_YAML).build(res.project,
                                             {res.project.chip_type: ct})
    if not bres.ok:
        raise RuntimeError(
            "build failed: " + "; ".join(str(e) for e in bres.errors[:5]))
    return res.project, bres, cat, ct


def stream_tags(project):
    """(tx_tag, rx_tag) from the built project's TX crossover (dest_b/dest_c)."""
    xo = next(b for b in project.blocks
              if b.type == "CrossoverBlock" and "tap" not in b.name
              and "tailxo" not in b.name and "retxo" not in b.name)
    return int(xo.params["dest_b"]), int(xo.params["dest_c"])


def run_duplex(project, build_result, tx_text: str, rx_text: str,
               panel_image=None):
    """Drive BOTH streams per-sample interleaved on real simKYT with the
    registered host panel. Returns (tx_samples, rx_chars)."""
    import simkyt
    from engine.sram_panel import SramPanelDevice
    from model.connection import ChipPortEndpoint

    lands = build_result.chips[0].input_landings
    by_sid = {}
    for c in project.connections:
        if (isinstance(c.source, ChipPortEndpoint)
                and getattr(c, "stream_id", None) and c.name in lands):
            by_sid[c.stream_id] = lands[c.name]
    tx_tag, rx_tag = stream_tags(project)
    panel = project.panels[0]
    dev = SramPanelDevice(size_words=panel.size_words,
                          addr_regs=panel.address_regs,
                          auto_inc_read=bool(getattr(panel, "auto_inc_read",
                                                     False)))
    dev.mem.update({int(a): int(w) & 0xFFFF
                    for a, w in (panel_image or panel.image).items()})
    chip = simkyt.Chip.from_yaml(CHIP_YAML)
    chip.load_bitstream_physical(build_result.words(0))
    chip.register_panel("x1_out", "x1_in", dev)

    tx_chars = [ord(c) for c in tx_text]
    rx_syms = rx_symbols(rx_text)
    out = {"tx": [], "rx": []}

    def pump(idle_max=200):
        idle = 0
        for _ in range(120000):
            chip.run(max_events=64)
            got = chip.read_port_words_timed("x16_out")
            if got:
                idle = 0
                for w, d, _t in got:
                    d = int(d)
                    if d == tx_tag:
                        out["tx"].append(_s16(w))
                    elif d == rx_tag:
                        out["rx"].append(_s16(w))
            else:
                idle += 1
            if idle > idle_max:
                break

    n = max(len(tx_chars), len(rx_syms))
    for k in range(n):
        if k < len(tx_chars):
            lin = by_sid["tx"]
            chip.queue_words_physical("x16_in", [
                _wr(lin["hop"], lin["data_addrs"][0]),
                tx_chars[k] & 0xFFFF, _jp(lin["hop"], lin["entry"])])
            pump()
        if k < len(rx_syms):
            lin = by_sid["rx"]
            chip.queue_words_physical("x16_in", [
                _wr(lin["hop"], lin["data_addrs"][0]),
                _q15(rx_syms[k]), _jp(lin["hop"], lin["entry"])])
            pump()
    pump(800)
    rx_chars = "".join(chr(w & 0x7F) for w in out["rx"] if 0 < w < 128)
    return out["tx"], rx_chars


def main():
    print("1. import psk31_transceiver.grc -> DUPLEX shared-panel auto "
          "place-and-route -> build ...")
    project, bres, cat, ct = import_and_pnr()
    used = sum(c.cell_count for c in bres.chips.values())
    print(f"   build OK — {used}/120 cells, {len(project.blocks)} blocks, "
          f"panel {len(project.panels[0].image)} words (both tables)")
    print(f"2. duplex on real simKYT: TX {TX_TEXT!r} while receiving "
          f"{RX_TEXT!r} ...")
    tx, rx = run_duplex(project, bres, TX_TEXT, RX_TEXT)
    from psk31_tx_golden import golden_tx_q15
    gold = golden_tx_q15(TX_TEXT, sps=8, amplitude=1.0)
    tx_exact = (tx == gold)
    rx_exact = (rx == RX_TEXT)
    print(f"   TX: {len(tx)}/{len(gold)} baseband samples, "
          f"sample-exact vs golden: {tx_exact}")
    print(f"   RX: decoded {rx!r}, exact: {rx_exact}")
    ok = tx_exact and rx_exact
    print("RESULT:", "EXACT — full duplex transceiver, TX == golden AND "
          "RX == sent text" if ok else "MISMATCH")
    if not ok:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
