# Prove ComplexMixerBlock (pipeline_lock=True) runs SATURATED to quiescence with
# bit-exact output == per-sample, mirroring proto_costas_pipe's driving style
# (place at 0,0, queue whole burst, UNBOUNDED c.run()). If this quiesces, the block
# is pipeline-saturation-capable; if it livelocks, the lock re-triggers phase.
import sys
from pathlib import Path

ROOT = Path("/home/system/placekyt")
for p in (ROOT / "placekyt", ROOT / "runtime" / "python", ROOT / "verification"):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

import simkyt  # noqa: E402

CHIP = str(ROOT / "placekyt" / "resources" / "chips" / "kyttar_10x12.yaml")


def _f2q15(f):
    q = max(-32768, min(32767, int(round(f * 32768.0))))
    return q & 0xFFFF


def _build(place):
    from kyttar_verify.dut_runner import _engine
    (app, BlockCatalog, load_chip_type, BuildEngine, AppController,
     ChipPortEndpoint, BlockEndpoint) = _engine()
    cat = BlockCatalog.from_gr_kyttar()
    ct = load_chip_type(CHIP); k = getattr(ct, "name", None) or "kyttar_10x12"
    ctrl = AppController(catalog=cat); ctrl.new_project("cm", k)
    blk = ctrl.place_block("ComplexMixerBlock", 0, place[0], place[1],
                           library="lattrex.official", params={"pipeline_lock": True})
    ctrl.add_logical_connection(ChipPortEndpoint(chip=0, port="x16_in"),
                                BlockEndpoint(block=blk, port="xi"), name="i")
    ctrl.add_logical_connection(ChipPortEndpoint(chip=0, port="x16_in"),
                                BlockEndpoint(block=blk, port="xq"), name="q")
    ctrl.add_logical_connection(BlockEndpoint(block=blk, port="yi"),
                                ChipPortEndpoint(chip=0, port="x16_out"), name="o")
    rep = ctrl.auto_route_all({k: ct}); assert rep.ok, rep
    bres = BuildEngine(cat, CHIP).build(ctrl.project, {k: ct}); assert bres.ok, bres.errors
    entry, ins = cat.resolved_io("ComplexMixerBlock", {"pipeline_lock": True},
                                 library="lattrex.official")
    a0, a1 = int(ins[0]), int(ins[1])
    port = ct.port("x16_in")
    bo = ctrl.project.block(blk); land = bo.placement.cells[0]
    hop = max(0, 31 - (abs(land.x - port.cell_x) + abs(land.y - port.cell_y) + 1))
    return dict(words=bres.words(0), entry=entry, a0=a0, a1=a1, hop=hop)


def run_pipe(info, iq, cap=None):
    c = simkyt.Chip.from_yaml(CHIP); c.load_bitstream_physical(info["words"])
    c.set_port_entry_address("x16_in", info["entry"])
    e, a0, a1, h = info["entry"], info["a0"], info["a1"], info["hop"]
    def w(a): return (0x6 << 12) | ((h & 0x1F) << 5) | (a & 0x1F)
    def j(): return (0x7 << 12) | ((h & 0x1F) << 5) | (e & 0x1F)
    words = []
    for i_f, q_f in iq:
        words += [w(a0), _f2q15(i_f), w(a1), _f2q15(q_f), j()]
    c.queue_words_physical("x16_in", words)
    res = c.run(max_events=cap) if cap else c.run()
    out = [int(v) & 0xFFFF for v, d, t in c.read_port_words_timed("x16_out")]
    return res, out


def main():
    iq = [(0.10, 0.40), (-0.20, 0.55), (0.30, 0.25), (-0.15, 0.35)]
    for place in ((0, 0), (1, 1)):
        info = _build(place)
        res, out = run_pipe(info, iq, cap=60000)
        comp = res.get("completed") if isinstance(res, dict) else True
        print("place=%s completed=%s stop=%s nout=%d out=%s" % (
            place, comp, (res or {}).get("stop_reason"), len(out), out[:8]), flush=True)


if __name__ == "__main__":
    main()
