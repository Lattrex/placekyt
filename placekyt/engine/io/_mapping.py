"""Small helpers for reading fields out of parsed YAML mappings.

ruamel round-trip mode returns ``CommentedMap`` / ``CommentedSeq`` which behave
like dict/list. These helpers give clear :class:`SchemaError` messages (with
the offending key path) instead of raw ``KeyError`` / ``TypeError`` when a
required field is missing or has the wrong shape.
"""

from __future__ import annotations

from typing import Any, Mapping, Sequence

from .errors import SchemaError


def require_mapping(node: Any, where: str) -> Mapping:
    if not isinstance(node, Mapping):
        raise SchemaError(f"{where}: expected a mapping, got {type(node).__name__}.")
    return node


def require(node: Mapping, key: str, where: str) -> Any:
    if key not in node:
        raise SchemaError(f"{where}: missing required field {key!r}.")
    return node[key]


def opt(node: Mapping, key: str, default: Any = None) -> Any:
    """Optional field. Treats an explicit ``null`` the same as absent (§2.1)."""
    if key not in node:
        return default
    val = node[key]
    return default if val is None else val


def require_seq(node: Any, where: str) -> Sequence:
    if not isinstance(node, Sequence) or isinstance(node, (str, bytes)):
        raise SchemaError(f"{where}: expected a list, got {type(node).__name__}.")
    return node


def opt_seq(node: Mapping, key: str, where: str) -> Sequence:
    """Optional list field; absent/null yields an empty list."""
    if key not in node or node[key] is None:
        return []
    return require_seq(node[key], f"{where}.{key}")
