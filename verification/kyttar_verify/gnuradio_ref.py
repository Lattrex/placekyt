# SPDX-License-Identifier: GPL-3.0-or-later
"""Run a GNU Radio flowgraph as the golden reference (predictor) for block
verification.

GNU Radio is invoked in a **separate system-Python subprocess**. This is
deliberate: GNU Radio is typically built against the system NumPy (often 1.x),
while a verification venv may use NumPy 2.x — importing both in one interpreter
crashes. The subprocess boundary is the contract: we pass a stimulus in and a
result list out as JSON, and never import gnuradio into this process.

The caller supplies a small GNU Radio script fragment that:
  * reads ``input_float`` (the Q15 stimulus pre-converted to float), and
  * sets ``output_float`` to the block's float output.

Returns BOTH the float reference and its Q15 quantization, so the comparison
engine can model Q15 saturation against the true float values.
"""

from __future__ import annotations

import json
import os
import subprocess
from dataclasses import dataclass
from typing import Optional

# The interpreter that has GNU Radio. Override via KYTTAR_GR_PYTHON for unusual
# installs (conda, a second venv, etc.).
SYSTEM_PYTHON = os.environ.get("KYTTAR_GR_PYTHON", "/usr/bin/python3")


def q15_to_float(val: int) -> float:
    """uint16 Q15 -> float in [-1.0, ~1.0)."""
    signed = val if val < 0x8000 else val - 0x10000
    return signed / 32768.0


def float_to_q15(val: float) -> int:
    """float -> uint16 Q15 with saturation (clip to [-1.0, +0.999969])."""
    q = int(round(val * 32768.0))
    q = max(-32768, min(32767, q))
    return q & 0xFFFF


@dataclass
class GrResult:
    """Golden-reference output, both float and Q15-quantized."""

    floats: list[float]
    q15: list[int]


def run_gnuradio_ref(
    input_q15: list[int],
    gnuradio_script: str,
    *,
    extra_args: Optional[dict] = None,
    timeout: int = 120,
    system_python: str | None = None,
) -> GrResult:
    """Run ``gnuradio_script`` against ``input_q15`` in a system-Python subprocess.

    Args:
        input_q15: stimulus as uint16 Q15 words. Exposed to the script as both
            ``input_q15`` (ints) and ``input_float`` (floats in [-1, 1)).
        gnuradio_script: a GNU Radio flowgraph fragment that sets ``output_float``.
        extra_args: extra variables injected into the script namespace
            (``repr``-serialized), e.g. ``{"taps": [...]}`.
        timeout: subprocess timeout (seconds).
        system_python: override the GNU Radio interpreter path.

    Returns:
        :class:`GrResult` with the float output and its Q15 quantization.

    Raises:
        RuntimeError: if the subprocess fails or the GR output contains NaN/Inf
            (a NaN/Inf in the reference is a hard failure, never silently
            quantized away).
    """
    py = system_python or SYSTEM_PYTHON
    args_code = ""
    for k, v in (extra_args or {}).items():
        args_code += f"{k} = {v!r}\n"

    full_script = f"""
import json, math, sys

input_q15 = {list(int(x) & 0xFFFF for x in input_q15)!r}
input_float = [(v if v < 0x8000 else v - 0x10000) / 32768.0 for v in input_q15]
{args_code}
{gnuradio_script}

out = [float(v) for v in output_float]
if any(math.isnan(v) or math.isinf(v) for v in out):
    sys.stderr.write("NaN/Inf in GNU Radio output\\n")
    sys.exit(3)

def _q15(v):
    q = int(round(v * 32768.0))
    q = max(-32768, min(32767, q))
    return q & 0xFFFF

print(json.dumps({{"floats": out, "q15": [_q15(v) for v in out]}}))
"""
    proc = subprocess.run(
        [py, "-c", full_script],
        capture_output=True, text=True, timeout=timeout,
    )
    if proc.returncode != 0:
        raise RuntimeError(
            f"GNU Radio reference subprocess failed (rc={proc.returncode}):\n"
            f"{proc.stderr.strip()}")
    data = json.loads(proc.stdout.strip())
    return GrResult(floats=data["floats"], q15=data["q15"])
