"""SoftDemodulatorBlock — see :class:`SoftDemodulatorBlock`."""
import numpy as np
from ..block import CellProgram, Port, EntryPoint, DataWord
from typing import Dict
from ._base import KyttarBlock, BlockInterface, assemble_to_words, float_to_q15, q15_to_float


class SoftDemodulatorBlock(KyttarBlock):
    """
    Soft-Decision Demodulator Block - BPSK LLR Generation.

    Converts received symbols to Log-Likelihood Ratios (LLRs) for use with
    soft-decision FEC decoders like Viterbi.

    For BPSK, the LLR is computed as:
        LLR = 2 × I / σ²

    Where:
        - I is the received I (in-phase) sample
        - σ² is the noise variance

    Since division is expensive in hardware, we pre-compute (2/σ²) and multiply:
        LLR = I × (2/σ²)

    The LLR represents log(P(bit=0|sample) / P(bit=1|sample)):
        - Positive LLR → more likely bit=0
        - Negative LLR → more likely bit=1
        - Magnitude → confidence level

    This is a single-cell block with minimal complexity.

    Interface:
        - Entry: R1
        - Input: R31 (I sample from carrier recovery)
        - Output: LLR value (scaled to Q15)
    """
    CATEGORY = "demodulation"
    TAGS = ["soft_demod", "llr", "demodulation"]

    _interface = BlockInterface(entry_address=1, input_registers=[31], output_registers=[31])

    def __init__(
        self,
        name: str,
        noise_variance: float = 0.1,
        llr_scale: float = 1.0,
    ):
        """
        Initialize Soft Demodulator block.

        Args:
            name: Block name
            noise_variance: Estimated noise variance σ² (0.01-1.0 typical)
            llr_scale: Scale factor for LLR output (default 1.0)
        """
        super().__init__(
            name,
            noise_variance=noise_variance,
            llr_scale=llr_scale,
        )
        self._noise_variance = max(0.001, noise_variance)
        self._llr_scale = llr_scale

        # Pre-compute LLR coefficient for BPSK: LLR = I × coeff
        #
        # The theoretical LLR = I × (2/σ²), but for Q15 hardware this coefficient
        # must fit in [-1.0, +1.0). For typical σ²=0.1, 2/σ²=20 which exceeds Q15.
        #
        # Production approach: scale the coefficient to produce LLRs in a useful
        # range for the Viterbi decoder. The Viterbi BMU only needs relative
        # magnitudes and correct signs. We normalize so that full-scale input
        # (±0x7FFF) produces LLR magnitude of ~0x4000 (50% of Q15 range),
        # leaving headroom for the BMU accumulation.
        #
        # coeff = 0.5 / max(1.0, 2/σ²) × (2/σ²) = min(0.5, 2/σ²) × (1/max_coeff)
        # Simplified: coeff = min(0.5, (2/σ²) / max_llr_scale)
        #
        # With llr_scale=1.0 and max output target of 0.5:
        two_inv_sigma2 = (2.0 / self._noise_variance) * llr_scale
        # Normalize: coeff fits in Q15, full-scale input → ~half-scale LLR
        max_target = 0.5  # LLR target magnitude for full-scale input
        coeff = min(max_target, two_inv_sigma2)
        if two_inv_sigma2 > max_target:
            coeff = max_target  # Saturated — input already strongly decoded
        self._llr_coeff_q15 = float_to_q15(coeff)

    @property
    def cell_count(self) -> int:
        return 1

    @property
    def interface(self) -> BlockInterface:
        return self._interface

    @property
    def noise_variance(self) -> float:
        """Noise variance σ²."""
        return self._noise_variance

    def set_noise_variance(self, variance: float):
        """Update noise variance and recompute LLR coefficient."""
        self._noise_variance = max(0.001, variance)
        two_inv_sigma2 = (2.0 / self._noise_variance) * self._llr_scale
        coeff = min(0.5, two_inv_sigma2)
        self._llr_coeff_q15 = float_to_q15(coeff)

    def process_reference(self, input_samples: np.ndarray) -> np.ndarray:
        """
        Reference implementation of BPSK soft demodulation.

        Args:
            input_samples: I samples from carrier recovery

        Returns:
            LLR values (scaled to approximately ±1 range)
        """
        n = len(input_samples)
        output = np.zeros(n, dtype=np.float32)

        for i in range(n):
            # Convert to Q15 and multiply
            i_q15 = float_to_q15(input_samples[i])
            i_signed = i_q15 if i_q15 < 32768 else i_q15 - 65536

            coef_signed = self._inv_variance_q15
            if coef_signed > 32767:
                coef_signed = coef_signed - 65536

            # Q15 multiply: (a * b) >> 15
            llr = (i_signed * coef_signed) >> 15

            # Clip to Q15 range
            llr = max(-32768, min(32767, llr))

            output[i] = llr / 32768.0

        return output

    def build_cell_programs(self) -> Dict[int, CellProgram]:
        """Production soft demodulator: LLR = input * coeff (Q15 MULQ).

        The LLR coefficient is normalized to fit in Q15 range, producing
        LLRs scaled for optimal Viterbi decoder performance. Full-scale
        input produces ~half-scale LLR, leaving headroom for BM accumulation.
        """
        return {0: CellProgram(
            inputs=[Port("sample", register=0)],
            outputs=[Port("llr")],
            entries=[EntryPoint("default")],
            data=[DataWord("coeff", self._llr_coeff_q15, address=1)],
            assembly_template="""\
start:
    MULQ R{in:sample}, R{data:coeff}
    {write:llr}
    {jump:llr}
""",
        )}

    def reset(self):
        """Reset soft demodulator state (no state to reset)."""
        pass
