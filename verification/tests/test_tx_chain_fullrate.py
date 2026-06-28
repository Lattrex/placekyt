"""Full TX chain: PSKSymbolMapper(BPSK) -> Upsampler -> RRC -> IQUpconvert.

Gate: N bits -> sps*N full-rate passband samples (NO collapse), values matching
the composed per-block reference within a small Q15 tolerance (the RRC's on-chip
Q15 datapath differs from a float RRC by a few LSB; each block's own bit-exact
fidelity is covered by its dedicated test — here we prove the IQUpconvert fan-in
LOCK keeps the chain full-rate AND correct end to end)."""
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
from gr_kyttar.placement.blocks.psk_symbol_mapper_block import PSKSymbolMapperBlock
from gr_kyttar.placement.blocks.upsampler_block import UpsamplerBlock
from gr_kyttar.placement.blocks.rrc_pulse_shaper_block import RRCPulseShaperBlock
from gr_kyttar.placement.blocks.iq_upconvert_block import IQUpconvertBlock

CHIP_YAML = str(_PLACEKYT / "resources" / "chips" / "kyttar_10x12.yaml")
SAMP = 65536.0
FW = 4096
SPS = 4
TOL = 2  # IQUpconvert is the only DUT/ref boundary; upstream taken from chip


def s16(w):
    return w - 0x10000 if w & 0x8000 else w


def build():
    cat = BlockCatalog.from_gr_kyttar()
    ct = load_chip_type(CHIP_YAML)
    k = getattr(ct, "name", None) or "kyttar_10x12"
    ctrl = AppController(catalog=cat)
    ctrl.new_project("tx", k)
    # Manual placement (the auto-placer's serpentine packer overlaps the 2-row
    # IQUpconvert footprint with the RRC row — a placer limitation, out of scope
    # for this fix). Deliberate non-overlapping rows with routing room.
    mp = ctrl.place_block("PSKSymbolMapperBlock", 0, 0, 0, library="lattrex.official",
                          params={"modulation": "bpsk"})
    up = ctrl.place_block("UpsamplerBlock", 0, 0, 2, library="lattrex.official",
                          params={"sps": SPS})
    rr = ctrl.place_block("RRCPulseShaperBlock", 0, 0, 4, library="lattrex.official",
                          params={})
    uc = ctrl.place_block("IQUpconvertBlock", 0, 2, 8, library="lattrex.official",
                          params={"sample_rate": SAMP, "frequency": float(FW)})
    ctrl.add_logical_connection(ChipPortEndpoint(chip=0, port="x16_in"),
                                BlockEndpoint(block=mp, port="sample"), name="a")
    ctrl.add_logical_connection(BlockEndpoint(block=mp, port="out_i"),
                                BlockEndpoint(block=up, port="x"), name="b")
    ctrl.add_logical_connection(BlockEndpoint(block=up, port="out"),
                                BlockEndpoint(block=rr, port="sample"), name="c")
    ctrl.add_logical_connection(BlockEndpoint(block=rr, port="out"),
                                BlockEndpoint(block=uc, port="xi"), name="d")
    ctrl.add_logical_connection(BlockEndpoint(block=uc, port="out"),
                                ChipPortEndpoint(chip=0, port="x16_out"), name="e")
    rep = ctrl.auto_route_all({k: ct})
    assert rep.ok, "route failed: " + "; ".join(f"{r.name}:{r.reason}" for r in rep.failed)
    bres = BuildEngine(cat, CHIP_YAML).build(ctrl.project, {k: ct})
    assert bres.ok, "build failed: " + "; ".join(str(e) for e in bres.errors)
    entry, ins = cat.resolved_io("PSKSymbolMapperBlock", {"modulation": "bpsk"})
    port = ct.port("x16_in")
    landing = ctrl.project.block(mp).placement.cells[0]
    dist = abs(landing.x - port.cell_x) + abs(landing.y - port.cell_y) + 1
    return bres.words(0), entry, (ins[0] if ins else 0), max(0, 31 - dist)


def drive(words, entry, da, hop, bits):
    chip = simkyt.Chip.from_yaml(CHIP_YAML)
    chip.load_bitstream_physical(words)
    chip.set_port_entry_address("x16_in", entry)
    flat, per = [], []
    for b in bits:
        chip.inject_data_physical([int(b) & 0xFFFF], target_hop_cnt=hop, target_addr=da)
        chip.run(max_events=8000)
        chip.inject_jump_physical(target_hop_cnt=hop, entry_addr=entry)
        chip.run(max_events=600000)
        got = []
        while chip.output_available("x16_out"):
            w = chip.read_port_i16("x16_out").view("uint16").tolist()
            got.extend(int(x) & 0xFFFF for x in w)
            chip.release_output_ack("x16_out")
            chip.run(max_events=8000)
        per.append(got)
        flat.extend(got)
    return flat, per


def build_pre_chain():
    """Build mapper -> upsampler -> RRC (output to x16_out) so we can capture the
    REAL chip RRC-stage output and feed it to IQUpconvert.process_reference. This
    isolates the IQUpconvert fan-in as the only DUT/ref boundary that could expose
    the burst collapse (upstream stages taken verbatim from the chip)."""
    cat = BlockCatalog.from_gr_kyttar()
    ct = load_chip_type(CHIP_YAML)
    k = getattr(ct, "name", None) or "kyttar_10x12"
    ctrl = AppController(catalog=cat)
    ctrl.new_project("pre", k)
    mp = ctrl.place_block("PSKSymbolMapperBlock", 0, 0, 0, library="lattrex.official",
                          params={"modulation": "bpsk"})
    up = ctrl.place_block("UpsamplerBlock", 0, 0, 2, library="lattrex.official",
                          params={"sps": SPS})
    rr = ctrl.place_block("RRCPulseShaperBlock", 0, 0, 4, library="lattrex.official",
                          params={})
    ctrl.add_logical_connection(ChipPortEndpoint(chip=0, port="x16_in"),
                                BlockEndpoint(block=mp, port="sample"), name="a")
    ctrl.add_logical_connection(BlockEndpoint(block=mp, port="out_i"),
                                BlockEndpoint(block=up, port="x"), name="b")
    ctrl.add_logical_connection(BlockEndpoint(block=up, port="out"),
                                BlockEndpoint(block=rr, port="sample"), name="c")
    ctrl.add_logical_connection(BlockEndpoint(block=rr, port="out"),
                                ChipPortEndpoint(chip=0, port="x16_out"), name="e")
    rep = ctrl.auto_route_all({k: ct})
    assert rep.ok, "pre route failed: " + "; ".join(f"{r.name}:{r.reason}" for r in rep.failed)
    bres = BuildEngine(cat, CHIP_YAML).build(ctrl.project, {k: ct})
    assert bres.ok, "pre build failed: " + "; ".join(str(e) for e in bres.errors)
    entry, ins = cat.resolved_io("PSKSymbolMapperBlock", {"modulation": "bpsk"})
    port = ct.port("x16_in")
    landing = ctrl.project.block(mp).placement.cells[0]
    dist = abs(landing.x - port.cell_x) + abs(landing.y - port.cell_y) + 1
    return bres.words(0), entry, (ins[0] if ins else 0), max(0, 31 - dist)


def reference(bits):
    """Reference: feed the REAL chip mapper->upsampler->RRC output into the
    IQUpconvert Q15 reference model (the proven per-sample upconvert)."""
    words, entry, da, hop = build_pre_chain()
    rrc_q15, _ = drive(words, entry, da, hop, bits)       # uint16 RRC stage output
    iq = np.array([complex(s16(w) / 32768.0, 0.0) for w in rrc_q15])
    ref = IQUpconvertBlock("iq", sample_rate=SAMP, frequency=float(FW)).process_reference(iq)
    return [s16(int(v) & 0xFFFF) for v in ref]


def main():
    bits = [0, 1, 1, 0, 1, 0, 0, 1]
    words, entry, da, hop = build()
    flat, per = drive(words, entry, da, hop, bits)
    counts = [len(g) for g in per]
    rate_ok = all(c == SPS for c in counts)
    got = [s16(x) for x in flat]
    ref = reference(bits)
    n = min(len(got), len(ref))
    diffs = [abs(got[i] - ref[i]) for i in range(n)]
    maxd = max(diffs) if diffs else 99999
    val_ok = (len(got) == len(ref)) and maxd <= TOL
    print(f"TX chain: {len(bits)} bits -> {len(flat)} samples "
          f"(expect {SPS*len(bits)}); per-trigger={counts}")
    print(f"  rate_ok={rate_ok}  len(got)={len(got)} len(ref)={len(ref)} "
          f"max_abs_diff={maxd} (tol {TOL})  value_ok={val_ok}")
    if not val_ok:
        print("  GOT:", got[:16])
        print("  REF:", ref[:16])
    print("RESULT:", "PASS" if (rate_ok and val_ok) else "FAIL")
    return 0 if (rate_ok and val_ok) else 1


def test_tx_chain_full_rate():
    """The full BPSK TX chain (mapper->upsampler->RRC->IQUpconvert) produces
    full-rate passband, value-exact — the IQUpconvert LOCK fix in a real chain."""
    assert main() == 0


if __name__ == "__main__":
    sys.exit(main())
