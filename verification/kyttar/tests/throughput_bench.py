# Throughput / latency / saturation benchmark for the coherent BPSK RX (or any
# built .kyt), driven SATURATED via queue_words_physical. Reports honest CHIP-TIME
# numbers from the cycle-accurate trace (simKYT's GLS timing), NOT host wall-clock:
#
#   • INGRESS: inter-sample gap at the input port. If min << mean, the port is NOT
#     the limiter — it's backpressured (single-outstanding, no FIFO) by the slowest
#     downstream block. A saturated input would show a ~constant gap == the port's
#     minimum accept interval.
#   • EGRESS: steady-state recovered-symbol rate + fill latency (first sample in ->
#     first bit out = pipeline depth).
#   • BOTTLENECK: the busiest cell (most exec_ticks) — the block that actually caps
#     throughput. For the coherent RX this is the Gardner resampler.
#
# Run: python verification/kyttar/tests/throughput_bench.py [nsym]
import sys
import statistics
from collections import Counter
from pathlib import Path

ROOT = Path("/home/system/placekyt")
for p in (ROOT / "placekyt", ROOT / "runtime" / "python", ROOT / "verification"):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

import importlib.util  # noqa: E402
import simkyt  # noqa: E402
from engine.sim_bridge import _float_to_q15  # noqa: E402

_S = importlib.util.spec_from_file_location(
    "rxsp", str(ROOT / "verification" / "kyttar" / "tests" / "proto_rx_server_pipelined.py"))
_rx = importlib.util.module_from_spec(_S)
_S.loader.exec_module(_rx)


def _w(h, a):
    return (0x6 << 12) | ((h & 0x1F) << 5) | (a & 0x1F)


def _j(h, e):
    return (0x7 << 12) | ((h & 0x1F) << 5) | (e & 0x1F)


def bench(nsym=120):
    chip, entry, hop = _rx._build_chip()
    chip.enable_trace(8_000_000)
    bits, inter = _rx._gen_iq(nsym)
    words = []
    for k in range(nsym):
        words += [_w(hop, 0), _float_to_q15(float(inter[2 * k])),
                  _w(hop, 1), _float_to_q15(float(inter[2 * k + 1])), _j(hop, entry)]
    chip.queue_words_physical("x16_in", words)
    chip.run(max_events=8_000_000)
    evs = chip.get_trace()

    pin = sorted(float(e["time_ns"]) for e in evs
                 if e.get("kind") == "port_injection" and e.get("port_name") == "x16_in")
    samp_in = pin[4::5]  # each 5-word packet's JUMP = one complex sample landed
    in_gaps = [samp_in[i + 1] - samp_in[i] for i in range(len(samp_in) - 1)]
    outs = sorted(float(e["time_ns"]) for e in evs
                  if e.get("kind") == "port_capture" and e.get("port_name") == "x16_out")
    out_gaps = [outs[i + 1] - outs[i] for i in range(len(outs) - 1)]
    exec_by_cell = Counter(e.get("cell_id") for e in evs if e.get("kind") == "exec_tick")

    print("=== INGRESS (input port accept) ===")
    print("  samples in: %d   span: %.0f ns" % (len(samp_in), samp_in[-1] - samp_in[0]))
    print("  inter-sample gap: min=%.0f mean=%.0f max=%.0f stdev=%.0f ns"
          % (min(in_gaps), statistics.mean(in_gaps), max(in_gaps), statistics.pstdev(in_gaps)))
    print("  actual ingress = %.3f MSa/s   |   port CAPACITY (min gap) = %.3f MSa/s"
          % (1e9 / statistics.mean(in_gaps) / 1e6, 1e9 / min(in_gaps) / 1e6))
    print("  SATURATED? %s (min<<mean => backpressured by chain, not port-limited)"
          % ("NO" if statistics.mean(in_gaps) > 1.5 * min(in_gaps) else "yes"))
    print("=== EGRESS (recovered symbols) ===")
    if len(outs) > 2:
        print("  symbols out: %d   fill latency: %.0f ns" % (len(outs), outs[0] - samp_in[0]))
        print("  steady output gap: mean=%.0f ns  => %.4f MSym/s"
              % (statistics.mean(out_gaps), 1e9 / statistics.mean(out_gaps) / 1e6))
    print("=== BOTTLENECK ===")
    for cid, c in exec_by_cell.most_common(4):
        print("  cell (%d,%d) id=%d: %d exec_ticks" % (cid % 10, cid // 10, cid, c))
    n_exec = sum(exec_by_cell.values())
    print("  aggregate: %d exec_ticks / %.0f ns = %.1f M exec/s"
          % (n_exec, chip.simulation_time, n_exec / (chip.simulation_time / 1e9) / 1e6))
    print("  total sim time: %.1f us" % (chip.simulation_time / 1e3))


if __name__ == "__main__":
    bench(int(sys.argv[1]) if len(sys.argv) > 1 else 120)
