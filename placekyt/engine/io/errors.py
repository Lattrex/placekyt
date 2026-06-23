"""Exceptions for placeKYT file I/O (the architecture notes §2.1)."""

from __future__ import annotations


class ProjectFileError(Exception):
    """Base class for any error loading/saving a placeKYT file.

    The CLI maps this to exit code 3 (file error) per §11.4.
    """


class YamlSizeError(ProjectFileError):
    """File exceeds the 16 MB hard cap (rejected before parsing)."""


class YamlBombError(ProjectFileError):
    """Anchor/alias expansion exceeded the node-count limit (billion-laughs)."""


class YamlDepthError(ProjectFileError):
    """Nesting exceeded the maximum parse depth."""


class PathTraversalError(ProjectFileError):
    """A referenced path escaped its allowed root (``..``, UNC, or symlink)."""


class UnsupportedFormatVersion(ProjectFileError):
    """The project's ``format_version`` is newer than this IDE supports."""


class SchemaError(ProjectFileError):
    """A required field is missing or has the wrong shape/type."""
