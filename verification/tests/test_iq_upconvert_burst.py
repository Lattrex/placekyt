"""Burst-chain validation for the IQUpconvert LOCK fix.

1. Upsampler(sps) -> IQUpconvert: N bits -> sps*N correct full-rate outputs,
   value-matched to the composed reference (upsample then IQ-upconvert).
2. Multiple sps values (rate-generality: the LOCK is not tuned to sps=4).
3. Per-cell exec counts: phase runs exactly sps per input bit (one sample in
   the phase..upmix pipeline at a time).
"""
import os, sys
from pathlib import Path
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
_PLACEKYT = Path(__file__).resolve().parents[2] / "placekyt"
for p in (str(_PLACEKYT), str(Path(__file__).resolve().parents[1])):
    if p not in sys.path:
        sys.path.insert(0, p)
import numpy as np
import simkyt
from PySide6.QtWidgets import QApplication
_app = QApplication.instance() or QApplication([])
from engine.catalog import BlockCatalog
from engine.io.chip_type_io import load_chip_type
from engine.build import BuildEngine
from ui.controller import AppController
from model.connection import ChipPortEndpoint, BlockEndpoint
from gr_kyttar.placement.blocks.iq_upconvert_block import IQUpconvertBlock
from gr_kyttar.placement.blocks.upsampler_block import UpsamplerBlock

CHIP_YAML = str(_PLACEKYT / "resources" / "chips" / "kyttar_10x12.yaml")
SAMP = 65536.0
FW = 4096


def fq(f):
    return int(round(max(-1.0, min(1.0, f)) * 32767)) & 0xFFFF


def s16(w):
    return w - 0x10000 if w & 0x8000 else w


def build_chain(sps):
    cat = BlockCatalog.from_gr_kyttar()
    ct = load_chip_type(CHIP_YAML)
    k = getattr(ct, "name", None) or "kyttar_10x12"
    ctrl = AppController(catalog=cat)
    ctrl.new_project("burst", k)
    up = ctrl.place_block("UpsamplerBlock", 0, 1, 1, library="lattrex.official",
                          params={"sps": sps})
    uc = ctrl.place_block("IQUpconvertBlock", 0, 1, 4, library="lattrex.official",
                          params={"sample_rate": SAMP, "frequency": float(FW)})
    ctrl.add_logical_connection(ChipPortEndpoint(chip=0, port="x16_in"),
                                BlockEndpoint(block=up, port="x"), name="a")
    ctrl.add_logical_connection(BlockEndpoint(block=up, port="out"),
                                BlockEndpoint(block=uc, port="xi"), name="b")
    ctrl.add_logical_connection(BlockEndpoint(block=uc, port="out"),
                                ChipPortEndpoint(chip=0, port="x16_out"), name="c")
    ctrl.auto_place(0)
    rep = ctrl.auto_route_all({k: ct})
    assert rep.ok, "route failed: " + "; ".join(f"{r.name}:{r.reason}" for r in rep.failed)
    bres = BuildEngine(cat, CHIP_YAML).build(ctrl.project, {k: ct})
    assert bres.ok, "build failed: " + "; ".join(str(e) for e in bres.errors)
    entry, ins = cat.resolved_io("UpsamplerBlock", {"sps": sps})
    port = ct.port("x16_in")
    landing = ctrl.project.block(up).placement.cells[0]
    dist = abs(landing.x - port.cell_x) + abs(landing.y - port.cell_y) + 1
    hop = max(0, 31 - dist)
    return bres.words(0), entry, (ins[0] if ins else 0), hop, ctrl, up, uc


def drive(words, entry, data_addr, hop, bits):
    chip = simkyt.Chip.from_yaml(CHIP_YAML)
    chip.load_bitstream_physical(words)
    chip.set_port_entry_address("x16_in", entry)
    flat = []
    per = []
    for b in bits:
        chip.inject_data_physical([int(b) & 0xFFFF], target_hop_cnt=hop,
                                  target_addr=data_addr)
        chip.run(max_events=8000)
        chip.inject_jump_physical(target_hop_cnt=hop, entry_addr=entry)
        chip.run(max_events=400000)
        got = []
        while chip.output_available("x16_out"):
            w = chip.read_port_i16("x16_out").view("uint16").tolist()
            got.extend(int(x) & 0xFFFF for x in w)
            chip.release_output_ack("x16_out")
            chip.run(max_events=8000)
        per.append(got)
        flat.extend(got)
    return flat, per


def reference(bits_q15, sps):
    """Compose: upsample (sym + sps-1 zeros) then IQ-upconvert (free-run NCO)."""
    us = UpsamplerBlock("u", sps=sps)
    up_words = us.process_reference_q15(bits_q15)  # uint16 stream
    iq = np.array([complex(s16(w) / 32768.0, 0.0) for w in up_words])
    ref = IQUpconvertBlock("iq", sample_rate=SAMP, frequency=float(FW)).process_reference(iq)
    return [int(v) & 0xFFFF for v in ref]


def main():
    ok = True
    for sps in (2, 4, 8):
        bits = [fq(0.7), fq(-0.5), fq(0.3), fq(-0.9), fq(0.6)]
        words, entry, da, hop, ctrl, up, uc = build_chain(sps)
        flat, per = drive(words, entry, da, hop, bits)
        ref = reference(bits, sps)
        counts = [len(g) for g in per]
        rate_ok = all(c == sps for c in counts)
        val_ok = (len(flat) == len(ref)) and all(a == b for a, b in zip(flat, ref))
        print(f"\nsps={sps}: N={len(bits)} bits -> {len(flat)} outputs "
              f"(expect {sps*len(bits)}); per-trigger counts={counts}")
        print(f"  rate_ok={rate_ok}  value_ok={val_ok}")
        if not val_ok:
            print("  GOT:", [hex(x) for x in flat])
            print("  REF:", [hex(x) for x in ref])
        ok = ok and rate_ok and val_ok
    print("\nRESULT:", "PASS" if ok else "FAIL")
    return 0 if ok else 1


def test_iq_upconvert_burst_full_rate():
    """The arbiter-LOCK fix makes IQUpconvert emit EVERY sample of a
    rate-expanding burst (sps=2/4/8), value-exact to the composed reference."""
    assert main() == 0


if __name__ == "__main__":
    sys.exit(main())
