#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Drive the FFT128 2P2S design and report EXACTLY where the words go.

This is the debugging vehicle. It builds the real 4-die 2P2S design, runs a
stimulus through chain A on ``simkyt.MultiChipSimulation``, and prints, per
trigger: how many words each die emitted, what crossed the carrier link, and
the first trigger at which forward progress ceases. Then it compares the
egressed stream against the whole-transform reference, word for word.

Run (from the repo root):

    QT_QPA_PLATFORM=offscreen .venv/bin/python \\
        examples/fft128_2p2s/fft128_2p2s_demo.py

    --samples N   how many samples to drive (default 200; the transform's
                  latency is 127, so fewer than ~140 only exercises the
                  zero-fill transient and proves nothing)
    --trace       per-trigger table + the carrier link's traffic (verbose)
    --pattern P   drive shape: 'paced' (the shipped one) or 'batched'
                  (the shape that makes no forward progress — kept so the
                  failure is reproducible on demand, not folklore)
    --concurrency report the PER-DIE event timeline: how much work each die
                  did per trigger and whether the dies overlapped. This is
                  the measurement behind the concurrency note in README.md.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

# Runnable from ANYWHERE (the README's command is repo-root relative), so put
# this script's own directory on the path rather than relying on the cwd.
sys.path.insert(0, str(Path(__file__).resolve().parent))

import fft128_2p2s as D  # noqa: E402


def _fmt(w):
    return f"{w:#06x}"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--samples", type=int, default=200)
    ap.add_argument("--trace", action="store_true")
    ap.add_argument("--pattern", choices=("paced", "batched"), default="paced")
    ap.add_argument("--concurrency", action="store_true")
    args = ap.parse_args()

    print("FFT128 on the 2P2S board — chain A: chip 0 = stage 0, "
          "chip 1 = stages 1..6")
    print("=" * 72)

    ctrl, bres, d0, d1 = D.build_2p2s()
    for cid in sorted(D.CHIP_LABELS):
        c = bres.chips[cid]
        role = ("die 0" if cid == D.CHIP_DIE0 else
                "die 1" if cid == D.CHIP_DIE1 else "idle ")
        print(f"  chip {cid}  {D.CHIP_LABELS[cid]:22s} {role}  "
              f"{c.cell_count:3d} cells   {len(bres.words(cid)):5d} "
              f"bitstream words")
    for cid in (D.CHIP_DIE0, D.CHIP_DIE1):
        il = list(bres.chips[cid].input_landings.values())[0]
        print(f"  chip {cid} landing: cell {tuple(il['cell'])} "
              f"entry {il['entry']} hop {il['hop']} "
              f"regs {il['data_addrs']}")
    print(f"  carrier links: chip0.x16_out -> chip1.x16_in (chain A, "
          f"carries the transform)")
    print(f"                 chip2.x16_out -> chip3.x16_in (chain B, wired "
          f"and idle)")

    words = D.stimulus(args.samples)
    ref = D.reference(words)
    mid = D.crossing_reference(words)
    nz = sum(1 for r in ref if r != (0, 0))
    print(f"\n  driving {len(words)} samples; reference carries {nz} non-zero "
          f"outputs past the latency-{D.LATENCY} transient")
    if nz == 0:
        print("  WARNING: the reference is all zeros — this run is VACUOUS. "
              "Drive more than the 127-sample latency.")

    trace_chips = (D.CHIP_DIE0, D.CHIP_DIE1) if args.concurrency else ()
    eng, landing = D.open_engine(bres, trace=trace_chips)

    # --- drive, watching for the trigger where progress stops ---------------
    stalled_at = None
    counts = []
    conc = []

    cursor = {D.CHIP_DIE0: 0, D.CHIP_DIE1: 0}

    def _new(cid):
        ev = eng._sim.get_trace(f"chip{cid}")
        out = ev[cursor[cid]:]
        cursor[cid] = len(ev)
        return out

    def on_sample(k, out, info):
        nonlocal stalled_at
        counts.append(len(out))
        quiescent = bool(info.get("completed"))
        if not quiescent and stalled_at is None:
            stalled_at = (k, dict(info))
        if args.concurrency:
            e0, e1 = _new(D.CHIP_DIE0), _new(D.CHIP_DIE1)
            conc.append((k, len(e0), len(e1),
                         e0[-1]["time_ns"] if e0 else None,
                         e1[-1]["time_ns"] if e1 else None))
        if args.trace:
            want = mid[k] if k < len(mid) else None
            print(f"    trig {k:4d}: die1 out {len(out)} word(s) "
                  f"{[_fmt(w) for w in out]}  | crossing carries "
                  f"{'(' + _fmt(want[0]) + ', ' + _fmt(want[1]) + ')' if want else '-'}"
                  f"  | rounds {info.get('rounds')} events "
                  f"{info.get('total_events')} quiescent {quiescent}")

    if args.pattern == "batched":
        print("\n  PATTERN 'batched': all three parts of the transaction are "
              "queued\n  with no pump between them. This is the shape that "
              "makes NO forward\n  progress — it is kept so the failure "
              "reproduces on demand.")
        got = _drive_batched(eng, landing, words, on_sample)
    else:
        got = D.drive(eng, landing, words, on_sample=on_sample)

    # --- where did the words stop? -----------------------------------------
    print(f"\n  WORD ACCOUNTING")
    print(f"    die 1 egress   : {len(got)} words "
          f"({len(got)//2} samples) of {2*len(words)} expected")
    if counts:
        print(f"    per-trigger yield: {sorted(set(counts))} "
              f"(a healthy run is [2] — out_i and out_q per trigger)")
        dead = next((i for i, c in enumerate(counts) if c == 0), None)
        print(f"    first trigger emitting NOTHING: "
              f"{dead if dead is not None else 'none'}")
    print(f"    first non-quiescent trigger: "
          f"{stalled_at[0] if stalled_at else 'none'}")
    if stalled_at:
        print(f"      run info there: {stalled_at[1]}")
        print("      (completed=False means the round cap was hit with work "
              "still pending —\n       either a genuine stall or a budget "
              "SHAPE that is too small; compare\n       total_events against "
              "a healthy trigger's before concluding.)")

    if args.concurrency:
        _report_concurrency(conc)

    # --- bit-exactness ------------------------------------------------------
    print(f"\n  CORRECTNESS vs the whole-transform reference")
    bad = []
    for k in range(len(words)):
        if 2 * k + 1 >= len(got):
            bad.append((k, None, ref[k]))
            break
        g = (got[2 * k], got[2 * k + 1])
        if g != ref[k]:
            bad.append((k, g, ref[k]))
    if not bad and len(got) == 2 * len(words):
        print(f"    BIT-EXACT — {len(words)}/{len(words)} samples, "
              f"{nz} of them non-zero")
        print(f"\nRESULT: EXACT — the N=128 transform computed across CHAIN A "
              f"of the\n        2P2S board, {len(words)} samples "
              f"word-for-word equal to whole(x).")
        return 0
    print(f"    MISMATCH — {len(bad)} bad sample(s); first few:")
    for k, g, e in bad[:6]:
        gs = "(no output)" if g is None else f"({_fmt(g[0])}, {_fmt(g[1])})"
        print(f"      sample {k:4d}: got {gs:24s} want "
              f"({_fmt(e[0])}, {_fmt(e[1])})")
    first = bad[0][0]
    print(f"\n    The first divergence is at sample {first}. The carrier link "
          f"should\n    carry {mid[first]} into die 1 at that trigger — "
          f"re-run with --trace to see\n    whether it did.")
    print(f"\nRESULT: NOT EXACT")
    return 1


def _report_concurrency(conc):
    """Per-die work + clock table — the evidence behind the concurrency note.

    Both dies doing work on EVERY trigger is the honest picture: the dies are
    concurrent ACROSS the run. What the per-sample drive cannot show is two
    dies busy in the same INSTANT, because it settles the pair before
    injecting the next sample — see README.md 'Do the dies run concurrently?'.
    """
    print(f"\n  PER-DIE CONCURRENCY (events each die processed per trigger)")
    both = sum(1 for _, a, b, _, _ in conc if a and b)
    print(f"    triggers where BOTH dies did work: {both}/{len(conc)}")
    print(f"    {'trig':>6} {'die0 ev':>9} {'die1 ev':>9} "
          f"{'die0 clock':>13} {'die1 clock':>13}")
    show = [c for c in conc[-6:]]
    for k, a, b, t0, t1 in show:
        print(f"    {k:6d} {a:9d} {b:9d} "
              f"{(f'{t0:.0f}' if t0 else '-'):>13} "
              f"{(f'{t1:.0f}' if t1 else '-'):>13}")
    if conc:
        _, _, _, t0, t1 = conc[-1]
        if t0 and t1:
            print(f"    NOTE: the two dies keep INDEPENDENT sim clocks and "
                  f"they diverge\n          ({t1/t0:.2f}x here). A GUI that "
                  f"orders animation steps by raw\n          time_ns would "
                  f"therefore replay one die's whole burst before the\n"
                  f"          other's — see sim_controller._interleave_chip_steps.")


def _drive_batched(eng, landing, words, on_sample):
    """The un-paced shape, preserved as a reproduction of the failure."""
    sim = eng._sim
    head = f"chip{D.CHIP_DIE0}"
    hop, entry = int(landing["hop"]), int(landing["entry"])
    a0, a1 = int(landing["data_addrs"][0]), int(landing["data_addrs"][1])
    got = []
    for k, (wi, wq) in enumerate(words):
        sim.inject_data_physical(head, [wi], hop, a0)
        sim.inject_data_physical(head, [wq], hop, a1)
        sim.inject_jump_physical(head, hop, entry)
        info = sim.run(*D.SETTLE)
        out = eng.capture(D.CHIP_DIE1, "x16_out")
        got.extend(out)
        on_sample(k, out, info)
    return got


if __name__ == "__main__":
    sys.exit(main())
