"""Path-traversal protection for file references in placeKYT files (§2.1).

Paths declared inside ``.kyt`` / ``.kbl`` / ``.kdb`` files (stimulus, golden,
flowgraph, board config, ...) are validated before any file access:

  * ``..`` segments are rejected outright.
  * Windows UNC paths (``\\\\server\\share``) are rejected.
  * The path is canonicalized (``resolve()``, following symlinks) and must
    still resolve *within* an allowed root — the project directory, or
    additionally the bundled resources directory for board configs.

Note that validation does NOT open the file — ``default_stimulus`` and
``gnuradio_flowgraph`` are loaded lazily only when the user runs simulation /
starts the bridge (§2.1, §3.2). Validation just confirms the path is in-bounds.
"""

from __future__ import annotations

from pathlib import Path, PureWindowsPath

from .errors import PathTraversalError


def _is_unc(raw: str) -> bool:
    """True for a Windows UNC path (``\\\\server\\share`` or ``//server/share``).

    Checked on the raw string so it is caught regardless of host OS (a project
    authored on Windows may be opened on Linux and vice versa).
    """
    # Normalize backslashes for the check; UNC begins with two separators.
    norm = raw.replace("\\", "/")
    if norm.startswith("//"):
        return True
    # PureWindowsPath understands drive/UNC anchors explicitly.
    return PureWindowsPath(raw).anchor.startswith("\\\\")


def validate_reference(
    raw_path: str,
    *,
    project_dir: Path,
    extra_roots: tuple[Path, ...] = (),
    field: str = "path",
) -> Path:
    """Validate and resolve a path referenced from a project file.

    Args:
        raw_path: the path string as it appears in the YAML.
        project_dir: the directory containing the project file (primary root).
        extra_roots: additional allowed roots (e.g. bundled resources for
            board configs).
        field: the schema field name, for clearer error messages.

    Returns:
        The resolved absolute :class:`Path`, guaranteed to lie within one of
        the allowed roots.

    Raises:
        PathTraversalError: on ``..`` segments, UNC paths, or escape from all
            allowed roots.
    """
    if _is_unc(raw_path):
        raise PathTraversalError(
            f"{field}: UNC paths are not allowed ({raw_path!r})."
        )

    # Reject explicit parent-dir traversal in the *declared* path. We check the
    # raw segments (both separator styles) before resolution so a path that
    # only stays in-bounds by luck of symlinks is still refused.
    norm = raw_path.replace("\\", "/")
    if ".." in norm.split("/"):
        raise PathTraversalError(
            f"{field}: '..' path segments are not allowed ({raw_path!r})."
        )

    candidate = Path(raw_path)
    base = project_dir.resolve()
    # Relative paths are interpreted against the project directory.
    resolved = (candidate if candidate.is_absolute() else base / candidate).resolve()

    roots = (base, *(r.resolve() for r in extra_roots))
    for root in roots:
        if _within(resolved, root):
            return resolved

    allowed = ", ".join(str(r) for r in roots)
    raise PathTraversalError(
        f"{field}: {raw_path!r} resolves to {resolved}, which is outside the "
        f"allowed root(s): {allowed}."
    )


def _within(path: Path, root: Path) -> bool:
    """True if ``path`` is ``root`` or a descendant of it (both pre-resolved)."""
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False
