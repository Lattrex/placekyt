"""Full SRAM-panel demo (Qt-free, engine layer) — REAL placed blocks + ports
+ REAL drawn routes that live IN the ``.kyt``.

Data enters the chip's **x16 input port**, a placed **SramController** (sitting
at the panel's ``x1_out`` port) carries it to the **panel**, and on read the
panel **pushes** each value back through ``x1_in`` and out the **x16 output
port** — so you watch data go IN one port, through the panel, and OUT the other.

The two on-chip data corridors (x16_in → controller, and x1_in → x16_out) cross
once; a single **Crossover** relay cell sits at that crossing so the streams
share the cell without colliding. There is **no** "consumer" cell — the read
return routes straight to ``x16_out``.

Every face the hardware needs is derived by the build from the **routes saved
in the project** (``Connection.route`` waypoint lists) — NOT applied in code.
``build_demo_project`` returns that fully-routed :class:`Project` (used by the
demo ``.kyt`` and the GUI); ``run_demo`` builds + runs it.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from engine.sram_panel import SramPanelDevice

DEMO_WORDS = [0xCAFE, 0x1234, 0xBEEF, 0x0A0A, 0xFFFF, 0x5555]
LIB = "lattrex.official"


def _wr(hop, dest):
    return (0x6 << 12) | ((hop & 0x1F) << 5) | (dest & 0x1F)


def _jp(hop, entry):
    return (0x7 << 12) | ((hop & 0x1F) << 5) | (entry & 0x1F)


def build_demo_stimulus(words=None) -> list[int]:
    """The SRAM demo's input-port STIMULUS as a raw bitstream (§stimulus).

    A self-contained sequence of WRITE+DATA+JUMP bursts injected verbatim at
    ``x16_in``. Phase 1 — for each data word, a WRITE+DATA+JUMP that jumps to the
    crossover's ``track_a`` (→ controller WRITE entry → panel stores it). Phase 2
    — for each word, a WRITE(0)+JUMP that jumps to the crossover's ``track_c``
    (→ controller READ entry → the panel pushes the stored value back out
    x1_in → x16_out). Writes and reads differ ONLY by the JUMP's target entry;
    everything is in the one stimulus, no magic in the cells.
    """
    _ctl_e, xo_e, _ctl_data, xo_relay = _block_layout()
    hopf = 31 - (len(IN_ROUTE) + 1)        # hop FIELD to reach crossover (8,6)
    words = list(words or DEMO_WORDS)
    stim: list[int] = []
    for w in words:                         # phase 1: writes (→ track_a)
        stim += [_wr(hopf, xo_relay), w & 0xFFFF, _jp(hopf, xo_e["track_a"])]
    for _ in words:                         # phase 2: reads (→ track_c)
        stim += [_wr(hopf, xo_relay), 0, _jp(hopf, xo_e["track_c"])]
    return stim


# --- Geometry ----------------------------------------------------------------
# Two corridors that cross at exactly ONE interior cell (the Crossover). The
# input corridor runs VERTICALLY through the crossover (north->south); the
# return corridor runs HORIZONTALLY through it (west->east). No other cell is
# shared, so each cell has a single unambiguous face.
#
#   Controller sits AT x1_out (9,11): its panel-protocol WRITE/JUMP @1 exit
#   directly out the port into the panel (the @1-at-port path keeps the dest).
CTL = (9, 11)
XO = (8, 6)                                   # the single crossing

# Input corridor: x16_in(0,0) -> EAST row 0 -> SOUTH col 8 -> Crossover(8,6).
# Includes the x16_in port cell (0,0) as the first waypoint so it is faced EAST
# and forwards the injected burst into the corridor (same as RET_ROUTE includes
# its x1_in port cell).
IN_ROUTE = (
    [(x, 0) for x in range(0, 9)]             # row 0 east: (0,0)..(8,0)
    + [(8, y) for y in range(1, 6)]           # col 8 south: (8,1)..(8,5) -> XO
)
# Crossover track_a -> controller: continue SOUTH down col 8 to (8,11), which
# abuts the controller(9,11) to the EAST (the last transit cell faces it).
XO_TO_CTL = [(8, y) for y in range(7, 12)]   # (8,7)..(8,11) -> abuts ctl(9,11)

# Return corridor: x1_in(0,11) -> NORTH col 0 -> EAST row 6 -> Crossover(8,6).
# Includes the x1_in port cell (0,11) as the first waypoint so the pushed value
# is forwarded NORTH into the corridor (the port cell needs a face too).
RET_ROUTE = (
    [(0, y) for y in range(11, 5, -1)]        # col 0 north: (0,11)..(0,6)
    + [(x, 6) for x in range(1, 8)]           # row 6 east: (1,6)..(7,6) -> XO
)
# Crossover track_b -> x16_out: continue EAST to col 9, then NORTH to (9,0).
XO_TO_OUT = (
    [(9, 6)]                                  # row 6 east: (9,6)
    + [(9, y) for y in range(5, -1, -1)]       # col 9 north: (9,5)..(9,0)=x16_out
)


def _rp(pts):
    from model.connection import RoutePoint
    return [RoutePoint(x, y) for (x, y) in pts]


def _block_layout():
    """Resolve controller + crossover register/entry layout (constant per
    block class)."""
    from gr_kyttar.placement.kyttar_block import (
        CrossoverBlock,
        SramControllerBlock,
    )
    from gr_kyttar.placement.resolver import CellProgramResolver
    r = CellProgramResolver()
    ctl_cp = SramControllerBlock("c").build_cell_programs()[0]
    xo_cp = CrossoverBlock("x").build_cell_programs()[0]
    ctl_e = r.compute_entry_addresses(ctl_cp)
    xo_e = r.compute_entry_addresses(xo_cp)
    ctl_cls = r.classify_addresses(ctl_cp)
    xo_cls = r.classify_addresses(xo_cp)
    ctl_data = [a for a, v in ctl_cls.items() if v.get("name") == "data"][0]
    xo_relay = [a for a, v in xo_cls.items() if v.get("name") == "relay"][0]
    return ctl_e, xo_e, ctl_data, xo_relay


def build_demo_project():
    """The placeable SRAM demo project: a panel + SramController + Crossover,
    wired with REAL routes (saved in the .kyt) so data flows
    x16_in -> Crossover -> SramController -> panel -> Crossover -> x16_out."""
    from model.block import Block
    from model.chip import ChipInstance
    from model.connection import (
        BlockEndpoint,
        ChipPortEndpoint,
        Connection,
        PanelConnection,
    )
    from model.enums import Face
    from model.panel import SramPanel
    from model.placement import Placement, PlacedCell
    from model.project import Project, ProjectMetadata

    ctl_e, xo_e, ctl_data, xo_relay = _block_layout()

    # Read push: panel -> x1_in -> [RET_ROUTE: x1_in port cell, up col0, east
    # row6] -> Crossover(8,6) track_b. The panel emits the read-out WRITE/JUMP
    # into x1_in; the descriptor's @N hop must land them AT the crossover, which
    # sits one cell past the last RET_ROUTE waypoint. HOP_CNT is consumed at 31,
    # so the descriptor field = 31 - (len(RET_ROUTE) + 1). (Verified against the
    # realized landing — the same +1 the input corridor needs to reach XO.)
    rwd = _wr(31 - (len(RET_ROUTE) + 1), xo_relay)
    rjd = _jp(31 - (len(RET_ROUTE) + 1), xo_e["track_b"])

    p = Project(
        metadata=ProjectMetadata(name="SRAM Panel Demo"),
        chip_type="kyttar_10x12")
    p.chips = [ChipInstance(0, "C0")]
    p.panels = [SramPanel(id=0, label="Symbol RAM",
                          position_x=240.0, position_y=840.0)]
    p.panels[0].mirror_h()
    p.panel_connections = [
        PanelConnection(0, "x1_in", 0, "x1_out"),    # chip x1_out -> panel input
        PanelConnection(0, "x1_out", 0, "x1_in"),    # panel output -> chip x1_in
    ]
    p.blocks = [
        Block("ctl", "SramControllerBlock", library=LIB,
              params={"panel_hop": 1, "read_wr_desc": rwd, "read_jp_desc": rjd},
              placement=Placement(0, [PlacedCell(0, CTL[0], CTL[1], Face.SOUTH)])),
        Block("xover", "CrossoverBlock", library=LIB,
              # track_a/c reach the controller: 5 transit cells (8,7)..(8,11)
              # then arrive at ctl -> @6. track_b reaches + EXITS x16_out:
              # 7 cells then exit the port -> @8.
              params={"face_a": "south", "hop_a": len(XO_TO_CTL) + 1,
                      "dest_a": ctl_data, "entry_a": ctl_e["write"],
                      "face_b": "east", "hop_b": len(XO_TO_OUT) + 1,
                      "dest_b": 0, "entry_b": 0,
                      "face_c": "south", "hop_c": len(XO_TO_CTL) + 1,
                      "entry_c": ctl_e["read"]},
              placement=Placement(0, [PlacedCell(0, XO[0], XO[1], Face.SOUTH)])),
    ]
    p.connections = [
        # x16_in -> Crossover (track_a entry): the input corridor.
        Connection("in_to_xo",
                   ChipPortEndpoint(0, "x16_in"),
                   BlockEndpoint("xover", "in"),
                   route=_rp(IN_ROUTE)),
        # Crossover -> controller: track_a relays the data south into the ctl.
        Connection("xo_to_ctl",
                   BlockEndpoint("xover", "out"),
                   BlockEndpoint("ctl", "in"),
                   route=_rp(XO_TO_CTL)),
        # x1_in (panel read return) -> Crossover (track_b entry).
        Connection("ret_to_xo",
                   ChipPortEndpoint(0, "x1_in"),
                   BlockEndpoint("xover", "in"),
                   route=_rp(RET_ROUTE)),
        # Crossover -> x16_out: track_b relays the read value straight out.
        Connection("xo_to_out",
                   BlockEndpoint("xover", "out"),
                   ChipPortEndpoint(0, "x16_out"),
                   route=_rp(XO_TO_OUT)),
    ]
    # The demo carries its own STIMULUS: a .kbs bitstream beside the .kyt that
    # writes the 6 words then reads them back. Plain Run injects it (no special
    # demo action). See build_demo_stimulus + sram_panel_demo.kbs.
    p.simulation.default_stimulus = "sram_panel_demo.kbs"
    return p


def write_demo_files(kyt_path: str) -> None:
    """Write the demo ``.kyt`` AND its ``.kbs`` stimulus side by side, so opening
    the project + pressing Run drives the full write-then-read loop."""
    from pathlib import Path

    from engine.io.kbs import write_stimulus_kbs
    from engine.io.project_io import save_project

    kyt = Path(kyt_path)
    save_project(build_demo_project(), kyt)
    write_stimulus_kbs(build_demo_stimulus(), kyt.with_suffix(".kbs"),
                       name="sram_panel_demo")


@dataclass
class DemoResult:
    device: SramPanelDevice
    timeline: list = field(default_factory=list)
    written: dict = field(default_factory=dict)
    read_back: list = field(default_factory=list)


def run_demo(chip_type_path: str, *, words=None, catalog=None) -> DemoResult:
    """Build the placeable demo and run it headlessly through the REAL stimulus
    path: inject the demo's ``.kbs`` bitstream (6 write-bursts then 6 read-bursts)
    verbatim at ``x16_in``, pumping the host panel each batch so reads push back.
    The chip faces come ENTIRELY from the routes in the project; the writes-vs-
    reads distinction is ENTIRELY in the stimulus (the JUMP entry). No bespoke
    per-phase injection — exactly what plain Run does."""
    import simkyt

    from engine.build import BuildEngine
    from engine.catalog import BlockCatalog
    from engine.io.chip_type_io import load_chip_type

    words = list(words or DEMO_WORDS)
    cat = catalog or BlockCatalog.from_gr_kyttar()
    ct = load_chip_type(chip_type_path)
    project = build_demo_project()
    res = BuildEngine(cat, chip_type_path).build(
        project, {project.chip_type: ct})
    if not res.ok:
        raise RuntimeError("SRAM demo build failed: "
                           + "; ".join(str(e) for e in res.errors[:3]))

    chip = simkyt.Chip.from_yaml(chip_type_path)
    chip.load_bitstream_physical(res.words(0))
    # The panel is now an IN-FABRIC handshake node (#193): register it with the
    # engine so `run()` SELF-PUMPS it — drains the held x1_out port, applies
    # WRITEs/JUMP-triggers to the device, injects push-reads into x1_in, and
    # releases the held ack — all inside run(), no host pump between calls. The
    # big SRAM array + register file stay host-side in `dev`. register_panel also
    # marks x1_out held-ack (no FIFO; single word in flight).
    dev = SramPanelDevice()
    chip.register_panel("x1_out", "x1_in", dev)
    timeline: list = []
    written = {addr: val for addr, val in enumerate(words)}
    read_back: list = []

    # PACED injection (#191): the stimulus enters x16_in one transaction at a
    # time through the queued input path, not flooded all at once. Combined with
    # per-cell single-word handshaking (#192) the 6 write- then 6 read-bursts
    # flow end-to-end one packet at a time, paced by downstream readiness. The
    # in-fabric panel (#193) self-pumps inside run(), so the loop just runs +
    # drains output.
    chip.queue_words_physical("x16_in", build_demo_stimulus(words))
    for _ in range(8000):
        info = chip.run(max_events=64)
        act = dev.take_activity()
        if act:
            timeline.append(act)
        got = chip.read_port_words_timed("x16_out")
        for v, _dest, _t in got:
            read_back.append(v & 0xFFFF)
        sr = info.get("stop_reason") if isinstance(info, dict) else None
        done = dev.writes_committed >= len(words) and dev.reads_issued >= len(words)
        acks_pending = chip.port_ack_pending("x1_out")
        if sr in ("QueueEmpty", "Deadlock") and not acks_pending \
                and (done or sr == "Deadlock"):
            break

    return DemoResult(device=dev, timeline=timeline,
                      written=written, read_back=read_back)
