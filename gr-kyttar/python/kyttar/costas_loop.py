"""
Kyttar Costas Loop Carrier Recovery GRC Block.

Implements carrier frequency and phase recovery using a Costas loop
for coherent demodulation of BPSK (and QPSK) signals.

GR marker; the real DSP runs on the placeKYT-hosted chip. This block keeps the
exact GR interface (class name, params, ports) so it places/wires identically in
GRC, but does NO in-process placement and streams pure pass-through.
"""

from .dsp_markers import _PassThrough


class costas_loop(_PassThrough):
    """
    Costas Loop Carrier Recovery Block.

    Performs carrier frequency and phase recovery for coherent demodulation.
    GR marker; the real DSP runs on the placeKYT-hosted chip.

    Parameters:
        device_id: Device ID to register with
        freq_word: Initial NCO frequency word (0-65535)
        loop_bw: Loop bandwidth (0.001-0.1 typical)
        damping: Loop damping factor (0.707 = critically damped)
        lpf_alpha: LPF smoothing factor (0-1)
    """

    def __init__(
        self,
        device_id: str = "kyttar_0",
        freq_word: int = 0,
        loop_bw: float = 0.02,
        damping: float = 0.707,
        lpf_alpha: float = 0.2,
    ):
        super().__init__(name="Kyttar Costas Loop", n_in=1, n_out=1)
        self._device_id = device_id
        self._freq_word = freq_word
        self._loop_bw = loop_bw
        self._damping = damping
        self._lpf_alpha = lpf_alpha

    @property
    def freq_word(self) -> int:
        """NCO frequency word."""
        return self._freq_word

    @freq_word.setter
    def freq_word(self, value: int):
        """Set NCO frequency word."""
        self._freq_word = value & 0xFFFF

    @property
    def loop_bw(self) -> float:
        """Loop bandwidth."""
        return self._loop_bw

    @property
    def damping(self) -> float:
        """Loop damping factor."""
        return self._damping
