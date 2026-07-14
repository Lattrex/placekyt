"""Trace the N=2 deadlock: run 2 saturated samples, dump the LAST 30 trace events
and per-cell exec counts so we can see WHERE the pipeline jams after sample 2."""
import os, sys
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", ".."))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "..", "placekyt"))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "..", "runtime", "python"))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

import simkyt
from collections import Counter
from kyttar_verify.dut_runner import _enc_write, _enc_jump, _engine

CHIP = os.path.join(os.path.dirname(__file__), "..", "..", "..",
                    "placekyt", "resources", "chips", "kyttar_10x12.yaml")


def q15(f):
    return (max(-32768, min(32767, int(round(f * 32768)))) & 0xFFFF)


(app, BC, lct, BE, AC, CPE, BPE) = _engine()
cat = BC.from_gr_kyttar()
ct = lct(CHIP)
k = getattr(ct, "name", None) or "kyttar_10x12"
c = AC(catalog=cat)
c.new_project("p", k)
blk = c.place_block("ComplexMixerBlock", 0, 1, 1, params={"pipeline_lock": True})
for i, ip in enumerate(("xi", "xq")):
    c.add_logical_connection(CPE(chip=0, port="x16_in"),
                             BPE(block=blk, port=ip), name="in%d" % i)
c.add_logical_connection(BPE(block=blk, port="yi"),
                         CPE(chip=0, port="x16_out"), name="out")
c.auto_route_all({k: ct})
br = BE(cat, CHIP).build(c.project, {k: ct})
words = br.words(0)
entry, ins = cat.resolved_io("ComplexMixerBlock", {"pipeline_lock": True})
addrs = [int(a) for a in ins[:2]]
pl = c.project.block(blk).placement
W = getattr(ct, "width", 10)
idmap = {(cc.x, cc.y): cc.cell_id for cc in pl.cells}
tr_names = {(t.x, t.y): "transit" for t in getattr(pl, "transit_cells", [])}
idmap.update(tr_names)

port = ct.port("x16_in")
px, py = next((cc.x, cc.y) for cc in pl.cells if cc.cell_id == "phase")
dist = abs(px - port.cell_x) + abs(py - port.cell_y) + 1
hop = max(0, 31 - dist)

N = 2
samp = [(q15(0.10), q15(0.40)), (q15(-0.20), q15(0.55)), (q15(0.30), q15(0.25))][:N]
chip = simkyt.Chip.from_yaml(CHIP)
chip.load_bitstream_physical(words)
chip.set_port_entry_address("x16_in", entry)
st = []
for (i, q) in samp:
    st += [_enc_write(hop, addrs[0]), i, _enc_write(hop, addrs[1]), q,
           _enc_jump(hop, entry)]
chip.queue_words_physical("x16_in", st)
chip.enable_trace()
res = chip.run(max_events=8000)
tr = chip.get_trace()


def name_of(cidx):
    if cidx is None:
        return "host"
    x, y = cidx % W, cidx // W
    return "%s@(%d,%d)" % (idmap.get((x, y), "?"), x, y)


execs = Counter()
for t in tr:
    if t.get("kind") == "exec_tick":
        execs[name_of(t.get("cell_id"))] += 1
print("completed=%s stop=%s events=%s" % (
    res.get("completed"), res.get("stop_reason"), res.get("events_processed")), flush=True)
print("exec_tick per cell:", dict(execs), flush=True)
out = [int(v) & 0xFFFF for (v, _d, _t) in chip.read_port_words_timed("x16_out")]
print("outwords:", out, flush=True)
print("--- last 30 events ---", flush=True)
for t in tr[-30:]:
    print("  t=%.1f %s %s dest=%s hop=%s" % (
        t.get("time_ns") or 0, name_of(t.get("cell_id")), t.get("kind"),
        t.get("dest"), t.get("target_hop")), flush=True)
print("pending_events =", chip.pending_events, flush=True)
