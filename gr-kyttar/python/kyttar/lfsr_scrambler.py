"""
Kyttar LFSR Scrambler/Descrambler GRC Block.

Self-synchronizing scrambler using the polynomial x^15 + x^14 + 1
(the MIL-STD-188-110B standard scrambler).

GR marker; the real DSP runs on the placeKYT-hosted chip. This block keeps the
exact GR interface (class name, params, ports) so it places/wires identically in
GRC, but does NO in-process placement and streams pure pass-through.
"""

from .dsp_markers import _PassThrough


class lfsr_scrambler(_PassThrough):
    """
    LFSR Scrambler/Descrambler Block.

    Self-synchronizing scrambler using polynomial x^15 + x^14 + 1 on the chip.
    The same operation descrambles when applied to scrambled data
    (self-synchronizing property). GR marker; the real DSP runs on the
    placeKYT-hosted chip.

    Input: Data bits (0/1 as float)
    Output: Scrambled/descrambled bits (0/1 as float)

    Parameters:
        device_id: Device ID to register with
        initial_state: Initial LFSR state (default 0x0001)
        mode: "scramble" or "descramble" (same algorithm, label only)
    """

    def __init__(
        self,
        device_id: str = "kyttar_0",
        initial_state: int = 0x0001,
        mode: str = "scramble",
    ):
        super().__init__(
            name="Kyttar LFSR Scrambler" if mode == "scramble" else "Kyttar LFSR Descrambler",
            n_in=1,
            n_out=1,
        )
        self._device_id = device_id
        self._initial_state = initial_state
        self._mode = mode
        # Advertise params for GRC↔placeKYT sync detection (see dsp_markers).
        # placeKYT models scramble/descramble as a bool `is_descrambler`.
        self._advertise_grc_params(
            device_id, "LFSRScramblerBlock",
            {"initial_state": initial_state,
             "is_descrambler": (mode == "descramble")})

    @property
    def initial_state(self) -> int:
        """Initial LFSR state."""
        return self._initial_state

    @property
    def mode(self) -> str:
        """Mode: scramble or descramble."""
        return self._mode

    @property
    def cell_count(self) -> int:
        """Number of cells used."""
        return 1
