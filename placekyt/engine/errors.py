"""Engine-layer exceptions (distinct from the file-I/O errors in engine.io)."""

from __future__ import annotations


class EngineError(Exception):
    """Base for engine-layer errors (build, simulate, registry)."""


class RegistryError(EngineError):
    """A chip-type name could not be resolved by the registry."""


class SimulationError(EngineError):
    """A simulation could not run (bad bitstream, missing port, etc.)."""
