"""
Bitstream generation for Kyttar arrays.

This module provides tools to generate programming bitstreams that configure
the cell array through the input port, exactly as real hardware would be programmed.

Key classes:
- BitstreamGenerator: Main class for creating programming sequences
- Bitstream: Container for the generated programming data
- IntelHexWriter: Writes standard Intel HEX format (*.hex)
- MycWriter: Writes annotated disassembly format (*.myc)

Example:
    from gr_kyttar.bitstream import BitstreamGenerator

    gen = BitstreamGenerator("configs/dev_12x12.yaml")

    # Add routing and cell programs
    gen.add_routing_path((0,0), (5,5))
    gen.add_cell_program((5,5), my_program)

    # Generate and save
    bitstream = gen.generate()
    bitstream.write_hex("output.hex")
    bitstream.write_myc("output.myc")
"""

from .generator import BitstreamGenerator, Bitstream
from .intel_hex import IntelHexWriter, IntelHexReader
from .myc_format import MycWriter

__all__ = [
    'BitstreamGenerator',
    'Bitstream',
    'IntelHexWriter',
    'IntelHexReader',
    'MycWriter',
]
