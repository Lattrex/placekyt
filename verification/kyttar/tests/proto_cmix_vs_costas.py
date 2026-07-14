# Side-by-side: drive Costas and ComplexMixer with the IDENTICAL saturated burst and
# count phase exec_ticks. Costas quiesces (phase fires ~N); mixer livelocks (phase fires
# many). This isolates WHAT re-triggers the mixer's phase after inputs dry up.
import sys
from pathlib import Path
from collections import Counter

ROOT = Path("/home/system/placekyt")
for p in (ROOT / "placekyt", ROOT / "runtime" / "python", ROOT / "verification"):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

import simkyt  # noqa: E402

CHIP = str(ROOT / "placekyt" / "resources" / "chips" / "kyttar_10x12.yaml")


def _f2q15(f):
    return (max(-32768, min(32767, int(round(f * 32768.0)))) & 0xFFFF)


def build(blkname, params, inports):
    from kyttar_verify.dut_runner import _engine
    (app, BC, lct, BE, AC, CPE, BPE) = _engine()
    cat = BC.from_gr_kyttar(); ct = lct(CHIP); k = "kyttar_10x12"
    c = AC(catalog=cat); c.new_project("p", k)
    blk = c.place_block(blkname, 0, 0, 0, params=params)
    for i, ip in enumerate(inports):
        c.add_logical_connection(CPE(chip=0, port="x16_in"), BPE(block=blk, port=ip), name="i%d" % i)
    c.add_logical_connection(BPE(block=blk, port="yi"), CPE(chip=0, port="x16_out"), name="o")
    c.auto_route_all({k: ct}); br = BE(cat, CHIP).build(c.project, {k: ct})
    entry, ins = cat.resolved_io(blkname, params)
    a0, a1 = int(ins[0]), int(ins[1])
    pl = c.project.block(blk).placement
    W = getattr(ct, "width", 10)
    idmap = {(cc.x, cc.y): cc.cell_id for cc in pl.cells}
    port = ct.port("x16_in"); land = pl.cells[0]
    hop = max(0, 31 - (abs(land.x - port.cell_x) + abs(land.y - port.cell_y) + 1))
    return dict(words=br.words(0), entry=entry, a0=a0, a1=a1, hop=hop, idmap=idmap, W=W)


def drive(info, iq, nsamp, cap=40000):
    c = simkyt.Chip.from_yaml(CHIP); c.load_bitstream_physical(info["words"])
    c.set_port_entry_address("x16_in", info["entry"])
    e, a0, a1, h = info["entry"], info["a0"], info["a1"], info["hop"]
    def w(a): return (0x6 << 12) | ((h & 0x1F) << 5) | (a & 0x1F)
    def j(): return (0x7 << 12) | ((h & 0x1F) << 5) | (e & 0x1F)
    words = []
    for (i_f, q_f) in iq[:nsamp]:
        words += [w(a0), _f2q15(i_f), w(a1), _f2q15(q_f), j()]
    c.queue_words_physical("x16_in", words); c.enable_trace()
    res = c.run(max_events=cap); tr = c.get_trace()
    W = info["W"]; idmap = info["idmap"]
    ex = Counter()
    for t in tr:
        if t.get("kind") == "exec_tick":
            ci = t.get("cell_id")
            ex[idmap.get((ci % W, ci // W), "?")] += 1
    comp = res.get("completed") if isinstance(res, dict) else True
    return comp, ex.get("phase", 0), dict(ex)


IQ = [(0.10, 0.40), (-0.20, 0.55), (0.30, 0.25), (-0.15, 0.35)]
for name, params, inports in (
        ("ComplexCostasLoopBlock", {}, ("xi", "xq")),
        ("ComplexMixerBlock", {"pipeline_lock": True}, ("xi", "xq"))):
    info = build(name, params, inports)
    for n in (1, 2, 4):
        comp, ph, ex = drive(info, IQ, n)
        print("%-24s N=%d completed=%s phase_execs=%d" % (name, n, comp, ph), flush=True)
