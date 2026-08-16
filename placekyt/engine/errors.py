# SPDX-License-Identifier: GPL-3.0-or-later
"""Engine-layer exceptions (distinct from the file-I/O errors in engine.io)."""

from __future__ import annotations


class EngineError(Exception):
    """Base for engine-layer errors (build, simulate, registry)."""


class RegistryError(EngineError):
    """A chip-type name could not be resolved by the registry."""


class SimulationError(EngineError):
    """A simulation could not run (bad bitstream, missing port, etc.)."""


class PlacementError(EngineError):
    """Auto-placement produced an ILLEGAL layout — a block cell off the array,
    or two blocks overlapping. The placer must repack-or-fail loudly rather than
    commit an illegal placement (which the router would only catch later as an
    off-grid endpoint). Carries the list of specific problems."""

    def __init__(self, message: str, problems: list[str] | None = None):
        super().__init__(message)
        self.problems = list(problems or ())
