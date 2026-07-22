# SPDX-License-Identifier: GPL-3.0-or-later
"""A serialize-LOCKED complex-output block (FrequencyModulator) egressing yi/yq to the
chip OUTPUT PORT over a MULTI-HOP route must deliver BOTH rails to the port.

The FM's saturation serialize-LOCK declares an ``output_cell_id`` (its emit cell also
carries the backward unlock WRITE.CFG). That made the build treat the FM as a single-
output block (``_output_cell_carries_handoffs`` → ``_patch_last_write_handoff``) instead
of taking the COMPLEX-EGRESS path, so:
  * the yi/yq WRITEs got the WRONG hop (a single last-WRITE hop, not the port distance),
    so on a multi-hop auto-route the word died mid-corridor → 0 egress (empty TX I/Q); and
  * the two rails never got their distinct out-tags (I on tag N, Q on tag N+1).

Fix: the complex-port-egress patch takes precedence over the output-cell-handoff path,
and it SKIPS the lock's WRITE.CFG. This test builds the FM TX chain, sets the yi/yq
out-tags the way the GRC importer does (10 / 11), routes it so the egress corridor is
multi-hop, and asserts BOTH tags egress 1:1 on the unit circle.

Run:
    KYTTAR_GR_PYTHON=/usr/bin/python3 QT_QPA_PLATFORM=offscreen \
      <venv>/python -m pytest verification/tests/test_complex_egress_locked_fm.py -q
"""

from __future__ import annotations

import math
import os
import sys
from collections import Counter
from pathlib import Path

import numpy as np
import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

_PLACEKYT = Path(__file__).resolve().parents[2] / "placekyt"
_RUNTIME = Path(__file__).resolve().parents[2] / "runtime" / "python"
for p in (str(_PLACEKYT), str(_RUNTIME)):
    if p not in sys.path:
        sys.path.insert(0, p)

CHIP_YAML = str(_PLACEKYT / "resources" / "chips" / "kyttar_10x12.yaml")
_CHIP_OK = os.path.exists(CHIP_YAML)


@pytest.mark.skipif(not _CHIP_OK, reason="chip yaml absent")
def test_locked_fm_complex_egress_both_rails_reach_port():
    from PySide6.QtWidgets import QApplication
    import simkyt
    from engine.catalog import BlockCatalog
    from engine.io.chip_type_io import load_chip_type
    from engine.build import BuildEngine
    from engine.registry import ChipTypeRegistry
    from engine.port_config import stream_targets
    from ui.controller import AppController
    from model.connection import BlockEndpoint, ChipPortEndpoint

    QApplication.instance() or QApplication([])
    key = "kyttar_10x12"
    cat = BlockCatalog.from_gr_kyttar()
    ct = load_chip_type(CHIP_YAML)
    ctrl = AppController(catalog=cat)
    ctrl.new_project("egress_fm", key)
    # A locked FM fed a real input, egressing yi/yq to x16_out. Place it away from the
    # port so the auto-routed egress corridor is several hops long (the case the WRONG
    # WRITE hop killed mid-corridor).
    fm = ctrl.place_block(
        "FrequencyModulatorBlock", 0, 1, 1, library="lattrex.official",
        params={"sensitivity": 1.5707963267948966, "pipeline_lock": True})
    add = ctrl.add_route
    add(ChipPortEndpoint(chip=0, port="x16_in"), BlockEndpoint(block=fm, port="x"), [])
    add(BlockEndpoint(block=fm, port="yi"),
        ChipPortEndpoint(chip=0, port="x16_out"), [])
    add(BlockEndpoint(block=fm, port="yq"),
        ChipPortEndpoint(chip=0, port="x16_out"), [])
    assert ctrl.auto_route_all({key: ct}, auto_orient=True, use_bus="always").ok

    # Assign the yi/yq out-tags the way the GRC importer does (yi=10, yq=11) — a raw
    # add_route leaves them None, but the build's complex-egress path needs distinct tags.
    for conn in ctrl.project.connections:
        s = getattr(conn, "source", None)
        if s is None:
            continue
        if getattr(s, "port", None) == "yi":
            conn.out_tag = 10
        elif getattr(s, "port", None) == "yq":
            conn.out_tag = 11
        elif getattr(s, "port", None) == "x16_in":
            conn.stream_id = "tx"

    bres = BuildEngine(cat, CHIP_YAML).build(ctrl.project, {key: ct})
    assert bres.ok, getattr(bres, "errors", None)
    reg = ChipTypeRegistry()
    reg.register_file(CHIP_YAML)
    tg = stream_targets(ctrl.project, reg, cat, 0, build_result=bres)["tx"]
    entry, hop, a0 = tg["entry_addr"], tg["hop_count"], tg["data_addrs"][0]
    base_tag = int(tg["out_tag"])
    assert tg.get("complex_out"), "FM egress must be complex_out"

    def _w(a):
        return (0x6 << 12) | ((hop & 0x1F) << 5) | (int(a) & 0x1F)

    def _j():
        return (0x7 << 12) | ((hop & 0x1F) << 5) | (int(entry) & 0x1F)

    def _q15(f):
        return int(np.clip(round(float(f) * 32768), -32768, 32767)) & 0xFFFF

    chip = simkyt.Chip.from_yaml(CHIP_YAML)
    chip.load_bitstream_physical(bres.words(0))
    xs = (np.sin(np.arange(120) * 0.4) * 0.7).astype(np.float32)   # real FM input
    stream = []
    for v in xs:
        stream += [_w(a0), _q15(v), _j()]
    chip.queue_words_physical("x16_in", stream)
    chip.run(max_events=max(300000, 20000 * len(stream)))

    def _s16(u):
        u &= 0xFFFF
        return u - 0x10000 if u >= 0x8000 else u
    words = [(int(d), int(v) & 0xFFFF)
             for (v, d, _t) in chip.read_port_words_timed("x16_out")]
    hist = Counter(d for d, _ in words)
    i_tag, q_tag = base_tag, base_tag + 1
    # BOTH rails must egress (the bug produced 0 words, or only one tag).
    assert hist.get(i_tag, 0) > 0, f"I rail (tag {i_tag}) produced NO output: {dict(hist)}"
    assert hist.get(q_tag, 0) > 0, f"Q rail (tag {q_tag}) produced NO output: {dict(hist)}"
    assert hist[i_tag] == hist[q_tag], (
        f"I/Q rail counts differ ({hist[i_tag]} vs {hist[q_tag]}) — rails not 1:1")
    # And the output is a real unit-circle complex baseband (not garbage/zeros).
    I = [_s16(v) / 32768.0 for d, v in words if d == i_tag]
    Q = [_s16(v) / 32768.0 for d, v in words if d == q_tag]
    mags = [math.hypot(i, q) for i, q in zip(I, Q)]
    mean_mag = sum(mags) / len(mags)
    assert 0.9 <= mean_mag <= 1.1, f"|IQ| not on the unit circle: mean {mean_mag:.3f}"
