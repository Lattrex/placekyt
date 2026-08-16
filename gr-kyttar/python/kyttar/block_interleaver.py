# SPDX-License-Identifier: GPL-3.0-or-later
"""
Kyttar Block Interleaver / Deinterleaver GRC block.

Classic rows x cols row-column (matrix) block interleaver: writes each block of
rows*cols symbols into the matrix row by row (arrival order) and reads it out
column by column; ``deinterleave=True`` applies the exact inverse (transpose)
permutation. Strict 1:1 rate with a group delay of exactly rows*cols samples.

GR marker; the real DSP runs on the placeKYT-hosted chip. This block keeps the
exact GR interface (class name, params, ports) so it places/wires identically in
GRC, but does NO in-process placement and streams pure pass-through.
"""

from .dsp_markers import _PassThrough


class block_interleaver(_PassThrough):
    """
    Kyttar Block Interleaver / Deinterleaver (rows x cols matrix, row-column).

    Interleave: write row-wise, read column-wise, per rows*cols block.
    Deinterleave: the inverse (transpose) permutation — one machinery, both
    directions. 1:1 rate, group delay = rows*cols samples (first block emits
    zeros). GR marker; the real DSP runs on the placeKYT-hosted chip.

    Parameters:
        device_id: ID of the kyttar.device to use
        rows: matrix rows (default 2)
        cols: matrix columns (default 2)
        deinterleave: True for the inverse permutation (default False).
            Hardware limit: rows*cols <= 12 (the ping-pong buffer must fit one
            32-word cell); a larger matrix is rejected at placement.
    """

    def __init__(
        self,
        device_id: str = "kyttar_0",
        rows: int = 2,
        cols: int = 2,
        deinterleave: bool = False,
    ):
        name = ("Kyttar Block Deinterleaver" if deinterleave
                else "Kyttar Block Interleaver")
        super().__init__(name=name, n_in=1, n_out=1)
        self._device_id = device_id
        self._rows = int(rows)
        self._cols = int(cols)
        self._deinterleave = bool(deinterleave)
        # Advertise params for GRC<->placeKYT sync detection (see dsp_markers).
        self._advertise_grc_params(
            device_id, "BlockInterleaverBlock",
            {"rows": int(rows), "cols": int(cols),
             "deinterleave": bool(deinterleave)})

    @property
    def rows(self) -> int:
        """Number of rows in the interleaver matrix."""
        return self._rows

    @property
    def cols(self) -> int:
        """Number of columns in the interleaver matrix."""
        return self._cols

    @property
    def deinterleave(self) -> bool:
        """True if this instance applies the inverse (transpose) permutation."""
        return self._deinterleave

    @property
    def block_size(self) -> int:
        """Block length N = rows * cols (= the group delay in samples)."""
        return self._rows * self._cols
