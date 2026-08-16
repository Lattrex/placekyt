# SPDX-License-Identifier: GPL-3.0-or-later
"""Headless data-link demo — an 11-block scrambled byte loopback, END TO END.

The chain (every stage a placed Kyttar block):

  bytes → UnpackKBits(8) → Not → AndConst(1) → MapBB([1,0]) → LFSRScrambler
        → DiffEncoder(2) → DiffDecoder(2) → LFSRScrambler (descrambler)
        → CharToFloat(128) → FloatToChar(128) → PackKBits(8) → bytes

Two goldens, both asserted:
  * the IDENTICAL stock-GNU-Radio flowgraph, run under the real GR interpreter
    in a subprocess — proving the whole placed composition is GR-equivalent
    (each stage 1:1: not_bb, and_const_bb, map_bb, additive_scrambler_bb,
    diff_encoder/decoder_bb, char_to_float/float_to_char at scale 128, ...);
  * the loopback identity (recovered bytes == payload bytes) — NOT/AND/map
    cancel, diff enc∘dec cancels, and the additive scrambler is self-inverse
    when applied twice in sync.

Pipeline: import data_link.grc → generic auto place-and-route (no panel — the
same sweep the modem examples use) → build → inject the payload bytes on real
simKYT per the build's input landings → capture x16_out.

Run::

    PYTHONPATH=runtime/python:placekyt QT_QPA_PLATFORM=offscreen \
        .venv/bin/python examples/data_link/data_link_demo.py "ANY PAYLOAD"
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
for _p in (str(ROOT / "runtime" / "python"), str(ROOT / "placekyt"), str(HERE)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

CHIP_YAML = str(ROOT / "placekyt" / "resources" / "chips" / "kyttar_10x12.yaml")
GRC_PATH = HERE / "data_link.grc"
KYT_PATH = HERE / "data_link.kyt"
GR_PYTHON = os.environ.get("KYTTAR_GR_PYTHON", "/usr/bin/python3")

DEMO_TEXT = "KYTTAR DATA LINK 73"

# The stock-GR reference chain — the same stages, same params, run under the
# real GNU Radio interpreter. Prints the output bytes.
_GR_GOLDEN = r"""
import sys
from gnuradio import gr, blocks, digital

payload = [int(x) for x in sys.argv[1].split(",")]
tb = gr.top_block()
src = blocks.vector_source_b(payload, False, 1, [])
unpack = blocks.unpack_k_bits_bb(8)
inv = blocks.not_bb()
mask = blocks.and_const_bb(1)
remap = digital.map_bb([1, 0])
scr = digital.additive_scrambler_bb(0x8A, 0x7F, 7, 0, 1)
denc = digital.diff_encoder_bb(2)
ddec = digital.diff_decoder_bb(2)
descr = digital.additive_scrambler_bb(0x8A, 0x7F, 7, 0, 1)
c2f = blocks.char_to_float(1, 128.0)
f2c = blocks.float_to_char(1, 128.0)
pack = blocks.pack_k_bits_bb(8)
snk = blocks.vector_sink_b()
tb.connect(src, unpack, inv, mask, remap, scr, denc, ddec, descr, c2f, f2c,
           pack, snk)
tb.run()
print("GOLDEN", " ".join(str(int(v)) for v in snk.data()))
"""


def _wr(h, d):
    return (0x6 << 12) | ((h & 0x1F) << 5) | (d & 0x1F)


def _jp(h, e):
    return (0x7 << 12) | ((h & 0x1F) << 5) | (e & 0x1F)


def import_and_pnr():
    """import the .grc, generic auto-P&R (no panel), build."""
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


def gr_golden(payload: list[int]) -> list[int]:
    """The stock-GR reference chain's output bytes for ``payload``."""
    script = Path(os.environ.get("TMPDIR", "/tmp")) / "data_link_golden.py"
    script.write_text(_GR_GOLDEN)
    r = subprocess.run(
        [GR_PYTHON, str(script), ",".join(str(b) for b in payload)],
        capture_output=True, text=True, timeout=300)
    line = next((ln for ln in r.stdout.splitlines()
                 if ln.startswith("GOLDEN")), None)
    if r.returncode != 0 or line is None:
        raise RuntimeError(f"GR golden failed: {r.stderr[-500:]}")
    return [int(x) for x in line.split()[1:]]


def run_link(project, build_result, payload: list[int]) -> list[int]:
    """Inject the payload bytes (paced per byte) and return the recovered bytes."""
    import simkyt

    landings = build_result.chips[0].input_landings
    lin = next(iter(landings.values()))
    chip = simkyt.Chip.from_yaml(CHIP_YAML)
    chip.load_bitstream_physical(build_result.words(0))
    out: list[int] = []
    for b in payload:
        chip.queue_words_physical("x16_in", [
            _wr(lin["hop"], lin["data_addrs"][0]), int(b) & 0xFF,
            _jp(lin["hop"], lin["entry"]),
        ])
        idle = 0
        for _ in range(120000):
            chip.run(max_events=64)
            got = chip.read_port_words_timed("x16_out")
            if got:
                idle = 0
                out.extend(v & 0xFFFF for v, _d, _t in got)
            else:
                idle += 1
            if idle > 200:
                break
    return out


def main():
    text = " ".join(sys.argv[1:]) or DEMO_TEXT
    payload = [ord(c) for c in text]
    print(f"payload: {text!r} ({len(payload)} bytes)")
    print("1. import data_link.grc -> generic auto place-and-route -> build ...")
    project, bres, cat, ct = import_and_pnr()
    used = sum(c.cell_count for c in bres.chips.values())
    print(f"   build OK — {used}/120 cells, "
          f"{len(project.blocks)} placed blocks")
    print("2. run the payload through the placed+routed chip on real simKYT ...")
    got = run_link(project, bres, payload)
    gold = gr_golden(payload)
    n = min(len(got), len(gold))
    mism = sum(1 for i in range(n) if got[i] != gold[i])
    ident = got == payload
    print(f"   chip: {len(got)} bytes, GR golden: {len(gold)}, "
          f"mismatches vs GR: {mism}, loopback identity: {ident}")
    ok = (got == gold) and ident
    print("RESULT:", "EXACT — placed chain == stock GNU Radio == the payload"
          if ok else "MISMATCH")
    if not ok:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
