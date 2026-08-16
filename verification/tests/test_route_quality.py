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
    "audio_meter.kyt": 2,
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
