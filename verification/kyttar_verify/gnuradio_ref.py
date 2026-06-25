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


@dataclass
class GrComplexResult:
    """Golden-reference output of a COMPLEX or LLR GNU Radio flowgraph.

    For a complex output (``output_complex``): ``i`` / ``q`` are the real/imag
    channels as floats. For a real LLR output (``output_float``): ``i`` holds the
    LLR stream and ``q`` is empty. ``is_complex`` records which the script set.
    """

    i: list[float]
    q: list[float]
    is_complex: bool


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


def run_gnuradio_ref_complex(
    inputs_iq,
    gnuradio_script: str,
    *,
    extra_args: Optional[dict] = None,
    timeout: int = 120,
    system_python: str | None = None,
) -> GrComplexResult:
    """Run a COMPLEX (or LLR) GNU Radio flowgraph against an I/Q stimulus.

    Follows :func:`run_gnuradio_ref`'s subprocess contract exactly (GR runs in the
    system Python so its NumPy never clashes with the verification venv's). The
    stimulus is exposed to the script as ``input_complex`` (a list of Python
    ``complex``) and as ``input_i`` / ``input_q`` (the float channels). The script
    must set EITHER:

      * ``output_complex`` — a list of complex GR outputs (e.g. ``multiply_cc``,
        ``sig_source_c``, ``fir_filter_ccf``); returned as the ``i``/``q`` channels;
      * ``output_float`` — a list of real GR outputs (e.g. an LLR /
        ``constellation_soft_decoder_cf``); returned in ``i`` with ``q`` empty.

    Args:
        inputs_iq: complex numpy array / list, or an (N,2) [i,q] float array.
        gnuradio_script: a GR flowgraph fragment setting ``output_complex`` or
            ``output_float``.
        extra_args: extra variables injected into the script namespace.
        timeout: subprocess timeout (seconds).
        system_python: override the GR interpreter path.

    Returns:
        :class:`GrComplexResult`.

    Raises:
        RuntimeError: if the subprocess fails, the script set neither output, or
            the output contains NaN/Inf (a hard failure, never silently dropped).
    """
    import numpy as _np  # local: keep module import cheap / GR-free

    arr = _np.asarray(inputs_iq)
    if _np.iscomplexobj(arr):
        pairs = [(float(c.real), float(c.imag)) for c in arr]
    elif arr.ndim == 2 and arr.shape[1] == 2:
        pairs = [(float(i), float(q)) for i, q in arr]
    else:
        raise RuntimeError("inputs_iq must be complex or an (N,2) [i,q] array")

    py = system_python or SYSTEM_PYTHON
    args_code = ""
    for k, v in (extra_args or {}).items():
        args_code += f"{k} = {v!r}\n"

    full_script = f"""
import json, math, sys

_pairs = {pairs!r}
input_i = [p[0] for p in _pairs]
input_q = [p[1] for p in _pairs]
input_complex = [complex(p[0], p[1]) for p in _pairs]
output_complex = None
output_float = None
{args_code}
{gnuradio_script}

if output_complex is not None:
    ci = [float(getattr(v, "real", v)) for v in output_complex]
    cq = [float(getattr(v, "imag", 0.0)) for v in output_complex]
    if any(math.isnan(x) or math.isinf(x) for x in ci + cq):
        sys.stderr.write("NaN/Inf in GNU Radio complex output\\n")
        sys.exit(3)
    print(json.dumps({{"is_complex": True, "i": ci, "q": cq}}))
elif output_float is not None:
    fi = [float(v) for v in output_float]
    if any(math.isnan(x) or math.isinf(x) for x in fi):
        sys.stderr.write("NaN/Inf in GNU Radio float output\\n")
        sys.exit(3)
    print(json.dumps({{"is_complex": False, "i": fi, "q": []}}))
else:
    sys.stderr.write("script set neither output_complex nor output_float\\n")
    sys.exit(4)
"""
    proc = subprocess.run(
        [py, "-c", full_script],
        capture_output=True, text=True, timeout=timeout,
    )
    if proc.returncode != 0:
        raise RuntimeError(
            f"GNU Radio complex reference subprocess failed (rc={proc.returncode}):\n"
            f"{proc.stderr.strip()}")
    data = json.loads(proc.stdout.strip())
    return GrComplexResult(i=data["i"], q=data["q"],
                           is_complex=bool(data["is_complex"]))
