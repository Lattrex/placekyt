# SPDX-License-Identifier: GPL-3.0-or-later
"""Gate: every Kyttar GRC block's ``.block.yml`` port dtypes MUST match the REAL
GR block's ``io_signature`` — and, for the DSP blocks, that signature must be the
one its VERIFIED GNU-Radio equivalent uses.

WHY THIS EXISTS (the hole this closes): a block's per-block verification test
(``test_complex_mixer.py`` etc.) drives the on-chip DUT via
``run_block_dut_complex``, which BYPASSES the GR shim's stream dtype entirely. So
the shim's ``io_signature`` and the ``.block.yml`` port declarations were NEVER
checked against the block's proven GNU-Radio equivalent. That let two lies ship:

  * ``kyttar_complex_mixer``: the shim omitted ``in_dtype/out_dtype`` -> defaulted
    to FLOAT, but its verified equivalent is ``multiply_cc`` (COMPLEX -> COMPLEX).
  * ``kyttar_iq_upconvert``: the ``.block.yml`` declared TWO float rails (xi/xq),
    but the real block + its verified equivalent (``multiply_cc -> complex_to_real``)
    is ONE COMPLEX in -> FLOAT out.

Both surface only when a REAL GNU Radio flowgraph is opened (GRC enforces strict
port-dtype matching): the SSB Weaver .grc showed "Source IO type 'float' does not
match sink IO type 'complex'". This gate makes that failure impossible to ship
again — it is proven to FAIL on a corrupted dtype (INV-4).

Everything here is MECHANICAL: it reads the real ``io_signature`` from the actual
GNU Radio block instance and the dtypes from the .block.yml. No reasoning about
"what GNU Radio does" — the block object is the ground truth.

Run::

    cd verification
    KYTTAR_GR_PYTHON=/usr/bin/python3 QT_QPA_PLATFORM=offscreen \
        .venv/bin/python -m pytest tests/test_grc_block_port_dtypes.py -q
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

_VERIFY = Path(__file__).resolve().parents[1]
_ROOT = _VERIFY.parent
_GRC_DIR = _ROOT / "gr-kyttar" / "grc"

_GR_PYTHON = os.environ.get("KYTTAR_GR_PYTHON", "/usr/bin/python3")
pytestmark = pytest.mark.skipif(
    not os.path.exists(_GR_PYTHON), reason="GNU Radio interpreter not available")

# GR item size (bytes) -> canonical dtype name used in .block.yml.
_SIZE_TO_DTYPE = {4: "float", 8: "complex", 1: "byte", 2: "short"}

# The DSP blocks whose port dtypes are pinned by a VERIFIED GNU-Radio equivalent.
# Each entry is transcribed DIRECTLY from that block's passing verification test
# (the cited file is the authority; this is not a fresh judgement):
#   (in_dtypes, out_dtypes)  -- one entry per stream port, in order.
_VERIFIED_EQUIV = {
    # multiply_cc(signal, sig_source_c): complex -> complex   [test_complex_mixer.py]
    "kyttar_complex_mixer": (["complex"], ["complex"]),
    # fir_filter_ccf: complex -> complex                       [test_complex_fir.py / INV-18]
    "kyttar_complex_low_pass_filter": (["complex"], ["complex"]),
    # I/Q upconvert exposes TWO OPTIONAL REAL rails (xi@R0, xq@R1) -> real passband:
    # out = xi*cos - xq*sin. This is the HARDWARE contract (two scalar reals) AND what
    # lets a real GNU Radio flowgraph wire a float signal straight into the mixer (a
    # BPSK/AM TX drives xi alone). A single-complex declaration made every float-fed
    # demo (modem/AM/SSB) fail to LOAD in GRC (float != complex). The verified GR
    # equivalent multiply_cc(bb, sig_source_c) -> complex_to_real is a documentation
    # note, not the port contract.                            [test_iq_upconvert.py]
    "kyttar_iq_upconvert": (["float", "float"], ["float"]),
    # gain: float -> float                                     [test_gain.py]
    "kyttar_gain": (["float"], ["float"]),
}


def _param_defaults(text: str) -> dict:
    """Map each ``- id: NAME`` parameter to its ``default:`` value (used to resolve a
    ``${ io_type }`` port dtype to the concrete default a fresh place selects)."""
    import re
    defaults, cur = {}, None
    for raw in text.splitlines():
        m = re.match(r"^-\s*id:\s*(\w+)", raw.strip())
        if m:
            cur = m.group(1); continue
        d = re.match(r"^default:\s*(.+)$", raw.strip())
        if d and cur is not None:
            defaults[cur] = d.group(1).strip().strip("'\"")
    return defaults


def _parse_yaml_ports(yml_path: Path):
    """Return (in_dtypes, out_dtypes) declared in a .block.yml, as canonical dtype
    strings in port order. A ``${ param }`` dtype is resolved to that param's
    ``default:`` (the dtype a fresh drop of the block selects). Reads only the
    top-level ``inputs:``/``outputs:`` port dtype fields — no full YAML dependency."""
    import re
    text = yml_path.read_text()
    defaults = _param_defaults(text)
    ins, outs = [], []
    section = None
    for raw in text.splitlines():
        line = raw.rstrip()
        stripped = line.strip()
        if re.match(r"^inputs:\s*$", line):
            section = "in"; continue
        if re.match(r"^outputs:\s*$", line):
            section = "out"; continue
        # a new top-level key ends the port section
        if line and not line[0].isspace() and not stripped.startswith("-"):
            if re.match(r"^[a-z_]+:", line):
                section = None
        if section is None:
            continue
        m = re.search(r"dtype:\s*([A-Za-z_$][\w${}. ]*)", stripped)
        if m:
            dt = m.group(1).strip()
            ref = re.match(r"\$\{\s*(\w+)\s*\}", dt)   # ${ io_type } -> its default
            if ref:
                dt = defaults.get(ref.group(1), dt)
            (ins if section == "in" else outs).append(dt)
    return ins, outs


def _real_io_sizes(make_expr: str):
    """Instantiate the REAL GR block (its ``make:`` factory, default params) in the
    GNU-Radio interpreter and return (in_item_sizes, out_item_sizes) in bytes.

    Runs in the GR subprocess so its NumPy/GNU-Radio never clash with the
    verification env — the same isolation the rest of the harness uses."""
    prog = f"""
import sys
from gnuradio import kyttar  # noqa: F401
try:
    blk = {make_expr}
except Exception as e:
    print("ERR " + type(e).__name__ + ": " + str(e)); sys.exit(0)
isig, osig = blk.input_signature(), blk.output_signature()
ins = [isig.sizeof_stream_item(i) for i in range(isig.max_streams())] if isig.max_streams() > 0 else []
outs = [osig.sizeof_stream_item(i) for i in range(osig.max_streams())] if osig.max_streams() > 0 else []
# Emit each list SPACE-FREE so the parent can split on a single space even for a
# multi-port block (repr([4, 4]) contains a space that would break split()).
print("OK " + repr(ins).replace(" ", "") + " " + repr(outs).replace(" ", ""))
"""
    out = subprocess.run([_GR_PYTHON, "-c", prog], capture_output=True, text=True,
                         timeout=60)
    line = (out.stdout or "").strip().splitlines()
    tail = line[-1] if line else ""
    if tail.startswith("ERR"):
        return None, tail
    if not tail.startswith("OK"):
        return None, (out.stderr or out.stdout or "no output").strip()[:300]
    _, ins_r, outs_r = tail.split(" ", 2)
    return (eval(ins_r), eval(outs_r)), None  # noqa: S307 — our own repr of int lists


# The DSP blocks under this gate, with a default-param make: expression whose ports
# are stable (no param changes the stream dtype). Non-DSP blocks (device, batch
# clients, sources/sinks whose dtype is param-driven) are covered separately.
_DSP_MAKES = {
    "kyttar_complex_mixer": "kyttar.complex_mixer()",
    "kyttar_complex_low_pass_filter": "kyttar.complex_low_pass_filter()",
    "kyttar_iq_upconvert": "kyttar.iq_upconvert()",
    "kyttar_gain": "kyttar.gain()",
    "kyttar_complex_rrc_matched_filter": "kyttar.complex_rrc_matched_filter()",
    "kyttar_rrc_pulse_shaper": "kyttar.rrc_pulse_shaper()",
    "kyttar_complex_to_float": "kyttar.complex_to_float()",
    "kyttar_upsampler": "kyttar.upsampler()",
}


@pytest.mark.parametrize("block_id", sorted(_DSP_MAKES))
def test_yaml_ports_match_real_io_signature(block_id):
    """(1) The .block.yml port dtypes MUST equal the real GR block's io_signature."""
    yml = _GRC_DIR / f"{block_id}.block.yml"
    assert yml.exists(), f"missing {yml}"
    y_in, y_out = _parse_yaml_ports(yml)
    sizes, err = _real_io_sizes(_DSP_MAKES[block_id])
    assert sizes is not None, f"could not instantiate {block_id}: {err}"
    real_in = [_SIZE_TO_DTYPE.get(s, f"?{s}") for s in sizes[0]]
    real_out = [_SIZE_TO_DTYPE.get(s, f"?{s}") for s in sizes[1]]
    print(f"\n{block_id}: yaml in={y_in} out={y_out} | real in={real_in} out={real_out}")
    assert y_in == real_in, (
        f"{block_id}.block.yml inputs {y_in} != real io_signature {real_in}")
    assert y_out == real_out, (
        f"{block_id}.block.yml outputs {y_out} != real io_signature {real_out}")


@pytest.mark.parametrize("block_id", sorted(_VERIFIED_EQUIV))
def test_real_io_signature_matches_verified_equivalent(block_id):
    """(2) For a block with a VERIFIED GNU-Radio equivalent, the real io_signature
    MUST be the equivalent's dtype (transcribed from the block's verification test).
    This catches a shim that omitted in_dtype/out_dtype (complex_mixer defaulted to
    float though its verified equivalent multiply_cc is complex)."""
    want_in, want_out = _VERIFIED_EQUIV[block_id]
    sizes, err = _real_io_sizes(_DSP_MAKES[block_id])
    assert sizes is not None, f"could not instantiate {block_id}: {err}"
    real_in = [_SIZE_TO_DTYPE.get(s, f"?{s}") for s in sizes[0]]
    real_out = [_SIZE_TO_DTYPE.get(s, f"?{s}") for s in sizes[1]]
    print(f"\n{block_id}: verified-equiv in={want_in} out={want_out} | "
          f"real in={real_in} out={real_out}")
    assert real_in == want_in, (
        f"{block_id} shim inputs {real_in} != verified equivalent {want_in}")
    assert real_out == want_out, (
        f"{block_id} shim outputs {real_out} != verified equivalent {want_out}")
