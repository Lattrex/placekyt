# SPDX-License-Identifier: GPL-3.0-or-later
"""Headless CW (Morse) FULL TRANSCEIVER demo — TX + RX duplex on ONE chip,
sharing ONE SRAM panel.

  TX (stream 'tx'): chars → CWKeyerBlock (SRAM Morse ROM, per-record
      completion flow control) → ITU-R keyed envelope (tagged 'tx').
  RX (stream 'rx'): keyed audio → Abs (envelope detector) → the STREAMING
      fixed-unit CWDecoder (SRAM reverse-Morse LUT at addr_base 12288) →
      ASCII chars (tagged 'rx').

The keyer's message ROM and the decoder's LUT SHARE the single panel (address-
disjoint; every read carries its own R3/R4 descriptors). The RX decoder is a
SKIMMER locked to the keyer's configured unit (samples_per_dot ==
unit_samples) — the honest streaming mode; the block's ADAPTIVE two-pass mode
(global-min unit) remains separately verified per-block.

STREAMING CONVENTIONS (documented):
  * word gaps decode as character boundaries only — NO SPACES (the space
    branch does not fit the classify cell's 32-word budget); compare letters.
  * an RX burst ends with an EOT BLIP (>= 2 units of silence then >= 1 ON
    sample): the blip flushes the final character's gap and is itself never
    decoded (a trailing unfinished run stays undecoded by design).

VERIFICATION (both asserted): TX BIT-EXACT vs the keyer's ITU-R golden
(``key_envelope_q15`` — the keyer's own ITU-R golden) while RX runs
interleaved; RX decodes the sent text's letters EXACTLY (and equals the
streaming golden ``process_reference_streaming``).

Run::

    PYTHONPATH=runtime/python:placekyt QT_QPA_PLATFORM=offscreen \
        .venv/bin/python examples/cw_transceiver/cw_transceiver_demo.py
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
for _p in (str(ROOT / "runtime" / "python"), str(ROOT / "placekyt"), str(HERE)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

CHIP_YAML = str(ROOT / "placekyt" / "resources" / "chips" / "kyttar_10x12.yaml")
GRC_PATH = HERE / "cw_transceiver.grc"
KYT_PATH = HERE / "cw_transceiver.kyt"
GR_PYTHON = os.environ.get("KYTTAR_GR_PYTHON", "/usr/bin/python3")

TX_TEXT = "CQ CQ DE KYTTAR"
RX_TEXT = "RST 599 73"
UNIT = 8


def _wr(h, d):
    return (0x6 << 12) | ((h & 0x1F) << 5) | (d & 0x1F)


def _jp(h, e):
    return (0x7 << 12) | ((h & 0x1F) << 5) | (e & 0x1F)


def _s16(v):
    v &= 0xFFFF
    return v - 0x10000 if v & 0x8000 else v


def keyed_envelope(text: str) -> list[int]:
    """The ITU-R keyed envelope (uint16 Q15 words) for ``text`` at the demo's
    unit — the keyer's own golden, used both as the TX expectation and the RX
    drive."""
    import numpy as np
    from gr_kyttar.placement.blocks.cw_keyer_block import CWKeyerBlock

    k = CWKeyerBlock("g", wpm=20, samples_per_dot=UNIT, edge_samples=2)
    chars = [ord(c) if c != " " else 0 for c in text]
    return [int(v) & 0xFFFF
            for v in k.key_envelope_q15(np.asarray(chars, dtype=np.int32))]


def rx_burst(text: str) -> list[int]:
    """The RX drive: the keyed envelope + tail silence + the EOT blip."""
    return keyed_envelope(text) + [0] * (3 * UNIT) + [30000] * 2


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
    """(tx_tag, rx_tag): the TX crossover's dest_b + the RX col-crossing's
    dest_a (the kicker-form duplex egress)."""
    txxo = next(b for b in project.blocks
                if b.type == "CrossoverBlock" and b.name.endswith("_xover"))
    colxo = next(b for b in project.blocks
                 if b.type == "CrossoverBlock" and b.name.endswith("_colxo"))
    return int(txxo.params["dest_b"]), int(colxo.params["dest_a"])


def run_duplex(project, build_result, tx_text: str, rx_text: str,
               panel_image=None):
    """Drive BOTH streams per-sample interleaved on real simKYT with the
    registered host panel. Returns (tx_env_words, rx_decoded_text)."""
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
                          auto_inc_read=bool(panel.auto_inc_read))
    dev.mem.update({int(a): int(w) & 0xFFFF
                    for a, w in (panel_image or panel.image).items()})
    chip = simkyt.Chip.from_yaml(CHIP_YAML)
    chip.load_bitstream_physical(build_result.words(0))
    chip.register_panel("x1_out", "x1_in", dev)

    tx_chars = [ord(c) if c != " " else 0 for c in tx_text]
    rx_sig = rx_burst(rx_text)
    out = {"tx": [], "rx": []}

    def pump(idle_max=150):
        idle = 0
        for _ in range(120000):
            chip.run(max_events=64)
            got = chip.read_port_words_timed("x16_out")
            if got:
                idle = 0
                for w, d, _t in got:
                    d = int(d)
                    if d == tx_tag:
                        out["tx"].append(int(w) & 0xFFFF)
                    elif d == rx_tag:
                        out["rx"].append(_s16(w))
            else:
                idle += 1
            if idle > idle_max:
                break

    n = max(len(tx_chars), len(rx_sig))
    for k in range(n):
        if k < len(tx_chars):
            lin = by_sid["tx"]
            chip.queue_words_physical("x16_in", [
                _wr(lin["hop"], lin["data_addrs"][0]),
                tx_chars[k] & 0xFFFF, _jp(lin["hop"], lin["entry"])])
            pump()
        if k < len(rx_sig):
            lin = by_sid["rx"]
            chip.queue_words_physical("x16_in", [
                _wr(lin["hop"], lin["data_addrs"][0]),
                rx_sig[k], _jp(lin["hop"], lin["entry"])])
            pump()
    pump(800)
    rx_txt = "".join(chr(w & 0x7F) for w in out["rx"] if 0 < w < 128)
    return out["tx"], rx_txt


def main():
    print("1. import cw_transceiver.grc -> DUPLEX shared-panel auto "
          "place-and-route -> build ...")
    project, bres, cat, ct = import_and_pnr()
    used = sum(c.cell_count for c in bres.chips.values())
    print(f"   build OK — {used}/120 cells, {len(project.blocks)} blocks, "
          f"panel {len(project.panels[0].image)} words (ROM + LUT)")
    print(f"2. duplex on real simKYT: keying {TX_TEXT!r} while decoding "
          f"{RX_TEXT!r} ...")
    tx, rx = run_duplex(project, bres, TX_TEXT, RX_TEXT)
    gold_tx = keyed_envelope(TX_TEXT)
    want_rx = RX_TEXT.replace(" ", "")
    tx_exact = (tx == gold_tx)
    rx_exact = (rx == want_rx)
    print(f"   TX: {len(tx)}/{len(gold_tx)} envelope samples, bit-exact vs "
          f"the ITU-R golden: {tx_exact}")
    print(f"   RX: decoded {rx!r} (letters of {RX_TEXT!r}), exact: {rx_exact}")
    ok = tx_exact and rx_exact
    print("RESULT:", "EXACT — full duplex CW transceiver, TX == golden AND "
          "RX == sent letters" if ok else "MISMATCH")
    if not ok:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
