"""
gr_kyttar - block placement & bitstream generation for the Kyttar cell array.

This package provides the placement engine and bitstream generator that turn a
set of DSP block definitions into a programmed Kyttar chip. It is the canonical
block-build path used by placeKYT.

Subpackages:
- ``gr_kyttar.placement`` - block definitions, placer, router, cell map, resolver
- ``gr_kyttar.bitstream`` - bitstream generation from a placed/routed cell map

Example:
    from gr_kyttar.placement import ArrayConfig, Placer, Router
    from gr_kyttar.bitstream import BitstreamGenerator
"""

from . import placement
from . import bitstream

__version__ = "0.8.0"
__all__ = ["placement", "bitstream"]
