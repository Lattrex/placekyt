# Step 4: the 4-block coherent BPSK RX chain (x16_in -> ComplexCostasLoop -> Gardner
# closed-loop -> BPSKSlicer -> x16_out), auto-placed + auto-routed + built by placeKYT,
# driven END-TO-END under TRUE SATURATION. This is the chain that actually exercises the
# newly-fixed CLOSED-loop Gardner (pipeline_lock=True). Compare per-sample vs pipelined
# recovered bits + BER. (proto_rx_bisect drives the FUSED CoherentRXBlock, whose Gardner is
# open-loop; THIS proto drives the 3 SEPARATE catalog blocks so the closed loop runs.)
import sys
from pathlib import Path

ROOT = Path("/home/system/placekyt")
for p in (ROOT / "placekyt", ROOT / "runtime" / "python", ROOT / "verification"):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

import math      # noqa: E402
import random    # noqa: E402
import simkyt    # noqa: E402

CHIP = str(ROOT / "placekyt" / "resources" / "chips" / "kyttar_10x12.yaml")


def _rrc(beta, sps, span):
    n = span * sps
    taps = []
    for i in range(n + 1):
        t = (i - n / 2) / sps
        if abs(t) < 1e-8:
            v = 1 - beta + 4 * beta / math.pi
        elif abs(abs(4 * beta * t) - 1.0) < 1e-8:
            v = (beta / math.sqrt(2)) * ((1 + 2 / math.pi) * math.sin(math.pi / (4 * beta))
                                         + (1 - 2 / math.pi) * math.cos(math.pi / (4 * beta)))
        else:
            num = (math.sin(math.pi * t * (1 - beta)) + 4 * beta * t * math.cos(math.pi * t * (1 + beta)))
            den = math.pi * t * (1 - (4 * beta * t) ** 2)
            v = num / den
        taps.append(v)
    e = math.sqrt(sum(v * v for v in taps))
    return [v / e for v in taps]


def _gen_iq(nsym, foff=0.008, toff=0.45, seed=5):
    random.seed(seed)
    bits = [random.randint(0, 1) for _ in range(nsym)]
    taps = _rrc(0.35, 2, 6)
    syms = [1.0 if b == 0 else -1.0 for b in bits]
    up = []
    for s in syms:
        up.append(s); up.extend([0.0])
    shaped = []
    for n in range(len(up)):
        acc = 0.0
        for k in range(len(taps)):
            if 0 <= n - k < len(up):
                acc += taps[k] * up[n - k]
        shaped.append(acc)
    out = []
    for n in range(len(shaped) - 1):
        i = n + int(math.floor(toff)); frac = toff - math.floor(toff)
        if 0 <= i < len(shaped) - 1:
            out.append(shaped[i] * (1 - frac) + shaped[i + 1] * frac)
        else:
            out.append(shaped[n])
    iq = []
    for n, s in enumerate(out):
        iq.append((s * math.cos(2 * math.pi * foff * n), s * math.sin(2 * math.pi * foff * n)))
    return bits, iq


def _f2q15(f):
    q = max(-32768, min(32767, int(round(f * 32768.0))))
    return q & 0xFFFF


def _build_chain():
    """Place Costas -> Gardner(closed-loop) -> Slicer, auto-route, build. Return drive info."""
    from kyttar_verify.dut_runner import _engine
    (app, BlockCatalog, load_chip_type, BuildEngine, AppController,
     ChipPortEndpoint, BlockEndpoint) = _engine()
    cat = BlockCatalog.from_gr_kyttar()
    ct = load_chip_type(CHIP); ct_key = getattr(ct, "name", None) or "kyttar_10x12"
    ctrl = AppController(catalog=cat); ctrl.new_project("rxchain", ct_key)
    LIB = "lattrex.official"

    # place_block(type, chip, x, y): all on chip 0, distinct x
    costas = ctrl.place_block("ComplexCostasLoopBlock", 0, 0, 0, library=LIB, params={})
    # closed-loop Gardner with the serialize-LOCK fix ON (the point of this proto)
    gard = ctrl.place_block("GardnerTimingRecovery", 0, 4, 0, library=LIB,
                            params={"pipeline_lock": True})
    slic = ctrl.place_block("BPSKSlicerBlock", 0, 8, 0, library=LIB, params={})

    # x16_in -> Costas (xi, xq)
    ctrl.add_logical_connection(ChipPortEndpoint(chip=0, port="x16_in"),
                                BlockEndpoint(block=costas, port="xi"), name="i")
    ctrl.add_logical_connection(ChipPortEndpoint(chip=0, port="x16_in"),
                                BlockEndpoint(block=costas, port="xq"), name="q")
    # Costas.yi_tap -> Gardner.xi
    ctrl.add_logical_connection(BlockEndpoint(block=costas, port="yi_tap"),
                                BlockEndpoint(block=gard, port="xi"), name="cg")
    # Gardner.out -> Slicer.llr
    ctrl.add_logical_connection(BlockEndpoint(block=gard, port="out"),
                                BlockEndpoint(block=slic, port="llr"), name="gs")
    # Slicer.out -> x16_out
    ctrl.add_logical_connection(BlockEndpoint(block=slic, port="out"),
                                ChipPortEndpoint(chip=0, port="x16_out"), name="o")

    # Real auto-P&R (the placer the shipped demo uses) — re-places compactly + routes,
    # perturbing on a routing failure. This is what gives the fan-in nets their brokers.
    rep = ctrl.auto_pnr({ct_key: ct}, chip=0)
    assert rep.ok, ("auto_pnr failed: %s" % rep)
    bres = BuildEngine(cat, CHIP).build(ctrl.project, {ct_key: ct})
    assert bres.ok, ("build failed: %s" % bres.errors)

    # CORRIDOR-ACCURATE injection landing from the BUILT chip (NOT a manhattan
    # estimate — after auto_pnr the corridor snakes + a broker delivers into a
    # non-corner input cell; manhattan consumes the JUMP a cell short → no output).
    # input_landings is keyed by connection name; nets "i" and "q" both feed the
    # Costas head (xi @ its data_addr[0], xq @ data_addr[1]).
    cb = bres.chips[0]
    land_i = cb.input_landings["i"]
    # Corridor-accurate entry/hop; xi->R0, xq->R1 (Costas input_registers=[0,1]).
    entry = int(land_i["entry"]); hop = int(land_i["hop"]) & 0x1F
    a0, a1 = 0, 1
    return dict(words=bres.words(0), entry=entry, a0=a0, a1=a1, hop=hop)


def _drain_bits(chip):
    return [int(v) & 1 for v, d, t in chip.read_port_words_timed("x16_out")]


def _ber(rx, tx):
    best = 1.0
    for lag in range(0, 20):
        a = rx[lag:]
        for cand in (a, [1 - x for x in a]):
            b = tx[:len(cand)]; m = min(len(cand), len(b))
            if m < 40:
                continue
            best = min(best, sum(1 for i in range(30, m) if cand[i] != b[i]) / (m - 30))
    return best


def run_persample(info, iq):
    chip = simkyt.Chip.from_yaml(CHIP); chip.load_bitstream_physical(info["words"])
    chip.set_port_entry_address("x16_in", info["entry"])
    for (i_f, q_f) in iq:
        chip.inject_data_physical([_f2q15(i_f)], target_hop_cnt=info["hop"], target_addr=info["a0"]); chip.run(max_events=6000)
        chip.inject_data_physical([_f2q15(q_f)], target_hop_cnt=info["hop"], target_addr=info["a1"]); chip.run(max_events=6000)
        chip.inject_jump_physical(target_hop_cnt=info["hop"], entry_addr=info["entry"]); chip.run(max_events=200000)
    return chip


def run_pipelined(info, iq):
    chip = simkyt.Chip.from_yaml(CHIP); chip.load_bitstream_physical(info["words"])
    chip.set_port_entry_address("x16_in", info["entry"])
    def enc_w(h, a): return (0x6 << 12) | ((h & 0x1F) << 5) | (a & 0x1F)
    def enc_j(h, e): return (0x7 << 12) | ((h & 0x1F) << 5) | (e & 0x1F)
    words = []
    h, a0, a1, e = info["hop"], info["a0"], info["a1"], info["entry"]
    for (i_f, q_f) in iq:
        words += [enc_w(h, a0), _f2q15(i_f), enc_w(h, a1), _f2q15(q_f), enc_j(h, e)]
    chip.queue_words_physical("x16_in", words); chip.run()
    return chip


def main():
    info = _build_chain()
    print("CHAIN build: entry=%d a0=%d a1=%d hop=%d words=%d" % (
        info["entry"], info["a0"], info["a1"], info["hop"], len(info["words"])), flush=True)
    bits, iq = _gen_iq(120)
    for name, fn in (("PER-SAMPLE", run_persample), ("PIPELINED", run_pipelined)):
        chip = fn(info, iq)
        rb = _drain_bits(chip)
        print("%s: bits=%d BER=%.4f sim=%.0fus" % (
            name, len(rb), _ber(rb, bits), chip.simulation_time / 1e3), flush=True)


if __name__ == "__main__":
    main()
