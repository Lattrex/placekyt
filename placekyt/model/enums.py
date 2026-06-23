"""Enumerations shared across the placeKYT data model.

These mirror the string values used in the ``.kyt`` / ``.kbl`` / ``.kdb`` /
chip-type YAML files (the architecture notes §2) so that serialization is a direct
``.value`` round-trip with no translation table.
"""

from __future__ import annotations

from enum import Enum


class Face(Enum):
    """Output direction of a cell (CONFIG[FACE]).

    Values match the hardware FACE encoding documented in the ISA
    (S=0, E=1, W=2, N=3) and the lowercase strings used in project YAML.
    """

    SOUTH = "south"
    EAST = "east"
    WEST = "west"
    NORTH = "north"

    @property
    def code(self) -> int:
        """The 2-bit FACE register value for this direction."""
        return _FACE_CODE[self]

    @property
    def opposite(self) -> "Face":
        """The direction 180° from this one (used by mirror/rotate transforms)."""
        return _FACE_OPPOSITE[self]

    @property
    def rotated_cw(self) -> "Face":
        """This direction rotated 90° CLOCKWISE on screen (y-down grid):
        N→E→S→W→N. Used by block rotate transforms."""
        return _FACE_ROT_CW[self]

    @property
    def rotated_ccw(self) -> "Face":
        """This direction rotated 90° COUNTER-clockwise on screen: N→W→S→E→N."""
        return _FACE_ROT_CCW[self]

    @property
    def mirrored_h(self) -> "Face":
        """This direction under a HORIZONTAL flip (mirror across a vertical
        axis): E↔W, N/S unchanged."""
        return _FACE_MIRROR_H[self]

    @property
    def mirrored_v(self) -> "Face":
        """This direction under a VERTICAL flip (mirror across a horizontal
        axis): N↔S, E/W unchanged."""
        return _FACE_MIRROR_V[self]

    @classmethod
    def from_str(cls, value: str) -> "Face":
        """Parse a YAML face string, case-insensitively.

        Raises ``ValueError`` on an unrecognized direction so that malformed
        project files fail loudly at load rather than silently mis-routing.
        """
        try:
            return cls(value.strip().lower())
        except ValueError as exc:
            valid = ", ".join(f.value for f in cls)
            raise ValueError(
                f"invalid face {value!r}; expected one of: {valid}"
            ) from exc


# Hardware FACE register encoding (ISA: S=0, E=1, W=2, N=3).
_FACE_CODE = {
    Face.SOUTH: 0,
    Face.EAST: 1,
    Face.WEST: 2,
    Face.NORTH: 3,
}

_FACE_OPPOSITE = {
    Face.SOUTH: Face.NORTH,
    Face.NORTH: Face.SOUTH,
    Face.EAST: Face.WEST,
    Face.WEST: Face.EAST,
}

# 90° clockwise on a y-DOWN screen grid: N→E→S→W→N.
_FACE_ROT_CW = {
    Face.NORTH: Face.EAST,
    Face.EAST: Face.SOUTH,
    Face.SOUTH: Face.WEST,
    Face.WEST: Face.NORTH,
}

_FACE_ROT_CCW = {v: k for k, v in _FACE_ROT_CW.items()}

# Horizontal flip (across a vertical axis): E↔W, N/S fixed.
_FACE_MIRROR_H = {
    Face.EAST: Face.WEST,
    Face.WEST: Face.EAST,
    Face.NORTH: Face.NORTH,
    Face.SOUTH: Face.SOUTH,
}

# Vertical flip (across a horizontal axis): N↔S, E/W fixed.
_FACE_MIRROR_V = {
    Face.NORTH: Face.SOUTH,
    Face.SOUTH: Face.NORTH,
    Face.EAST: Face.EAST,
    Face.WEST: Face.WEST,
}

_CODE_FACE = {v: k for k, v in _FACE_CODE.items()}


def face_code_after(code: int, kinds) -> int:
    """Map a hardware FACE register code (S=0,E=1,W=2,N=3) through a sequence of
    D4 transform ``kinds`` (``"cw"``/``"ccw"``/``"mirror_h"``/``"mirror_v"``).

    A block's in-program ``MOVE [FACE], const`` selects an ABSOLUTE direction;
    when the block is rotated/mirrored by the placer, that direction must rotate
    identically (the cell ``.face`` is transformed by ``Placement.transform`` —
    this is the same map for the program's face constants). Unknown codes /
    kinds are returned unchanged."""
    f = _CODE_FACE.get(int(code) & 0x3)
    if f is None:
        return int(code) & 0x3
    for kind in (kinds or []):
        f = {
            "cw": f.rotated_cw,
            "ccw": f.rotated_ccw,
            "mirror_h": f.mirrored_h,
            "mirror_v": f.mirrored_v,
        }.get(kind, f)
    return _FACE_CODE[f]


class PortDirection(Enum):
    """Direction of a chip I/O port (the architecture notes §2.3)."""

    INPUT = "input"
    OUTPUT = "output"


class Modulation(Enum):
    """Connection modulation metadata (the architecture notes §2.1 connections).

    Drives Constellation View, BER auto-derive, and throughput conversion.
    ``bits_per_symbol`` is used by the throughput calculation in
    ``sim.metrics()``.
    """

    BPSK = "bpsk"
    QPSK = "qpsk"
    PSK8 = "8psk"
    NONE = "none"

    @property
    def bits_per_symbol(self) -> int:
        return _BITS_PER_SYMBOL[self]

    @classmethod
    def from_str(cls, value: str) -> "Modulation":
        try:
            return cls(value.strip().lower())
        except ValueError as exc:
            valid = ", ".join(m.value for m in cls)
            raise ValueError(
                f"invalid modulation {value!r}; expected one of: {valid}"
            ) from exc


_BITS_PER_SYMBOL = {
    Modulation.BPSK: 1,
    Modulation.QPSK: 2,
    Modulation.PSK8: 3,
    Modulation.NONE: 0,
}


class IQFormat(Enum):
    """How complex I/Q samples are conveyed on a connection (§2.1, §3.8).

    Only ``q15_paired`` is supported: consecutive 16-bit writes carry I then
    Q, each a full Q15 value. (The 8-bit-packed Q7 format was removed — see
    the the architecture notes errata.)
    """

    Q15_PAIRED = "q15_paired"

    @classmethod
    def from_str(cls, value: str) -> "IQFormat":
        try:
            return cls(value.strip().lower())
        except ValueError as exc:
            valid = ", ".join(f.value for f in cls)
            raise ValueError(
                f"invalid iq_format {value!r}; expected one of: {valid}"
            ) from exc


# Code rates are kept as plain floats in the model (§2.1: 1.0 | 0.5 | 0.75)
# rather than an enum, since the schema permits any FEC rate and the value is
# used directly in bits/sec math. A small set of well-known rates is exposed
# for UI dropdowns and validation hints.
KNOWN_CODE_RATES: tuple[float, ...] = (1.0, 0.5, 0.75)
