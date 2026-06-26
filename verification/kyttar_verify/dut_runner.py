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
