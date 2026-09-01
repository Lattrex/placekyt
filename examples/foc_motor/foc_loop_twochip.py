# SPDX-License-Identifier: GPL-3.0-or-later
"""The FULL FOC loop across TWO arrays, closed around the motor model — and
the LOOP-RATE MEASUREMENT that answers what the whole loop costs.

WHY TWO ARRAYS. The whole loop does not route on ONE 10x12 array, and the
limit is corridor/arm budget rather than cells (INV-71): the full chain is 55
block cells of 120, yet the best of ~2600 whole-chain placements still left 2
of 13 nets unrouted, and the unrouted nets were always rendezvous arms. Split
across two dies each half routes comfortably — 75 cells for the measurement
half, 87 for the command half.

THE SPLIT is the natural one. The measurement half (Clarke + forward Park)
goes on one die, the command half (two PI controllers + inverse Park + SVPWM)
on the other. They are joined by (i_d, i_q) out and (e_d, e_q) back, plus
theta, so the crossing is narrow.

The two halves are NOT directly chainable on the fabric even so, and that is
by construction rather than by limitation: e = ref - measured sits between
them, and the reference is a supervisory quantity the host owns. So one
control period is chip0 -> host -> chip1 -> host(plant) -> chip0, and the
per-sample cost is the SUM of the two halves' on-chip times: the halves are
strictly SERIAL within a sample, since sample k's duties cannot be computed
until sample k's currents have been measured and rotated.

WHAT IS MEASURED HERE (simKYT's timing model, per-word capture times):

  measurement half alone   13,142.7 ns   76.09 kHz
  command half alone       17,940.5 ns   55.74 kHz
  the FULL LOOP per sample 31,083.2 ns   32.17 kHz

plus roughly 40 ns per chip crossing, which is negligible against a ~31 us
period. These are SIMULATED times, not silicon-certified numbers.

Run::

    QT_QPA_PLATFORM=offscreen \\
      .venv/bin/python examples/foc_motor/foc_loop_twochip.py [iterations]
"""
import os, sys
from pathlib import Path
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
_HERE = Path(__file__).resolve().parent
_ROOT = _HERE.parents[1]
for p in (_ROOT/"placekyt", _ROOT/"runtime"/"python", _ROOT/"verification",
          _ROOT/"verification"/"tests", _HERE):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

from PySide6.QtWidgets import QApplication
QApplication.instance() or QApplication([])

import simkyt
from engine.catalog import BlockCatalog
from engine.io.chip_type_io import load_chip_type
from ui.controller import AppController
from model.connection import ChipPortEndpoint as CPE, BlockEndpoint as BE

from foc_motor_demo import CHIP_YAML, LIB, PLACEMENT, NETS, ARMS
from foc_loop_model import (PMSMPlant, MotorParams, StatefulPI, DEFAULT_PI,
                            q15, from_q15, I_D_REF, measurement_half)
from gr_kyttar.placement.blocks.cordic_rotate_block import cordic_rotate_word as _cordic
from svpwm_golden import svpwm_duties as _svpwm

CT = load_chip_type(CHIP_YAML)
CTK = getattr(CT, "name", None) or "kyttar_10x12"

# ---- chip 0: the MEASUREMENT half (the anchors the gate pins) -------------- #
MEAS_A = {"cl": (4, 1), "pk": (4, 4), "r_ia": (0, 4), "r_ib": (9, 11), "r_th": (4, 8)}


def build_measurement():
    cat = BlockCatalog.from_gr_kyttar()
    c = AppController(catalog=cat)
    c.new_project("meas", CTK)
    cl = c.place_block("ClarkeTransformBlock", 0, *MEAS_A["cl"], library=LIB, params={})
    pk = c.place_block("CordicRotateBlock", 0, *MEAS_A["pk"], library=LIB,
                       params={"sign": -1})
    rl = {n: c.place_block("StreamSplitterBlock", 0, *MEAS_A[n], library=LIB, params={})
          for n in ("r_ia", "r_ib", "r_th")}
    C = c.add_logical_connection
    C(CPE(chip=0, port="x16_in"), BE(block=rl["r_ia"], port="x"), name="ia")
    C(CPE(chip=0, port="x16_in"), BE(block=rl["r_ib"], port="x"), name="ib")
    C(CPE(chip=0, port="x16_in"), BE(block=rl["r_th"], port="x"), name="th")
    C(BE(block=rl["r_ia"], port="out"), BE(block=cl, port="ia"), name="w1")
    C(BE(block=rl["r_ib"], port="out"), BE(block=cl, port="ib"), name="w2")
    C(BE(block=rl["r_th"], port="out"), BE(block=pk, port="theta"), name="w3")
    C(BE(block=cl, port="yi"), BE(block=pk, port="x"), name="cx")
    C(BE(block=cl, port="yq"), BE(block=pk, port="y"), name="cy")
    # BOTH rails out: we need i_d AND i_q.
    C(BE(block=pk, port="yi"), CPE(chip=0, port="x16_out"), name="o_i")
    C(BE(block=pk, port="yq"), CPE(chip=0, port="x16_out"), name="o_q")
    rep = c.auto_route_all({CTK: CT})
    if not rep.ok:
        raise RuntimeError("meas route failed: " +
                           "; ".join(f"{r.name}:{r.reason}" for r in (rep.failed or [])))
    b = c.build()
    if not b.ok:
        raise RuntimeError("meas build failed: " + "; ".join(str(e) for e in (b.errors or [])[:3]))
    return b


def build_command():
    cat = BlockCatalog.from_gr_kyttar()
    c = AppController(catalog=cat)
    c.new_project("cmd", CTK)
    keys = {}
    for name, (typ, params, (ax, ay)) in PLACEMENT.items():
        keys[name] = c.place_block(typ, 0, ax, ay, library=LIB, params=dict(params))
    for src, dst, nm in NETS:
        s = CPE(chip=0, port="x16_in") if src == "PORTIN" else BE(block=keys[src[0]], port=src[1])
        d = CPE(chip=0, port="x16_out") if dst == "PORTOUT" else BE(block=keys[dst[0]], port=dst[1])
        c.add_logical_connection(s, d, name=nm)
    rep = c.auto_route_all({CTK: CT})
    if not rep.ok:
        raise RuntimeError("cmd route failed: " +
                           "; ".join(f"{r.name}:{r.reason}" for r in (rep.failed or [])))
    b = c.build()
    if not b.ok:
        raise RuntimeError("cmd build failed: " + "; ".join(str(e) for e in (b.errors or [])[:3]))
    return b


def landings(bres, names):
    il = bres.chips[0].input_landings
    miss = [n for n in names if n not in il]
    if miss:
        raise RuntimeError(f"missing landings {miss}; have {sorted(il)}")
    out = {n: (int(il[n]["hop"]) & 0x1F, int(il[n]["data_addrs"][0]), int(il[n]["entry"]))
           for n in names}
    if len({h for h, _, _ in out.values()}) < len(names):
        raise RuntimeError(f"arms share a hop (INV-71): {out}")
    return out


class Half:
    """One built half on its own simkyt chip, driven arm by arm."""

    def __init__(self, bres, arms):
        self.bres = bres
        self.lands = landings(bres, arms)
        self.arms = arms
        self.chip = simkyt.Chip.from_yaml(CHIP_YAML)
        self.chip.load_bitstream_physical(bres.words(0))
        self.chip.set_port_entry_address("x16_in", self.lands[arms[0]][2])
        self.stops = []

    def iterate(self, values):
        """Fire every arm, then drain. Returns (words, times)."""
        got = []
        for a in self.arms:
            hop, addr, entry = self.lands[a]
            self.chip.inject_data_physical([int(values[a]) & 0xFFFF],
                                           target_hop_cnt=hop, target_addr=addr)
            self.chip.run(max_events=8_000)
            self.chip.inject_jump_physical(target_hop_cnt=hop, entry_addr=entry)
            res = self.chip.run(max_events=600_000)
            self.stops.append(res.get("stop_reason") if isinstance(res, dict) else None)
            while self.chip.output_available("x16_out"):
                for v, _d, t in self.chip.read_port_words_timed("x16_out"):
                    got.append((int(v) & 0xFFFF, t))
                self.chip.release_output_ack("x16_out")
                self.chip.run(max_events=8_000)
        return got


def run_two_chip(n_iter=12, verbose=True):
    """Build both halves, close the loop across two arrays for ``n_iter``
    control periods, and return the trace plus the measured intervals."""
    def say(*a):
        if verbose:
            print(*a)
    say("building the MEASUREMENT half (chip 0)...")
    bm = build_measurement()
    say(f"  ok: {sum(c.cell_count for c in bm.chips.values())} cells")
    say("building the COMMAND half (chip 1)...")
    bc = build_command()
    say(f"  ok: {sum(c.cell_count for c in bc.chips.values())} cells")

    meas = Half(bm, ("ia", "ib", "th"))
    cmd = Half(bc, ARMS)
    say(f"  meas arm landings: {meas.lands}")
    say(f"  cmd  arm landings: {cmd.lands}")

    plant = PMSMPlant(params=MotorParams(l_s=1.5e-3), omega_e=200.0)
    pi_d = StatefulPI("d", **DEFAULT_PI)
    pi_q = StatefulPI("q", **DEFAULT_PI)
    ref_d, ref_q = q15(I_D_REF), q15(0.30)

    N = int(n_iter)
    ia_w, ib_w, th_w = plant.sensed_words()
    t_meas0 = meas.chip.simulation_time
    t_cmd0 = cmd.chip.simulation_time
    rows = []
    for k in range(N):
        # --- chip 0: measurement half -------------------------------------- #
        mo = meas.iterate({"ia": ia_w, "ib": ib_w, "th": th_w})
        if len(mo) < 2:
            say(f"  iter {k}: measurement half emitted {len(mo)} words "
                f"(want 2) stops={meas.stops[-3:]}")
            break
        i_d, i_q = mo[0][0], mo[1][0]
        # host golden cross-check
        gi_d, gi_q = measurement_half([ia_w], [ib_w], [th_w])[0]
        exact_m = (i_d, i_q) == (gi_d, gi_q)

        # --- host: the error former ---------------------------------------- #
        def s16(w):
            w &= 0xFFFF
            return w - 0x10000 if w >= 0x8000 else w
        sat = lambda v: max(-32768, min(32767, v))
        e_d = sat(s16(ref_d) - s16(i_d)) & 0xFFFF
        e_q = sat(s16(ref_q) - s16(i_q)) & 0xFFFF

        # --- chip 1: command half ------------------------------------------ #
        co = cmd.iterate({"e_d": e_d, "e_q": e_q, "theta": th_w})
        if len(co) < 3:
            say(f"  iter {k}: command half emitted {len(co)} words (want 3) "
                f"stops={cmd.stops[-3:]}")
            break
        da, db, dc = co[0][0], co[1][0], co[2][0]
        # Host golden for the COMMAND half too: the same integer models, with
        # the SAME live integrators, so a bad constant or a mis-wired arm on
        # chip 1 is caught rather than merely timed.
        gv_d, gv_q = pi_d.step(e_d), pi_q.step(e_q)
        gva, gvb = _cordic(gv_d, gv_q, th_w, 1)
        gda, gdb, gdc = _svpwm(gva, gvb)
        exact_c = (da, db, dc) == (gda & 0xFFFF, gdb & 0xFFFF, gdc & 0xFFFF)
        rows.append((k, i_d, i_q, (da, db, dc), exact_m,
                     mo[-1][1], co[-1][1], exact_c))
        ia_w, ib_w, th_w = plant.step(da, db, dc)

    say(f"\n  completed {len(rows)} of {N} closed-loop iterations across TWO chips")
    for (k, i_d, i_q, duty, ex, tm, tc, exc) in rows[:6]:
        say(f"   iter {k}: i_d={from_q15(i_d):+.4f} i_q={from_q15(i_q):+.4f} "
            f"duty={[round(from_q15(d),4) for d in duty]} "
            f"meas_exact={ex} cmd_exact={exc}")
    say(f"  meas stop_reasons: {sorted(set(meas.stops))}")
    say(f"  cmd  stop_reasons: {sorted(set(cmd.stops))}")

    res = {"rows": rows, "n_done": len(rows), "n_want": N,
           "meas_stops": set(meas.stops), "cmd_stops": set(cmd.stops),
           "meas_cells": sum(c.cell_count for c in bm.chips.values()),
           "cmd_cells": sum(c.cell_count for c in bc.chips.values()),
           "meas_interval": None, "cmd_interval": None, "loop_interval": None}
    if len(rows) >= 2:
        # per-iteration on-chip time on EACH die, then the serial sum.
        mt = [r[5] for r in rows]
        ct = [r[6] for r in rows]
        dm = [mt[i+1]-mt[i] for i in range(len(mt)-1)]
        dc_ = [ct[i+1]-ct[i] for i in range(len(ct)-1)]
        am, ac = sum(dm)/len(dm), sum(dc_)/len(dc_)
        res["meas_interval"], res["cmd_interval"] = am, ac
        res["loop_interval"] = am + ac
        say(f"\n  measurement-half sustained interval : {am:,.1f} ns "
            f"({1e6/am:,.2f} kHz alone)")
        say(f"  command-half sustained interval     : {ac:,.1f} ns "
            f"({1e6/ac:,.2f} kHz alone)")
        say(f"  SERIAL SUM (the full loop per sample): {am+ac:,.1f} ns "
            f"=> {1e6/(am+ac):,.2f} kHz")
        say("  (+ ~40 ns per chip crossing, negligible against the above)")
        say("\n  NOTE: simulated timing from simKYT's model — not "
            "silicon-certified.")
    return res


def main():
    n = int(sys.argv[1]) if len(sys.argv) > 1 else 12
    r = run_two_chip(n)
    if r["n_done"] != r["n_want"]:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
