"""A block instance placed in a project.

Mirrors a ``blocks:`` entry in the ``.kyt`` schema (the architecture notes §2.1)::

    blocks:
      - name: agc
        type: AGCBlock
        library: lattrex.dsp
        params:
          target: 0.7
        placement:
          chip: 0
          cells: [...]

This is the project's reference to a block *type* (e.g. ``AGCBlock``) plus the
instance's parameter overrides and physical placement. The actual block
definition — cells, assembly, ports, entry points — lives in the existing
``gr_kyttar.placement`` ``KyttarBlock`` subclasses and is resolved by the
``engine/`` ``BlockCatalog`` adapter (§0.1). ``model/`` deliberately does not
import ``gr_kyttar``; it carries only the instance-level data needed to round-
trip the project file and drive the canvas.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .placement import Placement


@dataclass
class Block:
    """A named block instance.

    ``type`` is the block-type name resolved against the library (e.g.
    ``AGCBlock``). ``library`` is the namespaced source (``lattrex.dsp``); when
    ``None`` the catalog falls back to precedence search (§2.2). ``version`` is
    the block definition version recorded at save time for compatibility checks.

    ``placement`` is ``None`` when the block is unplaced (the key is omitted
    from YAML in that case, per §2.1's optional-field rule).

    ``resolved`` is a transient runtime flag (not serialized): ``False`` marks a
    block whose type/library could not be found in the library, which the canvas
    grays out and the build rejects.
    """

    name: str
    type: str
    library: str | None = None
    version: str | None = None
    params: dict[str, Any] = field(default_factory=dict)
    placement: Placement | None = None
    # Canvas display colour: a "#rrggbb" string when the user manually picks one;
    # ``None`` means use the auto colour rotation (so blocks are distinguishable).
    color: str | None = None
    resolved: bool = field(default=True, compare=False)

    @property
    def is_placed(self) -> bool:
        return self.placement is not None and bool(self.placement.cells)

    @property
    def chip(self) -> int | None:
        """The chip this block is placed on, or ``None`` if unplaced."""
        return self.placement.chip if self.placement is not None else None
