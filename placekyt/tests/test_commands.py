"""Tests for the command layer + model mutators (§4.2, §6, §11.2).

Pure model + commands — no Qt, no engine. Covers execute/undo round-trip for
every command, composite atomicity + rollback, MAX_HISTORY, redo-stack clear,
event-bus flush timing, and dirty-flag behavior.
"""

from __future__ import annotations

import pytest

from commands import (
    AddChipCommand,
    AddConnectionCommand,
    CommandManager,
    CompositeCommand,
    EditParamsCommand,
    MoveBlockCommand,
    PlaceBlockCommand,
    PlaceCellCommand,
    RemoveBlockCommand,
    RemoveConnectionCommand,
    RenameBlockCommand,
    SetCellFaceCommand,
    TransformBlockCommand,
)
from commands.base import Command
from model.block import Block
from model.chip import ChipInstance
from model.connection import (
    BlockEndpoint,
    ChipPortEndpoint,
    Connection,
    RoutePoint,
)
from model.enums import Face
from model.placement import PlacedCell
from model.project import Project


@pytest.fixture
def project():
    return Project()


@pytest.fixture
def mgr(project):
    return CommandManager(project)


def _placed_block(name="g", n=2):
    blk = Block(name, "GainBlock", library="lattrex.official")
    cells = [PlacedCell(i, 1 + i, 1, Face.EAST) for i in range(n)]
    return blk, cells


# --------------------------------------------------------------------------- #
# Mutators emit but do not flush (§6)
# --------------------------------------------------------------------------- #


class TestMutatorsDefer:
    def test_emit_queues_without_delivery(self, project):
        seen = []
        project.event_bus.subscribe_all(lambda t, **kw: seen.append(t))
        project._add_block(Block("g", "GainBlock"))
        project._place_cell("g", 0, 0, 1, 1, Face.EAST)
        assert seen == []  # deferred
        assert project.event_bus.pending == 2
        project.event_bus.flush()
        assert seen == ["block_added", "cell_placed"]


# --------------------------------------------------------------------------- #
# CommandManager flush + dirty + stacks
# --------------------------------------------------------------------------- #


class TestManager:
    def test_execute_flushes_and_marks_dirty(self, project, mgr):
        seen = []
        project.event_bus.subscribe_all(lambda t, **kw: seen.append(t))
        assert not project.project_dirty
        blk, cells = _placed_block()
        mgr.execute(PlaceBlockCommand(project, blk, 0, cells))
        # flushed by the manager after execute
        assert "block_added" in seen and "cell_placed" in seen
        assert project.project_dirty and project.build_dirty
        assert mgr.can_undo() and not mgr.can_redo()

    def test_undo_redo_round_trip(self, project, mgr):
        blk, cells = _placed_block()
        mgr.execute(PlaceBlockCommand(project, blk, 0, cells))
        mgr.undo()
        assert project.block("g") is None
        assert mgr.can_redo()
        mgr.redo()
        assert project.block("g").is_placed

    def test_new_command_clears_redo(self, project, mgr):
        blk, cells = _placed_block()
        mgr.execute(PlaceBlockCommand(project, blk, 0, cells))
        mgr.undo()
        assert mgr.can_redo()
        mgr.execute(AddChipCommand(project, ChipInstance(0, "C0")))
        assert not mgr.can_redo()  # redo stack cleared by the new command

    def test_max_history_drops_oldest(self, project):
        mgr = CommandManager(project)
        mgr.MAX_HISTORY = 5  # instance override for the test
        for i in range(8):
            mgr.execute(AddChipCommand(project, ChipInstance(i, f"C{i}")))
        assert mgr.undo_depth == 5  # capped
        # undoing all 5 leaves 3 chips that can no longer be undone
        for _ in range(5):
            mgr.undo()
        assert not mgr.can_undo()
        assert len(project.chips) == 3

    def test_undo_redo_text(self, project, mgr):
        mgr.execute(AddChipCommand(project, ChipInstance(0, "C0")))
        assert "chip 0" in mgr.undo_text().lower()
        mgr.undo()
        assert "chip 0" in mgr.redo_text().lower()

    def test_undo_redo_noop_when_empty(self, project, mgr):
        mgr.undo()  # nothing to undo — must not raise
        mgr.redo()
        assert not mgr.can_undo() and not mgr.can_redo()


# --------------------------------------------------------------------------- #
# Composite atomicity + rollback (§4.2, §6)
# --------------------------------------------------------------------------- #


class _BoomCommand(Command):
    def execute(self):
        raise RuntimeError("boom")

    def undo(self):
        pass

    def description(self):
        return "boom"


class TestComposite:
    def test_rollback_on_partial_failure(self, project, mgr):
        # A composite whose 2nd child fails must undo the 1st and not be pushed.
        good = AddChipCommand(project, ChipInstance(0, "C0"))
        composite = CompositeCommand("mixed", [good, _BoomCommand()])
        with pytest.raises(RuntimeError):
            mgr.execute(composite)
        assert project.chip(0) is None  # rolled back
        assert not mgr.can_undo()       # failed command not on the stack

    def test_failed_execute_emits_resync_and_clears_queue(self, project, mgr):
        seen = []
        project.event_bus.subscribe_all(lambda t, **kw: seen.append(t))
        composite = CompositeCommand(
            "mixed", [AddChipCommand(project, ChipInstance(0, "C0")), _BoomCommand()]
        )
        with pytest.raises(RuntimeError):
            mgr.execute(composite)
        # The half-emitted chip_added is cleared; only command_failed is seen.
        assert "chip_added" not in seen
        assert "command_failed" in seen


# --------------------------------------------------------------------------- #
# Concrete commands — execute/undo round-trips
# --------------------------------------------------------------------------- #


class TestPlaceCell:
    def test_place_new_then_undo_removes(self, project, mgr):
        mgr.execute(PlaceBlockCommand(project, Block("g", "GainBlock"), 0, []))
        mgr.execute(PlaceCellCommand(project, "g", 0, 0, 4, 4, Face.SOUTH))
        assert project.block("g").placement.cell(0).pos == (4, 4)
        mgr.undo()
        assert project.block("g").placement is None or \
            project.block("g").placement.cell(0) is None

    def test_reposition_existing_then_undo_restores(self, project, mgr):
        blk, cells = _placed_block(n=1)
        mgr.execute(PlaceBlockCommand(project, blk, 0, cells))
        orig = project.block("g").placement.cell(0).pos
        mgr.execute(PlaceCellCommand(project, "g", 0, 0, 7, 7, Face.WEST))
        assert project.block("g").placement.cell(0).pos == (7, 7)
        mgr.undo()
        assert project.block("g").placement.cell(0).pos == orig


class TestMoveBlock:
    def test_move_and_undo(self, project, mgr):
        blk, cells = _placed_block(n=2)
        mgr.execute(PlaceBlockCommand(project, blk, 0, cells))
        before = [c.pos for c in project.block("g").placement.cells]
        mgr.execute(MoveBlockCommand(project, "g", 2, 3))
        moved = [c.pos for c in project.block("g").placement.cells]
        assert moved == [(x + 2, y + 3) for x, y in before]
        mgr.undo()
        assert [c.pos for c in project.block("g").placement.cells] == before

    def test_move_keeps_connection_clears_route(self, project, mgr):
        """A move UNROUTES the block's connections (so no stale route-marker
        cells are left stranded at the old position, and the fly line reappears)
        but must NOT delete the net. Undo restores both placement and route."""
        blk, cells = _placed_block(name="g", n=1)
        mgr.execute(PlaceBlockCommand(project, blk, 0, cells))
        c = Connection("c", ChipPortEndpoint(0, "x16_in"),
                       BlockEndpoint("g", "in"), route=[RoutePoint(0, 0)])
        mgr.execute(AddConnectionCommand(project, c))
        mgr.execute(MoveBlockCommand(project, "g", 2, 3))
        conn = project.connection("c")
        assert conn is not None, "move must KEEP the connection (net)"
        assert not conn.is_routed, "move must clear the stale route (no orphan cells)"
        mgr.undo()
        assert project.connection("c").is_routed, "undo restores the route"


class TestTransformBlock:
    def test_rotate_cw_and_undo(self, project, mgr):
        blk, cells = _placed_block(n=2)        # (1,1)E (2,1)E
        mgr.execute(PlaceBlockCommand(project, blk, 0, cells))
        before = [(c.pos, c.face) for c in project.block("g").placement.cells]
        mgr.execute(TransformBlockCommand(project, "g", "cw"))
        rotated = [(c.pos, c.face) for c in project.block("g").placement.cells]
        assert rotated == [((1, 1), Face.SOUTH), ((1, 2), Face.SOUTH)]
        mgr.undo()
        assert [(c.pos, c.face)
                for c in project.block("g").placement.cells] == before

    def test_mirror_h_and_undo(self, project, mgr):
        blk, cells = _placed_block(n=2)
        mgr.execute(PlaceBlockCommand(project, blk, 0, cells))
        mgr.execute(TransformBlockCommand(project, "g", "mirror_h"))
        faces = {c.face for c in project.block("g").placement.cells}
        assert faces == {Face.WEST}            # E→W under horizontal flip
        mgr.undo()
        assert {c.face for c in project.block("g").placement.cells} == {Face.EAST}

    def test_unplaced_block_raises(self, project, mgr):
        blk = Block("g", "GainBlock")
        mgr.execute(PlaceBlockCommand(project, blk, 0, []))   # no cells
        with pytest.raises(KeyError):
            mgr.execute(TransformBlockCommand(project, "g", "cw"))

    def test_transform_keeps_connection_clears_route(self, project, mgr):
        """A transform UNROUTES the block's connections (fly line reappears) but
        must NOT delete them — previously it removed the nets, so the wiring
        vanished after a transform (the packed-layout breakage)."""
        blk, cells = _placed_block(name="g", n=1)
        mgr.execute(PlaceBlockCommand(project, blk, 0, cells))
        c = Connection("c", ChipPortEndpoint(0, "x16_in"),
                       BlockEndpoint("g", "in"), route=[RoutePoint(0, 0)])
        mgr.execute(AddConnectionCommand(project, c))
        mgr.execute(TransformBlockCommand(project, "g", "cw"))
        conn = project.connection("c")
        assert conn is not None, "transform must KEEP the connection (net)"
        assert not conn.is_routed, "transform must clear the stale route (fly line)"
        mgr.undo()
        assert project.connection("c").is_routed, "undo restores the route"


class TestSetCellFace:
    def test_set_and_undo(self, project, mgr):
        blk, cells = _placed_block(n=1)
        mgr.execute(PlaceBlockCommand(project, blk, 0, cells))
        mgr.execute(SetCellFaceCommand(project, "g", 0, Face.NORTH))
        assert project.block("g").placement.cell(0).face is Face.NORTH
        mgr.undo()
        assert project.block("g").placement.cell(0).face is Face.EAST


class TestEditParams:
    def test_edit_and_undo(self, project, mgr):
        blk = Block("g", "GainBlock", params={"gain": 0.5})
        mgr.execute(PlaceBlockCommand(project, blk, 0, []))
        mgr.execute(EditParamsCommand(project, "g", {"gain": 0.25}))
        assert project.block("g").params == {"gain": 0.25}
        mgr.undo()
        assert project.block("g").params == {"gain": 0.5}


class TestRenameBlock:
    def test_rename_updates_block_and_routes(self, project, mgr):
        blk, cells = _placed_block(name="g", n=1)
        mgr.execute(PlaceBlockCommand(project, blk, 0, cells))
        c = Connection("c", ChipPortEndpoint(0, "x16_in"),
                       BlockEndpoint("g", "in"), route=[RoutePoint(0, 0)])
        mgr.execute(AddConnectionCommand(project, c))
        mgr.execute(RenameBlockCommand(project, "g", "gain1"))
        assert project.block("g") is None
        assert project.block("gain1") is not None
        # the connection endpoint follows the rename (route preserved)
        conn = project.connection("c")
        assert conn.target.block == "gain1"
        assert conn.is_routed                       # route survived the rewrite

    def test_rename_undo_restores(self, project, mgr):
        blk, cells = _placed_block(name="g", n=1)
        mgr.execute(PlaceBlockCommand(project, blk, 0, cells))
        c = Connection("c", ChipPortEndpoint(0, "x16_in"), BlockEndpoint("g", "in"))
        mgr.execute(AddConnectionCommand(project, c))
        mgr.execute(RenameBlockCommand(project, "g", "gain1"))
        mgr.undo()
        assert project.block("g") is not None
        assert project.block("gain1") is None
        assert project.connection("c").target.block == "g"

    def test_rename_to_existing_raises(self, project, mgr):
        a, ac = _placed_block(name="a", n=1)
        mgr.execute(PlaceBlockCommand(project, a, 0, ac))
        b = Block("b", "GainBlock", library="lattrex.official")
        bc = [PlacedCell(0, 5, 1, Face.EAST)]
        mgr.execute(PlaceBlockCommand(project, b, 0, bc))
        with pytest.raises(ValueError):
            mgr.execute(RenameBlockCommand(project, "a", "b"))

    def test_rename_empty_raises(self, project, mgr):
        blk, cells = _placed_block(name="g", n=1)
        mgr.execute(PlaceBlockCommand(project, blk, 0, cells))
        with pytest.raises(ValueError):
            mgr.execute(RenameBlockCommand(project, "g", "   "))


class TestConnections:
    def test_add_and_undo(self, project, mgr):
        c = Connection("c", ChipPortEndpoint(0, "x16_in"), BlockEndpoint("g", "in"))
        mgr.execute(AddConnectionCommand(project, c))
        assert project.connection("c") is not None
        mgr.undo()
        assert project.connection("c") is None

    def test_remove_and_undo(self, project, mgr):
        c = Connection("c", ChipPortEndpoint(0, "x16_in"), BlockEndpoint("g", "in"))
        project._add_connection(c)  # set up directly
        mgr.execute(RemoveConnectionCommand(project, "c"))
        assert project.connection("c") is None
        mgr.undo()
        assert project.connection("c") is not None


class TestRemoveBlock:
    def test_removes_block_and_connections_atomically(self, project, mgr):
        blk, cells = _placed_block(n=2)
        mgr.execute(PlaceBlockCommand(project, blk, 0, cells))
        mgr.execute(AddConnectionCommand(
            project, Connection("c", ChipPortEndpoint(0, "x16_in"),
                                BlockEndpoint("g", "in"))))
        mgr.execute(RemoveBlockCommand(project, "g"))
        assert project.block("g") is None
        assert project.connection("c") is None  # connection cleaned up
        mgr.undo()
        assert project.block("g").is_placed
        assert project.connection("c") is not None  # connection restored
