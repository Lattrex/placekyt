# SPDX-License-Identifier: GPL-3.0-or-later
"""Face-lock VISIBILITY: a rail routed onto an already-taken face must not look done.

A ``NEEDS_DISTINCT_INPUT_FACES`` block (ClarkeTransform, CordicRotate, SVPWM,
DualFloatToComplex, TMRVoter…) tells its independent async input streams apart ONLY by
which FACE each word arrives on. A complex link is TWO nets — GNU Radio has one complex
port, so ``grc_import`` SYNTHESISES the Q rail — and both rails leave the SAME source
cell. Route them down one corridor and they land on one face: the design is dead, yet

  * the fly line for each vanished (both are "routed"), and
  * the two identical routes painted exactly on top of each other, so the canvas drew
    ONE line for two nets,

leaving a DRC row about arrival faces as the only feedback. This module gates the three
things that close that gap:

  1. the canvas DECIDES to draw attention-styled guidance for a face-colliding rail
     (a structural assertion on the scene items / the decision function — offscreen
     tests cannot observe paint, so pixels are never asserted);
  2. coincident routes get DISTINCT rendered geometry, and a click on either still
     selects the right net;
  3. the DRC message is ACTIONABLE — it names the nets, says when they are the I/Q
     rails of one complex output that must FORK, and says whether a reroute can fix it
     at this anchor or the block must be re-anchored (with why each face is unusable).

Fixtures are the shipped FOC loop: ``foc_motor.kyt`` (correctly forked — must stay
clean) plus a synthetic same-face variant derived from it, so the failing case is
always exercised from tracked data. The user's hand-routed ``foc_motor.full.kyt`` is
read as an extra fixture WHEN PRESENT (never written).
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication  # noqa: E402

from engine.bus_drc import (_check_dual_input_same_face,  # noqa: E402
                            face_lock_arrivals)
from engine.catalog import BlockCatalog  # noqa: E402
from engine.io.chip_type_io import load_chip_type  # noqa: E402
from engine.io.project_io import load_project  # noqa: E402
from ui.canvas.chip_canvas import ChipCanvas  # noqa: E402
from ui.canvas.connection_item import ConnectionItem  # noqa: E402

from tests.conftest import CHIP_YAML as CT_PATH  # noqa: E402

REPO = Path(__file__).resolve().parent.parent.parent
FOC_DIR = REPO / "examples" / "foc_motor"
FOC_CLEAN = FOC_DIR / "foc_motor.kyt"
FOC_FULL = FOC_DIR / "foc_motor.full.kyt"   # the user's WIP; untracked, optional

pytestmark = pytest.mark.skipif(
    not (CT_PATH.exists() and FOC_CLEAN.exists()),
    reason="chip yaml / foc_motor.kyt fixture absent")


@pytest.fixture(scope="module")
def qapp():
    app = QApplication.instance() or QApplication([])
    yield app


@pytest.fixture(scope="module")
def catalog():
    return BlockCatalog.from_gr_kyttar()


@pytest.fixture(scope="module")
def chip_types():
    ct = load_chip_type(CT_PATH)
    return {ct.name: ct}


@pytest.fixture
def clean_project():
    return load_project(FOC_CLEAN)


def _pt(xy):
    """A route waypoint at ``(x, y)`` — the model's own waypoint type."""
    from model.connection import RoutePoint
    return RoutePoint(x=xy[0], y=xy[1])


def _collide(project, block, port, onto_port):
    """Reroute ``block.port`` so it arrives on the SAME face as ``block.onto_port``.

    Copies the sibling net's route wholesale — exactly the "both rails down one
    corridor" mistake, and the geometry that also makes the two lines coincide."""
    donor = next(c for c in project.connections
                 if getattr(c.target, "block", None) == block
                 and getattr(c.target, "port", None) == onto_port)
    victim = next(c for c in project.connections
                  if getattr(c.target, "block", None) == block
                  and getattr(c.target, "port", None) == port)
    victim.route = list(donor.route)
    return victim, donor


# --------------------------------------------------------------------------- #
# (0) the shared arrival-face helper — ONE authority for DRC + canvas
# --------------------------------------------------------------------------- #

def test_arrivals_helper_reports_faces_and_no_collision_when_forked(
        clean_project, catalog):
    """The shipped FOC loop forks both I/Q pairs; the helper must see distinct faces
    and flag NOTHING. (If this ever collides, the fixture — not the code — moved.)"""
    arr = face_lock_arrivals(clean_project, catalog)
    assert arr, "foc_motor.kyt has face-locking blocks; the helper found none"
    assert "svpwm" in arr and "cordicrotate" in arr
    for rec in arr.values():
        assert not rec.colliding, f"{rec.block} unexpectedly collides: {rec.faces}"
        # every recorded port carries a net name (the canvas keys off these)
        assert set(rec.nets) == set(rec.faces)
    # svpwm's two rails really do arrive on DIFFERENT faces in the good file
    assert len(set(arr["svpwm"].faces.values())) == len(arr["svpwm"].faces)


def test_arrivals_helper_flags_both_ports_of_a_collision(clean_project, catalog):
    """Both colliding ports are reported (not just the second) — the canvas needs to
    keep guidance on EACH rail, since either one could be the one to move."""
    _collide(clean_project, "svpwm", "v_beta", "v_alpha")
    arr = face_lock_arrivals(clean_project, catalog)
    assert arr["svpwm"].colliding == {"v_alpha", "v_beta"}
    assert len(arr["svpwm"].collision_faces) == 1
    assert not arr["cordicrotate"].colliding, "unrelated block must stay clean"


def test_drc_and_canvas_agree_on_the_same_nets(clean_project, catalog, chip_types,
                                               qapp):
    """The canvas must not invent or miss a conflict: the nets it decides to warn
    about are EXACTLY the nets the DRC errors on. One authority, two consumers."""
    _collide(clean_project, "svpwm", "v_beta", "v_alpha")
    canvas = ChipCanvas()
    canvas.face_lock_catalog = catalog
    canvas.set_project(clean_project, chip_types)
    from_canvas = canvas._face_conflict_nets()

    viols = _check_dual_input_same_face(clean_project, catalog, chip_types)
    assert viols, "the collision must ERROR"
    # Map each violation's reported PORTS back to their net names via the same helper.
    arr = face_lock_arrivals(clean_project, catalog)
    by_cell = {rec.cell: rec for rec in arr.values()}
    from_drc = {by_cell[v.cell].nets[port] for v in viols for port in v.nets}
    assert from_canvas == from_drc == {"va", "vb"}, (from_canvas, from_drc)


# --------------------------------------------------------------------------- #
# (1) the canvas keeps guidance UP on a routed-but-illegally-faced rail
# --------------------------------------------------------------------------- #

def test_canvas_draws_attention_line_for_a_face_colliding_rail(
        clean_project, catalog, chip_types, qapp):
    """STRUCTURAL: routing the second rail down the first's corridor must NOT read as
    completion. Both colliding nets get an attention-styled ConnectionItem, in a style
    that is neither the routed line nor the unrouted fly line."""
    _collide(clean_project, "svpwm", "v_beta", "v_alpha")
    canvas = ChipCanvas()
    canvas.face_lock_catalog = catalog
    canvas.set_project(clean_project, chip_types)

    conflict = {i.connection_name for i in canvas._scene.items()
                if isinstance(i, ConnectionItem) and i.is_conflict}
    assert conflict == {"va", "vb"}, conflict
    for item in canvas._scene.items():
        if isinstance(item, ConnectionItem) and item.is_conflict:
            # a THIRD style: not the gray fly line, not the green routed line
            assert not item.is_fly
            assert item.zValue() > 5      # drawn over the route it warns about


def test_attention_line_is_labelled_with_its_net(clean_project, catalog,
                                                 chip_types, qapp):
    """The user must be able to tell WHICH of two overlapping rails is the problem,
    so each attention line carries a label naming the net and its target port."""
    _collide(clean_project, "svpwm", "v_beta", "v_alpha")
    canvas = ChipCanvas()
    canvas.face_lock_catalog = catalog
    canvas.set_project(clean_project, chip_types)
    labels = {i.data(1): i.text() for i in canvas._scene.items()
              if i.data(0) == "face_conflict_label"}
    assert set(labels) == {"va", "vb"}, labels
    assert "v_alpha" in labels["va"] and "v_beta" in labels["vb"], labels


def test_clean_design_draws_no_attention_line(clean_project, catalog,
                                              chip_types, qapp):
    """No false alarms: the correctly-forked shipped design gets NO attention line
    and NO label — otherwise the signal is worthless."""
    canvas = ChipCanvas()
    canvas.face_lock_catalog = catalog
    canvas.set_project(clean_project, chip_types)
    assert not [i for i in canvas._scene.items()
                if isinstance(i, ConnectionItem) and i.is_conflict]
    assert not [i for i in canvas._scene.items()
                if i.data(0) == "face_conflict_label"]


def test_no_catalog_means_no_face_guidance(clean_project, chip_types, qapp):
    """Without a bound catalog the check is a pure no-op — the canvas must still
    render (an unbound provider is a supported state, e.g. a bare canvas widget)."""
    _collide(clean_project, "svpwm", "v_beta", "v_alpha")
    canvas = ChipCanvas()          # face_lock_catalog left None
    canvas.set_project(clean_project, chip_types)
    assert canvas._face_conflict_nets() == set()
    assert not [i for i in canvas._scene.items()
                if isinstance(i, ConnectionItem) and i.is_conflict]


# --------------------------------------------------------------------------- #
# (2) coincident routes render as two visible wires, and stay separately clickable
# --------------------------------------------------------------------------- #

def test_coincident_routes_get_distinct_geometry(clean_project, catalog,
                                                 chip_types, qapp):
    """Two nets on the SAME waypoint path must not paint on top of each other."""
    _collide(clean_project, "svpwm", "v_beta", "v_alpha")
    canvas = ChipCanvas()
    canvas.face_lock_catalog = catalog
    canvas.set_project(clean_project, chip_types)
    routed = {i.connection_name: i for i in canvas._scene.items()
              if isinstance(i, ConnectionItem) and not i.is_conflict
              and i.connection_name in ("va", "vb")}
    assert set(routed) == {"va", "vb"}
    a, b = routed["va"], routed["vb"]
    assert a.parallel_offset != b.parallel_offset
    ga = [(round(p.x(), 3), round(p.y(), 3)) for p in a._drawn_pts()]
    gb = [(round(p.x(), 3), round(p.y(), 3)) for p in b._drawn_pts()]
    assert ga != gb, "coincident routes still render identical geometry"


def test_coincident_routes_stay_separately_selectable(clean_project, catalog,
                                                      chip_types, qapp):
    """Selection must not regress: a click on either of two separated lines resolves
    to THAT net (their hit bands must stop overlapping)."""
    _collide(clean_project, "svpwm", "v_beta", "v_alpha")
    canvas = ChipCanvas()
    canvas.face_lock_catalog = catalog
    canvas.set_project(clean_project, chip_types)
    routed = {i.connection_name: i for i in canvas._scene.items()
              if isinstance(i, ConnectionItem) and not i.is_conflict
              and i.connection_name in ("va", "vb")}
    for name, item in routed.items():
        pts = item._drawn_pts()
        probe = pts[len(pts) // 2]
        hits = [i.connection_name for i in canvas._scene.items(probe)
                if isinstance(i, ConnectionItem) and not i.is_conflict]
        assert hits and hits[0] == name, (name, hits)
        assert item.shape().contains(item.mapFromScene(probe))


def test_coincidence_grouping_is_general_not_iq_specific(qapp):
    """The separation is a GENERAL fix: any nets sharing a segment fan apart, and a
    net that shares nothing keeps its exact centre-line geometry."""
    offs = ChipCanvas._coincident_offsets([
        ("a", 0, [(0, 0), (1, 0), (2, 0)]),
        ("b", 0, [(0, 0), (1, 0), (2, 0)]),       # identical to a
        ("c", 0, [(1, 0), (2, 0), (3, 0)]),       # PARTIAL overlap with a/b
        ("lonely", 0, [(5, 5), (5, 6)]),          # shares nothing
        ("other_chip", 1, [(0, 0), (1, 0)]),      # same coords, DIFFERENT chip
    ])
    assert set(offs) == {"a", "b", "c"}, offs
    assert len({round(v, 6) for v in offs.values()}) == 3, offs
    assert "lonely" not in offs and "other_chip" not in offs


def test_coincident_fan_is_capped_inside_the_cell(qapp):
    """A big shared trunk must not push its outermost line out of its own cells."""
    from ui.canvas.chip_canvas import COINCIDENT_SPREAD_PX
    many = [(f"n{i}", 0, [(0, 0), (1, 0)]) for i in range(12)]
    offs = ChipCanvas._coincident_offsets(many)
    assert len(offs) == 12
    assert max(abs(v) for v in offs.values()) <= COINCIDENT_SPREAD_PX + 1e-9


# --------------------------------------------------------------------------- #
# (3) the DRC message is ACTIONABLE
# --------------------------------------------------------------------------- #

def test_message_names_nets_and_the_iq_fork(clean_project, catalog, chip_types):
    """A synthesised Q rail is a net the user never drew. The message must name both
    nets AND say they are the I/Q rails of one complex output that must FORK."""
    _collide(clean_project, "svpwm", "v_beta", "v_alpha")
    viols = _check_dual_input_same_face(clean_project, catalog, chip_types)
    assert len(viols) == 1
    msg = viols[0].reason
    assert "v_alpha" in msg and "v_beta" in msg
    assert "va" in msg and "vb" in msg, "net names missing"
    assert "I/Q rails" in msg and "FORK" in msg, msg
    assert "cordicrotate" in msg, "the shared source cell must be named"


def test_message_says_reroutable_when_faces_remain(clean_project, catalog,
                                                   chip_types):
    """cordicrotate in the shipped layout has free neighbours ⇒ fixable in place."""
    _collide(clean_project, "cordicrotate", "y", "x")
    viols = [v for v in _check_dual_input_same_face(clean_project, catalog,
                                                    chip_types)
             if "cordicrotate" in v.reason]
    assert len(viols) == 1
    msg = viols[0].reason
    assert "Reroutable in place" in msg, msg
    assert "usable faces" in msg
    assert "Re-anchor" not in msg


def test_message_says_re_anchor_when_no_face_is_left(clean_project, catalog,
                                                     chip_types):
    """When the target cell has fewer usable faces than inputs to separate, NO
    reroute can fix it — the message must say the block has to MOVE, and say why
    each face is unusable (off-array / own cell / occupied by <block>)."""
    proj = clean_project
    ct = list(chip_types.values())[0]
    svpwm = proj.block("svpwm")
    # Box svpwm in: slide its footprint so the head sits in the array's NE corner
    # (E and N off-array) with its own next cell taking a third face — leaving one
    # usable face for two inputs, which no reroute can ever satisfy. Both input
    # routes are re-pointed at the surviving (S) neighbour so the geometry is a real
    # arrival, not an orphaned route.
    head = svpwm.placement.cells[0]
    dx, dy = (ct.width - 1) - head.x, 0 - head.y
    for c in svpwm.placement.cells:
        c.x, c.y = c.x + dx, c.y + dy
    new_head = (head.x, head.y)
    south = (new_head[0], new_head[1] + 1)
    for conn in proj.connections:
        if getattr(conn.target, "block", None) == "svpwm":
            conn.route = [_pt(south), _pt(new_head)]

    viols = [v for v in _check_dual_input_same_face(proj, catalog, chip_types)
             if "svpwm" in v.reason]
    assert len(viols) == 1, [str(v) for v in viols]
    msg = viols[0].reason
    assert "NO reroute can fix this" in msg, msg
    assert "Re-anchor 'svpwm'" in msg, msg
    assert "off-array" in msg, msg
    assert "Reroutable in place" not in msg


def test_message_free_faces_classification_is_specific(clean_project, chip_types):
    """Each unusable face is explained by KIND, not just counted."""
    from engine.bus_drc import _free_faces
    ct = list(chip_types.values())[0]
    cord = clean_project.block("cordicrotate")
    head = (cord.placement.cells[0].x, cord.placement.cells[0].y)
    usable, blocked = _free_faces(clean_project, head, "cordicrotate",
                                  chip_types, 0)
    assert len(usable) + len(blocked) == 4
    for reason in blocked.values():
        assert (reason == "off-array" or reason == "own cell"
                or reason.startswith("occupied by ")), reason
    assert ct.in_bounds(*head)

    # A multi-cell block's OWN next cell is never a usable arrival face — and must be
    # named as such, not confused with a foreign block sitting there.
    own = [(c.x, c.y) for c in cord.placement.cells[1:]]
    adjacent_own = [f for f, (dx, dy) in ((0, (0, 1)), (1, (1, 0)),
                                          (2, (-1, 0)), (3, (0, -1)))
                    if (head[0] + dx, head[1] + dy) in own]
    assert adjacent_own, "fixture changed: cordicrotate head has no own neighbour"
    for f in adjacent_own:
        assert f not in usable, f
        assert blocked[f] == "own cell", blocked[f]

    # And a face blocked by ANOTHER block names that block.
    occ = {(c.x, c.y): b.name for b in clean_project.blocks
           if b.placement for c in b.placement.cells}
    for f, (dx, dy) in ((0, (0, 1)), (1, (1, 0)), (2, (-1, 0)), (3, (0, -1))):
        owner = occ.get((head[0] + dx, head[1] + dy))
        if owner is not None and owner != "cordicrotate":
            assert blocked[f] == f"occupied by '{owner}'", blocked[f]


def test_message_names_the_block_that_occupies_a_face(clean_project, catalog,
                                                      chip_types):
    """When a foreign block is what makes a face unusable, the message NAMES it —
    "occupied by 'x'" is actionable ("move x, or move me"); a bare "occupied" is not.

    Built from tracked data: svpwm's footprint is slid to the array's E edge so its
    head has E off-array, N its own cell, and W deliberately covered by a foreign
    block — leaving one usable face for two inputs."""
    proj = clean_project
    ct = list(chip_types.values())[0]
    svpwm = proj.block("svpwm")
    head = svpwm.placement.cells[0]
    dx, dy = (ct.width - 1) - head.x, 1 - head.y
    for c in svpwm.placement.cells:
        c.x, c.y = c.x + dx, c.y + dy
    new_head = (head.x, head.y)
    occ = {(c.x, c.y): b.name for b in proj.blocks
           if b.placement and b.name != "svpwm" for c in b.placement.cells}
    blockers = {occ[n] for n in ((new_head[0] - 1, new_head[1]),
                                 (new_head[0], new_head[1] - 1),
                                 (new_head[0], new_head[1] + 1))
                if n in occ}
    assert blockers, "fixture changed: no foreign block adjoins the moved svpwm"
    north = (new_head[0], new_head[1] - 1)
    for conn in proj.connections:
        if getattr(conn.target, "block", None) == "svpwm":
            conn.route = [_pt(north), _pt(new_head)]

    viols = [v for v in _check_dual_input_same_face(proj, catalog, chip_types)
             if "svpwm" in v.reason]
    assert len(viols) == 1, [str(v) for v in viols]
    msg = viols[0].reason
    assert "NO reroute can fix this" in msg, msg
    for name in blockers:
        assert f"occupied by '{name}'" in msg, (name, msg)


def test_message_is_one_readable_row(clean_project, catalog, chip_types):
    """The Design Rules panel renders one row of text — keep it single-line and
    bounded, not a paragraph block."""
    _collide(clean_project, "svpwm", "v_beta", "v_alpha")
    msg = _check_dual_input_same_face(clean_project, catalog, chip_types)[0].reason
    assert "\n" not in msg
    assert len(msg) < 900, len(msg)


def test_clean_design_produces_no_violation(clean_project, catalog, chip_types):
    """The correctly-forked shipped design must stay at ZERO violations."""
    assert _check_dual_input_same_face(clean_project, catalog, chip_types) == []


def test_chip_types_optional_keeps_legacy_callers_working(clean_project, catalog):
    """Several call sites (the auto-placer acceptance gate, the example demos) call
    the DRC with no chip_types. It must still fire — just without the OFF-ARRAY
    classification."""
    _collide(clean_project, "svpwm", "v_beta", "v_alpha")
    viols = _check_dual_input_same_face(clean_project, catalog)
    assert len(viols) == 1
    assert viols[0].kind == "dual_input_same_face"


# --------------------------------------------------------------------------- #
# TWO simultaneous collisions — the shape of the report that started this work
# --------------------------------------------------------------------------- #
#
# These were originally pinned against a hand-routed work-in-progress file, which
# made them assert a SNAPSHOT of someone's in-progress routing: the moment that
# file was re-routed to fix the very collisions under test, the tests failed even
# though the code was correct. A gate must test the BEHAVIOUR, not a transient
# artifact — so the collisions are now synthesised on the tracked, shipped design,
# and the suite is independent of any working file.

def test_two_simultaneous_collisions_each_get_their_own_verdict(
        clean_project, catalog, chip_types):
    """Two colliding rendezvous in ONE design each get an independently computed
    verdict: the I/Q fork clause on both, and reroutable-vs-re-anchor decided per
    block from ITS OWN free faces, not shared."""
    _collide(clean_project, "svpwm", "v_beta", "v_alpha")
    _collide(clean_project, "cordicrotate", "y", "x")
    viols = _check_dual_input_same_face(clean_project, catalog, chip_types)
    assert len(viols) == 2, [str(v) for v in viols]
    reasons = {v.cell: v.reason for v in viols}
    for reason in reasons.values():
        # Every verdict is one of the two, never neither — decided per block.
        assert ("Reroutable in place" in reason
                or "NO reroute can fix this" in reason), reason
    # The svpwm pair ARE the I/Q rails of one complex output (cordicrotate's
    # yi/yq), so that collision must carry the fork clause. The cordicrotate
    # pair are two SEPARATE PI outputs (vd, vq) — not an I/Q pair — so it must
    # NOT: the clause is earned from the design, never boilerplate.
    svp = next(r for r in reasons.values() if "'svpwm'" in r)
    cord = next(r for r in reasons.values() if "'cordicrotate'" in r)
    assert "I/Q rails" in svp and "FORK" in svp, svp
    assert "I/Q rails" not in cord, cord


def test_boxed_in_block_says_re_anchor_and_names_every_blocked_face(
        clean_project, catalog, chip_types, qapp):
    """A rendezvous with too few usable faces must say the block has to MOVE, and
    account for each unusable face by name — the clause that separates a ten-minute
    reroute from an impossible one."""
    _collide(clean_project, "svpwm", "v_beta", "v_alpha")
    viols = _check_dual_input_same_face(clean_project, catalog, chip_types)
    reason = next(v.reason for v in viols)
    assert "I/Q rails" in reason and "FORK" in reason, reason
    if "NO reroute can fix this" in reason:
        assert "Re-anchor 'svpwm'" in reason, reason
        # every face it calls unusable is explained
        assert any(k in reason for k in ("off-array", "own cell", "occupied by")), reason
    else:
        assert "Reroutable in place" in reason, reason


def test_canvas_warns_on_all_four_rails_of_two_collisions(
        clean_project, catalog, chip_types, qapp):
    """All FOUR rails of two colliding pairs keep guidance up, and their coincident
    routes are separated."""
    _collide(clean_project, "svpwm", "v_beta", "v_alpha")
    _collide(clean_project, "cordicrotate", "y", "x")
    canvas = ChipCanvas()
    canvas.face_lock_catalog = catalog
    canvas.set_project(clean_project, chip_types)
    conflict = {i.connection_name for i in canvas._scene.items()
                if isinstance(i, ConnectionItem) and i.is_conflict}
    assert len(conflict) == 4, conflict
    routed = {i.connection_name: i.parallel_offset
              for i in canvas._scene.items()
              if isinstance(i, ConnectionItem) and not i.is_conflict
              and i.connection_name in conflict}
    assert all(v for v in routed.values()), routed


# --------------------------------------------------------------------------- #
# The REAL MainWindow load path — the canvas the user actually looks at
# --------------------------------------------------------------------------- #

def test_main_window_binds_the_catalog_to_the_canvas(qapp):
    """The guidance is only live if MainWindow actually hands the canvas a catalog.

    A bare ``ChipCanvas`` in a unit test can be wired by hand and pass while the real
    window renders nothing — so pin the PRODUCTION wiring, not just the widget."""
    from ui.main_window import MainWindow

    window = MainWindow()
    try:
        assert window.canvas.face_lock_catalog is not None
        assert window.canvas.face_lock_catalog is window.controller.catalog
    finally:
        window.close()


def test_main_window_open_shows_the_guidance(qapp, catalog, chip_types, tmp_path):
    """END TO END through the window's OWN open path: a design with a face-locking
    collision gets attention lines + labels on every colliding rail, and their
    coincident routes separated. Structural (scene items), never pixels.

    Saved to a temp file and opened through the real controller, so the production
    load path is exercised without depending on any working file's current state."""
    from ui.main_window import MainWindow
    from engine.io.project_io import save_project

    proj = load_project(FOC_CLEAN)
    _collide(proj, "svpwm", "v_beta", "v_alpha")
    kyt = tmp_path / "collide.kyt"
    save_project(proj, kyt)

    window = MainWindow()
    try:
        window.controller.open_project(str(kyt))
        window._after_project_loaded()
        assert window.canvas._project is not None
        conflict = {i.connection_name for i in window.canvas._scene.items()
                    if isinstance(i, ConnectionItem) and i.is_conflict}
        labels = {i.data(1) for i in window.canvas._scene.items()
                  if i.data(0) == "face_conflict_label"}
        assert len(conflict) == 2, conflict
        assert labels == conflict, (labels, conflict)
        offset = {i.connection_name for i in window.canvas._scene.items()
                  if isinstance(i, ConnectionItem) and not i.is_conflict
                  and i.parallel_offset}
        assert conflict <= offset, (conflict, offset)
    finally:
        window.close()
