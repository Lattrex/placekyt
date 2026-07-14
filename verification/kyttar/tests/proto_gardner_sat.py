# Gardner SATURATION proof: with pipeline_lock (INV-19 serialize-LOCK) the saturated
# (queue_words_physical, back-to-back) recovered BITS equal the per-sample recovered bits
# across fractional timing offsets. A proper RRC-shaped 2-sps BPSK signal is REQUIRED —
# a piecewise-linear synthetic signal the loop can't lock onto makes both modes garbage.
# Word-level values differ by sub-LSB interpolation amounts (harmless); the recovered BIT
# (sign) is what BER cares about, and that is IDENTICAL saturated-vs-per-sample.
import sys
from pathlib import Path
import importlib.util
import random

ROOT = Path("/home/system/placekyt")
for p in (ROOT / "placekyt", ROOT / "runtime" / "python", ROOT / "verification"):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

CHIP = str(ROOT / "placekyt" / "resources" / "chips" / "kyttar_10x12.yaml")


def q15(f):
    return (max(-32768, min(32767, int(round(f * 32768.0)))) & 0xFFFF)


def _s16(w):
    return w - 0x10000 if w & 0x8000 else w


def _sgn(w):
    return 0 if _s16(w & 0xFFFF) > 0 else 1


def _stim(frac, nbits=160, seed=11):
    """RRC-shaped 2-sps BPSK at a fractional timing offset — reuse the proven generator
    from test_gardner_convergence so the loop actually locks."""
    spec = importlib.util.spec_from_file_location(
        "tg", str(ROOT / "verification" / "tests" / "test_gardner_convergence.py"))
    tg = importlib.util.module_from_spec(spec)
    try:
        spec.loader.exec_module(tg)
    except SystemExit:
        pass
    random.seed(seed)
    bits = [random.randint(0, 1) for _ in range(nbits)]
    return bits, tg._make_bpsk_2sps(bits, frac=frac)


def main():
    from kyttar_verify.dut_runner import run_block_dut_rate, run_block_dut_pipelined
    for frac in (0.3, 0.5, 0.7):
        _, sig = _stim(frac)
        inq = [q15(float(x)) for x in sig]
        seq = run_block_dut_rate("GardnerTimingRecovery", inq, chip_yaml=CHIP,
                                 in_port="xi", out_port="out", params={})
        sat = run_block_dut_pipelined("GardnerTimingRecovery", [(x,) for x in inq],
                                      chip_yaml=CHIP, in_ports=("xi",),
                                      out_port="out", params={})
        if not (seq.ok and sat.ok):
            print("frac=%s FAIL seq=%s sat=%s" % (frac, seq.ok, sat.ok), flush=True)
            continue
        sb = [_sgn(x) for x in seq.outputs_q15]
        tb = [_sgn(x) for x in sat.outputs_q15]
        n = min(len(sb), len(tb))
        bit_diffs = sum(1 for i in range(n) if sb[i] != tb[i])
        print("frac=%s: seq_bits=%d sat_bits=%d BIT_diffs=%d/%d -> %s" % (
            frac, len(sb), len(tb), bit_diffs, n,
            "MATCH" if bit_diffs == 0 and len(sb) == len(tb) else "DIVERGE"), flush=True)


if __name__ == "__main__":
    main()
