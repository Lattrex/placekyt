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
    entry, ins = cat.resolved_io(block_type)
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
