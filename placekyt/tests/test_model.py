"""Unit tests for the placeKYT data model (``model/``).

Pure Python — no Qt, no simkyt, no gr_kyttar (the architecture notes §6).
Covers enum parsing/validation, the deferred-delivery event bus contract,
placement geometry, and project aggregate wiring.
"""

from __future__ import annotations

import logging

import pytest

from model import (
    AUTO_ROUTE,
    Block,
    BlockEndpoint,
    Board,
    ChipConnection,
    ChipInstance,
    ChipPortEndpoint,
    ChipType,
    Connection,
    EventBus,
    Face,
    IQFormat,
    InterChipConnection,
    Modulation,
    Placement,
    PlacedCell,
    PortDirection,
    PortSpec,
    Project,
    RoutePoint,
    TransitCell,
)
from model.events import MAX_EVENTS_PER_DRAIN


# --------------------------------------------------------------------------- #
# Enums
# --------------------------------------------------------------------------- #


class TestFace:
    def test_values_match_yaml_strings(self):
        assert Face.SOUTH.value == "south"
        assert Face.EAST.value == "east"
        assert Face.WEST.value == "west"
        assert Face.NORTH.value == "north"

    def test_hardware_codes(self):
        # ISA encoding S=0, E=1, W=2, N=3.
        assert Face.SOUTH.code == 0
        assert Face.EAST.code == 1
        assert Face.WEST.code == 2
        assert Face.NORTH.code == 3

    def test_opposite(self):
        assert Face.NORTH.opposite is Face.SOUTH
        assert Face.SOUTH.opposite is Face.NORTH
        assert Face.EAST.opposite is Face.WEST
        assert Face.WEST.opposite is Face.EAST

    def test_from_str_is_case_insensitive(self):
        assert Face.from_str("NORTH") is Face.NORTH
        assert Face.from_str(" East ") is Face.EAST

    def test_from_str_rejects_garbage(self):
        with pytest.raises(ValueError, match="invalid face"):
            Face.from_str("up")

    def test_rotate_cw_cycles(self):
        # 90° clockwise on a y-down screen: N→E→S→W→N.
        assert Face.NORTH.rotated_cw is Face.EAST
        assert Face.EAST.rotated_cw is Face.SOUTH
        assert Face.SOUTH.rotated_cw is Face.WEST
        assert Face.WEST.rotated_cw is Face.NORTH

    def test_rotate_ccw_is_inverse_of_cw(self):
        for f in Face:
            assert f.rotated_cw.rotated_ccw is f
            assert f.rotated_ccw.rotated_cw is f

    def test_four_cw_rotations_identity(self):
        for f in Face:
            g = f
            for _ in range(4):
                g = g.rotated_cw
            assert g is f

    def test_mirror_h(self):
        assert Face.EAST.mirrored_h is Face.WEST
        assert Face.WEST.mirrored_h is Face.EAST
        assert Face.NORTH.mirrored_h is Face.NORTH
        assert Face.SOUTH.mirrored_h is Face.SOUTH

    def test_mirror_v(self):
        assert Face.NORTH.mirrored_v is Face.SOUTH
        assert Face.SOUTH.mirrored_v is Face.NORTH
        assert Face.EAST.mirrored_v is Face.EAST
        assert Face.WEST.mirrored_v is Face.WEST

    def test_mirror_is_self_inverse(self):
        for f in Face:
            assert f.mirrored_h.mirrored_h is f
            assert f.mirrored_v.mirrored_v is f


class TestModulation:
    def test_bits_per_symbol(self):
        assert Modulation.BPSK.bits_per_symbol == 1
        assert Modulation.QPSK.bits_per_symbol == 2
        assert Modulation.PSK8.bits_per_symbol == 3
        assert Modulation.NONE.bits_per_symbol == 0

    def test_8psk_string_value(self):
        assert Modulation.from_str("8psk") is Modulation.PSK8

    def test_invalid(self):
        with pytest.raises(ValueError, match="invalid modulation"):
            Modulation.from_str("16qam")


class TestIQFormat:
    def test_only_q15_paired(self):
        assert IQFormat.from_str("q15_paired") is IQFormat.Q15_PAIRED
        # The packed-Q7 format was removed in the errata pass.
        assert [f.value for f in IQFormat] == ["q15_paired"]

    def test_invalid(self):
        with pytest.raises(ValueError, match="invalid iq_format"):
            IQFormat.from_str("packed_q7")


# --------------------------------------------------------------------------- #
# Event bus (deferred delivery contract, §6)
# --------------------------------------------------------------------------- #


class TestEventBus:
    def test_emit_does_not_dispatch_until_flush(self):
        bus = EventBus()
        seen = []
        bus.subscribe("cell_placed", lambda t, **kw: seen.append((t, kw)))

        bus.emit("cell_placed", x=1, y=2)
        assert seen == []  # deferred — not delivered yet
        assert bus.pending == 1

        bus.flush()
        assert seen == [("cell_placed", {"x": 1, "y": 2})]
        assert bus.pending == 0

    def test_subscribe_all_receives_every_type(self):
        bus = EventBus()
        seen = []
        bus.subscribe_all(lambda t, **kw: seen.append(t))

        bus.emit("a")
        bus.emit("b")
        bus.flush()
        assert seen == ["a", "b"]

    def test_unsubscribe(self):
        bus = EventBus()
        seen = []
        cb = lambda t, **kw: seen.append(t)  # noqa: E731
        unsub = bus.subscribe("x", cb)
        unsub()
        bus.emit("x")
        bus.flush()
        assert seen == []

    def test_breadth_first_cascade_in_same_drain(self):
        # A callback that emits during flush() has its event drained in the
        # same pass (breadth-first), not dropped.
        bus = EventBus()
        order = []

        def on_first(t, **kw):
            order.append("first")
            bus.emit("second")

        def on_second(t, **kw):
            order.append("second")

        bus.subscribe("first", on_first)
        bus.subscribe("second", on_second)

        bus.emit("first")
        bus.flush()
        assert order == ["first", "second"]
        assert bus.pending == 0

    def test_reentrant_flush_is_noop(self):
        # A callback calling flush() must not start a nested drain; the outer
        # drain owns the queue. Each event is delivered exactly once.
        bus = EventBus()
        counts = {"a": 0, "b": 0}

        def on_a(t, **kw):
            counts["a"] += 1
            bus.emit("b")
            bus.flush()  # re-entrant — should be a no-op

        def on_b(t, **kw):
            counts["b"] += 1

        bus.subscribe("a", on_a)
        bus.subscribe("b", on_b)
        bus.emit("a")
        bus.flush()
        assert counts == {"a": 1, "b": 1}

    def test_callback_exception_is_isolated(self, caplog):
        bus = EventBus()
        seen = []

        def bad(t, **kw):
            raise RuntimeError("boom")

        def good(t, **kw):
            seen.append(t)

        bus.subscribe("e", bad)
        bus.subscribe("e", good)

        bus.emit("e")
        with caplog.at_level(logging.ERROR):
            bus.flush()  # must not raise
        # The good callback still ran despite the bad one raising.
        assert seen == ["e"]
        assert any("raised" in r.message for r in caplog.records)

    def test_clear_discards_undelivered(self):
        bus = EventBus()
        seen = []
        bus.subscribe("x", lambda t, **kw: seen.append(t))
        bus.emit("x")
        bus.clear()
        bus.flush()
        assert seen == []

    def test_subscription_during_dispatch_does_not_affect_current_event(self):
        # Handler lists are snapshotted per-event, so subscribing mid-dispatch
        # doesn't fire the new handler for the in-flight event.
        bus = EventBus()
        seen = []

        def adder(t, **kw):
            bus.subscribe("x", lambda t2, **kw2: seen.append("late"))

        bus.subscribe("x", adder)
        bus.emit("x")
        bus.flush()
        assert seen == []  # 'late' handler only fires on the NEXT 'x'

        bus.emit("x")
        bus.flush()
        assert "late" in seen

    def test_runaway_cascade_truncates(self, caplog):
        bus = EventBus()

        # Each event spawns two more -> unbounded cascade.
        def explode(t, **kw):
            bus.emit("boom")
            bus.emit("boom")

        bus.subscribe("boom", explode)
        bus.emit("boom")
        with caplog.at_level(logging.WARNING):
            bus.flush()  # must terminate, not hang
        assert bus.pending == 0
        assert any("infinite cascade" in r.message for r in caplog.records)
        # Sanity: the guard tripped near the configured ceiling.
        assert MAX_EVENTS_PER_DRAIN == 10_000


# --------------------------------------------------------------------------- #
# Placement geometry
# --------------------------------------------------------------------------- #


class TestPlacement:
    def test_occupied_positions_includes_transit(self):
        p = Placement(
            chip=0,
            cells=[
                PlacedCell("ff0", 7, 1, Face.WEST),
                PlacedCell("ff1", 6, 1, Face.WEST),
            ],
            transit_cells=[TransitCell(8, 0, Face.EAST)],
        )
        assert p.occupied_positions() == {(7, 1), (6, 1), (8, 0)}

    def test_cell_lookup(self):
        p = Placement(chip=0, cells=[PlacedCell("a", 1, 1, Face.SOUTH)])
        assert p.cell("a").pos == (1, 1)
        assert p.cell("missing") is None

    def test_bounding_box_excludes_transit(self):
        p = Placement(
            chip=0,
            cells=[
                PlacedCell("a", 2, 3, Face.SOUTH),
                PlacedCell("b", 5, 1, Face.SOUTH),
            ],
            transit_cells=[TransitCell(9, 9, Face.EAST)],
        )
        assert p.bounding_box() == (2, 1, 5, 3)

    def test_bounding_box_empty(self):
        assert Placement(chip=0).bounding_box() is None

    def _hpair(self):
        # two E-facing cells in a row at (1,1),(2,1)
        return Placement(chip=0, cells=[
            PlacedCell("a", 1, 1, Face.EAST),
            PlacedCell("b", 2, 1, Face.EAST),
        ])

    def test_transform_cw_rotates_positions_and_faces(self):
        p = self._hpair()
        p.transform("cw")
        # anchored at the same top-left; row → column, E → S
        assert p.cell("a").pos == (1, 1) and p.cell("a").face is Face.SOUTH
        assert p.cell("b").pos == (1, 2) and p.cell("b").face is Face.SOUTH

    def test_transform_ccw_is_inverse_of_cw(self):
        p = self._hpair()
        before = [(c.cell_id, c.pos, c.face) for c in p.cells]
        p.transform("cw")
        p.transform("ccw")
        after = [(c.cell_id, c.pos, c.face) for c in p.cells]
        assert after == before

    def test_four_cw_rotations_return_to_start(self):
        p = self._hpair()
        before = [(c.cell_id, c.pos, c.face) for c in p.cells]
        for _ in range(4):
            p.transform("cw")
        assert [(c.cell_id, c.pos, c.face) for c in p.cells] == before

    def test_transform_mirror_h(self):
        p = self._hpair()
        p.transform("mirror_h")
        # positions swap across the vertical axis; E↔W; same footprint
        assert p.cell("a").pos == (2, 1) and p.cell("a").face is Face.WEST
        assert p.cell("b").pos == (1, 1) and p.cell("b").face is Face.WEST

    def test_transform_mirror_v_self_inverse(self):
        p = self._hpair()
        before = [(c.cell_id, c.pos, c.face) for c in p.cells]
        p.transform("mirror_v")
        p.transform("mirror_v")
        assert [(c.cell_id, c.pos, c.face) for c in p.cells] == before

    def test_transform_carries_transit_cells(self):
        p = Placement(chip=0,
                      cells=[PlacedCell("a", 1, 1, Face.EAST)],
                      transit_cells=[TransitCell(2, 1, Face.EAST)])
        p.transform("cw")
        # transit cell rotates with the block (footprint pivot)
        assert p.transit_cells[0].pos == (1, 2)
        assert p.transit_cells[0].face is Face.SOUTH

    def test_transform_preserves_footprint_anchor(self):
        # the min corner stays put after any transform (block doesn't wander)
        for kind in ("cw", "ccw", "mirror_h", "mirror_v"):
            p = Placement(chip=0, cells=[
                PlacedCell("a", 4, 2, Face.EAST),
                PlacedCell("b", 6, 5, Face.EAST),
            ])
            p.transform(kind)
            box = p.full_bounding_box()
            assert box[0] == 4 and box[1] == 2   # same top-left corner


class TestSramPanel:
    def test_defaults_full_16bit_array(self):
        from model.panel import SramPanel
        p = SramPanel(id=0)
        assert p.size_words == 1 << 16        # full 16-bit-addressable array
        assert p.address_bits == 16
        assert p.address_regs == 1            # one 16-bit address register (R5)

    def test_default_ports_x16_and_x1(self):
        from model.enums import PortDirection
        from model.panel import PORT_WIDTH_X1, PORT_WIDTH_X16, SramPanel
        p = SramPanel(id=1)
        names = {pt.name for pt in p.ports}
        assert names == {"x16_in", "x1_in", "x16_out", "x1_out"}
        assert p.port("x16_in").direction is PortDirection.INPUT
        assert p.port("x16_out").direction is PortDirection.OUTPUT
        # widths: x16 ports are 16-bit, x1 ports are 1-bit
        assert p.port("x16_in").width == PORT_WIDTH_X16
        assert p.port("x1_out").width == PORT_WIDTH_X1

    def test_default_ports_inputs_west_outputs_east(self):
        from model.enums import Face, PortDirection
        from model.panel import SramPanel
        p = SramPanel(id=0)
        for pt in p.ports:
            if pt.direction is PortDirection.INPUT:
                assert pt.face is Face.WEST
            else:
                assert pt.face is Face.EAST

    def test_mirror_h_swaps_port_edges(self):
        from model.enums import Face
        from model.panel import SramPanel
        p = SramPanel(id=0)
        p.mirror_h()
        assert p.mirrored is True
        # inputs now EAST, outputs now WEST
        assert p.port("x16_in").face is Face.EAST
        assert p.port("x16_out").face is Face.WEST
        p.mirror_h()                              # self-inverse
        assert p.mirrored is False
        assert p.port("x16_in").face is Face.WEST

    def test_address_regs_scale_with_size(self):
        from model.panel import SramPanel
        # a >64k panel needs a second address register (R5 + R6)
        big = SramPanel(id=2, size_words=1 << 18)
        assert big.address_bits == 18
        assert big.address_regs == 2

    def test_address_regs_small_panel(self):
        from model.panel import SramPanel
        small = SramPanel(id=3, size_words=256)
        assert small.address_bits == 8
        assert small.address_regs == 1


# --------------------------------------------------------------------------- #
# Chip type
# --------------------------------------------------------------------------- #


class TestChipType:
    def _ct(self) -> ChipType:
        return ChipType(
            name="kyttar_10x12",
            width=10,
            height=12,
            ports=(
                PortSpec("x16_in", PortDirection.INPUT, 16, 0, 0, Face.NORTH),
                PortSpec("x16_out", PortDirection.OUTPUT, 16, 9, 0, Face.EAST),
            ),
        )

    def test_cell_count_and_id(self):
        ct = self._ct()
        assert ct.cell_count == 120
        # cell_id = y * width + x  (simkyt convention)
        assert ct.cell_id(0, 0) == 0
        assert ct.cell_id(9, 0) == 9
        assert ct.cell_id(0, 1) == 10
        assert ct.cell_id(3, 2) == 23

    def test_in_bounds(self):
        ct = self._ct()
        assert ct.in_bounds(9, 11)
        assert not ct.in_bounds(10, 0)
        assert not ct.in_bounds(0, 12)
        assert not ct.in_bounds(-1, 0)

    def test_port_lookup(self):
        ct = self._ct()
        assert ct.port("x16_in").direction is PortDirection.INPUT
        assert ct.port("nope") is None


# --------------------------------------------------------------------------- #
# Board
# --------------------------------------------------------------------------- #


class TestBoard:
    def test_has_chip_connection(self):
        board = Board(
            name="KYT-DEV-2",
            chip_connections=(
                ChipConnection(0, "x16_out", 1, "x16_in", wire_delay_ns=1.0),
            ),
        )
        assert board.has_chip_connection(0, "x16_out", 1, "x16_in")
        # Reverse direction is a different (un-wired) link.
        assert not board.has_chip_connection(1, "x16_in", 0, "x16_out")
        assert not board.has_chip_connection(0, "x1_out", 1, "x1_in")


# --------------------------------------------------------------------------- #
# Connection
# --------------------------------------------------------------------------- #


class TestConnection:
    def test_block_to_block_unrouted_is_fly_line(self):
        c = Connection(
            "agc_to_dc",
            source=BlockEndpoint("agc", "out"),
            target=BlockEndpoint("dc_blocker", "sample"),
        )
        assert not c.is_routed
        assert not c.is_auto

    def test_explicit_route(self):
        c = Connection(
            "r",
            source=BlockEndpoint("agc", "out"),
            target=BlockEndpoint("dc", "in"),
            route=[RoutePoint(1, 1), RoutePoint(2, 1)],
        )
        assert c.is_routed
        assert c.route[0].pos == (1, 1)

    def test_auto_route_is_not_routed(self):
        c = Connection(
            "r",
            source=BlockEndpoint("a", "out"),
            target=BlockEndpoint("b", "in"),
            route=AUTO_ROUTE,
        )
        assert c.is_auto
        assert not c.is_routed  # Phase 1 treats auto as a fly line

    def test_chip_port_endpoints(self):
        c = Connection(
            "adc_to_agc",
            source=ChipPortEndpoint(0, "x16_in"),
            target=BlockEndpoint("agc", "in"),
            modulation=Modulation.QPSK,
            code_rate=0.5,
            iq_format=IQFormat.Q15_PAIRED,
        )
        assert isinstance(c.source, ChipPortEndpoint)
        assert c.source.chip == 0
        assert c.modulation.bits_per_symbol == 2


# --------------------------------------------------------------------------- #
# Project aggregate
# --------------------------------------------------------------------------- #


class TestProject:
    def _project(self) -> Project:
        p = Project(chip_type="KYT16A120")
        p.chips = [
            ChipInstance(0, "RX Front-End", 0.0, 0.0),
            ChipInstance(1, "RX Back-End", 720.0, 0.0),
        ]
        p.blocks = [
            Block("agc", "AGCBlock", library="lattrex.dsp", params={"target": 0.7}),
            Block("dc", "DCBlockerBlock", library="lattrex.dsp"),
        ]
        p.connections = [
            Connection(
                "agc_to_dc",
                source=BlockEndpoint("agc", "out"),
                target=BlockEndpoint("dc", "sample"),
            ),
            Connection(
                "adc_to_agc",
                source=ChipPortEndpoint(0, "x16_in"),
                target=BlockEndpoint("agc", "in"),
            ),
        ]
        p.inter_chip_connections = [
            InterChipConnection(0, "x16_out", 1, "x16_in"),
        ]
        return p

    def test_lookups(self):
        p = self._project()
        assert p.block("agc").type == "AGCBlock"
        assert p.block("missing") is None
        assert p.chip(1).label == "RX Back-End"
        assert p.chip(99) is None
        assert p.connection("agc_to_dc") is not None

    def test_connections_for_block_matches_both_endpoints(self):
        p = self._project()
        names = {c.name for c in p.connections_for_block("agc")}
        # agc is the target of adc_to_agc and the source of agc_to_dc.
        assert names == {"agc_to_dc", "adc_to_agc"}
        # dc only appears as a target.
        assert {c.name for c in p.connections_for_block("dc")} == {"agc_to_dc"}

    def test_block_unplaced_by_default(self):
        p = self._project()
        agc = p.block("agc")
        assert agc.placement is None
        assert not agc.is_placed
        assert agc.chip is None

    def test_block_placed(self):
        p = self._project()
        agc = p.block("agc")
        agc.placement = Placement(
            chip=0, cells=[PlacedCell("0", 1, 1, Face.SOUTH)]
        )
        assert agc.is_placed
        assert agc.chip == 0

    def test_dirty_flags(self):
        p = Project(chip_type="KYT16A120")
        # build_dirty starts True (nothing built yet); project_dirty starts clean.
        assert p.build_dirty
        assert not p.project_dirty

        p.mark_dirty()
        assert p.project_dirty and p.build_dirty

        # A fresh build clears only build_dirty (engine layer does this).
        p.build_dirty = False
        assert not p.build_dirty
        assert p.project_dirty  # still unsaved

    def test_generation_id_monotonic(self):
        p = Project()
        assert p.next_generation_id() == 1
        assert p.next_generation_id() == 2
        assert p.current_generation_id == 2

    def test_event_bus_present_and_independent(self):
        p = Project()
        assert isinstance(p.event_bus, EventBus)
        # Each project gets its own bus.
        assert Project().event_bus is not p.event_bus
