"""Backward-compatible aggregator for the Kyttar block library.

The blocks themselves now live one-per-file under
``gr_kyttar.placement.blocks``. This module re-exports the base class, the
helpers, and every registered block class (built-in AND any external blocks
discovered via entry points or ``KYTTAR_BLOCK_PATH``) so existing imports such
as ``from gr_kyttar.placement.kyttar_block import GainBlock`` keep working, and
so consumers that scan this module's namespace (e.g. placeKYT's catalog) still
see every block.
"""
from . import blocks as _blocks
from .blocks import (  # noqa: F401  re-exported for back-compat
    KyttarBlock,
    BlockInterface,
    float_to_q15,
    q15_to_float,
    assemble_to_words,
    build_block_chain,
    get_block_metrics,
    all_block_classes,
)

# Bind every registered block class into this module's namespace so that
# ``from .kyttar_block import SomeBlock`` and ``vars(kyttar_block)`` scans both
# resolve, including external blocks.
globals().update(all_block_classes())

__all__ = [
    "KyttarBlock", "BlockInterface",
    "float_to_q15", "q15_to_float", "assemble_to_words",
    "build_block_chain", "get_block_metrics", "all_block_classes",
    *all_block_classes().keys(),
]
