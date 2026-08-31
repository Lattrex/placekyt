# SPDX-License-Identifier: GPL-3.0-or-later
"""Field-oriented control (FOC) current loop on one Kyttar array — the
placement, the build, the drive, and the LOOP-RATE MEASUREMENT.

WHAT IS ON THE CHIP
-------------------
The torque-producing half of a PMSM field-oriented controller::

    e_d ──> PI(d) ──┐
                     ├──> CordicRotate(sign=+1, theta)  ──> SVPWM ──> duty a,b,c
    e_q ──> PI(q) ──┘            (inverse Park)
    theta ───────────────────────^

``e_d`` / ``e_q`` are the d- and q-axis current errors (reference minus
measured, formed host-side), ``theta`` is the rotor electrical angle in the
shipped 16-bit half-turn convention (word/32768 * pi radians, so plain 16-bit
wrap IS arithmetic mod 2*pi). The two PI controllers produce the voltage
command (v_d, v_q) in the rotor frame; the inverse Park rotation carries it
back to the stationary frame as (v_alpha, v_beta); SVPWM turns that into the
three inverter duty cycles by min-max (common-mode) injection.

WHAT IS NOT ON THE CHIP, AND WHY THIS FILE SAYS SO PLAINLY
-----------------------------------------------------------
The Clarke transform and the forward Park rotation — the measurement side of
the loop — are NOT in this build. They are shipped, chip-proven blocks
(``ClarkeTransformBlock``, ``CordicRotateBlock`` with ``sign=-1``), and the
front-half sub-chain ``Clarke -> CordicRotate`` routes on its own. What does
not fit is the WHOLE loop on ONE 10x12 array: the limit is corridor/face
budget, not cells. See ``README.md`` and INV-71 for the measurement (55 block
cells of 120, and the best whole-chain placement over ~2600 candidates still
left 2 of 13 nets unrouted; the failures are always rendezvous-arm corridors).

THE MEASUREMENT THIS FILE EXISTS FOR
------------------------------------
Control latency, the packet cadence, and the SUSTAINED iteration rate, from the
simulator's own timing model via ``read_port_words_timed`` (each word carries
its capture time in ns) and ``performance_report()``. These are SIMULATED times
from simKYT's timing model — they are not silicon-certified numbers.

The chain STREAMS: consecutive iterations with different inputs are each
bit-exact, at a measured 55.8 kHz. The sustained interval essentially equals the
first packet's latency, because each rendezvous bars its arms until the current
group has cleared — one iteration is in flight at a time, so the chain re-arms
rather than pipelines.

Run::

    QT_QPA_PLATFORM=offscreen .venv/bin/python examples/foc_motor/foc_motor_demo.py
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

_HERE = Path(__file__).resolve().parent
_ROOT = _HERE.parents[1]
for _p in (_ROOT / "placekyt", _ROOT / "runtime" / "python",
           _ROOT / "verification", _ROOT / "verification" / "tests", _HERE):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

CHIP_YAML = str(_ROOT / "placekyt" / "resources" / "chips" / "kyttar_10x12.yaml")
KYT_PATH = _HERE / "foc_motor.kyt"
LIB = "lattrex.official"

# The PI gains. Both axes share them (the standard symmetric current-loop
# tuning for a surface-PMSM, where Ld == Lq).
PI_PARAMS = {"kp": 0.25, "ki": 0.01, "limit": 1.0, "pipeline_lock": True}

# The hand-placed anchors. MEASURED, not chosen for looks: this set is one of
# very few that route AND deliver. auto_pnr re-packs the placement compactly
# and herds the three arm corridors together, which is exactly the INV-70
# head-of-line wedge; so the flow is place-by-anchor + auto_route_all
# (route-only), never auto_pnr.
#
# Each of the three ingress arms gets its OWN StreamSplitterBlock relay. That
# is load-bearing, not decorative: nets fanned straight out of the chip input
# port land on the PORT CELL (the INV-24 port-divert turn programs) and so all
# arrive on the SAME face, which a face-locking rendezvous gates — the chain
# then builds, routes, and emits NOTHING. See INV-71.
PLACEMENT = {
    "pi_d":  ("PIControllerBlock",  dict(PI_PARAMS),   (5, 8)),
    "pi_q":  ("PIControllerBlock",  dict(PI_PARAMS),   (2, 3)),
    "ipark": ("CordicRotateBlock",  {"sign": 1},       (5, 1)),
    "svpwm": ("SVPWMBlock",         {},                (1, 10)),
    "r_ed":  ("StreamSplitterBlock", {},               (0, 2)),
    "r_eq":  ("StreamSplitterBlock", {},               (1, 4)),
    "r_th":  ("StreamSplitterBlock", {},               (6, 7)),
}

# (source, target, net name). "PORTIN"/"PORTOUT" are the chip ports.
NETS = [
    ("PORTIN", ("r_ed", "x"), "e_d"),
    ("PORTIN", ("r_eq", "x"), "e_q"),
    ("PORTIN", ("r_th", "x"), "theta"),
    (("r_ed", "out"), ("pi_d", "sample"), "w_ed"),
    (("r_eq", "out"), ("pi_q", "sample"), "w_eq"),
    (("r_th", "out"), ("ipark", "theta"), "w_th"),
    (("pi_d", "out"), ("ipark", "x"), "vd"),
    (("pi_q", "out"), ("ipark", "y"), "vq"),
    (("ipark", "yi"), ("svpwm", "v_alpha"), "va"),
    (("ipark", "yq"), ("svpwm", "v_beta"), "vb"),
    (("svpwm", "out"), "PORTOUT", "duties"),
]

ARMS = ("e_d", "e_q", "theta")


# --------------------------------------------------------------------------- #
#  Build                                                                       #
# --------------------------------------------------------------------------- #

def _engine():
    from PySide6.QtWidgets import QApplication
    QApplication.instance() or QApplication([])
    from engine.catalog import BlockCatalog
    from engine.io.chip_type_io import load_chip_type
    from ui.controller import AppController
    from model.connection import ChipPortEndpoint, BlockEndpoint
    return BlockCatalog, load_chip_type, AppController, ChipPortEndpoint, BlockEndpoint


def place_route_build():
    """Place by the authored anchors, ROUTE-ONLY, build. Returns
    ``(project, bres, cat, ct)``."""
    BlockCatalog, load_chip_type, AppController, CPE, BE = _engine()
    cat = BlockCatalog.from_gr_kyttar()
    ct = load_chip_type(CHIP_YAML)
    ctk = getattr(ct, "name", None) or "kyttar_10x12"
    ctrl = AppController(catalog=cat)
    ctrl.new_project("foc_motor", ctk)
    keys = {}
    for name, (typ, params, (ax, ay)) in PLACEMENT.items():
        keys[name] = ctrl.place_block(typ, 0, ax, ay, library=LIB, params=dict(params))
    for src, dst, nm in NETS:
        s = CPE(chip=0, port="x16_in") if src == "PORTIN" else BE(block=keys[src[0]], port=src[1])
        d = CPE(chip=0, port="x16_out") if dst == "PORTOUT" else BE(block=keys[dst[0]], port=dst[1])
        ctrl.add_logical_connection(s, d, name=nm)
    rep = ctrl.auto_route_all({ctk: ct})
    if not rep.ok:
        raise RuntimeError("route failed: " + "; ".join(
            f"{r.name}:{r.reason}" for r in (rep.failed or [])))
    bres = ctrl.build()
    if not bres.ok:
        raise RuntimeError("build failed: " + "; ".join(
            str(e) for e in (bres.errors or [])[:5]))
    return ctrl.project, bres, cat, ct


def load_and_build(kyt_path=KYT_PATH):
    """Load the SHIPPED .kyt and build it — the path the hosted GUI runs."""
    from engine.build import BuildEngine
    from engine.catalog import BlockCatalog
    from engine.io.chip_type_io import load_chip_type
    from engine.io.project_io import load_project
    from PySide6.QtWidgets import QApplication
    QApplication.instance() or QApplication([])

    project = load_project(str(kyt_path))
    cat = BlockCatalog.from_gr_kyttar()
    ct = load_chip_type(CHIP_YAML)
    bres = BuildEngine(cat, CHIP_YAML).build(project, {project.chip_type: ct})
    if not bres.ok:
        raise RuntimeError("build failed: " + "; ".join(
            str(e) for e in (bres.errors or [])[:5]))
    return project, bres, cat, ct


def arm_landings(bres):
    """{arm: (hop, data_addr, entry)} for the three ingress arms.

    The three MUST differ in HOP, not merely in entry/data address: equal hops
    mean the words all arrive on one face and the rendezvous LOCK bars them
    (INV-71). This function raises rather than let that pass silently."""
    il = bres.chips[0].input_landings
    missing = [a for a in ARMS if a not in il]
    if missing:
        raise RuntimeError(f"arm landings missing: {missing}")
    out = {a: (int(il[a]["hop"]) & 0x1F, int(il[a]["data_addrs"][0]),
               int(il[a]["entry"])) for a in ARMS}
    if len({h for h, _a, _e in out.values()}) < 3:
        raise RuntimeError(
            f"the three arms share a hop — they land on one face and the "
            f"rendezvous will bar them (INV-71): {out}")
    return out


# --------------------------------------------------------------------------- #
#  Drive                                                                       #
# --------------------------------------------------------------------------- #

def _wr(hop, addr):
    return (0x6 << 12) | ((hop & 0x1F) << 5) | (addr & 0x1F)


def _jp(hop, entry):
    return (0x7 << 12) | ((hop & 0x1F) << 5) | (entry & 0x1F)


class FocChain:
    """A built FOC chain plus the host-side driver."""

    def __init__(self, bres, chip, lands):
        self.bres, self.chip, self.lands = bres, chip, lands
        self.out = []          # (word, time_ns)
        self.stops = []        # stop_reason per run — INV-56

    def fire(self, arm, value, cap=600_000):
        hop, addr, entry = self.lands[arm]
        self.chip.inject_data_physical([int(value) & 0xFFFF],
                                       target_hop_cnt=hop, target_addr=addr)
        self.chip.run(max_events=8_000)
        self.chip.inject_jump_physical(target_hop_cnt=hop, entry_addr=entry)
        res = self.chip.run(max_events=cap)
        self.stops.append(res.get("stop_reason") if isinstance(res, dict) else None)
        self._drain()
        return res

    def _drain(self):
        while self.chip.output_available("x16_out"):
            for value, _dest, t_ns in self.chip.read_port_words_timed("x16_out"):
                self.out.append((int(value) & 0xFFFF, t_ns))
            self.chip.release_output_ack("x16_out")
            self.chip.run(max_events=8_000)

    def iteration(self, e_d, e_q, theta, order=ARMS):
        """One control iteration: the three arm words, then drain."""
        vals = {"e_d": e_d, "e_q": e_q, "theta": theta}
        for arm in order:
            self.fire(arm, vals[arm])

    @property
    def words(self):
        return [w for w, _t in self.out]

    @property
    def times(self):
        return [t for _w, t in self.out]


def chip_for(bres, lands):
    import simkyt
    chip = simkyt.Chip.from_yaml(CHIP_YAML)
    chip.load_bitstream_physical(bres.words(0))
    chip.set_port_entry_address("x16_in", lands["e_d"][2])
    return chip


# --------------------------------------------------------------------------- #
#  Golden — the identical discretization, in double precision on the host      #
# --------------------------------------------------------------------------- #

def golden(e_d_words, e_q_words, theta_words, pi_params=None):
    """Host model of what is ON THE CHIP: PI(d), PI(q) -> inverse Park ->
    SVPWM. Bit-exact: every stage is the block's own pinned integer model, so
    this is a contract, not an approximation."""
    from gr_kyttar.placement.blocks.cordic_rotate_block import cordic_rotate_word
    from gr_kyttar.placement.blocks.pi_controller_block import PIControllerBlock
    from svpwm_golden import svpwm_duties

    p = dict(pi_params or PI_PARAMS)
    v_d = PIControllerBlock("d", **p).process_reference_q15(e_d_words)
    v_q = PIControllerBlock("q", **p).process_reference_q15(e_q_words)
    duties = []
    for i in range(len(theta_words)):
        v_alpha, v_beta = cordic_rotate_word(v_d[i], v_q[i], theta_words[i], 1)
        duties.extend(svpwm_duties(v_alpha, v_beta))
    return [d & 0xFFFF for d in duties]


# --------------------------------------------------------------------------- #
#  main                                                                        #
# --------------------------------------------------------------------------- #

def main():
    print("building the FOC current loop (anchors + route-only)...")
    project, bres, cat, ct = place_route_build()
    lands = arm_landings(bres)
    used = sum(c.cell_count for c in bres.chips.values())
    print(f"  built: {used} cells of 120 used; arm landings {lands}")

    # SIX consecutive iterations with DIFFERENT inputs — the chain streams.
    # The golden is computed over the WHOLE sequence in one call because the PI
    # integrators EVOLVE across samples; a per-iteration call would cold-start
    # the accumulator each time and disagree with the chip.
    e_d = [1000, 0x0333, -1500 & 0xFFFF, 300, 0x0700, -200 & 0xFFFF]
    e_q = [2000, 0x1500, 900, -2200 & 0xFFFF, 0x0123, 1750]
    theta = [0x1234, 0x4000, 0x8000, 0xC000, 0x2468, 0x9ABC]
    exp = golden(e_d, e_q, theta)

    chip = chip_for(bres, lands)
    t_inject = chip.simulation_time          # after the bitstream load
    chain = FocChain(bres, chip, lands)
    for i in range(len(e_d)):
        chain.iteration(e_d[i], e_q[i], theta[i])

    got = chain.words
    ok = got == exp
    print()
    for i in range(len(e_d)):
        g, w = got[3 * i:3 * i + 3], exp[3 * i:3 * i + 3]
        print(f"  iter {i}: {[hex(v) for v in g]} "
              f"{'==' if g == w else '!='} golden {[hex(v) for v in w]}")
    print(f"  EXACT (all {len(e_d)} iterations): {ok}")
    print(f"  stop_reasons: {sorted(set(chain.stops))}")

    if chain.out:
        ends = [chain.times[3 * i + 2] for i in range(len(chain.times) // 3)]
        t_first = chain.times[0] - t_inject
        t_last = ends[0] - t_inject
        print(f"\n  latency to the FIRST duty word     : {t_first:,.1f} ns")
        print(f"  latency to the COMPLETE packet     : {t_last:,.1f} ns")
        cad = [chain.times[3 * i + j + 1] - chain.times[3 * i + j]
               for i in range(len(ends)) for j in range(2)]
        print(f"  duty-word cadence within a packet  : "
              f"{sum(cad) / len(cad):,.2f} ns")
        if len(ends) >= 2:
            gaps = [ends[i + 1] - ends[i] for i in range(len(ends) - 1)]
            mean = sum(gaps) / len(gaps)
            print(f"  SUSTAINED inter-iteration interval : {mean:,.1f} ns "
                  f"(spread {max(gaps) - min(gaps):,.1f} ns)")
            print(f"  => {1e9 / mean:,.0f} control iterations/s "
                  f"({1e6 / mean:,.2f} kHz) sustained")
            print(f"     the interval ~= the fill latency: the chain RE-ARMS "
                  f"rather than pipelines (one iteration in flight).")

    perf = chip.performance_report()
    print(f"\n  perf: sim_time_ns={perf.get('simulation_time_ns'):,.1f} "
          f"instrs={perf.get('total_instructions')} "
          f"active_cells={perf.get('active_cells')}/{perf.get('total_cells')}")
    print(f"  power: avg_mW={perf.get('average_power_mw')} "
          f"(power_data_available={perf.get('power_data_available')})")
    print("\n  NOTE: simulated timing from simKYT's model — not silicon-certified.")

    if not ok:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
