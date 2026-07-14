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
    # edge cell). Derive it from the actual landing-cell position rather than the
    # routed point list — the chip-input net's route is unreliable for this (it
    # may be absent, or include the port edge cell inconsistently). The landing
    # cell is the block's input-port cell (first placed cell for a simple block).
    port = ct.port("x16_in")
    blk_obj = ctrl.project.block(blk)
    landing = (blk_obj.placement.cells[0]
               if blk_obj and blk_obj.placement and blk_obj.placement.cells
               else None)
    if landing is not None:
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
        return RateDUTResult(
            False, reason=f"pipeline did NOT reach quiescence under saturated drive "
            f"(stop_reason={res.get('stop_reason')}, events={res.get('events_processed')}, "
            f"cap={cap}) — block livelocks when the pipeline is full")

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

    # INV-1: placement-dependent hop derived from the landing cell, never a const.
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
    for (i_f, q_f) in pairs:
        # ONE complex sample = WRITE xi -> a0, WRITE xq -> a1, then JUMP entry.
        chip.inject_data_physical([_to_q15(i_f)], target_hop_cnt=hop,
                                  target_addr=a0)
        chip.run(max_events=data_run)
        chip.inject_data_physical([_to_q15(q_f)], target_hop_cnt=hop,
                                  target_addr=a1)
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
