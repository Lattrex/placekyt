"""
Kyttar Soft Demodulator GRC Block.

Converts received BPSK symbols to Log-Likelihood Ratios (LLRs) for use
with soft-decision FEC decoders like Viterbi.

GR marker; the real DSP runs on the placeKYT-hosted chip. This block keeps the
exact GR interface (class name, params, ports) so it places/wires identically in
GRC, but does NO in-process placement and streams pure pass-through.
"""

from .dsp_markers import _PassThrough


class soft_demodulator(_PassThrough):
    """
    Soft-Decision Demodulator Block.

    Generates Log-Likelihood Ratios (LLRs) for BPSK symbols on the chip.
    GR marker; the real DSP runs on the placeKYT-hosted chip.

    Parameters:
        device_id: Device ID to register with
        noise_variance: Estimated noise variance σ² (0.01-1.0 typical)
        llr_scale: Scale factor for LLR output
    """

    def __init__(
        self,
        device_id: str = "kyttar_0",
        noise_variance: float = 0.1,
        llr_scale: float = 1.0,
    ):
        super().__init__(name="Kyttar Soft Demodulator", n_in=1, n_out=1)
        self._device_id = device_id
        self._noise_variance = noise_variance
        self._llr_scale = llr_scale
        # Advertise params for GRC↔placeKYT sync detection (see dsp_markers).
        self._advertise_grc_params(
            device_id, "SoftDemodulatorBlock",
            {"noise_variance": noise_variance, "llr_scale": llr_scale})

    @property
    def noise_variance(self) -> float:
        """Noise variance σ²."""
        return self._noise_variance

    def set_noise_variance(self, variance: float):
        """Update noise variance."""
        self._noise_variance = variance

    @property
    def llr_scale(self) -> float:
        """LLR scale factor."""
        return self._llr_scale
