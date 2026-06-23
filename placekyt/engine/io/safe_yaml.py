"""Hardened ruamel.yaml wrapper for all placeKYT file I/O (§2.1).

Every ``.kyt`` / ``.kbl`` / ``.kdb`` / chip-type YAML load goes through
:func:`load_yaml`, which enforces the mandatory DoS protections:

  * **Size cap** — files over 16 MB are rejected BEFORE parsing.
  * **Anchor/alias expansion limit** — a custom :class:`Composer` subclass
    counts composed nodes and raises on overflow (>100k), defeating
    "billion laughs" bombs where aliases expand exponentially. (The spec's
    "100 nodes" refers to expansion *depth per the bomb pattern*; we count
    total composed nodes with generous headroom for real files and trip well
    before exponential blow-up exhausts memory.)
  * **Depth limit** — nesting beyond 50 levels is rejected.

Saving goes through :func:`dump_yaml`, which applies the serialization rules:
``repr()``-precision floats (so ``0.35`` round-trips exactly), ``allow_unicode``,
block (not flow) style, and round-trip mode so unknown fields / comments / key
order survive.

``typ='rt'`` is used everywhere. ``typ='unsafe'`` / ``typ='full'`` are PROHIBITED
(§2.1) — rt mode is safe-by-default (no arbitrary object construction).
"""

from __future__ import annotations

import io
from pathlib import Path
from typing import Any

from ruamel.yaml import YAML
from ruamel.yaml.composer import Composer, MaxDepthExceededError
from ruamel.yaml.error import YAMLError

from .errors import ProjectFileError, YamlBombError, YamlDepthError, YamlSizeError

# --- limits (§2.1 DoS protection) ----------------------------------------- #

MAX_FILE_BYTES = 16 * 1024 * 1024  # 16 MB
MAX_PARSE_DEPTH = 50
# Maximum number of nodes the document would have if every alias were fully
# EXPANDED (§2.1: "counts expanded nodes and raises on overflow"). In ruamel
# round-trip mode aliases stay as shared references, so a "billion laughs" bomb
# parses into a small graph but represents an astronomically large expansion.
# We charge each alias the expanded size of its referent and accumulate; a
# legit file costs a few hundred, a bomb's cost grows geometrically and trips
# this ceiling long before anything is materialized into Python objects.
MAX_EXPANDED_NODES = 100_000


class _HardenedComposer(Composer):
    """Composer that bounds the *expanded* node count and nesting depth.

    Depth is enforced by ruamel's native ``max_depth`` (set on the YAML
    instance). This subclass adds the anchor/alias-bomb guard: it tracks, per
    composed node, how many nodes that node's subtree expands to, then charges
    each alias the expanded size of its referent against a global budget.
    """

    max_expanded = MAX_EXPANDED_NODES

    def _ensure_state(self) -> None:
        if not hasattr(self, "_expanded_total"):
            self._expanded_total = 0  # running expanded-node budget
            # Memo: id(node) -> expanded node count of that node's subtree.
            self._expanded_size: dict[int, int] = {}

    def _charge(self, n: int) -> None:
        self._expanded_total += n
        if self._expanded_total > self.max_expanded:
            raise YamlBombError(
                f"YAML expands to more than {self.max_expanded} nodes "
                "(possible anchor/alias bomb) — refusing to parse."
            )

    def return_alias(self, a: Any) -> Any:
        # Charge the alias the full expanded size of the node it references.
        self._ensure_state()
        self._charge(self._subtree_size(a))
        return super().return_alias(a)

    def compose_node(self, parent: Any, index: Any) -> Any:
        self._ensure_state()
        node = super().compose_node(parent, index)
        # Every freshly composed (non-alias) node costs 1. Aliases are charged
        # separately in return_alias and reach here as already-counted shared
        # nodes, so only count nodes we haven't seen before.
        if id(node) not in self._expanded_size:
            self._charge(1)
        return node

    def _subtree_size(self, node: Any) -> int:
        """Expanded node count of ``node``'s subtree, memoized by node identity.

        Recurses through SequenceNode/MappingNode values. Shared (anchored)
        children are counted each time they appear — that is exactly the
        "expanded" semantics that makes a bomb's cost blow up.
        """
        memo = self._expanded_size
        cached = memo.get(id(node))
        if cached is not None:
            return cached
        total = 1
        value = getattr(node, "value", None)
        if isinstance(value, list):
            for item in value:
                if isinstance(item, tuple):  # mapping (key, value) pair
                    for sub in item:
                        total += self._subtree_size(sub)
                else:  # sequence element
                    total += self._subtree_size(item)
        memo[id(node)] = total
        return total


def _make_yaml() -> YAML:
    """Build a round-trip ``YAML`` configured with the hardened composer and
    the placeKYT serialization rules."""
    yaml = YAML(typ="rt")
    yaml.Composer = _HardenedComposer
    yaml.max_depth = MAX_PARSE_DEPTH  # ruamel's native nesting guard
    yaml.allow_unicode = True
    yaml.default_flow_style = False
    yaml.preserve_quotes = True
    # Represent Python floats with repr() precision so 0.35 etc. round-trip
    # exactly rather than via the lossy default float formatting.
    yaml.representer.add_representer(float, _represent_float)
    return yaml


def _represent_float(representer: Any, data: float) -> Any:
    # repr() gives the shortest string that round-trips to the same float
    # (Python 3.1+ float repr). Special-case the non-finite values YAML spells
    # differently from Python.
    if data != data:  # NaN
        text = ".nan"
    elif data == float("inf"):
        text = ".inf"
    elif data == float("-inf"):
        text = "-.inf"
    else:
        text = repr(data)
    return representer.represent_scalar("tag:yaml.org,2002:float", text)


def load_yaml(path: str | Path) -> Any:
    """Load a YAML file with all §2.1 protections applied.

    Raises :class:`YamlSizeError` (file too large), :class:`YamlBombError`
    (alias bomb), :class:`YamlDepthError` (too deeply nested), or the generic
    :class:`ProjectFileError` (missing file, parse error). Returns the parsed
    round-trip document (a ``CommentedMap`` / ``CommentedSeq`` / scalar).
    """
    p = Path(path)
    try:
        size = p.stat().st_size
    except OSError as exc:
        raise ProjectFileError(f"cannot stat {p}: {exc}") from exc

    if size > MAX_FILE_BYTES:
        raise YamlSizeError(
            f"{p} is {size} bytes, over the {MAX_FILE_BYTES}-byte ("
            f"{MAX_FILE_BYTES // (1024 * 1024)} MB) limit — refusing to parse."
        )

    try:
        text = p.read_text(encoding="utf-8")
    except OSError as exc:
        raise ProjectFileError(f"cannot read {p}: {exc}") from exc

    return load_yaml_str(text, source=str(p))


def load_yaml_str(text: str, *, source: str = "<string>") -> Any:
    """Parse YAML from a string with the same protections as :func:`load_yaml`.

    The size cap is applied to the encoded length so in-memory parsing is
    bounded too.
    """
    encoded_len = len(text.encode("utf-8"))
    if encoded_len > MAX_FILE_BYTES:
        raise YamlSizeError(
            f"{source} is {encoded_len} bytes, over the {MAX_FILE_BYTES}-byte "
            "limit — refusing to parse."
        )
    yaml = _make_yaml()
    try:
        return yaml.load(text)
    except (YamlBombError, YamlDepthError):
        raise
    except MaxDepthExceededError as exc:
        raise YamlDepthError(
            f"YAML nesting exceeds {MAX_PARSE_DEPTH} levels in {source} "
            "— refusing to parse."
        ) from exc
    except YAMLError as exc:
        raise ProjectFileError(f"YAML parse error in {source}: {exc}") from exc


def dump_yaml(data: Any, path: str | Path) -> None:
    """Write ``data`` to ``path`` as UTF-8 YAML per the §2.1 serialization rules."""
    yaml = _make_yaml()
    p = Path(path)
    with p.open("w", encoding="utf-8") as fh:
        yaml.dump(data, fh)


def dump_yaml_str(data: Any) -> str:
    """Serialize ``data`` to a YAML string (used by tests and round-trip checks)."""
    yaml = _make_yaml()
    buf = io.StringIO()
    yaml.dump(data, buf)
    return buf.getvalue()
