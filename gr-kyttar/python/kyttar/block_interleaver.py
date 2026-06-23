"""
Kyttar Block Interleaver/Deinterleaver GRC Block.

Block interleaver for burst error protection. Writes data row-wise
into a matrix, reads column-wise (or vice versa for deinterleaving).

GR marker; the real DSP runs on the placeKYT-hosted chip. This block keeps the
exact GR interface (class name, params, ports) so it places/wires identically in
GRC, but does NO in-process placement and streams pure pass-through.
"""

from .dsp_markers import _PassThrough


class block_interleaver(_PassThrough):
    """
    Block Interleaver/Deinterleaver Block.

    Interleaver: writes bits row-wise, reads column-wise.
    Deinterleaver: writes bits column-wise, reads row-wise.
    Runs on the chip. GR marker; the real DSP runs on the placeKYT-hosted chip.

    Input: Data bits (0/1 as float)
    Output: Interleaved/deinterleaved bits (0/1 as float)

    Parameters:
        device_id: Device ID to register with
        rows: Number of rows in interleaver matrix (default 4)
        cols: Number of columns in interleaver matrix (default 16)
        is_deinterleaver: True for deinterleaving, False for interleaving
    """

    def __init__(
        self,
        device_id: str = "kyttar_0",
        rows: int = 4,
        cols: int = 16,
        is_deinterleaver: bool = False,
    ):
        name = "Kyttar Block Deinterleaver" if is_deinterleaver else "Kyttar Block Interleaver"
        super().__init__(name=name, n_in=1, n_out=1)

        self._device_id = device_id
        self._rows = rows
        self._cols = cols
        self._is_deinterleaver = is_deinterleaver
        self._block_size = rows * cols

        # Set output multiple to block size (preserves the GR scheduling interface)
        self.set_output_multiple(self._block_size)

    @property
    def rows(self) -> int:
        """Number of rows in interleaver matrix."""
        return self._rows

    @property
    def cols(self) -> int:
        """Number of columns in interleaver matrix."""
        return self._cols

    @property
    def block_size(self) -> int:
        """Block size (rows * cols)."""
        return self._block_size

    @property
    def is_deinterleaver(self) -> bool:
        """True if deinterleaver, False if interleaver."""
        return self._is_deinterleaver

    @property
    def cell_count(self) -> int:
        """Number of cells used."""
        return self._rows  # One cell per row
