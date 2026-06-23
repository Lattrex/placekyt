"""Kyttar DSP block library.

Each block lives in its own module here (``gain.py``, ``costas_loop.py``, …) and
subclasses :class:`KyttarBlock`. This package imports every built-in block and
then discovers any EXTERNAL blocks the user has installed, so adding a block —
built-in or third-party — never means editing a giant shared file.

External block discovery (two mechanisms, both optional):

1. **Entry points.** A pip-installable package may advertise blocks under the
   ``gr_kyttar.blocks`` entry-point group; each entry point is imported and any
   :class:`KyttarBlock` subclasses it defines are registered.

2. **``KYTTAR_BLOCK_PATH``.** A colon-separated list of directories. Every
   ``*.py`` in them is imported and its :class:`KyttarBlock` subclasses are
   registered. Handy for local, un-packaged block libraries.

``all_block_classes()`` returns the merged registry (built-in + external).
placeKYT's catalog builds from this, so external blocks appear automatically.
"""
from __future__ import annotations

import importlib
import importlib.util
import os
import sys
import inspect

from ._base import (
    KyttarBlock,
    BlockInterface,
    float_to_q15,
    q15_to_float,
    assemble_to_words,
    build_block_chain,
    get_block_metrics,
)
from ._modmap import BUILTIN_BLOCKS

# --- import every built-in block module and bind its class here --------------
_registry: dict[str, type] = {}


def _register(cls: type) -> None:
    if inspect.isclass(cls) and issubclass(cls, KyttarBlock) and cls is not KyttarBlock:
        _registry[cls.__name__] = cls
        globals()[cls.__name__] = cls


for _cls_name, _mod_name in BUILTIN_BLOCKS.items():
    _mod = importlib.import_module(f".{_mod_name}", __name__)
    _cls = getattr(_mod, _cls_name, None)
    if _cls is not None:
        _register(_cls)


# --- external block discovery ------------------------------------------------
def _discover_entry_points() -> None:
    try:
        from importlib.metadata import entry_points
    except Exception:  # pragma: no cover - very old Python
        return
    try:
        eps = entry_points()
        group = (eps.select(group="gr_kyttar.blocks")
                 if hasattr(eps, "select") else eps.get("gr_kyttar.blocks", []))
    except Exception:
        return
    for ep in group:
        try:
            obj = ep.load()
        except Exception as e:  # don't let a bad plugin break the catalog
            sys.stderr.write(f"[gr_kyttar] skipping block entry point {ep.name!r}: {e}\n")
            continue
        _register_from_object(obj)


def _register_from_object(obj) -> None:
    """Register KyttarBlock subclasses found on a module or a class."""
    if inspect.isclass(obj):
        _register(obj)
    else:  # a module
        for _, member in inspect.getmembers(obj, inspect.isclass):
            _register(member)


def _discover_path() -> None:
    raw = os.environ.get("KYTTAR_BLOCK_PATH", "")
    for d in filter(None, raw.split(os.pathsep)):
        if not os.path.isdir(d):
            continue
        for fn in sorted(os.listdir(d)):
            if not fn.endswith(".py") or fn.startswith("_"):
                continue
            path = os.path.join(d, fn)
            mod_name = f"_kyttar_ext_{os.path.splitext(fn)[0]}"
            try:
                spec = importlib.util.spec_from_file_location(mod_name, path)
                mod = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(mod)
            except Exception as e:
                sys.stderr.write(f"[gr_kyttar] skipping external block {path!r}: {e}\n")
                continue
            _register_from_object(mod)


_discover_entry_points()
_discover_path()


def all_block_classes() -> dict[str, type]:
    """Every registered block class (built-in + external), by class name."""
    return dict(_registry)


__all__ = [
    "KyttarBlock", "BlockInterface",
    "float_to_q15", "q15_to_float", "assemble_to_words",
    "build_block_chain", "get_block_metrics",
    "all_block_classes",
    *_registry.keys(),
]
