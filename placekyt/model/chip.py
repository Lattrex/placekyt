"""Project-level chip instance — a placed chip in the multi-chip canvas.

Mirrors the ``chips:`` section of a ``.kyt`` project (the architecture notes §2.1)::

    chips:
      - id: 0
        label: "RX Front-End"
        position: {x: 0, y: 0}

This is the project's *instance* of a chip, distinct from :class:`ChipType`
(the hardware description) and from ``simkyt.Chip`` (the simulator runtime).
The name ``ChipInstance`` avoids shadowing either of those.

``position`` is a canvas-relative coordinate in scene pixels at zoom=1
(§3.2 ``chip_cell_to_scene``), not a cell coordinate. On project open the IDE
calls ``fit_to_view()`` so positions created on another monitor are still
visible.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass
class ChipInstance:
    """One chip placed on the canvas.

    The chip's *type* (geometry, ports, timing) is resolved via the project's
    ``chip_type`` against the chip-type registry. For homogeneous boards every
    instance shares the project's single ``chip_type``; ``type_name`` is carried
    per-instance to allow heterogeneous boards later without a schema change.
    """

    id: int
    label: str = ""
    position_x: float = 0.0
    position_y: float = 0.0
    type_name: str | None = None  # None -> use the project's chip_type

    @property
    def position(self) -> tuple[float, float]:
        return (self.position_x, self.position_y)
