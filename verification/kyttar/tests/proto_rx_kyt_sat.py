# Step 4: drive the SHIPPED coherent BPSK RX .kyt (the 4-block point-to-point-routed
# receiver: RRC-MF -> ComplexCostasLoop -> GardnerTimingRecovery(closed loop) ->
# BPSKSlicer) end-to-end, per-sample vs SATURATED. This is the hand-routed layout CM
# fixed to point-to-point (no shared bus => no routing-cell congestion/lockup). Loads
# the .kyt via open_project, builds it, injects at the BUILT corridor-accurate landing.
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
KYT = str(ROOT / "examples" / "coherent_bpsk_rx" / "coherent_bpsk_rx.kyt")


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


def _build_from_kyt():
    from kyttar_verify.dut_runner import _engine
    (app, BlockCatalog, load_chip_type, BuildEngine, AppController,
     ChipPortEndpoint, BlockEndpoint) = _engine()
    cat = BlockCatalog.from_gr_kyttar()
    ct = load_chip_type(CHIP); ct_key = getattr(ct, "name", None) or "kyttar_10x12"
    ctrl = AppController(catalog=cat)
    ctrl.open_project(KYT)
    bres = BuildEngine(cat, CHIP).build(ctrl.project, {ct_key: ct})
    assert bres.ok, ("build failed: %s" % bres.errors)

    # Corridor-accurate injection landing for the MF head (nets net5=xi, net6=xq).
    cb = bres.chips[0]
    landings = cb.input_landings
    # net5 -> MF.xi (R0), net6 -> MF.xq (R1); both land at the same corridor cell.
    land_i = landings["net5"]
    entry = int(land_i["entry"]); hop = int(land_i["hop"]) & 0x1F
    das = land_i.get("data_addrs") or [0, 1]
    a0 = int(das[0]); a1 = int(das[1]) if len(das) > 1 else int(das[0]) + 1
    return dict(words=bres.words(0), entry=entry, a0=a0, a1=a1, hop=hop,
                landings={k: dict(v) for k, v in landings.items()})


def _drain_bits(chip):
    return [int(v) & 1 for v, d, t in chip.read_port_words_timed("x16_out")]


def _ber(rx, tx):
    best = 1.0
    for lag in range(0, 24):
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
        chip.inject_data_physical([_f2q15(i_f)], target_hop_cnt=info["hop"], target_addr=info["a0"]); chip.run(max_events=8000)
        chip.inject_data_physical([_f2q15(q_f)], target_hop_cnt=info["hop"], target_addr=info["a1"]); chip.run(max_events=8000)
        chip.inject_jump_physical(target_hop_cnt=info["hop"], entry_addr=info["entry"]); chip.run(max_events=300000)
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
    info = _build_from_kyt()
    print("KYT build: entry=%d a0=%d a1=%d hop=%d words=%d" % (
        info["entry"], info["a0"], info["a1"], info["hop"], len(info["words"])), flush=True)
    print("landings:", {k: {kk: v[kk] for kk in ("cell", "entry", "hop", "data_addrs") if kk in v}
                        for k, v in info["landings"].items()}, flush=True)
    bits, iq = _gen_iq(120)
    for name, fn in (("PER-SAMPLE", run_persample), ("PIPELINED", run_pipelined)):
        chip = fn(info, iq)
        rb = _drain_bits(chip)
        print("%s: bits=%d BER=%.4f sim=%.0fus" % (
            name, len(rb), _ber(rb, bits), chip.simulation_time / 1e3), flush=True)


if __name__ == "__main__":
    main()
