"""Tests for the PortMap — a block's bus-facing I/O geometry (auto-P&R P2.2)."""

from __future__ import annotations

import os

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication  # noqa: E402

from engine.catalog import BlockCatalog  # noqa: E402
from engine.portmap import PortInfo, PortMap, build_port_map  # noqa: E402
from model.enums import Face  # noqa: E402


@pytest.fixture(scope="module")
def qapp():
    return QApplication.instance() or QApplication([])


@pytest.fixture(scope="module")
def catalog(qapp):
    return BlockCatalog.from_gr_kyttar()


# -- builder over every catalog block ------------------------------------------

def test_every_catalog_block_builds_a_portmap(catalog):
    """P2.2: a PortMap derives for EVERY catalog block without error, and every
    block exposes at least one input or output port (a block with no external I/O
    would be unreachable)."""
    empty = []
    for spec in catalog.all():
        pm = build_port_map(catalog, spec.type_name, library=spec.library)
        assert isinstance(pm, PortMap)
        if not pm.ports:
            empty.append(spec.type_name)
    assert not empty, f"blocks with no external ports: {empty}"


def test_portmap_offsets_in_footprint(catalog):
    """Every port offset lies within the declared footprint (no port off-block)."""
    for spec in catalog.all():
        pm = build_port_map(catalog, spec.type_name, library=spec.library)
        w, h = pm.footprint
        for p in pm.ports:
            assert 0 <= p.dx <= w and 0 <= p.dy <= h, \
                f"{spec.type_name}.{p.name} at {p.offset} outside {pm.footprint}"


# -- transform algebra ---------------------------------------------------------

def test_four_cw_rotations_is_identity(catalog):
    """Four 90° CW rotations return every block's PortMap to its original offsets
    and faces — the transform algebra is consistent (matches Placement.transform)."""
    for spec in catalog.all():
        pm = build_port_map(catalog, spec.type_name, library=spec.library)
        r = pm
        for _ in range(4):
            r = r.transformed("cw")
        for a, b in zip(pm.ports, r.ports):
            assert a.offset == b.offset and a.face == b.face, spec.type_name


def test_cw_then_ccw_is_identity(catalog):
    for spec in catalog.all():
        pm = build_port_map(catalog, spec.type_name, library=spec.library)
        r = pm.transformed("cw").transformed("ccw")
        for a, b in zip(pm.ports, r.ports):
            assert a.offset == b.offset and a.face == b.face, spec.type_name


def test_double_mirror_is_identity(catalog):
    for spec in catalog.all():
        pm = build_port_map(catalog, spec.type_name, library=spec.library)
        for kind in ("mirror_h", "mirror_v"):
            r = pm.transformed(kind).transformed(kind)
            for a, b in zip(pm.ports, r.ports):
                assert a.offset == b.offset and a.face == b.face, \
                    f"{spec.type_name} {kind}"


def test_cw_rotation_swaps_footprint():
    """A 90° rotation swaps footprint w/h and maps a port's offset + face."""
    pm = PortMap(
        block_type="t",
        ports=(PortInfo("in", "in", "a", 0, 0, Face.EAST, register=0, entry=1),
               PortInfo("out", "out", "b", 3, 0, Face.EAST)),
        footprint=(3, 0),
        bus_facing_edge=None, io_colocated=False,
    )
    r = pm.transformed("cw")
    assert r.footprint == (0, 3)            # w/h swapped
    # (0,0) EAST -> (0,0) SOUTH ; (3,0) EAST -> (0,3) SOUTH
    assert r.port("in").offset == (0, 0) and r.port("in").face == Face.SOUTH
    assert r.port("out").offset == (0, 3) and r.port("out").face == Face.SOUTH


# -- specific known blocks -----------------------------------------------------

def test_single_cell_block_io_colocated(catalog):
    """A single-cell block (AGC, BPSK slicer) has its input and output on ONE cell
    => trivially co-located (one bus tap), §4.3."""
    for name in ("AGCBlock", "BPSKSlicerBlock"):
        pm = catalog.port_map(name)
        assert pm.footprint == (0, 0)
        assert pm.inputs() and pm.outputs()
        assert pm.inputs()[0].cell_id == pm.outputs()[0].cell_id
        assert pm.io_colocated, name


def test_coherent_rx_ports_and_no_feedback_input(catalog):
    """CoherentRXBlock: external inputs are xi/xq at the landing cell; the internal
    dphase feedback is NOT an external port; the recovered bit leaves the slicer.
    Straight pipeline => I/O on opposite edges => NOT co-located (the §4.3
    fall-back case the general router must cover)."""
    pm = catalog.port_map("CoherentRXBlock")
    in_names = {p.name for p in pm.inputs()}
    assert in_names == {"xi", "xq"}, in_names
    assert "dphase" not in in_names          # internal feedback, not a bus port
    out_names = {p.name for p in pm.outputs()}
    assert out_names == {"bit"}, out_names
    # input lands at the origin, output exits at the far end of the row
    assert pm.port("xi").offset == (0, 0)
    assert pm.port("bit").offset == (9, 0)
    assert not pm.io_colocated


def test_input_ports_carry_register_and_entry(catalog):
    """Input ports record the landing register + JUMP entry (so the router can
    configure the chip input port / upstream broker to deliver into them)."""
    pm = catalog.port_map("CoherentRXBlock")
    xi = pm.port("xi")
    assert xi.register == 0 and xi.entry is not None and xi.entry > 0
    assert pm.port("xq").register == 1
