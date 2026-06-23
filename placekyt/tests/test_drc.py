"""Tests for project-level DRC (engine/drc.py, §5.2).

One positive + one negative per project-level ERROR category (§11.2), plus the
warnings and the utilization INFO. Pure model — no gr_kyttar / simkyt.
"""

from __future__ import annotations

import pytest

from engine.drc import MAX_HOPS, Severity, check_project
from model.board import Board, ChipConnection
from model.chip_type import ChipType, PortSpec
from model.connection import (
    AUTO_ROUTE,
    BlockEndpoint,
    ChipPortEndpoint,
    Connection,
    InterChipConnection,
    RoutePoint,
)
from model.enums import Face, PortDirection
from model.chip import ChipInstance
from model.block import Block
from model.placement import Placement, PlacedCell, TransitCell
from model.project import Project


def _chip_type(w=10, h=12) -> ChipType:
    return ChipType(
        name="t",
        width=w,
        height=h,
        ports=(
            PortSpec("x16_in", PortDirection.INPUT, 16, 0, 0, Face.NORTH),
            PortSpec("x16_out", PortDirection.OUTPUT, 16, 9, 0, Face.EAST),
        ),
    )


def _project(blocks=None, connections=None, chips=(0,)) -> Project:
    p = Project(chip_type="t")
    p.chips = [ChipInstance(c, f"C{c}") for c in chips]
    p.blocks = blocks or []
    p.connections = connections or []
    return p


def _placed(name, x, y, chip=0, cell_id=0, face=Face.EAST):
    return Block(
        name, "AGCBlock", library="lattrex.official",
        placement=Placement(chip, [PlacedCell(cell_id, x, y, face)]),
    )


def _categories(result):
    return {f.category for f in result.findings}


CHIP_TYPES = {"t": _chip_type()}


# --- overlap --------------------------------------------------------------- #

class TestOverlap:
    def test_clean(self):
        p = _project([_placed("a", 1, 1), _placed("b", 2, 1)])
        assert "overlap" not in _categories(check_project(p, CHIP_TYPES))

    def test_violation(self):
        p = _project([_placed("a", 1, 1), _placed("b", 1, 1)])
        r = check_project(p, CHIP_TYPES)
        assert any(e.category == "overlap" for e in r.errors)


# --- unplaced_cell / out of bounds ----------------------------------------- #

class TestUnplaced:
    def test_clean(self):
        p = _project([_placed("a", 0, 0)])
        assert "unplaced_cell" not in _categories(check_project(p, CHIP_TYPES))

    def test_empty_placement(self):
        b = Block("a", "AGCBlock", placement=Placement(0, []))
        r = check_project(_project([b]), CHIP_TYPES)
        assert any(e.category == "unplaced_cell" for e in r.errors)

    def test_out_of_bounds(self):
        p = _project([_placed("a", 99, 99)])
        r = check_project(p, CHIP_TYPES)
        assert any(e.category == "unplaced_cell" for e in r.errors)


# --- block_spans_chips (placed on nonexistent chip) ------------------------ #

class TestBlockChip:
    def test_clean(self):
        p = _project([_placed("a", 1, 1, chip=0)], chips=(0,))
        assert "block_spans_chips" not in _categories(check_project(p, CHIP_TYPES))

    def test_block_on_missing_chip(self):
        p = _project([_placed("a", 1, 1, chip=5)], chips=(0,))
        r = check_project(p, CHIP_TYPES)
        assert any(e.category == "block_spans_chips" for e in r.errors)


# --- unrouted -------------------------------------------------------------- #

class TestUnrouted:
    def test_clean_with_route(self):
        c = Connection("c", BlockEndpoint("a", "out"), BlockEndpoint("b", "in"),
                       route=[RoutePoint(1, 1), RoutePoint(2, 1)])
        p = _project([_placed("a", 1, 1), _placed("b", 2, 1)], [c])
        assert "unrouted" not in _categories(check_project(p, CHIP_TYPES))

    def test_no_route(self):
        c = Connection("c", BlockEndpoint("a", "out"), BlockEndpoint("b", "in"))
        p = _project([_placed("a", 1, 1), _placed("b", 2, 1)], [c])
        r = check_project(p, CHIP_TYPES)
        assert any(e.category == "unrouted" for e in r.errors)

    def test_auto_route_is_unrouted_phase1(self):
        c = Connection("c", BlockEndpoint("a", "out"), BlockEndpoint("b", "in"),
                       route=AUTO_ROUTE)
        p = _project([_placed("a", 1, 1), _placed("b", 2, 1)], [c])
        r = check_project(p, CHIP_TYPES)
        assert any(e.category == "unrouted" for e in r.errors)


# --- route_gap (WARNING) --------------------------------------------------- #

class TestRouteGap:
    def test_adjacent_clean(self):
        c = Connection("c", BlockEndpoint("a", "out"), BlockEndpoint("b", "in"),
                       route=[RoutePoint(1, 1), RoutePoint(2, 1), RoutePoint(2, 2)])
        p = _project([_placed("a", 1, 1), _placed("b", 2, 2)], [c])
        assert "route_gap" not in _categories(check_project(p, CHIP_TYPES))

    def test_gap(self):
        c = Connection("c", BlockEndpoint("a", "out"), BlockEndpoint("b", "in"),
                       route=[RoutePoint(1, 1), RoutePoint(5, 5)])
        p = _project([_placed("a", 1, 1), _placed("b", 5, 5)], [c])
        r = check_project(p, CHIP_TYPES)
        assert any(w.category == "route_gap" for w in r.warnings)


# --- route_crosses_chips --------------------------------------------------- #

class TestRouteCrossesChips:
    def test_in_bounds_clean(self):
        c = Connection("c", BlockEndpoint("a", "out"), BlockEndpoint("b", "in"),
                       route=[RoutePoint(1, 1), RoutePoint(2, 1)])
        p = _project([_placed("a", 1, 1), _placed("b", 2, 1)], [c])
        assert "route_crosses_chips" not in _categories(check_project(p, CHIP_TYPES))

    def test_waypoint_off_chip(self):
        c = Connection("c", BlockEndpoint("a", "out"), BlockEndpoint("b", "in"),
                       route=[RoutePoint(1, 1), RoutePoint(50, 1)])
        p = _project([_placed("a", 1, 1), _placed("b", 2, 1)], [c])
        r = check_project(p, CHIP_TYPES)
        assert any(e.category == "route_crosses_chips" for e in r.errors)


# --- hop_overflow ---------------------------------------------------------- #

class TestHopOverflow:
    def test_short_route_clean(self):
        c = Connection("c", BlockEndpoint("a", "out"), BlockEndpoint("b", "in"),
                       route=[RoutePoint(0, i) for i in range(5)])
        p = _project([_placed("a", 0, 0), _placed("b", 0, 4)], [c])
        # use a tall chip so the route stays in bounds
        assert "hop_overflow" not in _categories(check_project(p, {"t": _chip_type(10, 40)}))

    def test_overflow(self):
        # 33 waypoints -> distance 32 > 31
        c = Connection("c", BlockEndpoint("a", "out"), BlockEndpoint("b", "in"),
                       route=[RoutePoint(0, i) for i in range(33)])
        p = _project([_placed("a", 0, 0), _placed("b", 0, 32)], [c])
        r = check_project(p, {"t": _chip_type(10, 40)})
        assert any(e.category == "hop_overflow" for e in r.errors)

    def test_chip_output_plus_one_rule(self):
        # 31 waypoints -> distance 30; +1 for chip-output exit -> 31, still OK.
        # 32 waypoints -> distance 31; +1 -> 32 > 31 -> overflow.
        c = Connection("c", BlockEndpoint("a", "out"),
                       ChipPortEndpoint(0, "x16_out"),
                       route=[RoutePoint(0, i) for i in range(32)])
        p = _project([_placed("a", 0, 0)], [c])
        r = check_project(p, {"t": _chip_type(10, 40)})
        assert any(e.category == "hop_overflow" for e in r.errors)


# --- long_route (WARNING) -------------------------------------------------- #

class TestLongRoute:
    def test_short_clean(self):
        c = Connection("c", BlockEndpoint("a", "out"), BlockEndpoint("b", "in"),
                       route=[RoutePoint(0, i) for i in range(5)])
        p = _project([_placed("a", 0, 0), _placed("b", 0, 4)], [c])
        assert "long_route" not in _categories(check_project(p, {"t": _chip_type(10, 40)}))

    def test_long(self):
        c = Connection("c", BlockEndpoint("a", "out"), BlockEndpoint("b", "in"),
                       route=[RoutePoint(0, i) for i in range(25)])
        p = _project([_placed("a", 0, 0), _placed("b", 0, 24)], [c])
        r = check_project(p, {"t": _chip_type(10, 40)})
        assert any(w.category == "long_route" for w in r.warnings)


# --- inter_chip_not_wired -------------------------------------------------- #

class TestInterChipNotWired:
    def _board(self):
        return Board(name="B", chip_connections=(
            ChipConnection(0, "x16_out", 1, "x16_in", wire_delay_ns=1.0),
        ))

    def test_wired_clean(self):
        p = _project(chips=(0, 1))
        p.inter_chip_connections = [InterChipConnection(0, "x16_out", 1, "x16_in")]
        r = check_project(p, CHIP_TYPES, board=self._board())
        assert "inter_chip_not_wired" not in _categories(r)

    def test_not_wired(self):
        p = _project(chips=(0, 1))
        p.inter_chip_connections = [InterChipConnection(0, "x1_out", 1, "x1_in")]
        r = check_project(p, CHIP_TYPES, board=self._board())
        assert any(e.category == "inter_chip_not_wired" for e in r.errors)

    def test_skipped_without_board(self):
        p = _project(chips=(0, 1))
        p.inter_chip_connections = [InterChipConnection(0, "x1_out", 1, "x1_in")]
        r = check_project(p, CHIP_TYPES, board=None)
        assert "inter_chip_not_wired" not in _categories(r)


# --- unused_port (WARNING) ------------------------------------------------- #

class TestUnusedPort:
    def test_connected_clean(self):
        c = Connection("c", BlockEndpoint("a", "out"), BlockEndpoint("b", "in"),
                       route=[RoutePoint(1, 1), RoutePoint(2, 1)])
        p = _project([_placed("a", 1, 1), _placed("b", 2, 1)], [c])
        assert "unused_port" not in _categories(check_project(p, CHIP_TYPES))

    def test_unconnected_block(self):
        p = _project([_placed("a", 1, 1)])
        r = check_project(p, CHIP_TYPES)
        assert any(w.category == "unused_port" for w in r.warnings)


# --- utilization (INFO) ---------------------------------------------------- #

class TestUtilization:
    def test_reports_per_chip(self):
        p = _project([_placed("a", 1, 1)], chips=(0,))
        r = check_project(p, CHIP_TYPES)
        infos = [i for i in r.infos if i.category == "utilization"]
        assert len(infos) == 1
        assert "1/120" in infos[0].message  # 1 cell of 10x12


# --- result semantics ------------------------------------------------------ #

class TestResultSemantics:
    def test_ok_with_only_warnings(self):
        # An unconnected block is a warning only -> ok stays True.
        p = _project([_placed("a", 1, 1)])
        r = check_project(p, CHIP_TYPES)
        assert r.warnings
        assert r.ok

    def test_not_ok_with_errors(self):
        p = _project([_placed("a", 1, 1), _placed("b", 1, 1)])
        assert not check_project(p, CHIP_TYPES).ok

    def test_collects_multiple_errors(self):
        # overlap AND out-of-bounds AND unrouted in one project.
        c = Connection("c", BlockEndpoint("a", "out"), BlockEndpoint("b", "in"))
        p = _project([_placed("a", 1, 1), _placed("b", 1, 1), _placed("z", 99, 99)], [c])
        cats = _categories(check_project(p, CHIP_TYPES))
        assert {"overlap", "unplaced_cell", "unrouted"} <= cats


# --- SRAM panels ----------------------------------------------------------- #

def _chip_type_with_x1() -> ChipType:
    return ChipType(
        name="t", width=10, height=12,
        ports=(
            PortSpec("x16_in", PortDirection.INPUT, 16, 0, 0, Face.NORTH),
            PortSpec("x16_out", PortDirection.OUTPUT, 16, 9, 0, Face.EAST),
            PortSpec("x1_in", PortDirection.INPUT, 1, 0, 11, Face.NORTH),
            PortSpec("x1_out", PortDirection.OUTPUT, 1, 9, 11, Face.SOUTH),
        ),
    )


class TestPanelDRC:
    def _panel_project(self, *, panel=None, conn=None):
        from model.connection import PanelConnection
        from model.panel import SramPanel
        p = _project()
        p.panels = [panel] if panel else [SramPanel(id=0)]
        if conn is not None:
            p.panel_connections = [conn]
        return p

    def test_valid_link_clean(self):
        from model.connection import PanelConnection
        # panel x16_out (output, x16) → chip x16_in (input, x16): clean
        p = self._panel_project(conn=PanelConnection(0, "x16_out", 0, "x16_in"))
        cats = _categories(check_project(p, CHIP_TYPES))
        assert "panel_port_mismatch" not in cats

    def test_x1_link_clean(self):
        from model.connection import PanelConnection
        # panel x1_out (output, x1) → chip x1_in (input, x1): clean
        p = self._panel_project(conn=PanelConnection(0, "x1_out", 0, "x1_in"))
        cats = _categories(check_project(p, {"t": _chip_type_with_x1()}))
        assert "panel_port_mismatch" not in cats

    def test_width_mismatch_errors(self):
        from model.connection import PanelConnection
        # panel x16_out (x16, output) → chip x1_in (x1, input): direction OK but
        # width mismatch.
        p = self._panel_project(conn=PanelConnection(0, "x16_out", 0, "x1_in"))
        r = check_project(p, {"t": _chip_type_with_x1()})
        assert "panel_port_mismatch" in _categories(r)
        assert not r.ok

    def test_same_direction_errors(self):
        from model.connection import PanelConnection
        # panel x16_out (output) → chip x16_out (output): both outputs
        p = self._panel_project(conn=PanelConnection(0, "x16_out", 0, "x16_out"))
        assert "panel_port_mismatch" in _categories(check_project(p, CHIP_TYPES))

    def test_unknown_port_errors(self):
        from model.connection import PanelConnection
        p = self._panel_project(conn=PanelConnection(0, "nope", 0, "x16_in"))
        assert "panel_port_mismatch" in _categories(check_project(p, CHIP_TYPES))

    def test_unknown_chip_port_errors(self):
        from model.connection import PanelConnection
        p = self._panel_project(
            conn=PanelConnection(0, "x16_out", 0, "no_port"))
        assert "panel_port_mismatch" in _categories(check_project(p, CHIP_TYPES))

    def test_address_regs_scale_so_no_overflow(self):
        # A panel is self-consistent by construction: address_regs derives from
        # size_words, so a >64k panel reports 2 regs (no overflow possible).
        from model.panel import SramPanel
        assert SramPanel(id=0, size_words=1 << 20).address_regs == 2
