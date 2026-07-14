# Reproduce the Gardner saturation failure: per-sample (run_block_dut_rate) vs saturated
# (run_block_dut_pipelined) over a 2-sps BPSK stream. Per-sample should recover the symbols
# (loop converges, KB says BER 0); saturated is expected to collapse (few strobes) UNTIL the
# INV-19 arbiter-LOCK fix. Prints strobe count + output length for both.
import sys
from pathlib import Path
ROOT = Path("/home/system/placekyt")
for p in (ROOT / "placekyt", ROOT / "runtime" / "python", ROOT / "verification"):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

import math
from kyttar_verify.dut_runner import run_block_dut_rate, run_block_dut_pipelined

CHIP = str(ROOT / "placekyt" / "resources" / "chips" / "kyttar_10x12.yaml")


def q15(f):
    return (max(-32768, min(32767, int(round(f * 32768.0)))) & 0xFFFF)


def gen_2sps(nsym, offset=0.4, seed=3):
    """Simple 2-samples-per-symbol BPSK with a fractional timing offset — a linear
    interpolation between symbols so the Gardner TED has a real error to null."""
    import random
    random.seed(seed)
    bits = [random.randint(0, 1) for _ in range(nsym)]
    syms = [1.0 if b == 0 else -1.0 for b in bits]
    # upsample x2 with a fractional offset: sample the piecewise-linear symbol train
    xs = []
    for n in range(2 * nsym):
        t = (n - offset) / 2.0            # symbol-time index
        i0 = int(math.floor(t))
        fr = t - i0
        a = syms[i0] if 0 <= i0 < nsym else 0.0
        b = syms[i0 + 1] if 0 <= i0 + 1 < nsym else 0.0
        xs.append(a * (1 - fr) + b * fr)
    return bits, xs


def main():
    NS = 60
    bits, xs = gen_2sps(NS)
    stim = [q15(x) for x in xs]

    seq = run_block_dut_rate("GardnerTimingRecovery", stim, chip_yaml=CHIP,
                             in_port="xi", out_port="out", params={})
    seq_flat = list(seq.outputs_q15) if seq.ok else None
    print("PER-SAMPLE ok=%s nstrobe=%d out[:8]=%s" % (
        seq.ok, len(seq_flat) if seq_flat else 0,
        seq_flat[:8] if seq_flat else str(seq.reason)[:60]), flush=True)

    sat = run_block_dut_pipelined("GardnerTimingRecovery", [(s,) for s in stim],
                                  chip_yaml=CHIP, in_ports=("xi",), out_port="out",
                                  params={})
    print("SATURATED  ok=%s nstrobe=%s out[:8]=%s" % (
        sat.ok, len(sat.outputs_q15) if sat.ok else "-",
        sat.outputs_q15[:8] if sat.ok else str(sat.reason)[:90]), flush=True)

    if seq.ok and sat.ok:
        n = len(seq_flat)
        print("MATCH=%s (seq=%d sat=%d strobes)" % (
            sat.outputs_q15[:n] == seq_flat and len(sat.outputs_q15) >= n,
            n, len(sat.outputs_q15)), flush=True)


if __name__ == "__main__":
    main()
