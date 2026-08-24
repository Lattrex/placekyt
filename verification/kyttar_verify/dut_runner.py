# SPDX-License-Identifier: GPL-3.0-or-later
"""Build a single Kyttar block between x16_in and x16_out and run a stimulus
through it on simKYT — the DUT side of block verification.

The proven path (see ``tests/test_autoroute.py`` and the coherent-RX demo test):

    new_project -> place_block(library=...) -> add_logical_connection x2
    -> auto_route_all -> BuildEngine.build -> simKYT inject/run/read

Critical substrate invariant captured here: the port's **target hop count** is
placement-dependent (``31 - distance`` from the input-port cell to the block's
landing cell), NOT a constant. A harness that hardcodes a hop count silently
gets zero outputs for any block whose landing cell is not exactly where the demo
happened to place it. This runner derives the hop from the routed input
connection and sets it once via ``set_port_target_hop_count``.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")


@dataclass
class DUTResult:
    """Outcome of building + running one block on simKYT."""

    ok: bool
    outputs_q15: list[int] = field(default_factory=list)   # uint16 words, one per input
    n_words: int = 0                                        # bitstream size
    entry_addr: int = 0
    hop_count: int = 0
    reason: str = ""                                        # populated when not ok


@dataclass
class ComplexDUTResult:
    """Outcome of building + running ONE complex (I/Q-in) block on simKYT.

    A complex block's input is delivered as a TWO-operand sample (xi, xq) — the
    same representation the live bridge (``engine/sim_bridge.py`` ``process_batch``
    ``complex=True``) and the on-chip Costas/MF lock tests use: each sample is
    ``WRITE xi -> in_regs[0]`` + ``WRITE xq -> in_regs[1]`` + one ``JUMP entry``.

    Its OUTPUT may itself be complex (the block's single output cell emits ``yi``
    then ``yq`` per trigger — e.g. the complex matched filter) OR a single real
    value (a soft/LLR demodulator: one word per trigger). The driver drains ALL
    words egressing per trigger and reports them:

      * ``outputs_q15`` — the FLAT per-trigger word lists (one list per sample),
        exactly as drained (so a caller can see how many words each sample emitted).
      * ``i_q15`` / ``q_q15`` — the de-interleaved I and Q channels (Q is empty for
        a real-output block, where each trigger emits one word).
    """

    ok: bool
    outputs_q15: list[list[int]] = field(default_factory=list)  # per-sample word lists
    i_q15: list[int] = field(default_factory=list)              # I channel (word 0)
    q_q15: list[int] = field(default_factory=list)              # Q channel (word 1)
    words_per_sample: int = 0          # how many words egressed per trigger (1 or 2)
    n_words: int = 0                   # bitstream size
    entry_addr: int = 0
    hop_count: int = 0
    in_regs: tuple[int, ...] = ()      # the resolved complex input registers (a0, a1)
    reason: str = ""                   # populated when not ok


# --- internal: lazy imports so importing this module never pulls Qt/engine
#     until a DUT is actually built (keeps `import kyttar_verify` cheap). ------

def _engine():
    from PySide6.QtWidgets import QApplication  # noqa: PLC0415
    app = QApplication.instance() or QApplication([])
    from engine.catalog import BlockCatalog  # noqa: PLC0415
    from engine.io.chip_type_io import load_chip_type  # noqa: PLC0415
    from engine.build import BuildEngine  # noqa: PLC0415
    from ui.controller import AppController  # noqa: PLC0415
    from model.connection import ChipPortEndpoint, BlockEndpoint  # noqa: PLC0415
    return (app, BlockCatalog, load_chip_type, BuildEngine, AppController,
            ChipPortEndpoint, BlockEndpoint)


def run_block_dut(
    block_type: str,
    inputs_q15: list[int],
    *,
    params: dict | None = None,
    chip_yaml: str,
    library: str = "lattrex.official",
    in_port: str = "sample",
    out_port: str = "out",
    place_xy: tuple[int, int] = (1, 1),
    orient: list[str] | None = None,
    data_run: int = 6000,
    jump_run: int = 90000,
    drain_run: int = 4000,
) -> DUTResult:
    """Build ``block_type`` wired x16_in -> block -> x16_out, run ``inputs_q15``
    through it on simKYT, and return the per-input output words.

    Args:
        block_type: catalog block type name (e.g. ``"GainBlock"``).
        inputs_q15: stimulus as uint16 Q15 words.
        params: block constructor params (e.g. ``{"gain": 0.5}``).
        chip_yaml: path to the chip-type YAML.
        library: block library namespace.
        in_port / out_port: the block's input/output port names.
        place_xy: where to anchor the block (default (1,1)).
        orient: D4 orientation ops applied to the placement BEFORE routing (a list
            of ``"cw"``/``"ccw"``/``"mirror_h"``/``"mirror_v"``). A correct block is
            ORIENTATION-INVARIANT: identical on-chip output in all 8 orientations.
        data_run / jump_run / drain_run: simKYT event budgets per step.

    Returns:
        :class:`DUTResult`. ``ok`` is False with ``reason`` set if routing or the
        build fails; in that case ``outputs_q15`` is empty.
    """
    import simkyt  # noqa: PLC0415

    (app, BlockCatalog, load_chip_type, BuildEngine, AppController,
     ChipPortEndpoint, BlockEndpoint) = _engine()

    cat = BlockCatalog.from_gr_kyttar()
    ct = load_chip_type(chip_yaml)
    # Derive the chip-type registry key from the YAML's declared name.
    ct_key = getattr(ct, "name", None) or "kyttar_10x12"

    ctrl = AppController(catalog=cat)
    ctrl.new_project("dut", ct_key)
    px, py = place_xy
    blk = ctrl.place_block(block_type, 0, px, py, library=library,
                           params=params or {})
    for _k in (orient or []):
        ctrl.project.block(blk).placement.transform(_k)

    ctrl.add_logical_connection(
        ChipPortEndpoint(chip=0, port="x16_in"),
        BlockEndpoint(block=blk, port=in_port), name="in_blk")
    ctrl.add_logical_connection(
        BlockEndpoint(block=blk, port=out_port),
        ChipPortEndpoint(chip=0, port="x16_out"), name="blk_out")

    rep = ctrl.auto_route_all({ct_key: ct})
    if not rep.ok:
        return DUTResult(False, reason="route failed: "
                         + "; ".join(f"{r.name}:{r.reason}" for r in rep.failed))

    bres = BuildEngine(cat, chip_yaml).build(ctrl.project, {ct_key: ct})
    if not bres.ok:
        return DUTResult(False, reason="build failed: "
                         + "; ".join(str(e) for e in bres.errors))

    words = bres.words(0)
    # Resolve the entry/input WITH the block's actual params, not the bare type
    # name. v2 blocks pack data low and instructions high, so a block's program
    # length — and therefore its entry address — shifts with its parameters (e.g.
    # a 3-tap FIR enters at 23, a default 1-tap FIR at 27). Resolving against the
    # default construction would land the JUMP mid-program and the block would
    # echo its input instead of computing. (GainBlock hid this: its program
    # length is fixed regardless of gain.)
    entry, ins = cat.resolved_io(block_type, params or {})
    data_addr = ins[0] if ins else 0

    # Placement-dependent hop: 31 - (number of cells the word transits from the
    # x16_in port cell to the block's landing cell, inclusive of the port's own
    # edge cell). The word rides its built fwd_face corridor cell-by-cell until it
    # has transited exactly `dist` cells, so `dist` MUST be the true corridor
    # length — NOT the manhattan span. Under most orientations the auto-router
    # draws a straight (manhattan) corridor and the two agree, but under some D4
    # rotations the corridor SNAKES (e.g. a single-real-rail block whose input
    # cell lands on the far edge): the routed path is longer than |dx|+|dy|, and a
    # manhattan hop stops the injected word SHORT of the block → zero/None output
    # (the NCO cw+cw / mirror_v+cw+cw+cw anti-orientation case). So derive `dist`
    # from the ACTUAL routed corridor when present: the port→block input net's
    # route is a point list [port_cell, ...transit..., landing]; `len(route)` is
    # exactly the transit count incl. the port's edge cell. Fall back to the
    # manhattan span only when the net is unrouted / direct-on-port (route absent
    # or not anchored at the port cell) — the proven explicit-placement path.
    # PREFER the build's own resolved landing (BuildResult.input_landings) — the
    # LIVE production contract. It walks the BUILT corridor faces and resolves
    # BOTH delivery shapes: ride-straight-into-the-block (entry = block entry,
    # regs = block input regs) AND the brokered shape, where the router legally
    # ends the corridor at a BROKER cell abutting the block (its turn program
    # flips toward the block and relays) — there the host must land the burst AT
    # the broker, in the broker's burst reg, and JUMP the broker's deliver
    # entry, NOT the block's. The old len(route)/manhattan derivation silently
    # mis-drives the brokered shape (word consumed at the broker with no turn
    # fired → zero output that looks exactly like an orientation failure — the
    # BlockInterleaver mirror_h+cw+cw case, INV-23 failure-mode-4 class).
    _chip_build = (getattr(bres, "chips", {}) or {}).get(0)
    land = (getattr(_chip_build, "input_landings", {}) or {}).get("in_blk")
    if land:
        hop = int(land["hop"]) & 0x1F
        entry = int(land["entry"])
        data_addr = (list(land.get("data_addrs")) or [data_addr])[0]
    else:
        port = ct.port("x16_in")
        port_cell = (port.cell_x, port.cell_y)
        blk_obj = ctrl.project.block(blk)
        landing = (blk_obj.placement.cells[0]
                   if blk_obj and blk_obj.placement and blk_obj.placement.cells
                   else None)
        in_conn = next((c for c in ctrl.project.connections
                        if c.name == "in_blk"), None)
        route = getattr(in_conn, "route", None) if in_conn is not None else None
        if (isinstance(route, list) and route
                and (route[0].x, route[0].y) == port_cell):
            # True corridor length: transit cells from the port cell to (and
            # incl.) the landing, matching how the word rides fwd_faces to
            # HOP_CNT==31.
            dist = len(route)
        elif landing is not None:
            # transit cells = |dx| + |dy| from port cell to landing, + 1 for the
            # port's own edge cell that the word is consumed past.
            dist = abs(landing.x - port.cell_x) + abs(landing.y - port.cell_y) + 1
        else:
            dist = abs(px - port.cell_x) + abs(py - port.cell_y) + 1
        hop = max(0, 31 - dist)

    chip = simkyt.Chip.from_yaml(chip_yaml)
    chip.load_bitstream_physical(words)
    chip.set_port_entry_address("x16_in", entry)

    outputs: list[int] = []
    for v in inputs_q15:
        chip.inject_data_physical([int(v) & 0xFFFF], target_hop_cnt=hop,
                                  target_addr=data_addr)
        chip.run(max_events=data_run)
        chip.inject_jump_physical(target_hop_cnt=hop, entry_addr=entry)
        chip.run(max_events=jump_run)
        got: list[int] = []
        while chip.output_available("x16_out"):
            w = chip.read_port_i16("x16_out").view("uint16").tolist()
            got.extend(int(x) & 0xFFFF for x in w)
            chip.release_output_ack("x16_out")
            chip.run(max_events=drain_run)
        outputs.append(got[-1] if got else None)

    return DUTResult(True, outputs_q15=outputs, n_words=len(words),
                     entry_addr=entry, hop_count=hop)


def _enc_write(hop: int, addr: int) -> int:
    """WRITE opcode 0x6, hop in [9:5], dest in [4:0]."""
    return (0x6 << 12) | ((hop & 0x1F) << 5) | (addr & 0x1F)


def _enc_jump(hop: int, entry: int) -> int:
    """JUMP opcode 0x7, hop in [9:5], entry in [4:0]."""
    return (0x7 << 12) | ((hop & 0x1F) << 5) | (entry & 0x1F)


def run_block_dut_pipelined(
    block_type: str,
    samples,
    *,
    params: dict | None = None,
    chip_yaml: str,
    library: str = "lattrex.official",
    in_ports=("sample",),
    out_port: str = "out",
    place_xy: tuple[int, int] = (1, 1),
    max_events: int | None = None,
) -> RateDUTResult:
    """Build ``block_type`` (x16_in -> block -> x16_out) and drive it SATURATED —
    the WHOLE burst is enqueued as raw WRITE/DATA/JUMP words via
    ``queue_words_physical`` and processed in ONE continuous ``run()`` with NO
    drain-between-samples. The input port's single-outstanding handshake paces the
    corridor as a FIFO; multiple samples are in flight at once.

    This is the PIPELINE-SATURATION oracle: a correct block's saturated output must
    equal its own per-sample output (:func:`run_block_dut` / ``_rate`` / ``_complex``),
    which is already the GNU-Radio-verified reference. A block that DIVERGES when the
    pipeline is full has a feedback/handshake hazard the per-sample harness cannot
    see (e.g. a data-only feedback loop that assumes inter-sample quiescence — the
    Costas dphase / Gardner period case). Returns the FLAT egress word stream.

    ``samples`` is a list of per-sample operand tuples: ``(w,)`` for a 1-operand
    real block, ``(i, q)`` for a complex block (matching ``in_ports``). Operands are
    already uint16 words (Q15 or raw); the caller quantises. ``in_ports`` names the
    block's input ports in operand order (their registers come from ``resolved_io``).
    """
    import simkyt  # noqa: PLC0415

    (app, BlockCatalog, load_chip_type, BuildEngine, AppController,
     ChipPortEndpoint, BlockEndpoint) = _engine()

    cat = BlockCatalog.from_gr_kyttar()
    ct = load_chip_type(chip_yaml)
    ct_key = getattr(ct, "name", None) or "kyttar_10x12"
    ctrl = AppController(catalog=cat)
    ctrl.new_project("dut_pipe", ct_key)
    px, py = place_xy
    blk = ctrl.place_block(block_type, 0, px, py, library=library,
                           params=params or {})
    # Wire every input port to x16_in (multi-operand blocks land on one corridor).
    for i, ip in enumerate(in_ports):
        ctrl.add_logical_connection(
            ChipPortEndpoint(chip=0, port="x16_in"),
            BlockEndpoint(block=blk, port=ip), name=f"in{i}")
    ctrl.add_logical_connection(
        BlockEndpoint(block=blk, port=out_port),
        ChipPortEndpoint(chip=0, port="x16_out"), name="blk_out")

    rep = ctrl.auto_route_all({ct_key: ct})
    if not rep.ok:
        return RateDUTResult(False, reason="route failed: "
                             + "; ".join(f"{r.name}:{r.reason}" for r in rep.failed))
    bres = BuildEngine(cat, chip_yaml).build(ctrl.project, {ct_key: ct})
    if not bres.ok:
        return RateDUTResult(False, reason="build failed: "
                             + "; ".join(str(e) for e in bres.errors))
    words = bres.words(0)
    entry, ins = cat.resolved_io(block_type, params or {}, library=library)
    if len(ins) < len(in_ports):
        return RateDUTResult(False, reason=f"block resolved {len(ins)} input reg(s); "
                             f"need {len(in_ports)} for ports {in_ports}")
    addrs = [int(a) for a in ins[:len(in_ports)]]

    port = ct.port("x16_in")
    blk_obj = ctrl.project.block(blk)
    landing = (blk_obj.placement.cells[0]
               if blk_obj and blk_obj.placement and blk_obj.placement.cells else None)
    if landing is not None:
        dist = abs(landing.x - port.cell_x) + abs(landing.y - port.cell_y) + 1
    else:
        dist = abs(px - port.cell_x) + abs(py - port.cell_y) + 1
    hop = max(0, 31 - dist)

    chip = simkyt.Chip.from_yaml(chip_yaml)
    chip.load_bitstream_physical(words)
    chip.set_port_entry_address("x16_in", entry)

    # Build the whole burst: per sample, WRITE(addr_k),op_k ... then JUMP(entry).
    stream: list[int] = []
    for tup in samples:
        ops = tup if isinstance(tup, (tuple, list)) else (tup,)
        for k, w in enumerate(ops):
            stream.append(_enc_write(hop, addrs[k]))
            stream.append(int(w) & 0xFFFF)
        stream.append(_enc_jump(hop, entry))
    chip.queue_words_physical("x16_in", stream)
    # SAFETY CEILING: never call run() unbounded. A block that livelocks under
    # saturated drive (multi-cell fan-out that needs per-sample quiescence, or an
    # unclearable feedback loop) leaves the event queue permanently non-empty, so an
    # uncapped run() spins forever at 100% CPU. Default to a generous per-sample
    # budget; a genuine block needs only a few hundred events/sample. If we hit the
    # cap the run did NOT complete -> report it as a livelock, don't return garbage.
    cap = max_events if max_events is not None else max(50_000, 2_000 * max(1, len(samples)))
    res = chip.run(max_events=cap)
    if isinstance(res, dict) and not res.get("completed", True):
        # DO NOT name a conclusion here. Cap expiry has TWO causes and this
        # function cannot tell them apart: a genuine livelock, or a default
        # budget too small for a big block. The 2000/sample default is sized
        # for small blocks; a large multi-stage pipeline can legitimately cost
        # more (measured: FFT64, 84 cells over six serialize-LOCKed stages,
        # 2873 events/sample), and reporting that as "block livelocks" cost a
        # real investigation. Distinguish them by MEASURING the per-sample
        # cost on the per-sample path and re-running with a DERIVED budget: a
        # livelock never reaches quiescence at any cap; a shortfall completes
        # as soon as the budget is real.
        per = cap / max(1, len(samples))
        return RateDUTResult(
            False, reason=f"pipeline did NOT reach quiescence under saturated drive "
            f"(stop_reason={res.get('stop_reason')}, events={res.get('events_processed')}, "
            f"cap={cap} = {per:.0f}/sample over {len(samples)} samples) — EITHER a "
            f"livelock OR a budget shortfall. Measure this block's per-sample event "
            f"cost on the per-sample path and pass max_events derived from it; if it "
            f"still does not complete, the livelock is real")

    flat = [int(v) & 0xFFFF for (v, _d, _t) in chip.read_port_words_timed("x16_out")]
    return RateDUTResult(True, outputs_q15=flat, n_words=len(words),
                         entry_addr=entry, hop_count=hop)


@dataclass
class RateDUTResult:
    """Outcome of running a RATE-CHANGING (real-in) block on simKYT.

    Unlike :func:`run_block_dut` (which keeps only the LAST word per trigger — fine
    for 1-in-1-out and rate-REDUCING blocks), this drains EVERY word that egresses
    per trigger and returns the FLAT output stream. Use for rate-EXPANDING blocks
    (upsampler / interpolating filter): one input -> N outputs in a burst.

      * ``outputs_q15`` — the flat output word stream (all triggers concatenated).
      * ``per_trigger`` — list of per-trigger word lists (to assert the rate).
    """

    ok: bool
    outputs_q15: list[int] = field(default_factory=list)        # flat output stream
    per_trigger: list[list[int]] = field(default_factory=list)  # words per input
    n_words: int = 0
    entry_addr: int = 0
    hop_count: int = 0
    reason: str = ""


def run_block_dut_rate(
    block_type: str,
    inputs_q15: list[int],
    *,
    params: dict | None = None,
    chip_yaml: str,
    library: str = "lattrex.official",
    in_port: str = "x",
    out_port: str = "out",
    place_xy: tuple[int, int] = (1, 1),
    orient: list[str] | None = None,
    data_run: int = 6000,
    jump_run: int = 120000,
    drain_run: int = 6000,
) -> RateDUTResult:
    """Build ``block_type`` (x16_in -> block -> x16_out) and run ``inputs_q15``,
    draining ALL words per trigger — the rate-aware driver for rate-CHANGING blocks.

    One input is injected + triggered per element; every word that egresses before
    the next input is captured (a rate-expanding block emits a burst). Returns the
    flat output stream + the per-trigger word lists.

    NOTE the no-FIFO output port is single-outstanding, so we drain (read + ack +
    run) in a loop after each trigger until the port is empty — the burst surfaces
    one word at a time as each is consumed. This is why ``run_block_dut`` (which
    keeps only the last word) cannot verify a rate-expanding block.
    """
    import simkyt  # noqa: PLC0415

    (app, BlockCatalog, load_chip_type, BuildEngine, AppController,
     ChipPortEndpoint, BlockEndpoint) = _engine()

    cat = BlockCatalog.from_gr_kyttar()
    ct = load_chip_type(chip_yaml)
    ct_key = getattr(ct, "name", None) or "kyttar_10x12"
    ctrl = AppController(catalog=cat)
    ctrl.new_project("dut_rate", ct_key)
    px, py = place_xy
    blk = ctrl.place_block(block_type, 0, px, py, library=library,
                           params=params or {})
    for _k in (orient or []):
        ctrl.project.block(blk).placement.transform(_k)
    ctrl.add_logical_connection(
        ChipPortEndpoint(chip=0, port="x16_in"),
        BlockEndpoint(block=blk, port=in_port), name="in_blk")
    ctrl.add_logical_connection(
        BlockEndpoint(block=blk, port=out_port),
        ChipPortEndpoint(chip=0, port="x16_out"), name="blk_out")
    rep = ctrl.auto_route_all({ct_key: ct})
    if not rep.ok:
        return RateDUTResult(False, reason="route failed: "
                             + "; ".join(f"{r.name}:{r.reason}" for r in rep.failed))
    bres = BuildEngine(cat, chip_yaml).build(ctrl.project, {ct_key: ct})
    if not bres.ok:
        return RateDUTResult(False, reason="build failed: "
                             + "; ".join(str(e) for e in bres.errors))
    words = bres.words(0)
    entry, ins = cat.resolved_io(block_type, params or {})
    data_addr = ins[0] if ins else 0
    # Placement-dependent hop, CORRIDOR-accurate (INV-1 refinement — the same
    # derivation as run_block_dut): prefer the build's own resolved landing
    # (BuildResult.input_landings — covers the brokered delivery shape), then the
    # routed corridor length, then the manhattan fallback. The bare manhattan
    # span stops the injected word SHORT whenever the auto-router SNAKES the
    # input corridor (the 180°-family D4 orientations of a single-real-rail
    # block: NCO historically, ChirpGenerator's rate gate concretely) — a
    # harness bug that masquerades as an orientation failure of the block.
    _chip_build = (getattr(bres, "chips", {}) or {}).get(0)
    land = (getattr(_chip_build, "input_landings", {}) or {}).get("in_blk")
    if land:
        hop = int(land["hop"]) & 0x1F
        entry = int(land["entry"])
        data_addr = (list(land.get("data_addrs")) or [data_addr])[0]
    else:
        port = ct.port("x16_in")
        port_cell = (port.cell_x, port.cell_y)
        blk_obj = ctrl.project.block(blk)
        landing = (blk_obj.placement.cells[0]
                   if blk_obj and blk_obj.placement and blk_obj.placement.cells
                   else None)
        in_conn = next((c for c in ctrl.project.connections
                        if c.name == "in_blk"), None)
        route = getattr(in_conn, "route", None) if in_conn is not None else None
        if (isinstance(route, list) and route
                and (route[0].x, route[0].y) == port_cell):
            dist = len(route)
        elif landing is not None:
            dist = abs(landing.x - port.cell_x) + abs(landing.y - port.cell_y) + 1
        else:
            dist = abs(px - port.cell_x) + abs(py - port.cell_y) + 1
        hop = max(0, 31 - dist)

    chip = simkyt.Chip.from_yaml(chip_yaml)
    chip.load_bitstream_physical(words)
    chip.set_port_entry_address("x16_in", entry)

    per_trigger: list[list[int]] = []
    flat: list[int] = []
    for v in inputs_q15:
        chip.inject_data_physical([int(v) & 0xFFFF], target_hop_cnt=hop,
                                  target_addr=data_addr)
        chip.run(max_events=data_run)
        chip.inject_jump_physical(target_hop_cnt=hop, entry_addr=entry)
        chip.run(max_events=jump_run)
        got: list[int] = []
        # Drain the whole burst: read + ack + run until the port stops producing.
        while chip.output_available("x16_out"):
            w = chip.read_port_i16("x16_out").view("uint16").tolist()
            got.extend(int(x) & 0xFFFF for x in w)
            chip.release_output_ack("x16_out")
            chip.run(max_events=drain_run)
        per_trigger.append(got)
        flat.extend(got)

    return RateDUTResult(True, outputs_q15=flat, per_trigger=per_trigger,
                         n_words=len(words), entry_addr=entry, hop_count=hop)


def _to_q15(v: float) -> int:
    """float in [-1, 1) -> uint16 Q15 (saturating). Mirrors the live bridge's
    ``_float_to_q15`` so the DUT is driven with the SAME quantization the
    GNURadio<->placeKYT bridge uses (no harness/bridge skew)."""
    q = int(round(float(v) * 32768.0))
    q = max(-32768, min(32767, q))
    return q & 0xFFFF


def run_block_dut_complex(
    block_type: str,
    inputs_iq,
    *,
    params: dict | None = None,
    chip_yaml: str,
    library: str = "lattrex.official",
    in_ports: tuple[str, str] = ("xi", "xq"),
    out_port: str | None = None,
    place_xy: tuple[int, int] = (1, 1),
    orient: list[str] | None = None,
    words_per_sample: int | None = None,
    data_run: int = 6000,
    jump_run: int = 200000,
    drain_run: int = 8000,
) -> ComplexDUTResult:
    """Build ``block_type`` (a COMPLEX-input block) wired ``x16_in`` -> block ->
    ``x16_out`` and run an I/Q stimulus through it on simKYT.

    This is the complex twin of :func:`run_block_dut`. A complex sample is
    delivered as a TWO-operand transaction — ``WRITE xi -> in_regs[0]``,
    ``WRITE xq -> in_regs[1]``, then one ``JUMP entry`` — exactly the
    representation the proven complex blocks ingest (the ComplexCostasLoop /
    matched-filter landing cell: xi@R0, xq@R1) and the live bridge's
    ``process_batch`` ``complex=True`` path uses. The two input registers come
    from :meth:`BlockCatalog.resolved_io` (INV-6); the port hop is derived from
    the landing cell (INV-1) — never hardcoded.

    OUTPUT capture: a complex block's single output cell emits its words (``yi``
    then ``yq``, or one real LLR) per trigger, all egressing through ``x16_out``.
    Critically, ONLY the block's PRIMARY output port is wired to ``x16_out`` — a
    complex output cell emits both operands from one cell, and they ride the SAME
    bus corridor out interleaved; wiring a SECOND net (yq) to the same port
    creates a dual-route-to-one-port conflict that silently kills egress (verified:
    yi-only -> bit-exact [yi,yq]; yi+yq -> zero output). The driver drains all
    words egressing per trigger and de-interleaves them into I and Q.

    Args:
        block_type: catalog block type (e.g. ``"ComplexRRCMatchedFilterBlock"``).
        inputs_iq: stimulus as a complex numpy array / list of complex, or a list
            of ``(i, q)`` float pairs.
        params: block constructor params.
        chip_yaml: path to the chip-type YAML.
        library: block library namespace.
        in_ports: the block's two complex input port names (default ``xi``/``xq``).
        out_port: the block's PRIMARY output port name; if None, the first
            ``out``-direction port from the block's port map is used.
        place_xy: where to anchor the block.
        words_per_sample: how many output words each trigger emits (1 for a real
            LLR output, 2 for a complex yi/yq output). Auto-detected from the first
            non-empty drain when None.
        data_run / jump_run / drain_run: simKYT event budgets per step.

    Returns:
        :class:`ComplexDUTResult`. ``ok`` is False with ``reason`` set on failure.
    """
    import numpy as np  # noqa: PLC0415
    import simkyt  # noqa: PLC0415

    (app, BlockCatalog, load_chip_type, BuildEngine, AppController,
     ChipPortEndpoint, BlockEndpoint) = _engine()

    # --- normalize the I/Q stimulus to (i_float, q_float) pairs ----------------
    arr = np.asarray(inputs_iq)
    if np.iscomplexobj(arr):
        pairs = [(float(c.real), float(c.imag)) for c in arr]
    elif arr.ndim == 2 and arr.shape[1] == 2:
        pairs = [(float(i), float(q)) for i, q in arr]
    else:
        return ComplexDUTResult(
            False, reason="inputs_iq must be complex or an (N,2) [i,q] array")

    cat = BlockCatalog.from_gr_kyttar()
    ct = load_chip_type(chip_yaml)
    ct_key = getattr(ct, "name", None) or "kyttar_10x12"

    ctrl = AppController(catalog=cat)
    ctrl.new_project("dut_cplx", ct_key)
    px, py = place_xy
    blk = ctrl.place_block(block_type, 0, px, py, library=library,
                           params=params or {})
    for _k in (orient or []):
        ctrl.project.block(blk).placement.transform(_k)

    # Resolve the block's external output port (the PRIMARY one) if unspecified.
    if out_port is None:
        pm = cat.port_map(block_type, params or {}, library=library)
        outs = [p.name for p in pm.ports if p.direction == "out"]
        if not outs:
            return ComplexDUTResult(False, reason="block declares no output port")
        out_port = outs[0]

    # Wire the complex input: x16_in -> xi AND x16_in -> xq (both operands land on
    # the block's two input registers). Wire ONLY the primary output to x16_out.
    ctrl.add_logical_connection(
        ChipPortEndpoint(chip=0, port="x16_in"),
        BlockEndpoint(block=blk, port=in_ports[0]), name="in_xi")
    ctrl.add_logical_connection(
        ChipPortEndpoint(chip=0, port="x16_in"),
        BlockEndpoint(block=blk, port=in_ports[1]), name="in_xq")
    ctrl.add_logical_connection(
        BlockEndpoint(block=blk, port=out_port),
        ChipPortEndpoint(chip=0, port="x16_out"), name="blk_out")

    rep = ctrl.auto_route_all({ct_key: ct})
    if not rep.ok:
        return ComplexDUTResult(False, reason="route failed: "
                                + "; ".join(f"{r.name}:{r.reason}"
                                            for r in rep.failed))

    bres = BuildEngine(cat, chip_yaml).build(ctrl.project, {ct_key: ct})
    if not bres.ok:
        return ComplexDUTResult(False, reason="build failed: "
                                + "; ".join(str(e) for e in bres.errors))

    words = bres.words(0)
    # INV-6: resolve entry + the TWO input registers WITH params, not the type.
    entry, ins = cat.resolved_io(block_type, params or {}, library=library)
    if len(ins) < 2:
        return ComplexDUTResult(
            False, reason=f"block resolved {len(ins)} input register(s); a complex "
            "block must declare two (xi, xq)")
    a0, a1 = int(ins[0]), int(ins[1])

    # INV-1: placement-dependent hop. The naive ``31 - manhattan(port, landing)`` is
    # correct ONLY when the input corridor runs STRAIGHT (the flyline placer normally
    # guarantees this). Under an arbitrary D4 orientation the corridor SNAKES — the word
    # transits cells on their built fwd_face, not a straight line — so the manhattan hop
    # lands the WRITE/JUMP short of (or past) the real landing cell and the block never
    # fires. The BUILD resolves the corridor-ACCURATE landing (hop / entry / data_addrs)
    # by walking the built faces (build._resolve_input_landings), and the LIVE bridge
    # (engine.port_config) drives the chip from exactly that. So the DUT — to be a
    # faithful oracle — prefers the built landing, falling back to the manhattan estimate
    # only when no net has a recorded landing (an unrouted direct-on-port placement).
    port = ct.port("x16_in")
    blk_obj = ctrl.project.block(blk)
    landing = (blk_obj.placement.cells[0]
               if blk_obj and blk_obj.placement and blk_obj.placement.cells
               else None)
    if landing is not None:
        dist = abs(landing.x - port.cell_x) + abs(landing.y - port.cell_y) + 1
    else:
        dist = abs(px - port.cell_x) + abs(py - port.cell_y) + 1
    hop = max(0, 31 - dist)

    # Corridor-accurate landing (preferred). The two input nets (xi, xq) deliver ONE
    # complex sample; when the corridor rides straight both resolve to the block's input
    # cell with data_addrs = [xi_reg, xq_reg] and entry = block entry. Pick the landing
    # whose data_addrs cover BOTH operands (the straight, complete delivery) so the two
    # operands + trigger all address the same cell the corridor actually reaches.
    cb = getattr(bres, "chips", {}).get(0)
    il = (getattr(cb, "input_landings", {}) or {}) if cb is not None else {}
    hop_i = hop; addr_i = a0; addr_q = a1; entry_i = entry
    best = None
    for lname in ("in_xi", "in_xq"):
        ld = il.get(lname)
        if ld and ld.get("data_addrs"):
            # Prefer a landing that carries both operand registers (straight complex ride).
            if best is None or len(ld["data_addrs"]) > len(best["data_addrs"]):
                best = ld
    if best is not None:
        das = best["data_addrs"]
        hop_i = int(best["hop"]) & 0x1F
        entry_i = int(best["entry"])
        addr_i = int(das[0])
        addr_q = int(das[1]) if len(das) > 1 else int(das[0])

    chip = simkyt.Chip.from_yaml(chip_yaml)
    chip.load_bitstream_physical(words)
    chip.set_port_entry_address("x16_in", entry)

    per_sample: list[list[int]] = []
    for (i_f, q_f) in pairs:
        # ONE complex sample = WRITE xi -> a0, WRITE xq -> a1, then JUMP entry.
        chip.inject_data_physical([_to_q15(i_f)], target_hop_cnt=hop_i,
                                  target_addr=addr_i)
        chip.run(max_events=data_run)
        chip.inject_data_physical([_to_q15(q_f)], target_hop_cnt=hop_i,
                                  target_addr=addr_q)
        chip.run(max_events=data_run)
        chip.inject_jump_physical(target_hop_cnt=hop_i, entry_addr=entry_i)
        chip.run(max_events=jump_run)
        got: list[int] = []
        while chip.output_available("x16_out"):
            w = chip.read_port_i16("x16_out").view("uint16").tolist()
            got.extend(int(x) & 0xFFFF for x in w)
            chip.release_output_ack("x16_out")
            chip.run(max_events=drain_run)
        per_sample.append(got)

    # Determine words-per-sample (auto-detect from the first non-empty drain).
    wps = words_per_sample
    if wps is None:
        first = next((len(g) for g in per_sample if g), 0)
        wps = first if first in (1, 2) else (first or 1)

    # De-interleave. A sample that emitted fewer words than wps is recorded as a
    # missing (None) entry for that channel — the comparator treats None as a hard
    # egress failure, so a stalled/short output cannot silently read "green".
    i_ch: list = []
    q_ch: list = []
    for g in per_sample:
        i_ch.append(g[0] if len(g) >= 1 else None)
        if wps >= 2:
            q_ch.append(g[1] if len(g) >= 2 else None)

    return ComplexDUTResult(
        True, outputs_q15=per_sample, i_q15=i_ch, q_q15=q_ch,
        words_per_sample=wps, n_words=len(words), entry_addr=entry,
        hop_count=hop, in_regs=(a0, a1))


def run_block_dut_real_to_complex(
    block_type: str,
    inputs,
    *,
    params: dict | None = None,
    chip_yaml: str,
    library: str = "lattrex.official",
    in_port: str | None = None,
    out_port: str | None = None,
    place_xy: tuple[int, int] = (1, 1),
    words_per_sample: int = 2,
    data_run: int = 6000,
    jump_run: int = 200000,
    drain_run: int = 8000,
) -> ComplexDUTResult:
    """Build a REAL-input, COMPLEX-output block (e.g. the FM modulator / VCO
    ``analog.frequency_modulator_fc``) wired ``x16_in`` -> block -> ``x16_out`` and
    run a REAL stimulus through it on simKYT.

    Unlike :func:`run_block_dut_complex` (two-operand xi/xq sample), a real->complex
    block ingests ONE real word per trigger: ``WRITE x -> in_regs[0]`` + one
    ``JUMP entry``. The output cell emits ``yi`` then ``yq`` per trigger (the same
    complex-egress convention as the NCO), drained + de-interleaved into I/Q.

    Args:
        block_type: catalog block type (e.g. ``"FrequencyModulatorBlock"``).
        inputs: real stimulus — a list/array of floats in [-1, 1], or uint16 Q15
            words (auto-detected: ints in [0, 0xFFFF] are treated as Q15 words).
        in_port: the block's single real input port name; first ``in`` port if None.
        out_port: the block's PRIMARY output port name; first ``out`` port if None.
        words_per_sample: output words per trigger (2 for a complex yi/yq output).
        Others: as :func:`run_block_dut_complex`.

    Returns:
        :class:`ComplexDUTResult` (``in_regs`` is the single resolved input reg).
    """
    import numpy as np  # noqa: PLC0415
    import simkyt  # noqa: PLC0415

    (app, BlockCatalog, load_chip_type, BuildEngine, AppController,
     ChipPortEndpoint, BlockEndpoint) = _engine()

    # Normalize the real stimulus to Q15 words (accept floats OR uint16 words).
    arr = list(inputs)
    q15_in: list[int] = []
    for v in arr:
        if isinstance(v, (int,)) and not isinstance(v, bool) and 0 <= v <= 0xFFFF:
            q15_in.append(int(v) & 0xFFFF)
        else:
            q15_in.append(_to_q15(float(v)))

    cat = BlockCatalog.from_gr_kyttar()
    ct = load_chip_type(chip_yaml)
    ct_key = getattr(ct, "name", None) or "kyttar_10x12"

    ctrl = AppController(catalog=cat)
    ctrl.new_project("dut_r2c", ct_key)
    px, py = place_xy
    blk = ctrl.place_block(block_type, 0, px, py, library=library,
                           params=params or {})

    pm = cat.port_map(block_type, params or {}, library=library)
    if in_port is None:
        ins_p = [p.name for p in pm.ports if p.direction == "in"]
        if not ins_p:
            return ComplexDUTResult(False, reason="block declares no input port")
        in_port = ins_p[0]
    if out_port is None:
        outs_p = [p.name for p in pm.ports if p.direction == "out"]
        if not outs_p:
            return ComplexDUTResult(False, reason="block declares no output port")
        out_port = outs_p[0]

    ctrl.add_logical_connection(
        ChipPortEndpoint(chip=0, port="x16_in"),
        BlockEndpoint(block=blk, port=in_port), name="in_x")
    ctrl.add_logical_connection(
        BlockEndpoint(block=blk, port=out_port),
        ChipPortEndpoint(chip=0, port="x16_out"), name="blk_out")

    rep = ctrl.auto_route_all({ct_key: ct})
    if not rep.ok:
        return ComplexDUTResult(False, reason="route failed: "
                                + "; ".join(f"{r.name}:{r.reason}"
                                            for r in rep.failed))

    bres = BuildEngine(cat, chip_yaml).build(ctrl.project, {ct_key: ct})
    if not bres.ok:
        return ComplexDUTResult(False, reason="build failed: "
                                + "; ".join(str(e) for e in bres.errors))

    words = bres.words(0)
    entry, ins = cat.resolved_io(block_type, params or {}, library=library)
    if len(ins) < 1:
        return ComplexDUTResult(False, reason="block resolved 0 input registers")
    a0 = int(ins[0])

    port = ct.port("x16_in")
    blk_obj = ctrl.project.block(blk)
    landing = (blk_obj.placement.cells[0]
               if blk_obj and blk_obj.placement and blk_obj.placement.cells
               else None)
    if landing is not None:
        dist = abs(landing.x - port.cell_x) + abs(landing.y - port.cell_y) + 1
    else:
        dist = abs(px - port.cell_x) + abs(py - port.cell_y) + 1
    hop = max(0, 31 - dist)

    chip = simkyt.Chip.from_yaml(chip_yaml)
    chip.load_bitstream_physical(words)
    chip.set_port_entry_address("x16_in", entry)

    per_sample: list[list[int]] = []
    for w_in in q15_in:
        # ONE real sample = WRITE x -> a0, then JUMP entry.
        chip.inject_data_physical([w_in], target_hop_cnt=hop, target_addr=a0)
        chip.run(max_events=data_run)
        chip.inject_jump_physical(target_hop_cnt=hop, entry_addr=entry)
        chip.run(max_events=jump_run)
        got: list[int] = []
        while chip.output_available("x16_out"):
            ww = chip.read_port_i16("x16_out").view("uint16").tolist()
            got.extend(int(x) & 0xFFFF for x in ww)
            chip.release_output_ack("x16_out")
            chip.run(max_events=drain_run)
        per_sample.append(got)

    wps = words_per_sample
    i_ch: list = []
    q_ch: list = []
    for g in per_sample:
        i_ch.append(g[0] if len(g) >= 1 else None)
        if wps >= 2:
            q_ch.append(g[1] if len(g) >= 2 else None)

    return ComplexDUTResult(
        True, outputs_q15=per_sample, i_q15=i_ch, q_q15=q_ch,
        words_per_sample=wps, n_words=len(words), entry_addr=entry,
        hop_count=hop, in_regs=(a0,))


def run_block_dut_complex2(
    block_type: str,
    a_stream,
    b_stream,
    *,
    params: dict | None = None,
    chip_yaml: str,
    library: str = "lattrex.official",
    in_ports: tuple[str, str, str, str] = ("ai", "aq", "bi", "bq"),
    out_port: str | None = None,
    place_xy: tuple[int, int] = (1, 1),
    orient: list[str] | None = None,
    words_per_sample: int = 2,
    data_run: int = 6000,
    jump_run: int = 200000,
    drain_run: int = 8000,
) -> ComplexDUTResult:
    """Build a TWO-EXTERNAL-COMPLEX-STREAM block (4 operands per sample as two
    (re, im) pairs from two independent sources — ``add_cc`` / ``sub_cc`` /
    ``multiply_cc``) wired ``x16_in`` -> block -> ``x16_out`` and run the paired
    stimulus through it on simKYT.

    This is the 2-stream generalization of :func:`run_block_dut_complex`'s
    two-operand transaction. Each sample is delivered as TWO complex PACKETS —
    exactly the on-chip representation two upstream complex blocks produce
    (each source: multi-WRITE + ONE JUMP; the landing cell's counting-join
    entry single-fires on the second trigger in any order):

        WRITE a_re -> in_regs[0]; WRITE a_im -> in_regs[1]; JUMP entry   (a)
        WRITE b_re -> in_regs[2]; WRITE b_im -> in_regs[3]; JUMP entry   (b)

    All four operand ports must live on ONE landing cell (the port-map/
    ``resolved_io`` contract); ``entry`` is the landing cell's first entry —
    for a counting-join block that IS the join. Only the block's primary
    complex output rail (``yi``) is wired to ``x16_out``; the (yi, yq) packet
    egresses interleaved and is de-interleaved here (same convention as
    :func:`run_block_dut_complex`).

    REUSE NOTE: this driver is deliberately block-agnostic — the MultiplyCCBlock
    (complex product, same 4-operand/2-source shape) should drive through it
    unchanged. For the SATURATED (pipelined) twin see
    :func:`run_block_dut_complex2_pipelined`.

    Args:
        a_stream / b_stream: the two complex stimuli (complex arrays/lists, or
            (N,2) [i,q] float arrays), equal length.
        in_ports: the four operand port names in register order
            (a_re, a_im, b_re, b_im).
        out_port: primary output port; first ``out`` port when None.
        orient: D4 orientation ops applied BEFORE routing (invariance gate).
        words_per_sample: output words per fired sample (2 = complex pair).

    Returns:
        :class:`ComplexDUTResult` — ``i_q15``/``q_q15`` are the de-interleaved
        output rails, one entry per sample.
    """
    import numpy as np  # noqa: PLC0415
    import simkyt  # noqa: PLC0415

    (app, BlockCatalog, load_chip_type, BuildEngine, AppController,
     ChipPortEndpoint, BlockEndpoint) = _engine()

    def _pairs(stream, tag):
        arr = np.asarray(stream)
        if np.iscomplexobj(arr):
            return [(float(c.real), float(c.imag)) for c in arr]
        if arr.ndim == 2 and arr.shape[1] == 2:
            return [(float(i), float(q)) for i, q in arr]
        raise ValueError(f"{tag} must be complex or an (N,2) [i,q] array")

    try:
        pa, pb = _pairs(a_stream, "a_stream"), _pairs(b_stream, "b_stream")
    except ValueError as e:
        return ComplexDUTResult(False, reason=str(e))
    if len(pa) != len(pb):
        return ComplexDUTResult(
            False, reason=f"stream length mismatch: a={len(pa)} b={len(pb)}")

    cat = BlockCatalog.from_gr_kyttar()
    ct = load_chip_type(chip_yaml)
    ct_key = getattr(ct, "name", None) or "kyttar_10x12"

    ctrl = AppController(catalog=cat)
    ctrl.new_project("dut_cplx2", ct_key)
    px, py = place_xy
    blk = ctrl.place_block(block_type, 0, px, py, library=library,
                           params=params or {})
    for _k in (orient or []):
        ctrl.project.block(blk).placement.transform(_k)

    if out_port is None:
        pm = cat.port_map(block_type, params or {}, library=library)
        outs = [p.name for p in pm.ports if p.direction == "out"]
        if not outs:
            return ComplexDUTResult(False, reason="block declares no output port")
        out_port = outs[0]

    for i, ip in enumerate(in_ports):
        ctrl.add_logical_connection(
            ChipPortEndpoint(chip=0, port="x16_in"),
            BlockEndpoint(block=blk, port=ip), name=f"in_{i}")
    ctrl.add_logical_connection(
        BlockEndpoint(block=blk, port=out_port),
        ChipPortEndpoint(chip=0, port="x16_out"), name="blk_out")

    rep = ctrl.auto_route_all({ct_key: ct})
    if not rep.ok:
        return ComplexDUTResult(False, reason="route failed: "
                                + "; ".join(f"{r.name}:{r.reason}"
                                            for r in rep.failed))
    bres = BuildEngine(cat, chip_yaml).build(ctrl.project, {ct_key: ct})
    if not bres.ok:
        return ComplexDUTResult(False, reason="build failed: "
                                + "; ".join(str(e) for e in bres.errors))

    words = bres.words(0)
    entry, ins = cat.resolved_io(block_type, params or {}, library=library)
    if len(ins) < 4:
        return ComplexDUTResult(
            False, reason=f"block resolved {len(ins)} input register(s); a "
            "two-complex-stream block must declare four (a_re, a_im, b_re, b_im)")
    regs = [int(ins[k]) for k in range(4)]

    # INV-1: placement-dependent hop; prefer the build's corridor-accurate
    # landing (all four nets deliver to ONE cell — pick the landing whose
    # data_addrs cover all four operand registers).
    port = ct.port("x16_in")
    blk_obj = ctrl.project.block(blk)
    landing = (blk_obj.placement.cells[0]
               if blk_obj and blk_obj.placement and blk_obj.placement.cells
               else None)
    if landing is not None:
        dist = abs(landing.x - port.cell_x) + abs(landing.y - port.cell_y) + 1
    else:
        dist = abs(px - port.cell_x) + abs(py - port.cell_y) + 1
    hop = max(0, 31 - dist)
    cb = getattr(bres, "chips", {}).get(0)
    il = (getattr(cb, "input_landings", {}) or {}) if cb is not None else {}
    best = None
    for k in range(4):
        ld = il.get(f"in_{k}")
        if ld and ld.get("data_addrs"):
            if best is None or len(ld["data_addrs"]) > len(best["data_addrs"]):
                best = ld
    if best is not None and len(best["data_addrs"]) >= 4:
        hop = int(best["hop"]) & 0x1F
        entry = int(best["entry"])
        regs = [int(a) for a in best["data_addrs"][:4]]

    chip = simkyt.Chip.from_yaml(chip_yaml)
    chip.load_bitstream_physical(words)
    chip.set_port_entry_address("x16_in", entry)

    per_sample: list[list[int]] = []
    for (ar, ai_), (br, bi_) in zip(pa, pb):
        # Stream a's packet, then stream b's packet (the join fires on the 2nd).
        for (re_f, im_f), (r_re, r_im) in (((ar, ai_), regs[0:2]),
                                           ((br, bi_), regs[2:4])):
            chip.inject_data_physical([_to_q15(re_f)], target_hop_cnt=hop,
                                      target_addr=r_re)
            chip.run(max_events=data_run)
            chip.inject_data_physical([_to_q15(im_f)], target_hop_cnt=hop,
                                      target_addr=r_im)
            chip.run(max_events=data_run)
            chip.inject_jump_physical(target_hop_cnt=hop, entry_addr=entry)
            chip.run(max_events=jump_run)
        got: list[int] = []
        while chip.output_available("x16_out"):
            w = chip.read_port_i16("x16_out").view("uint16").tolist()
            got.extend(int(x) & 0xFFFF for x in w)
            chip.release_output_ack("x16_out")
            chip.run(max_events=drain_run)
        per_sample.append(got)

    wps = words_per_sample
    i_ch: list = []
    q_ch: list = []
    for g in per_sample:
        i_ch.append(g[0] if len(g) >= 1 else None)
        if wps >= 2:
            q_ch.append(g[1] if len(g) >= 2 else None)

    return ComplexDUTResult(
        True, outputs_q15=per_sample, i_q15=i_ch, q_q15=q_ch,
        words_per_sample=wps, n_words=len(words), entry_addr=entry,
        hop_count=hop, in_regs=tuple(regs))


def run_block_dut_complex2_pipelined(
    block_type: str,
    a_words,
    b_words,
    *,
    params: dict | None = None,
    chip_yaml: str,
    library: str = "lattrex.official",
    in_ports: tuple[str, str, str, str] = ("ai", "aq", "bi", "bq"),
    out_port: str | None = None,
    place_xy: tuple[int, int] = (1, 1),
    max_events: int | None = None,
) -> RateDUTResult:
    """SATURATED twin of :func:`run_block_dut_complex2`: the WHOLE two-stream
    burst is enqueued as raw WRITE/DATA/JUMP words (``queue_words_physical``)
    and processed in ONE continuous bounded ``run()`` — the real GNU-Radio /
    hardware streaming condition (INV-19/20). Per sample the stream carries
    stream a's packet then stream b's packet:

        W(r0) a_re, W(r1) a_im, J(entry),  W(r2) b_re, W(r3) b_im, J(entry)

    so the landing cell's counting join sees its two triggers back-to-back with
    NO inter-sample quiescence. A correct block's saturated output must equal
    its per-sample output bit-exact. ``a_words`` / ``b_words`` are lists of
    pre-quantized ``(re, im)`` uint16 pairs (the caller quantizes). Bounded run:
    a non-``completed`` run is reported as a livelock failure, never a hang.
    Reusable by any 4-operand/2-source block (MultiplyCCBlock)."""
    import simkyt  # noqa: PLC0415

    (app, BlockCatalog, load_chip_type, BuildEngine, AppController,
     ChipPortEndpoint, BlockEndpoint) = _engine()

    if len(a_words) != len(b_words):
        return RateDUTResult(
            False, reason=f"stream length mismatch: a={len(a_words)} b={len(b_words)}")

    cat = BlockCatalog.from_gr_kyttar()
    ct = load_chip_type(chip_yaml)
    ct_key = getattr(ct, "name", None) or "kyttar_10x12"
    ctrl = AppController(catalog=cat)
    ctrl.new_project("dut_cplx2_pipe", ct_key)
    px, py = place_xy
    blk = ctrl.place_block(block_type, 0, px, py, library=library,
                           params=params or {})
    if out_port is None:
        pm = cat.port_map(block_type, params or {}, library=library)
        outs = [p.name for p in pm.ports if p.direction == "out"]
        if not outs:
            return RateDUTResult(False, reason="block declares no output port")
        out_port = outs[0]
    for i, ip in enumerate(in_ports):
        ctrl.add_logical_connection(
            ChipPortEndpoint(chip=0, port="x16_in"),
            BlockEndpoint(block=blk, port=ip), name=f"in_{i}")
    ctrl.add_logical_connection(
        BlockEndpoint(block=blk, port=out_port),
        ChipPortEndpoint(chip=0, port="x16_out"), name="blk_out")

    rep = ctrl.auto_route_all({ct_key: ct})
    if not rep.ok:
        return RateDUTResult(False, reason="route failed: "
                             + "; ".join(f"{r.name}:{r.reason}" for r in rep.failed))
    bres = BuildEngine(cat, chip_yaml).build(ctrl.project, {ct_key: ct})
    if not bres.ok:
        return RateDUTResult(False, reason="build failed: "
                             + "; ".join(str(e) for e in bres.errors))
    words = bres.words(0)
    entry, ins = cat.resolved_io(block_type, params or {}, library=library)
    if len(ins) < 4:
        return RateDUTResult(
            False, reason=f"block resolved {len(ins)} input reg(s); needs 4")
    regs = [int(ins[k]) for k in range(4)]

    port = ct.port("x16_in")
    blk_obj = ctrl.project.block(blk)
    landing = (blk_obj.placement.cells[0]
               if blk_obj and blk_obj.placement and blk_obj.placement.cells else None)
    if landing is not None:
        dist = abs(landing.x - port.cell_x) + abs(landing.y - port.cell_y) + 1
    else:
        dist = abs(px - port.cell_x) + abs(py - port.cell_y) + 1
    hop = max(0, 31 - dist)
    cb = getattr(bres, "chips", {}).get(0)
    il = (getattr(cb, "input_landings", {}) or {}) if cb is not None else {}
    best = None
    for k in range(4):
        ld = il.get(f"in_{k}")
        if ld and ld.get("data_addrs"):
            if best is None or len(ld["data_addrs"]) > len(best["data_addrs"]):
                best = ld
    if best is not None and len(best["data_addrs"]) >= 4:
        hop = int(best["hop"]) & 0x1F
        entry = int(best["entry"])
        regs = [int(a) for a in best["data_addrs"][:4]]

    chip = simkyt.Chip.from_yaml(chip_yaml)
    chip.load_bitstream_physical(words)
    chip.set_port_entry_address("x16_in", entry)

    stream: list[int] = []
    for (a_re, a_im), (b_re, b_im) in zip(a_words, b_words):
        for w, r in ((a_re, regs[0]), (a_im, regs[1])):
            stream.append(_enc_write(hop, r))
            stream.append(int(w) & 0xFFFF)
        stream.append(_enc_jump(hop, entry))
        for w, r in ((b_re, regs[2]), (b_im, regs[3])):
            stream.append(_enc_write(hop, r))
            stream.append(int(w) & 0xFFFF)
        stream.append(_enc_jump(hop, entry))
    chip.queue_words_physical("x16_in", stream)
    # SAFETY: bounded run (INV-19 harness rule) — a livelock is a clean failure.
    cap = max_events if max_events is not None else max(
        50_000, 4_000 * max(1, len(a_words)))
    res = chip.run(max_events=cap)
    if isinstance(res, dict) and not res.get("completed", True):
        return RateDUTResult(
            False, reason=f"pipeline did NOT reach quiescence under saturated drive "
            f"(stop_reason={res.get('stop_reason')}, events={res.get('events_processed')}, "
            f"cap={cap}) — block livelocks when the pipeline is full")

    flat = [int(v) & 0xFFFF for (v, _d, _t) in chip.read_port_words_timed("x16_out")]
    return RateDUTResult(True, outputs_q15=flat, n_words=len(words),
                         entry_addr=entry, hop_count=hop)


def run_block_dut_nstream(
    block_type: str,
    streams,
    *,
    params: dict | None = None,
    chip_yaml: str,
    library: str = "lattrex.official",
    in_ports: tuple[str, ...],
    out_port: str | None = None,
    place_xy: tuple[int, int] = (1, 1),
    data_run: int = 6000,
    jump_run: int = 200000,
    drain_run: int = 8000,
) -> DUTResult:
    """Build an N-input block (``num_inputs`` real streams fanned in from
    ``x16_in``) and run it on simKYT — the N-operand generalization of
    :func:`run_block_dut_complex`.

    Each sample is delivered as an N-operand transaction: ``WRITE s0 ->
    in_regs[0]`` … ``WRITE s(N-1) -> in_regs[N-1]``, then one ``JUMP entry`` —
    the same multi-WRITE-one-JUMP fan-in the complex (2-operand) driver uses,
    extended to N. The block emits ONE real word per trigger (the chained
    product / sum). All N input ports are wired from ``x16_in``; only the
    primary output is wired to ``x16_out``.

    Args:
        block_type: catalog block type (e.g. ``"MultiplyBlock"``).
        streams: a sequence of N equal-length float lists — ``streams[k]`` is the
            stimulus for input register ``in_regs[k]``. (Transposed per-sample
            internally.)
        in_ports: the block's N input port names (``len`` must equal ``len(streams)``).
        out_port: primary output port name; first ``out`` port if None.

    Returns:
        :class:`DUTResult` (``outputs_q15`` = one uint16 word per sample).
    """
    import simkyt  # noqa: PLC0415

    (app, BlockCatalog, load_chip_type, BuildEngine, AppController,
     ChipPortEndpoint, BlockEndpoint) = _engine()

    n = len(streams)
    if n != len(in_ports):
        return DUTResult(False, reason=f"{n} streams but {len(in_ports)} in_ports")
    if n < 2:
        return DUTResult(False, reason="need >= 2 streams")
    lens = {len(s) for s in streams}
    if len(lens) != 1:
        return DUTResult(False, reason=f"streams have unequal lengths: {lens}")
    nsamp = lens.pop()

    cat = BlockCatalog.from_gr_kyttar()
    ct = load_chip_type(chip_yaml)
    ct_key = getattr(ct, "name", None) or "kyttar_10x12"

    ctrl = AppController(catalog=cat)
    ctrl.new_project("dut_nstream", ct_key)
    px, py = place_xy
    blk = ctrl.place_block(block_type, 0, px, py, library=library,
                           params=params or {})

    if out_port is None:
        pm = cat.port_map(block_type, params or {}, library=library)
        outs = [p.name for p in pm.ports if p.direction == "out"]
        if not outs:
            return DUTResult(False, reason="block declares no output port")
        out_port = outs[0]

    # Wire all N inputs from x16_in; only the primary output to x16_out.
    for k, port_name in enumerate(in_ports):
        ctrl.add_logical_connection(
            ChipPortEndpoint(chip=0, port="x16_in"),
            BlockEndpoint(block=blk, port=port_name), name=f"in_{k}")
    ctrl.add_logical_connection(
        BlockEndpoint(block=blk, port=out_port),
        ChipPortEndpoint(chip=0, port="x16_out"), name="blk_out")

    rep = ctrl.auto_route_all({ct_key: ct})
    if not rep.ok:
        return DUTResult(False, reason="route failed: "
                         + "; ".join(f"{r.name}:{r.reason}" for r in rep.failed))

    bres = BuildEngine(cat, chip_yaml).build(ctrl.project, {ct_key: ct})
    if not bres.ok:
        return DUTResult(False, reason="build failed: "
                         + "; ".join(str(e) for e in bres.errors))

    words = bres.words(0)
    entry, ins = cat.resolved_io(block_type, params or {}, library=library)
    if len(ins) < n:
        return DUTResult(
            False, reason=f"block resolved {len(ins)} input register(s); needs {n}")
    regs = [int(ins[k]) for k in range(n)]

    port = ct.port("x16_in")
    blk_obj = ctrl.project.block(blk)
    landing = (blk_obj.placement.cells[0]
               if blk_obj and blk_obj.placement and blk_obj.placement.cells
               else None)
    if landing is not None:
        dist = abs(landing.x - port.cell_x) + abs(landing.y - port.cell_y) + 1
    else:
        dist = abs(px - port.cell_x) + abs(py - port.cell_y) + 1
    hop = max(0, 31 - dist)

    chip = simkyt.Chip.from_yaml(chip_yaml)
    chip.load_bitstream_physical(words)
    chip.set_port_entry_address("x16_in", entry)

    out: list = []
    for s in range(nsamp):
        for k in range(n):
            chip.inject_data_physical([_to_q15(float(streams[k][s]))],
                                      target_hop_cnt=hop, target_addr=regs[k])
            chip.run(max_events=data_run)
        chip.inject_jump_physical(target_hop_cnt=hop, entry_addr=entry)
        chip.run(max_events=jump_run)
        got = None
        while chip.output_available("x16_out"):
            w = chip.read_port_i16("x16_out").view("uint16").tolist()
            if got is None and w:
                got = int(w[0]) & 0xFFFF
            chip.release_output_ack("x16_out")
            chip.run(max_events=drain_run)
        out.append(got)

    return DUTResult(True, outputs_q15=out, n_words=len(words),
                     entry_addr=entry, hop_count=hop)


@dataclass
class DualComplexDUTResult:
    """Result of a TWO-COMPLEX-OUTPUT DUT run (:func:`run_block_dut_complex2_dual`).

    The block egresses TWO complex pairs on their own nets with per-rail
    out_tags (pair0 -> dests 0/1, pair1 -> dests 2/3); the drain demuxes by the
    captured dest, so the four streams are order-free (the two packets'
    interleave at the port legitimately varies with corridor lengths /
    orientation)."""
    ok: bool
    reason: str = ""
    # Per-sample lists of (value, dest) tuples (per-sample mode) or the flat
    # tagged word stream (pipelined mode).
    raw: list = field(default_factory=list)
    # Demuxed per-dest word streams: streams[d] = [w0, w1, ...].
    streams: dict = field(default_factory=dict)
    n_words: int = 0
    entry_addr: int = 0
    hop_count: int = 0
    in_regs: tuple = ()


def run_block_dut_complex2_dual(
    block_type: str,
    a_stream,
    b_stream,
    *,
    params: dict | None = None,
    chip_yaml: str,
    library: str = "lattrex.official",
    in_ports: tuple[str, str, str, str] = ("ai", "aq", "bi", "bq"),
    out_ports: tuple[str, str] = ("so_i", "do_i"),
    rail_tags: dict | None = None,
    place_xy: tuple[int, int] = (1, 1),
    orient: list[str] | None = None,
    pipelined: bool = False,
    max_events: int | None = None,
    data_run: int = 6000,
    jump_run: int = 200000,
    drain_run: int = 8000,
) -> DualComplexDUTResult:
    """Drive a 2-complex-in / 2-complex-out block (the R2Butterfly shape).

    Input side = :func:`run_block_dut_complex2` (two complex packets per
    sample, counting-join landing).  Output side: BOTH complex pairs are wired
    to ``x16_out`` on their own nets (the controller synthesises each pair's
    Q-half sibling net), and every egress rail is given an explicit
    ``out_tag`` (default: pair0 -> 0/1, pair1 -> 2/3 via ``rail_tags``) so the
    captured words demux by dest REGARDLESS of the two packets' arrival
    interleave (which varies with corridor length / orientation).

    ``pipelined=True`` runs the saturated twin: the whole two-packet burst is
    enqueued via ``queue_words_physical`` and processed in ONE bounded
    ``run()`` (a non-completed run is a livelock FAILURE, never a hang —
    INV-19 harness rule).

    Returns :class:`DualComplexDUTResult`; ``streams[d]`` is dest ``d``'s word
    list (0/1 = pair0 I/Q, 2/3 = pair1 I/Q with the default tags).
    """
    import numpy as np  # noqa: PLC0415
    import simkyt  # noqa: PLC0415

    (app, BlockCatalog, load_chip_type, BuildEngine, AppController,
     ChipPortEndpoint, BlockEndpoint) = _engine()

    def _pairs(stream, tag):
        arr = np.asarray(stream)
        if np.iscomplexobj(arr):
            return [(float(c.real), float(c.imag)) for c in arr]
        if arr.ndim == 2 and arr.shape[1] == 2:
            return [(float(i), float(q)) for i, q in arr]
        raise ValueError(f"{tag} must be complex or an (N,2) [i,q] array")

    try:
        pa, pb = _pairs(a_stream, "a_stream"), _pairs(b_stream, "b_stream")
    except ValueError as e:
        return DualComplexDUTResult(False, reason=str(e))
    if len(pa) != len(pb):
        return DualComplexDUTResult(
            False, reason=f"stream length mismatch: a={len(pa)} b={len(pb)}")

    cat = BlockCatalog.from_gr_kyttar()
    ct = load_chip_type(chip_yaml)
    ct_key = getattr(ct, "name", None) or "kyttar_10x12"
    ctrl = AppController(catalog=cat)
    ctrl.new_project("dut_cplx2_dual", ct_key)
    px, py = place_xy
    blk = ctrl.place_block(block_type, 0, px, py, library=library,
                           params=params or {})
    for _k in (orient or []):
        ctrl.project.block(blk).placement.transform(_k)

    for i, ip in enumerate(in_ports):
        ctrl.add_logical_connection(
            ChipPortEndpoint(chip=0, port="x16_in"),
            BlockEndpoint(block=blk, port=ip), name=f"in_{i}")
    for i, op in enumerate(out_ports):
        ctrl.add_logical_connection(
            BlockEndpoint(block=blk, port=op),
            ChipPortEndpoint(chip=0, port="x16_out"), name=f"out_{i}")

    # Tag every egress rail (incl. the synthesised Q siblings) so the port
    # demux is order-free.  Default: I-rails keep their pair base, Q siblings
    # get base+1 (mirrors the .grc importer's out_tag+1 convention).
    if rail_tags is None:
        rail_tags = {}
        for i, op in enumerate(out_ports):
            rail_tags[op] = 2 * i
            if op.endswith("i"):
                rail_tags[op[:-1] + "q"] = 2 * i + 1
    for c in ctrl.project.connections:
        sp = getattr(c.source, "port", None)
        if getattr(c.target, "port", None) == "x16_out" and sp in rail_tags:
            c.out_tag = int(rail_tags[sp])

    rep = ctrl.auto_route_all({ct_key: ct})
    if not rep.ok:
        return DualComplexDUTResult(False, reason="route failed: "
                                    + "; ".join(f"{r.name}:{r.reason}"
                                                for r in rep.failed))
    bres = BuildEngine(cat, chip_yaml).build(ctrl.project, {ct_key: ct})
    if not bres.ok:
        return DualComplexDUTResult(False, reason="build failed: "
                                    + "; ".join(str(e) for e in bres.errors))

    words = bres.words(0)
    entry, ins = cat.resolved_io(block_type, params or {}, library=library)
    if len(ins) < 4:
        return DualComplexDUTResult(
            False, reason=f"block resolved {len(ins)} input register(s); "
            "a two-complex-stream block must declare four")
    regs = [int(ins[k]) for k in range(4)]

    port = ct.port("x16_in")
    blk_obj = ctrl.project.block(blk)
    landing = (blk_obj.placement.cells[0]
               if blk_obj and blk_obj.placement and blk_obj.placement.cells
               else None)
    if landing is not None:
        dist = abs(landing.x - port.cell_x) + abs(landing.y - port.cell_y) + 1
    else:
        dist = abs(px - port.cell_x) + abs(py - port.cell_y) + 1
    hop = max(0, 31 - dist)
    cb = getattr(bres, "chips", {}).get(0)
    il = (getattr(cb, "input_landings", {}) or {}) if cb is not None else {}
    best = None
    for k in range(4):
        ld = il.get(f"in_{k}")
        if ld and ld.get("data_addrs"):
            if best is None or len(ld["data_addrs"]) > len(best["data_addrs"]):
                best = ld
    if best is not None and len(best["data_addrs"]) >= 4:
        hop = int(best["hop"]) & 0x1F
        entry = int(best["entry"])
        regs = [int(a) for a in best["data_addrs"][:4]]

    chip = simkyt.Chip.from_yaml(chip_yaml)
    chip.load_bitstream_physical(words)
    chip.set_port_entry_address("x16_in", entry)

    if pipelined:
        stream: list[int] = []
        for (a_re, a_im), (b_re, b_im) in zip(pa, pb):
            for w, r in ((_to_q15(a_re), regs[0]), (_to_q15(a_im), regs[1])):
                stream.append(_enc_write(hop, r))
                stream.append(int(w) & 0xFFFF)
            stream.append(_enc_jump(hop, entry))
            for w, r in ((_to_q15(b_re), regs[2]), (_to_q15(b_im), regs[3])):
                stream.append(_enc_write(hop, r))
                stream.append(int(w) & 0xFFFF)
            stream.append(_enc_jump(hop, entry))
        chip.queue_words_physical("x16_in", stream)
        cap = max_events if max_events is not None else max(
            50_000, 4_000 * max(1, len(pa)))
        res = chip.run(max_events=cap)
        if isinstance(res, dict) and not res.get("completed", True):
            return DualComplexDUTResult(
                False, reason="pipeline did NOT reach quiescence under "
                f"saturated drive (stop_reason={res.get('stop_reason')}, "
                f"events={res.get('events_processed')}, cap={cap})")
        raw = [(int(v) & 0xFFFF, int(d))
               for (v, d, _t) in chip.read_port_words_timed("x16_out")]
        streams: dict = {}
        for v, d in raw:
            streams.setdefault(d, []).append(v)
        return DualComplexDUTResult(True, raw=raw, streams=streams,
                                    n_words=len(words), entry_addr=entry,
                                    hop_count=hop, in_regs=tuple(regs))

    per_sample: list = []
    streams = {}
    for (a_re, a_im), (b_re, b_im) in zip(pa, pb):
        for (re_f, im_f), (r_re, r_im) in (((a_re, a_im), regs[0:2]),
                                           ((b_re, b_im), regs[2:4])):
            chip.inject_data_physical([_to_q15(re_f)], target_hop_cnt=hop,
                                      target_addr=r_re)
            chip.run(max_events=data_run)
            chip.inject_data_physical([_to_q15(im_f)], target_hop_cnt=hop,
                                      target_addr=r_im)
            chip.run(max_events=data_run)
            chip.inject_jump_physical(target_hop_cnt=hop, entry_addr=entry)
            chip.run(max_events=jump_run)
        got = [(int(v) & 0xFFFF, int(d))
               for (v, d, _t) in chip.read_port_words_timed("x16_out")]
        per_sample.append(got)
        for v, d in got:
            streams.setdefault(d, []).append(v)
    return DualComplexDUTResult(True, raw=per_sample, streams=streams,
                                n_words=len(words), entry_addr=entry,
                                hop_count=hop, in_regs=tuple(regs))
