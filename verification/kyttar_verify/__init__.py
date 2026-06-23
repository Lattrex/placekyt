# SPDX-License-Identifier: GPL-3.0-or-later
"""kyttar_verify — block verification framework for placeKYT.

Verifies that a Kyttar DSP block is a drop-in equivalent of its GNU Radio
Companion counterpart, by running the same stimulus through both and comparing
the outputs within a quantization-aware tolerance.

Two reference levels:
  * the GNU Radio block (float64) is the golden predictor;
  * the Kyttar block is built and run on simKYT (Q15 fixed-point) as the DUT.

Public entry points:
  * :func:`run_block_dut` — build a single block between x16_in/x16_out and run
    a stimulus through it on simKYT.
  * :func:`run_gnuradio_ref` — run a GNU Radio flowgraph as the golden predictor.
  * :func:`compare_against_grc` — the comparison engine (alignment + tolerance +
    per-class metrics).
"""

from .dut_runner import run_block_dut, DUTResult
from .gnuradio_ref import run_gnuradio_ref, GrResult, q15_to_float, float_to_q15
from .compare import compare_against_grc, write_report, CompareResult, Metric

__all__ = [
    "run_block_dut",
    "DUTResult",
    "run_gnuradio_ref",
    "GrResult",
    "q15_to_float",
    "float_to_q15",
    "compare_against_grc",
    "write_report",
    "CompareResult",
    "Metric",
]
