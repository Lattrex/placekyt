# SPDX-License-Identifier: GPL-3.0-or-later
"""LIVE coefficient writes — the GRC-slider → running-fabric retune path.

Guards the end-to-end plumbing that makes a GRC slider retune the hosted chip
with NO reflash:

  engine.port_config.live_coeff_writes   (placed design → {block: WRITE target})
  SimServer.set_coeff_writes / _apply_live_coeffs   (grc_params → WRITE inject)
  kyttar markers' set_gain → session.update_param → burst header + live push

History: commit 92e6d62 added the server half but nothing populated it and the
GR-side setter only touched a local attribute — the "live slider" was dead code
end-to-end (verified 2026-08-13). These gates are MUTATION-PROVEN: with the
coefficient map absent, the same batches return the BUILT gain unchanged.

Two levels, per the GR-client-loop rule (a hand-rolled RPC test alone is NOT
sufficient — the 2026-08-09 lesson):
  1. server-level RPC gates (fast, single process);
  2. a REAL gr.top_block run (system python + kyttar OOT) whose set_gain()
     between two Runs retunes the persistently-hosted chip.
"""

from __future__ import annotations

import os
import socket
import subprocess
import sys
import textwrap
import time
from pathlib import Path

import numpy as np
import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
pytest.importorskip("PySide6")

REPO = Path(__file__).resolve().parents[2]
GR_PYTHON = os.environ.get("KYTTAR_GR_PYTHON", "/usr/bin/python3")
GR_OOT = REPO / "gr-kyttar" / "python"

from tests.conftest import CHIP_YAML as CT_PATH  # noqa: E402

pytestmark = pytest.mark.skipif(not CT_PATH.exists(), reason="chip yaml absent")


# --------------------------------------------------------------------------- #
# Shared harness: a built x16_in -> GainBlock(0.5) -> x16_out design + server
# --------------------------------------------------------------------------- #

def _build_gain_design(gain: float = 0.5):
    """(ctrl, build_result, coeff_map, port_cfg) for a routed+built gain design."""
    from PySide6.QtWidgets import QApplication
    QApplication.instance() or QApplication([])
    from engine.catalog import BlockCatalog
    from engine.io.chip_type_io import load_chip_type
    from ui.controller import AppController
    from model.connection import ChipPortEndpoint, BlockEndpoint
    from engine.port_config import live_coeff_writes, input_port_config

    cat = BlockCatalog.from_gr_kyttar()
    ct = load_chip_type(str(CT_PATH))
    ctrl = AppController(catalog=cat)
    ctrl.new_project("live_gain", "kyttar_10x12")
    g = ctrl.place_block("GainBlock", 0, 1, 0, library="lattrex.official",
                         params={"gain": gain})
    ctrl.add_logical_connection(ChipPortEndpoint(chip=0, port="x16_in"),
                                BlockEndpoint(block=g, port="sample"), name="in")
    ctrl.add_logical_connection(BlockEndpoint(block=g, port="out"),
                                ChipPortEndpoint(chip=0, port="x16_out"), name="out")
    rep = ctrl.auto_route_all({"kyttar_10x12": ct})
    assert rep.ok, f"route failed: {[f'{r.name}:{r.reason}' for r in rep.failed]}"
    res = ctrl.build()
    assert res.ok, f"build failed: {res.errors}"
    coeff = live_coeff_writes(ctrl.project, ctrl.registry, cat, 0,
                              build_result=res)
    assert g in coeff, f"gain block not resolved as live-tunable: {coeff}"
    cfg = input_port_config(ctrl.project, ctrl.registry, cat, 0, build_result=res)
    assert cfg is not None
    return ctrl, res, coeff, cfg


def _host_server(res, cfg, coeff_map):
    """A SimServer hosting the built design (fresh simkyt chip); returns
    (server, port)."""
    import simkyt
    from engine.sim_bridge import SimServer

    chip = simkyt.Chip.from_yaml(str(CT_PATH))
    chip.load_bitstream_physical(res.words(0))
    port_name, kw = cfg
    srv = SimServer(chip, host="127.0.0.1", port=0,
                    default_entries={port_name: kw["entry_addr"]},
                    default_hops={port_name: kw["hop_count"]})
    srv.set_coeff_writes(coeff_map)
    return srv, srv.start()


def _batch(port: int, samples, grc_params=None):
    """One process_batch RPC (real burst); returns the recovered floats."""
    from engine.sim_bridge import send_message, recv_message
    header = {"op": "process_batch", "port": "x16_out", "in_port": "x16_in",
              "complex": False, "raw": False, "data_addrs": [0]}
    if grc_params is not None:
        header["grc_params"] = grc_params
    conn = socket.create_connection(("127.0.0.1", port), timeout=30)
    try:
        send_message(conn, header, np.asarray(samples, dtype="<f4"))
        reply, out = recv_message(conn)
    finally:
        conn.close()
    assert reply.get("ok"), f"server error: {reply.get('error')}"
    return np.asarray(out if out is not None else [], dtype=np.float32)


_STIM = [0.5, -0.5, 0.25, 0.75, -0.75, 0.125]


def _assert_gain(out, expect_gain, label):
    assert len(out) == len(_STIM), f"{label}: {len(out)}/{len(_STIM)} words"
    want = np.array(_STIM) * expect_gain
    err = np.max(np.abs(out - want))
    assert err < 2e-4, (f"{label}: output does not match gain {expect_gain} "
                        f"(max err {err:.6f}; got {out.tolist()})")


# --------------------------------------------------------------------------- #
# 1. Server-level gates
# --------------------------------------------------------------------------- #

def test_live_gain_write_retunes_running_chip():
    """grc_params carrying a NEW gain retunes the hosted chip before the burst;
    the write PERSISTS (a later batch with no params still runs at the new gain)."""
    ctrl, res, coeff, cfg = _build_gain_design(0.5)
    assert coeff["gain"]["params"] == {"gain": 0.5}   # design values seed the dedup
    srv, port = _host_server(res, cfg, coeff)
    try:
        _assert_gain(_batch(port, _STIM), 0.5, "built gain (no params)")
        # A design-MATCHING advertised value is a no-op (seeded dedup): every
        # gain-bearing example advertises on every Run, and a gratuitous WRITE
        # is misdeliverable on broker-routed layouts (the echo/meter gates
        # caught exactly that before seeding).
        _assert_gain(_batch(port, _STIM, {"gain": {"gain": 0.5}}), 0.5,
                     "design-matching value writes nothing")
        _assert_gain(_batch(port, _STIM, {"gain": {"gain": 0.25}}), 0.25,
                     "live write 0.25")
        _assert_gain(_batch(port, _STIM, {"gain": {"gain": 0.75}}), 0.75,
                     "live write 0.75")
        # Persistence: the coefficient lives ON the chip now, not in the header.
        _assert_gain(_batch(port, _STIM), 0.75, "persisted (no params)")
    finally:
        srv.stop()


def test_without_coeff_map_params_do_not_retune():
    """MUTATION CONTROL: with no registered coefficient map (the pre-fix state),
    the same grc_params batches return the BUILT gain — proving the passing gate
    above measures the live write, not some header side-effect."""
    ctrl, res, coeff, cfg = _build_gain_design(0.5)
    srv, port = _host_server(res, cfg, {})     # <- map absent
    try:
        _assert_gain(_batch(port, _STIM, {"gain": {"gain": 0.25}}), 0.5,
                     "params ignored without map")
    finally:
        srv.stop()


def test_standalone_set_grc_params_rpc_retunes():
    """The set_grc_params op (the slider's LIVE push path) retunes the hosted
    chip immediately — the next paramless batch runs at the pushed gain."""
    from engine.sim_bridge import send_message, recv_message
    ctrl, res, coeff, cfg = _build_gain_design(0.5)
    srv, port = _host_server(res, cfg, coeff)
    try:
        conn = socket.create_connection(("127.0.0.1", port), timeout=10)
        try:
            send_message(conn, {"op": "set_grc_params",
                                "params": {"gain": {"gain": 0.125}}}, None)
            reply, _ = recv_message(conn)
        finally:
            conn.close()
        assert reply.get("ok")
        _assert_gain(_batch(port, _STIM), 0.125, "after live push")
    finally:
        srv.stop()


def test_dual_gain_retunes_each_cell_independently():
    """TWO chained gains: each block's tunable cell is INDIVIDUALLY hop-addressed
    — retuning one never touches the other (mid-chain WRITE delivery through an
    abutted chain, no corruption of the head cell). This is the per-block
    independence the multi-slider demo (gain_hw) rides on."""
    from PySide6.QtWidgets import QApplication
    QApplication.instance() or QApplication([])
    from engine.catalog import BlockCatalog
    from engine.io.chip_type_io import load_chip_type
    from ui.controller import AppController
    from model.connection import ChipPortEndpoint, BlockEndpoint
    from engine.port_config import live_coeff_writes, input_port_config

    cat = BlockCatalog.from_gr_kyttar()
    ct = load_chip_type(str(CT_PATH))
    ctrl = AppController(catalog=cat)
    ctrl.new_project("dual", "kyttar_10x12")
    g1 = ctrl.place_block("GainBlock", 0, 1, 0, library="lattrex.official",
                          params={"gain": 0.5})
    g2 = ctrl.place_block("GainBlock", 0, 2, 0, library="lattrex.official",
                          params={"gain": 0.5})
    ctrl.add_logical_connection(ChipPortEndpoint(chip=0, port="x16_in"),
                                BlockEndpoint(block=g1, port="sample"), name="in")
    ctrl.add_logical_connection(BlockEndpoint(block=g1, port="out"),
                                BlockEndpoint(block=g2, port="sample"), name="mid")
    ctrl.add_logical_connection(BlockEndpoint(block=g2, port="out"),
                                ChipPortEndpoint(chip=0, port="x16_out"), name="out")
    rep = ctrl.auto_route_all({"kyttar_10x12": ct})
    assert rep.ok
    res = ctrl.build()
    assert res.ok
    coeff = live_coeff_writes(ctrl.project, ctrl.registry, cat, 0,
                              build_result=res)
    assert set(coeff) == {g1, g2}, f"both gains must resolve: {sorted(coeff)}"
    cfg = input_port_config(ctrl.project, ctrl.registry, cat, 0, build_result=res)
    srv, port = _host_server(res, cfg, coeff)
    try:
        _assert_gain(_batch(port, _STIM), 0.25, "baseline 0.5*0.5")
        _assert_gain(_batch(port, _STIM, {g2: {"gain": 0.25}}), 0.125,
                     "g2 alone -> 0.5*0.25")
        _assert_gain(_batch(port, _STIM, {g1: {"gain": 0.25}}), 0.0625,
                     "g1 alone -> 0.25*0.25")
    finally:
        srv.stop()


def test_multicell_block_params_resolve_to_inner_cell():
    """MULTI-CELL blocks: a tunable param living in an INNER cell (CoherentRX's
    kp/ki in its string-keyed 'loop_filter' cell) resolves to THAT cell's hop,
    and to_writes diffs the whole block's data words — several params, several
    writes, all addressed to the right cell."""
    from PySide6.QtWidgets import QApplication
    QApplication.instance() or QApplication([])
    from engine.catalog import BlockCatalog
    from engine.io.chip_type_io import load_chip_type
    from ui.controller import AppController
    from model.connection import ChipPortEndpoint, BlockEndpoint
    from engine.port_config import live_coeff_writes

    cat = BlockCatalog.from_gr_kyttar()
    ct = load_chip_type(str(CT_PATH))
    ctrl = AppController(catalog=cat)
    ctrl.new_project("multicell", "kyttar_10x12")
    b = ctrl.place_block("CoherentRXBlock", 0, 1, 0, library="lattrex.official",
                         params={})
    blk = ctrl.project.block(b)
    m = live_coeff_writes(ctrl.project, ctrl.registry, cat, 0)
    assert b in m, f"CoherentRX not resolved as live-tunable: {sorted(m)}"
    spec = m[b]
    assert set(spec["params"]) == {"kp", "ki"}, spec["params"]
    assert set(spec["hops"]) == {"loop_filter"}, spec["hops"]
    # The hop addresses the loop_filter CELL, not the block corner.
    in_port = next(p for p in ct.ports if p.direction.value == "input")
    lf_cell = next(pc for pc in blk.placement.cells if pc.cell_id == "loop_filter")
    want = 30 - (abs(lf_cell.x - in_port.cell_x) + abs(lf_cell.y - in_port.cell_y))
    assert spec["hops"]["loop_filter"] == want, (spec["hops"], want)
    # to_writes: changing BOTH params yields per-word writes on that cell.
    base_kp, base_ki = spec["params"]["kp"], spec["params"]["ki"]
    writes = spec["to_writes"]({"kp": base_kp * 0.5, "ki": base_ki * 2.0})
    assert writes and all(cid == "loop_filter" for cid, _a, _w in writes), writes
    assert len({a for _c, a, _w in writes}) == len(writes) >= 2, writes
    # Unchanged values -> no writes (the diff is against the design baseline).
    assert spec["to_writes"]({"kp": base_kp, "ki": base_ki}) == []


def test_prog_structure_detects_shape_changes():
    """The ShapeChange comparator: equal programs match; a changed template,
    entry set, data-word layout, or FACE-word value each break identity — while
    a changed non-face data-word VALUE does not (that's what live WRITEs are)."""
    from gr_kyttar.placement.block import CellProgram, Port, EntryPoint, DataWord
    from engine.port_config import _prog_structure

    def prog(template="MULQ R0, R1", dw_value=100, dw_addr=1, face_val=2,
             entry="default"):
        return {0: CellProgram(
            inputs=[Port("sample", register=0)],
            outputs=[Port("out")],
            entries=[EntryPoint(entry)],
            data=[DataWord("gain", dw_value, address=dw_addr),
                  DataWord("dir", face_val, address=4, is_face=True)],
            assembly_template=template)}

    base = _prog_structure(prog())
    assert _prog_structure(prog(dw_value=200)) == base          # value-only: OK
    assert _prog_structure(prog(template="MUL R0, R1")) != base  # code change
    assert _prog_structure(prog(dw_addr=2)) != base              # layout change
    assert _prog_structure(prog(face_val=3)) != base             # face change
    assert _prog_structure(prog(entry="alt")) != base            # entry change


_2P2S_KYT = REPO / "examples" / "gain_2p2s" / "gain_2p2s.kyt"


@pytest.mark.skipif(not _2P2S_KYT.exists(), reason="gain_2p2s.kyt absent")
def test_multichip_live_writes_retune_each_die(qapp_placeholder=None):
    """MULTI-CHIP (2P2S) live tuning through the GUI server path: each of the
    four gains — including the FAR-chip ones reached by the COMPOSITE cross-chip
    hop (29 - 10 = 19 through the transparent wire) — retunes independently via
    set_grc_params, with zero crosstalk between the four streams, and the writes
    persist across batches."""
    from PySide6.QtWidgets import QApplication
    QApplication.instance() or QApplication([])
    from engine.catalog import BlockCatalog
    from ui.controller import AppController
    from ui.sim_controller import SimController
    from engine.sim_bridge import send_message, recv_message
    from engine.port_config import multi_chip_stream_targets

    cat = BlockCatalog.from_gr_kyttar()
    ctrl = AppController(catalog=cat)
    ctrl.open_project(str(_2P2S_KYT))
    r = ctrl.build()
    assert r.ok
    tg = multi_chip_stream_targets(ctrl.project, ctrl.registry, ctrl.catalog,
                                   build_result=r)
    assert set(tg) == {"A", "B", "C", "D"}

    # Stream -> gain block: A taps chip0's 'gain', B chip1's 'gain_1' (chain A),
    # C chip2's 'gain_2', D chip3's 'gain_3' (chain B) — the shipped layout.
    stream_block = {"A": "gain", "B": "gain_1", "C": "gain_2", "D": "gain_3"}
    inputs = {s: [0.5, 0.25] for s in "ABCD"}

    sim = SimController(ctrl)
    port = sim.start_gnuradio_server(port=0)
    try:
        assert port is not None and sim._multi is True

        def batch():
            c = socket.create_connection(("127.0.0.1", port), timeout=60)
            try:
                payload = np.concatenate(
                    [np.asarray(inputs[s], dtype=np.float32) for s in "ABCD"])
                streams = []
                for s in "ABCD":
                    t = tg[s]
                    streams.append({
                        "stream_id": s, "chip_id": t["chip_id"],
                        "out_chip": t["out_chip"], "entry_addr": t["entry_addr"],
                        "hop_count": t["hop_count"], "data_addrs": t["data_addrs"],
                        "out_tag": t["out_tag"], "complex": False, "raw": False,
                        "n_samples": 2})
                send_message(c, {"op": "process_batch_multichip",
                                 "streams": streams}, payload)
                rh, out = recv_message(c)
            finally:
                c.close()
            assert rh.get("ok", True) and "lengths" in rh, rh
            vals = [float(v) for v in np.asarray(out, dtype=np.float32)]
            got, off = {}, 0
            for sid, ln in zip(rh["stream_ids"], rh["lengths"]):
                got[sid] = vals[off:off + ln]
                off += ln
            return got

        def push(block, gain):
            c = socket.create_connection(("127.0.0.1", port), timeout=10)
            try:
                send_message(c, {"op": "set_grc_params",
                                 "params": {block: {"gain": gain}}})
                reply, _ = recv_message(c)
            finally:
                c.close()
            assert reply.get("ok"), reply

        def check(got, gains, label):
            for s in "ABCD":
                exp = [v * gains[s] for v in inputs[s]]
                err = max(abs(a - b) for a, b in zip(got[s], exp))
                assert len(got[s]) == 2 and err < 2e-3, \
                    f"{label}: stream {s} expected x{gains[s]}, got {got[s]}"

        check(batch(), {s: 0.5 for s in "ABCD"}, "baseline")
        # Retune the FAR-chip gain on chain A (chip1 — composite hop) alone.
        push(stream_block["B"], 0.25)
        check(batch(), {"A": 0.5, "B": 0.25, "C": 0.5, "D": 0.5}, "far-chip B")
        # Retune a HEAD gain on chain B alone; B's earlier write persists.
        push(stream_block["C"], 0.75)
        check(batch(), {"A": 0.5, "B": 0.25, "C": 0.75, "D": 0.5}, "head C")
        # Every remaining gain, distinct values — full independence.
        push(stream_block["A"], 0.125)
        push(stream_block["D"], 0.875)
        check(batch(), {"A": 0.125, "B": 0.25, "C": 0.75, "D": 0.875}, "all four")
    finally:
        sim.stop_gnuradio_server()


# --------------------------------------------------------------------------- #
# 2. Real GR client loop (the user path — NOT a hand-rolled RPC)
# --------------------------------------------------------------------------- #

def _gr_available() -> bool:
    try:
        r = subprocess.run(
            [GR_PYTHON, "-c", "from gnuradio import gr, blocks; import kyttar"],
            env={**os.environ, "PYTHONPATH": str(GR_OOT)},
            capture_output=True, timeout=30)
        return r.returncode == 0
    except Exception:  # noqa: BLE001
        return False


@pytest.mark.skipif(not _gr_available(), reason="GR python with kyttar OOT absent")
def test_repeat_bursts_apply_slider_within_one_run():
    """THE demo semantics for batch simulation: with ``repeat=True`` the source
    re-dispatches a burst each time the sink drains the previous one, so the
    flowgraph is a continuous burst loop — and a set_gain mid-run (the slider)
    changes the output ONE BURST LATER, within the SAME Run. Asserts the
    vector sink accumulated bursts at the OLD gain first and the NEW gain last."""
    ctrl, res, coeff, cfg = _build_gain_design(0.5)
    srv, port = _host_server(res, cfg, coeff)
    try:
        gr_script = textwrap.dedent(f"""
            import json, time
            import numpy as np
            from gnuradio import gr, blocks
            import kyttar
            PORT = {port}
            STIM = [0.5, -0.5, 0.25, 0.75, -0.75, 0.125]
            src = blocks.vector_source_f(STIM, True)      # repeating stimulus
            ksrc = kyttar.source(device_id='kyttar_0', port_name='x16_in',
                                 num_channels=1, server_host='127.0.0.1',
                                 server_port=PORT, complex_in=False,
                                 burst_len=len(STIM), repeat=True)
            gblk = kyttar.gain(device_id='kyttar_0', gain=0.5)
            ksink = kyttar.sink(device_id='kyttar_0', port_name='x16_out',
                                num_channels=1, server_port=PORT, hold_secs=5.0)
            vs = blocks.vector_sink_f()
            tb = gr.top_block()
            tb.connect(src, ksrc, gblk, ksink, vs)
            tb.start()

            def wait_len(n, timeout=30.0):
                t0 = time.time()
                while len(vs.data()) < n and time.time() - t0 < timeout:
                    time.sleep(0.1)
                return len(vs.data())

            got = wait_len(2 * len(STIM))          # >= 2 bursts at gain 0.5
            gblk.set_gain(0.25)                    # THE SLIDER, mid-run
            mark = len(vs.data())
            got2 = wait_len(mark + 3 * len(STIM))  # >= 3 more bursts after the drag
            tb.stop(); tb.wait()
            print('RESULT ' + json.dumps({{'n_before': int(mark),
                                           'data': list(vs.data())}}))
        """)
        genv = {**os.environ, "PYTHONPATH": str(GR_OOT)}
        r = subprocess.run([GR_PYTHON, "-c", gr_script], cwd=str(REPO), env=genv,
                           capture_output=True, text=True, timeout=180)
        out = r.stdout + r.stderr
        import json
        result = None
        for line in out.splitlines():
            if line.startswith("RESULT "):
                result = json.loads(line[len("RESULT "):])
        assert result is not None, "GR run produced no RESULT:\n" + out

        stim = np.array(_STIM)
        data = np.array(result["data"], dtype=float)
        nb = len(stim)
        assert len(data) >= result["n_before"] + 2 * nb, \
            f"repeat loop did not keep producing after set_gain: {len(data)} words"
        first = data[:nb]
        last = data[-nb:]

        def _matches(burst, gain):
            # Burst boundaries drift across the repeating stimulus (the source
            # keeps consuming while a dispatch is in flight), so compare as a
            # sorted multiset — amplitude is the claim, not phase.
            return np.max(np.abs(np.sort(burst) - np.sort(stim * gain))) < 2e-3

        assert _matches(first, 0.5), \
            f"first burst not at built gain 0.5: {first.tolist()}"
        assert _matches(last, 0.25), \
            f"post-slider bursts not at 0.25 (mid-run set_gain had no effect): " \
            f"{last.tolist()}"
    finally:
        srv.stop()


@pytest.mark.skipif(not _gr_available(), reason="GR python with kyttar OOT absent")
def test_set_gain_through_real_gr_client():
    """The REAL user path against ONE persistent server, both live-tune routes:

    1. LIVE PUSH: a gr.top_block runs at the built gain (0.5), then calls
       set_gain(0.25) mid-session and exits WITHOUT another burst. A paramless
       probe afterwards must read 0.25 — only the push (set_grc_params RPC)
       could have retuned the hosted chip, no burst header ever carried 0.25.
    2. HEADER PATH: a second GR run constructs gain(0.75) (what GRC's codegen
       does after a slider move + Run). Its burst header re-tunes the chip to
       0.75 even though the BUILT design still says 0.5."""
    # Server subprocess (this venv) hosting the built gain design + coeff map.
    port_holder = {}
    script = textwrap.dedent(f"""
        import time
        import simkyt
        from PySide6.QtWidgets import QApplication
        QApplication.instance() or QApplication([])
        from engine.catalog import BlockCatalog
        from engine.io.chip_type_io import load_chip_type
        from ui.controller import AppController
        from model.connection import ChipPortEndpoint, BlockEndpoint
        from engine.port_config import live_coeff_writes, input_port_config
        from engine.sim_bridge import SimServer
        from tests.conftest import CHIP_YAML

        cat = BlockCatalog.from_gr_kyttar()
        ct = load_chip_type(str(CHIP_YAML))
        ctrl = AppController(catalog=cat)
        ctrl.new_project('live_gain', 'kyttar_10x12')
        g = ctrl.place_block('GainBlock', 0, 1, 0, library='lattrex.official',
                             params={{'gain': 0.5}})
        ctrl.add_logical_connection(ChipPortEndpoint(chip=0, port='x16_in'),
                                    BlockEndpoint(block=g, port='sample'), name='in')
        ctrl.add_logical_connection(BlockEndpoint(block=g, port='out'),
                                    ChipPortEndpoint(chip=0, port='x16_out'), name='out')
        ctrl.auto_route_all({{'kyttar_10x12': ct}})
        res = ctrl.build()
        coeff = live_coeff_writes(ctrl.project, ctrl.registry, cat, 0, build_result=res)
        cfg = input_port_config(ctrl.project, ctrl.registry, cat, 0, build_result=res)
        chip = simkyt.Chip.from_yaml(str(CHIP_YAML))
        chip.load_bitstream_physical(res.words(0))
        pn, kw = cfg
        srv = SimServer(chip, host='127.0.0.1', port=0,
                        default_entries={{pn: kw['entry_addr']}},
                        default_hops={{pn: kw['hop_count']}})
        srv.set_coeff_writes(coeff)
        print('SERVER_READY', srv.start(), flush=True)
        try:
            while True: time.sleep(1)
        except KeyboardInterrupt:
            srv.stop()
    """)
    env = {**os.environ, "PYTHONPATH": str(REPO / "placekyt"),
           "QT_QPA_PLATFORM": "offscreen"}
    p = subprocess.Popen([sys.executable, "-c", script], cwd=str(REPO), env=env,
                         stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    try:
        t0 = time.time()
        while time.time() - t0 < 90:
            line = p.stdout.readline()
            if not line and p.poll() is not None:
                raise RuntimeError("server died:\n" + (p.stdout.read() or ""))
            if line.startswith("SERVER_READY"):
                port_holder["port"] = int(line.split()[1])
                break
        assert "port" in port_holder, "server did not become ready"
        port = port_holder["port"]

        gr_template = textwrap.dedent(f"""
            import json, time
            from gnuradio import gr, blocks
            import kyttar
            PORT = {port}
            STIM = [0.5, -0.5, 0.25, 0.75, -0.75, 0.125]
            GAIN = __GAIN__

            src = blocks.vector_source_f(STIM, False)
            ksrc = kyttar.source(device_id='kyttar_0', port_name='x16_in',
                                 num_channels=1, server_host='127.0.0.1',
                                 server_port=PORT, complex_in=False,
                                 burst_len=len(STIM))
            gblk = kyttar.gain(device_id='kyttar_0', gain=GAIN)
            ksink = kyttar.sink(device_id='kyttar_0', port_name='x16_out',
                                num_channels=1, server_port=PORT, hold_secs=0.0)
            vs = blocks.vector_sink_f()
            tb = gr.top_block()
            tb.connect(src, ksrc, gblk, ksink, vs)
            tb.run()
            if __PUSH__ is not None:
                # THE SLIDER: mid-session set_gain — the LIVE push retunes the
                # persistently-hosted chip immediately (no further burst here).
                gblk.set_gain(__PUSH__)
                time.sleep(1.0)   # let the daemon push land before exit
            print('RESULT ' + json.dumps(list(vs.data())))
        """)

        def _run_gr(gain, push=None):
            script = (gr_template.replace("__GAIN__", repr(float(gain)))
                      .replace("__PUSH__", repr(push)))
            genv = {**os.environ, "PYTHONPATH": str(GR_OOT)}
            r = subprocess.run([GR_PYTHON, "-c", script], cwd=str(REPO),
                               env=genv, capture_output=True, text=True,
                               timeout=180)
            out = r.stdout + r.stderr
            import json
            for line in out.splitlines():
                if line.startswith("RESULT "):
                    return np.array(json.loads(line[len("RESULT "):]), dtype=float)
            raise AssertionError("GR run produced no RESULT:\n" + out)

        stim = np.array(_STIM)
        # Run 1: built gain, then a mid-session set_gain(0.25) push, no burst after.
        out1 = _run_gr(0.5, push=0.25)
        assert len(out1) == len(stim) and np.max(np.abs(out1 - stim * 0.5)) < 2e-3, \
            f"run1 not at built gain 0.5: {out1.tolist()}"
        # LIVE PUSH proof: a paramless probe reads the pushed 0.25 — no burst
        # header ever carried that value, only the set_grc_params RPC could have.
        _assert_gain(_batch(port, _STIM), 0.25, "after mid-session set_gain push")
        # Run 2 (HEADER path): GRC codegen after a slider move constructs the new
        # value; its burst header retunes the chip although the design says 0.5.
        out2 = _run_gr(0.75)
        assert len(out2) == len(stim) and np.max(np.abs(out2 - stim * 0.75)) < 2e-3, \
            f"run2 header path did not retune to 0.75: {out2.tolist()}"

        # EXPLICIT block_name keying (the multi-instance robustness contract):
        # a marker constructed with block_name registers under that name
        # VERBATIM — construction order plays no part (GR codegen order is NOT
        # the .grc walk order; order-based keying can swap same-type blocks
        # and a live-tune would retune the WRONG cell — the gain_hw hazard).
        key_script = textwrap.dedent("""
            import kyttar
            from kyttar._batch_session import get_session
            b = kyttar.gain(device_id='kyttar_kt', gain=0.5, block_name='gain_2')
            a = kyttar.gain(device_id='kyttar_kt', gain=0.5)   # order-based
            b.start(); a.start()
            keys = sorted(get_session('kyttar_kt').grc_params.keys())
            print('KEYS ' + ','.join(keys))
        """)
        genv = {**os.environ, "PYTHONPATH": str(GR_OOT)}
        r = subprocess.run([GR_PYTHON, "-c", key_script], cwd=str(REPO),
                           env=genv, capture_output=True, text=True, timeout=60)
        out = r.stdout + r.stderr
        keys = None
        for line in out.splitlines():
            if line.startswith("KEYS "):
                keys = line[len("KEYS "):].split(",")
        assert keys == ["gain", "gain_2"], \
            f"explicit block_name keying broken: {keys} ({out[-400:]})"
    finally:
        p.terminate()
        try:
            p.wait(timeout=5)
        except Exception:  # noqa: BLE001
            p.kill()
