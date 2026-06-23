"""File serialization for placeKYT (the architecture notes §2).

All YAML I/O goes through :mod:`engine.io.safe_yaml`, which enforces the §2.1
DoS protections (size cap, anchor/alias node-count limit, depth limit) and the
serialization rules (float precision, unicode, optional-field omission).

Public entry points:
  * :func:`engine.io.project_io.load_project` / ``save_project`` — ``.kyt``
  * :func:`engine.io.chip_type_io.load_chip_type` — chip-type ``.yaml``
  * :func:`engine.io.board_io.load_board` — ``.kdb``
"""

from .board_io import load_board
from .chip_type_io import load_chip_type
from .kbs import Kbs, KbsChip, KbsError, chip_type_hash, read_kbs, write_kbs
from .errors import (
    PathTraversalError,
    ProjectFileError,
    SchemaError,
    UnsupportedFormatVersion,
    YamlBombError,
    YamlDepthError,
    YamlSizeError,
)
from .paths import validate_reference
from .project_io import load_project, project_from_str, save_project

__all__ = [
    # errors
    "ProjectFileError",
    "YamlSizeError",
    "YamlBombError",
    "YamlDepthError",
    "PathTraversalError",
    "SchemaError",
    "UnsupportedFormatVersion",
    # loaders / savers
    "load_project",
    "save_project",
    "project_from_str",
    "load_chip_type",
    "load_board",
    "validate_reference",
    # .kbs bitstream container
    "Kbs",
    "KbsChip",
    "KbsError",
    "read_kbs",
    "write_kbs",
    "chip_type_hash",
]
