"""Chip-type registry — resolve a chip-type NAME to its definition + YAML path.

The project model stores ``chip_type`` as a name (e.g. ``"kyttar_10x12"``), but
``gr_kyttar.bitstream.BitstreamGenerator`` needs the chip-type YAML *path*,
and the build/DRC needs the loaded :class:`ChipType`. This registry bridges the
two: it scans a set of search directories for chip-type ``.yaml`` files, loads
each, and indexes by ``chip_type.name`` (the architecture notes §2.3, §9.2).

Search precedence (first match wins, like §2.2's library precedence):
bundled resources, then ``~/.placekyt/chips``, then any caller-supplied dirs.
Explicit single-file registration is also supported (used by tests / CLI when
a chip-type path is passed directly).
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from model.chip_type import ChipType

from .errors import RegistryError
from .io.chip_type_io import load_chip_type
from .io.errors import ProjectFileError


@dataclass(frozen=True)
class ChipTypeEntry:
    name: str
    chip_type: ChipType
    path: Path


class ChipTypeRegistry:
    """Name → (ChipType, path) lookup built by scanning chip-type YAML dirs."""

    def __init__(self) -> None:
        self._by_name: dict[str, ChipTypeEntry] = {}

    # -- construction ---------------------------------------------------------

    @classmethod
    def from_dirs(cls, dirs: list[Path | str]) -> "ChipTypeRegistry":
        reg = cls()
        for d in dirs:
            reg.scan_dir(d)
        return reg

    def scan_dir(self, directory: Path | str) -> None:
        """Index every ``*.yaml`` in ``directory`` that parses as a chip type.

        Lower-precedence: a name already registered is NOT overwritten (the
        first directory scanned wins).
        """
        d = Path(directory)
        if not d.is_dir():
            return
        for yaml_path in sorted(d.glob("*.yaml")):
            try:
                ct = load_chip_type(yaml_path)
            except ProjectFileError:
                continue  # not a valid chip-type file; skip
            self._by_name.setdefault(
                ct.name, ChipTypeEntry(ct.name, ct, yaml_path.resolve())
            )

    def register_file(self, yaml_path: Path | str) -> ChipTypeEntry:
        """Register a single chip-type YAML file explicitly (highest priority)."""
        p = Path(yaml_path)
        ct = load_chip_type(p)
        entry = ChipTypeEntry(ct.name, ct, p.resolve())
        self._by_name[ct.name] = entry  # explicit overrides scanned
        return entry

    # -- queries --------------------------------------------------------------

    def get(self, name: str) -> ChipTypeEntry | None:
        return self._by_name.get(name)

    def require(self, name: str) -> ChipTypeEntry:
        entry = self._by_name.get(name)
        if entry is None:
            known = ", ".join(sorted(self._by_name)) or "(none)"
            raise RegistryError(
                f"unknown chip type {name!r}. Known types: {known}."
            )
        return entry

    def chip_types(self) -> dict[str, ChipType]:
        """All ``name → ChipType`` (for passing to BuildEngine.build())."""
        return {n: e.chip_type for n, e in self._by_name.items()}

    def paths(self) -> dict[str, str]:
        """All ``name → yaml path`` (for BuildEngine's chip_type_paths)."""
        return {n: str(e.path) for n, e in self._by_name.items()}

    def names(self) -> list[str]:
        return sorted(self._by_name)
