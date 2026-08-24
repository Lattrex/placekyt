# SPDX-License-Identifier: GPL-3.0-or-later
"""Route-QUALITY ratchet over every shipped example ``.kyt`` (2026-08-11).

The auto-router (bus_router) is a strict shortest-path router since the
loop-back/zigzag fix: corridor sharing is a sub-hop TIE-BREAK, a net may
broker on its OWN source's emit cell, brokers are chosen by routed distance,
and equal-length ties prefer straight runs. Before that fix shipped ``.kyt``s
carried routes like 21 cells for a manhattan-5 hop (data_link) and 25-for-3
with self-crossings (audio_meter) — under saturated (pipelined) drive a long
weaving corridor is a real hazard, not just ugly: more in-flight words pile
back-to-back into more shared cells.

This gate RATCHETS the audited quality of the committed artifacts:

  * no routed net may EXCEED its endpoint manhattan distance by more than
    ``MAX_NET_EXCESS`` (the worst placement-forced case today: the
    channel_selector filter wall and the data_link col-9 wall, both +8);
  * no route may REVISIT a cell (a literal loop);
  * each ``.kyt``'s TOTAL excess must not grow past its pinned value
    (placement-forced detours are pinned exactly; regressions fail loudly).

If a legitimate change (new example, deliberate re-placement) moves a number,
re-pin it CONSCIOUSLY in ``TOTAL_EXCESS`` with a comment — never loosen
``MAX_NET_EXCESS`` to make a wandering route pass.
"""
from __future__ import annotations

from pathlib import Path

import pytest
import yaml

_ROOT = Path(__file__).resolve().parents[2]
EXAMPLES = _ROOT / "examples"

MAX_NET_EXCESS = 8

# Pinned per-file total excess (sum over routed nets of len - manhattan).
# Anything not listed must be 0. Every nonzero value below is explained by a
# concrete constraint in the committed placement:
TOTAL_EXCESS = {
    # port shares its single exit face with the near arm; the far arm detours
    # around the lead block (INV-24 fork) — 2 per affected net:
    "bpsk_modem.kyt": 4,
    "coherent_bpsk_rx.kyt": 2,
    "qam16_modem.kyt": 2,
    "fsk4_modem.kyt": 6,
    "am_transceiver.kyt": 2,
    "fm_transceiver.kyt": 4,
    # three-stream layout (audio + meter + true-RMS row): the meter head (Abs)
    # tucks into the DC-blocker pocket at (4,3) — the only routable AND whole-
    # DRC-clean seat found by exhaustive scan (audio_meter_demo._refine_third_
    # stream_placement) — and its port ingress corridor rounds the 13-cell
    # DC-blocker wall (+6 on that one net; every other net shortest-path):
    "audio_meter.kyt": 6,
    # the shared x16_in fans out to THREE two-input blocks; the two corridors
    # to the far MultiplyCC round the AddCC/SubCC row (+2 each, the INV-24
    # fork detour class):
    "complex_math.kyt": 4,
    # the 22-cell FLL serpentine (7x4, 2026-08-17 re-fold of the old 8x5
    # ring — same pinned total on the regenerated placement) + two Costas
    # folds: each Costas→slicer tap corridor rounds its own 4x2 fold's south
    # edge to reach the col-8 slicer (+2 each) — placement-forced wall
    # detours:
    "robust_rx.kyt": 4,
    # v2 backbone TX thread (duplex SRAM-panel design): one +2 wrap:
    "psk31_transceiver.kyt": 2,
    # abutted pack hugs the input port; the comb's port fan-out far arm rounds
    # the delay/gain arm (+2, the INV-24 fork detour):
    "effect_comb.kyt": 2,
    # shared x16_in fan-out: the far landing's delivery detours around the
    # near block (the INV-24 fork), and the equalizer's splitter arm wraps
    # its own emit cell (+2 each):
    "lms_equalizer.kyt": 4,
    # duplex guided anchors (arg rows 1-4, mag rows 6-7): the mag stream's
    # input corridor rounds the arg block down the col-0 spine (+2) and its
    # egress joins the shared col-9 north spine behind the arg tap (+2):
    "cordic_polar.kyt": 4,
    # M&M-timing duplex re-P&R (2026-08-16, Gardner→MMTiming swap): one input
    # corridor rounds a block edge down col 1 to the RX head (+2) and one
    # egress jogs along row 6 into the col-9 north highway (+2) — both
    # placement-forced wall detours around the 14-cell MMTiming footprint:
    "qpsk_modem.kyt": 4,
    # GRU classifier (2026-08-24). The wide-flat 10x6 GRUCellBlock occupies
    # rows 6-11 across the FULL width, so the entire front end is confined to
    # rows 1-4 and both detours are that confinement, not router wander:
    #   * pow_mean +2 — power (3,2) -> boxcar (6,1): the boxcar's own 2x4
    #     footprint (cols 6-7, rows 1-4) blocks the direct approach, so the
    #     corridor rounds its south-west corner along row 3.
    #   * root_decim +4 — sqrt (9,2) -> decim (4,2): a straight row-2 run is
    #     walled by the sqrt and boxcar cells at (6,2),(7,2),(8,2), so the
    #     corridor drops to the free row 5 (the only clear lane between the
    #     front end and the GRU band) and climbs back.
    # Both are the placement-forced wall-detour class, and the placement is not
    # free to improve: a 400-layout search over the free band found exactly ONE
    # arrangement that routes AND builds at all (102/120).
    "gru_classifier.kyt": 6,
}


def _kyts():
    return sorted(p for p in EXAMPLES.glob("*/*.kyt")
                  if not p.name.endswith(".orig.kyt"))


def _audit(path):
    doc = yaml.safe_load(path.read_text())
    rows = []
    for conn in doc.get("connections") or []:
        route = conn.get("route")
        if not isinstance(route, list) or len(route) < 2:
            continue
        pts = [(p["x"], p["y"]) for p in route]
        length = len(pts) - 1
        manh = abs(pts[0][0] - pts[-1][0]) + abs(pts[0][1] - pts[-1][1])
        rows.append((conn["name"], length, manh, pts))
    return rows


@pytest.mark.parametrize("kyt", _kyts(), ids=lambda p: f"{p.parent.name}/{p.name}")
def test_route_quality(kyt):
    rows = _audit(kyt)
    total_excess = 0
    for name, length, manh, pts in rows:
        excess = length - manh
        assert excess <= MAX_NET_EXCESS, (
            f"{kyt.name} {name}: routed {length} cells for manhattan {manh} "
            f"(+{excess} > {MAX_NET_EXCESS}) — the router is regressing toward "
            f"the loop-back/zigzag pathology: {pts}")
        assert len(pts) == len(set(pts)), (
            f"{kyt.name} {name}: route REVISITS a cell (literal loop): {pts}")
        total_excess += max(0, excess)
    budget = TOTAL_EXCESS.get(kyt.name, 0)
    assert total_excess <= budget, (
        f"{kyt.name}: total route excess {total_excess} exceeds the pinned "
        f"budget {budget} — a route got longer; find out why before re-pinning")
