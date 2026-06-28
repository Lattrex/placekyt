"""Full TX chain via AUTO-PLACE + bus/broker auto-route (the folded-block P&R fix).

Same chain as ``test_tx_chain_fullrate.py`` (PSKSymbolMapper(BPSK) -> Upsampler ->
RRC -> IQUpconvert) but DROPPED ANYWHERE and arranged by ``auto_place`` + routed by
the §1.2 bus/broker router (``use_bus="always"``) — the path that previously failed
``rrcpulseshaper_to_iqupconvert :: no bus path from source to the broker tap`` because
the serpentine packer jammed the IQUpconvert 4x2 fold against the array wall, leaving
its input cell no free bus-facing neighbour.

Gate: auto-route succeeds (rep.ok), the design builds, and simKYT yields FULL-RATE
(sps samples/trigger, NO burst collapse) value-exact passband — the same value check
as the manual-placement test, here proving the auto-placer seats the fold so its
input broker and output egress do not contend (the IQUpconvert LOCK stays serialized).
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

CHIP_YAML = str(_PLACEKYT / "resources" / "chips" / "kyttar_10x12.yaml")
SAMP = 65536.0
FW = 4096
SPS = 4
TOL = 2
LIB = "lattrex.official"


def s16(w):
    return w - 0x10000 if w & 0x8000 else w


def _common_build(*, with_iq):
    """Drop the blocks at ARBITRARY positions, then auto_place + bus-route them. With
    ``with_iq=False`` the chain stops at the RRC (egressing to x16_out) so the chip's
    real RRC-stage output can feed the IQUpconvert reference (isolating the fold)."""
    cat = BlockCatalog.from_gr_kyttar()
    ct = load_chip_type(CHIP_YAML)
    k = getattr(ct, "name", None) or "kyttar_10x12"
    ctrl = AppController(catalog=cat)
    ctrl.new_project("tx", k)
    mp = ctrl.place_block("PSKSymbolMapperBlock", 0, 1, 1, library=LIB,
                          params={"modulation": "bpsk"})
    up = ctrl.place_block("UpsamplerBlock", 0, 3, 1, library=LIB,
                          params={"sps": SPS})
    rr = ctrl.place_block("RRCPulseShaperBlock", 0, 5, 1, library=LIB, params={})
    ctrl.add_logical_connection(ChipPortEndpoint(chip=0, port="x16_in"),
                                BlockEndpoint(block=mp, port="sample"), name="a")
    ctrl.add_logical_connection(BlockEndpoint(block=mp, port="out"),
                                BlockEndpoint(block=up, port="x"), name="b")
    ctrl.add_logical_connection(BlockEndpoint(block=up, port="out"),
                                BlockEndpoint(block=rr, port="sample"), name="c")
    if with_iq:
        uc = ctrl.place_block("IQUpconvertBlock", 0, 7, 1, library=LIB,
                              params={"sample_rate": SAMP, "frequency": float(FW)})
        ctrl.add_logical_connection(BlockEndpoint(block=rr, port="out"),
                                    BlockEndpoint(block=uc, port="xi"), name="d")
        ctrl.add_logical_connection(BlockEndpoint(block=uc, port="out"),
                                    ChipPortEndpoint(chip=0, port="x16_out"), name="e")
    else:
        ctrl.add_logical_connection(BlockEndpoint(block=rr, port="out"),
                                    ChipPortEndpoint(chip=0, port="x16_out"), name="e")
    ctrl.auto_place(0)
    rep = ctrl.auto_route_all({k: ct}, auto_orient=False, use_bus="always")
    assert rep.ok, "auto-route failed: " + "; ".join(
        f"{r.name}:{r.reason}" for r in rep.failed)
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


def reference(bits):
    words, entry, da, hop = _common_build(with_iq=False)
    rrc_q15, _ = drive(words, entry, da, hop, bits)
    iq = np.array([complex(s16(w) / 32768.0, 0.0) for w in rrc_q15])
    ref = IQUpconvertBlock("iq", sample_rate=SAMP, frequency=float(FW)).process_reference(iq)
    return [s16(int(v) & 0xFFFF) for v in ref]


def main():
    bits = [0, 1, 1, 0, 1, 0, 0, 1]
    words, entry, da, hop = _common_build(with_iq=True)
    flat, per = drive(words, entry, da, hop, bits)
    counts = [len(g) for g in per]
    rate_ok = all(c == SPS for c in counts)
    got = [s16(x) for x in flat]
    ref = reference(bits)
    n = min(len(got), len(ref))
    diffs = [abs(got[i] - ref[i]) for i in range(n)]
    maxd = max(diffs) if diffs else 99999
    val_ok = (len(got) == len(ref)) and maxd <= TOL
    print(f"AUTO-PLACE TX chain: {len(bits)} bits -> {len(flat)} samples "
          f"(expect {SPS*len(bits)}); per-trigger={counts}")
    print(f"  rate_ok={rate_ok} len(got)={len(got)} len(ref)={len(ref)} "
          f"max_abs_diff={maxd} (tol {TOL}) value_ok={val_ok}")
    if not val_ok:
        print("  GOT:", got[:16]); print("  REF:", ref[:16])
    print("RESULT:", "PASS" if (rate_ok and val_ok) else "FAIL")
    return 0 if (rate_ok and val_ok) else 1


def test_tx_chain_autoplace_full_rate():
    """The BPSK TX chain DROPPED ANYWHERE -> auto_place -> bus-route -> build ->
    full-rate value-exact passband (the folded-block auto-P&R fix)."""
    assert main() == 0


if __name__ == "__main__":
    sys.exit(main())
