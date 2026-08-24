# SPDX-License-Identifier: GPL-3.0-or-later
"""GRC-binding completeness gate (INV-22) — every DONE block MUST be placeable in GRC.

The product boundary: a Kyttar block is a 1:1 drop-in for a GRC block. A block you
cannot drop into a flowgraph — or one whose params you cannot set from GRC — is NOT
done, however green its DSP test is. This gate makes INV-22 an ENFORCED check instead of
a rule that drifts: it enumerates every ``status: "done"`` block in the manifest and
asserts

  1. a ``gr-kyttar/grc/<id>.block.yml`` exists that placeKYT's OWN importer
     (``engine.grc_import._grc_id_to_type``) resolves to that block's class, and
  2. that ``.block.yml`` exposes EVERY parameter the block class accepts (same names),
     except parameters explicitly declared UNSUPPORTED by the block (a documented
     HW-deviation that raises — those must NOT appear in GRC), and
  3. if the YAML ``make:`` calls a ``kyttar.<shim>``, that ``<shim>`` is importable
     from the ``kyttar`` package (a standalone shim module OR a ``dsp_markers`` class
     re-exported by ``kyttar/__init__``).

A block that fails this is listed with EXACTLY what's missing so the fix is mechanical.
This is the same rigor as the pipeline-saturation coverage gate — no silent gaps.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

_VERIFY = Path(__file__).resolve().parents[1]
_REPO = _VERIFY.parent
_GRC_DIR = _REPO / "gr-kyttar" / "grc"
_SHIM_DIR = _REPO / "gr-kyttar" / "python" / "kyttar"
_MANIFEST = _VERIFY / "manifest.json"

import sys  # noqa: E402
for _p in (str(_REPO / "placekyt"), str(_REPO / "runtime" / "python")):
    if _p not in sys.path:
        sys.path.insert(0, _p)


def _catalog():
    from engine.catalog import BlockCatalog
    return BlockCatalog.from_gr_kyttar()


def _resolver():
    from engine.grc_import import _grc_id_to_type
    return _grc_id_to_type


# Done blocks that are ROUTING INFRASTRUCTURE, not GR DSP drop-ins: they cannot
# appear in a GNU Radio flowgraph at all (the router/hand-placement instantiates
# them), so INV-22 (every done block placeable from GRC) does not apply. The
# manifest must AGREE by declaring grc_block "(none ...)" — a block that maps to
# a real GR block can never be exempted here (asserted below at import).
_INFRASTRUCTURE_BLOCKS = {"CrossoverBlock"}


def _done_blocks() -> list[str]:
    m = json.loads(_MANIFEST.read_text())
    for b in m["blocks"]:
        if (b["kyttar_block"] in _INFRASTRUCTURE_BLOCKS
                and not str(b.get("grc_block", "")).startswith("(none")):
            raise AssertionError(
                f"{b['kyttar_block']} is exempted as infrastructure but its "
                f"manifest grc_block is {b.get('grc_block')!r} — a block with a "
                f"GR counterpart must have a binding (INV-22)")
    return sorted(b["kyttar_block"] for b in m["blocks"]
                  if b.get("status") == "done"
                  and b["kyttar_block"] not in _INFRASTRUCTURE_BLOCKS)


def _aliases_for(name: str, cat) -> set[str]:
    """``name`` plus its manifest-alias twin, so a block the manifest lists by a legacy
    short name (``QuadratureDemod`` for class ``QuadratureDemodBlock``) matches a binding
    that resolves to EITHER name. Uses the catalog's alias tables when present."""
    out = {name}
    fwd = getattr(cat, "_MANIFEST_ALIASES", None)
    rev = getattr(cat, "_ALIAS_TO_TYPE_NAME", None)
    # module-level tables (imported for use by the catalog):
    try:
        from engine import catalog as _c
        fwd = fwd or getattr(_c, "_MANIFEST_ALIASES", {})
        rev = rev or getattr(_c, "_ALIAS_TO_TYPE_NAME", {})
    except Exception:  # noqa: BLE001
        fwd, rev = fwd or {}, rev or {}
    if name in fwd:
        out.add(fwd[name])
    if name in rev:
        out.add(rev[name])
    return out


def _yaml_for_class(cls: str, cat, resolve) -> Path | None:
    """The .block.yml whose id resolves (via placeKYT's importer) to ``cls`` — or to its
    manifest-alias twin (the importer resolves to the concrete class type_name, while the
    manifest may list the block by a legacy short name)."""
    wanted = _aliases_for(cls, cat)
    for f in sorted(_GRC_DIR.glob("*.block.yml")):
        mid = re.search(r"^id:\s*(\S+)", f.read_text(), re.M)
        if mid and resolve(mid.group(1).strip(), cat) in wanted:
            return f
    return None


def _yaml_param_ids(yml: Path) -> set[str]:
    """Ids declared under the top-level ``parameters:`` section (indented ``- id:``),
    excluding the ``inputs:``/``outputs:`` sections which also use ``- id:``. Walk the
    file line-by-line tracking the current top-level section (robust to YAML spacing)."""
    ids: set[str] = set()
    section = None
    for line in yml.read_text().splitlines():
        # A top-level MAPPING KEY starts with a word char at col 0 and ends `:` — NOT a
        # list item (`- id:`), which in this repo's YAML also sits at col 0.
        if re.match(r"^[A-Za-z_]\w*:", line):
            section = line.split(":", 1)[0].strip()
            continue
        if section == "parameters":
            m = re.match(r"\s*-\s*id:\s*(\w+)", line)
            if m:
                ids.add(m.group(1))
    return ids


def _unsupported_params(cls_obj) -> set[str]:
    """Params the block INTENTIONALLY does not expose to GRC (documented HW-deviations
    that raise). A class may declare ``GRC_UNSUPPORTED_PARAMS`` to whitelist them."""
    return set(getattr(cls_obj, "GRC_UNSUPPORTED_PARAMS", ()) or ())


CAT = None
RESOLVE = None
try:
    CAT = _catalog()
    RESOLVE = _resolver()
except Exception:  # noqa: BLE001 — surfaced as a skip if the env can't build the catalog
    CAT = None

_DONE = _done_blocks()


@pytest.mark.skipif(CAT is None, reason="catalog unavailable")
@pytest.mark.parametrize("block", _DONE)
def test_done_block_has_resolvable_grc_binding(block):
    """Every done block resolves to a .block.yml via placeKYT's own GRC importer."""
    yml = _yaml_for_class(block, CAT, RESOLVE)
    assert yml is not None, (
        f"{block} is manifest-done but NO gr-kyttar/grc/*.block.yml resolves to it "
        f"(INV-22: it renders as a red 'Missing Block' in GRC). Add the binding.")


@pytest.mark.skipif(CAT is None, reason="catalog unavailable")
@pytest.mark.parametrize("block", _DONE)
def test_done_block_grc_exposes_every_param(block):
    """The binding exposes every class param (minus documented-unsupported ones)."""
    yml = _yaml_for_class(block, CAT, RESOLVE)
    if yml is None:
        pytest.skip("no binding (covered by the resolvable-binding test)")
    spec = CAT.get(block)
    cls_obj = getattr(spec, "cls", None)
    # `*_range` params are GUI slider hints (e.g. gain_range), not settable GRC params.
    class_params = {p.name for p in (spec.params or ()) if not p.name.endswith("_range")}
    exposed = _yaml_param_ids(yml)
    unsupported = _unsupported_params(cls_obj)
    missing = class_params - exposed - unsupported
    assert not missing, (
        f"{block}: GRC binding {yml.name} is MISSING params {sorted(missing)} "
        f"(INV-22: every class param must be settable from GRC, or declared in "
        f"GRC_UNSUPPORTED_PARAMS). Class params={sorted(class_params)}, "
        f"exposed={sorted(exposed)}.")


def _kyttar_exports() -> set[str]:
    """The block-shim/marker names the kyttar OOT package RE-EXPORTS from __init__ — i.e.
    the names GRC's ``make: kyttar.<shim>(...)`` can actually resolve as a package attribute.

    A bare ``kyttar/<shim>.py`` file is NOT sufficient: ``kyttar.<shim>`` would resolve to
    the *submodule*, not the block class the ``make:`` template calls, so the flowgraph
    crashes with ``AttributeError: module 'gnuradio.kyttar' has no attribute '<shim>'``
    (exactly the diff_encoder bug). The name MUST be brought into the package namespace by a
    ``from .<mod> import <shim>`` in __init__ (or be a dsp_markers class re-exported there).
    Parsed statically so the test needs no GNU Radio."""
    names: set[str] = set()
    init = (_SHIM_DIR / "__init__.py").read_text()
    # `from .<mod> import <a>, <b>` and the MULTI-LINE `from .dsp_markers import (\n a,\n b,\n)`.
    # DOTALL so the parenthesised import spanning many lines is captured whole.
    for m in re.finditer(r"from\s+\.\w+\s+import\s+(\(.*?\)|[^\n(]+)", init, re.S):
        for nm in m.group(1).strip("()").replace("\n", " ").split(","):
            nm = nm.strip()
            if nm and nm.isidentifier():
                names.add(nm)
    return names


_KYTTAR_EXPORTS = None
if CAT is not None:
    try:
        _KYTTAR_EXPORTS = _kyttar_exports()
    except Exception:  # noqa: BLE001
        _KYTTAR_EXPORTS = None


@pytest.mark.skipif(CAT is None or _KYTTAR_EXPORTS is None,
                    reason="catalog / kyttar package unavailable")
@pytest.mark.parametrize("block", _DONE)
def test_done_block_grc_shim_importable(block):
    """If the YAML make: calls kyttar.<shim>, that <shim> must be importable from the
    kyttar package (a shim module OR a dsp_markers class re-exported by __init__)."""
    yml = _yaml_for_class(block, CAT, RESOLVE)
    if yml is None:
        pytest.skip("no binding (covered by the resolvable-binding test)")
    mk = re.search(r"make:\s*kyttar\.([a-zA-Z0-9_]+)\s*\(", yml.read_text())
    if not mk:
        pytest.skip("binding has no kyttar.<shim> make: (device-only / templated)")
    shim = mk.group(1)
    assert shim in _KYTTAR_EXPORTS, (
        f"{block}: GRC binding {yml.name} calls kyttar.{shim}(...) but '{shim}' is not "
        f"importable from the kyttar package (INV-22: neither a shim module nor a "
        f"dsp_markers class re-exported by kyttar/__init__).")


# GR block-name type suffixes -> the (in, out) stream item type each side must be.
# 'b'=byte(uint8), 'f'=float(float32), 'c'=complex(complex64), 's'=short, 'i'=int.
_TYPE_CHAR = {"b": "byte", "f": "float", "c": "complex", "s": "short", "i": "int"}

# DOCUMENTED DEVIATIONS — blocks whose REAL Kyttar I/O legitimately differs from the
# manifest grc_block's name suffix. Each entry pins the shim's CORRECT (in, out) item
# types, verified under real GNU Radio (input_signature().sizeof_stream_item), so the
# gate still asserts a specific truth instead of skipping. Do NOT add an entry to make
# a red test green — only when the deviation is understood and explained here.
_DTYPE_DEVIATIONS = {
    # multiply_cc names the NCO-mix half, but the Kyttar block produces the REAL
    # passband (I*cos - Q*sin), i.e. multiply_cc -> complex_to_real fused: float out.
    "IQUpconvertBlock": ("complex", "float"),
    # costas_loop_cc is always complex-out; the Kyttar block's out dtype is
    # PARAM-DEPENDENT: at the default order=2 (BPSK) it emits only the recovered I
    # tap (float), at order=4 the (I,Q) pair (complex). The gate pins the default.
    "ComplexCostasLoopBlock": ("complex", "float"),
    # chunks_to_symbols_bf packs dibits into bytes; the Kyttar block consumes the
    # on-chip convention — a float 0/1 BIT stream (2 LSB-first bits -> 1 level).
    # The shipped fsk4_modem (BER 0) wires it float; byte-in would break it.
    "FSK4SymbolMapperBlock": ("float", "float"),
    # chunks_to_symbols_bc: same float-bit-stream input convention as FSK4 above.
    "QAM16SymbolMapperBlock": ("float", "complex"),
    # constellation_decoder_cb emits bytes; the Kyttar slicer emits the 4-bit symbol
    # index on the FLOAT rail feeding the shared kyttar.sink (the shipped BER-0
    # qam16_modem wiring).
    "QAM16SlicerBlock": ("complex", "float"),
    # constellation_decoder_cb emits bytes; the Kyttar QPSK slicer emits the 2-bit
    # Gray symbol index on the FLOAT rail feeding the shared kyttar.sink (the
    # shipped qpsk_modem wiring — the same convention as QAM16SlicerBlock).
    "QPSKSlicerBlock": ("complex", "float"),
    # constellation_receiver_cb includes the slice-to-bits; the Kyttar block is ONLY
    # the decision-directed carrier recovery — it outputs the recovered COMPLEX pair
    # and a separate QAM16SlicerBlock does the slicing.
    "QAM16ComplexCostasLoopBlock": ("complex", "complex"),
    # constellation_soft_decoder_cf takes complex; the Kyttar BPSK soft demod takes
    # the REAL recovered-symbol rail (single 'sample' input on chip) and emits LLRs.
    "SoftDemodulatorBlock": ("float", "float"),
    # pack_k_bits_bb is byte-out; the CSS mapper deliberately LIFTS the uint8
    # output cap — a symbol is a RAW 16-bit word (m up to 32768, k up to 15), so
    # the output rail is short (dtype short in the yml and int16 in the marker;
    # documented in the class + binding).
    "ChirpSymbolMapperBlock": ("byte", "short"),
}


def _expected_io_types(grc_block: str):
    """From a GR block id's 2-char type suffix (e.g. _fb = float-in/byte-out), return
    (in_type, out_type) as {'byte','float','complex',...} — or None if no clean suffix."""
    m = re.search(r"[a-z0-9]+_([bfcsi][bfcsi])(?:\s|$|\()", grc_block.strip())
    if not m:
        return None
    a, b = m.group(1)
    return _TYPE_CHAR[a], _TYPE_CHAR[b]


def _shim_declared_types(shim: str):
    """Parse the shim's stream item types from its ``__init__`` call — either the
    _PassThrough form (``super().__init__(..., in_dtype=X, out_dtype=Y)``, defaults
    float32) or a direct ``gr.sync_block.__init__(..., in_sig=[np.uint8], ...)``
    (standalone module OR a dsp_markers class). Returns (in_type, out_type).

    The extraction is BALANCED-PAREN (a name string like "Kyttar Map (map_bb)" must
    not truncate the argument scan — the non-greedy-regex bug that falsely flagged
    map_bb/and_const/xor as float shims)."""
    def _classify(expr: str) -> str:
        e = expr.lower()
        if "uint8" in e or "byte" in e or "np.int8" in e:
            return "byte"
        if "complex" in e:
            return "complex"
        if "int16" in e or "short" in e:
            return "short"
        return "float"  # np.float32 / default

    def _call_args(src: str, opener: str) -> str | None:
        """The full argument text of the first ``opener(``, parens balanced."""
        i = src.find(opener)
        if i < 0:
            return None
        i += len(opener)
        depth, out = 1, []
        for ch in src[i:]:
            if ch == "(":
                depth += 1
            elif ch == ")":
                depth -= 1
                if depth == 0:
                    break
            out.append(ch)
        return "".join(out)

    def _from_init(src: str):
        args = _call_args(src, "super().__init__(")
        if args is not None:
            ind = re.search(r"in_dtype\s*=\s*([\w.]+)", args)
            outd = re.search(r"out_dtype\s*=\s*([\w.]+)", args)
            return (_classify(ind.group(1)) if ind else "float",
                    _classify(outd.group(1)) if outd else "float")
        args = _call_args(src, "gr.sync_block.__init__(")
        if args is not None:
            ins = re.search(r"in_sig\s*=\s*\[([^\]]*)\]", args)
            outs = re.search(r"out_sig\s*=\s*\[([^\]]*)\]", args)
            if ins and outs:
                return _classify(ins.group(1)), _classify(outs.group(1))
        return "float", "float"

    p = _SHIM_DIR / f"{shim}.py"
    if p.exists():
        return _from_init(p.read_text())
    # dsp_markers class named `class <shim>(`
    dm = (_SHIM_DIR / "dsp_markers.py").read_text()
    cm = re.search(rf"\nclass {re.escape(shim)}\b.*?(?=\nclass |\Z)", dm, re.S)
    if cm:
        return _from_init(cm.group(0))
    return None


@pytest.mark.skipif(CAT is None, reason="catalog unavailable")
@pytest.mark.parametrize("block", _DONE)
def test_done_block_shim_dtype_matches_gr(block):
    """The shim's stream item types must match the GR block's type suffix (_bb=byte in/out,
    _fb=float in/byte out, _cc=complex, ...). A _bb block whose shim defaults to float
    (itemsize 4) fails to CONNECT in GRC: 'itemsize mismatch ... using 4 ... using 1'
    (the diff_encoder bug). Only enforced when the manifest grc_block has a clean suffix."""
    m = json.loads(_MANIFEST.read_text())
    entry = next((b for b in m["blocks"] if b["kyttar_block"] == block), None)
    grc_block = (entry or {}).get("grc_block", "") or ""
    # A documented deviation OVERRIDES the suffix: the gate asserts the block's real
    # (GNU-Radio-verified) I/O from _DTYPE_DEVIATIONS instead of skipping it.
    want = _DTYPE_DEVIATIONS.get(block) or _expected_io_types(grc_block)
    if want is None:
        pytest.skip(f"grc_block {grc_block!r} has no clean type suffix")
    yml = _yaml_for_class(block, CAT, RESOLVE)
    if yml is None:
        pytest.skip("no binding (covered by the resolvable-binding test)")
    mk = re.search(r"make:\s*kyttar\.([a-zA-Z0-9_]+)\s*\(", yml.read_text())
    if not mk:
        pytest.skip("binding has no kyttar.<shim> make:")
    got = _shim_declared_types(mk.group(1))
    if got is None:
        pytest.skip(f"shim {mk.group(1)} source not found")
    assert got == want, (
        f"{block}: GR {grc_block} is {want[0]}-in/{want[1]}-out, but shim "
        f"kyttar.{mk.group(1)} declares {got[0]}-in/{got[1]}-out. GRC stream connections "
        f"will fail with an itemsize mismatch. Set in_dtype/out_dtype in the shim's "
        f"super().__init__ (np.uint8=byte, np.complex64=complex, np.float32=float).")


@pytest.mark.skipif(CAT is None, reason="catalog unavailable")
def _shim_init_params(shim: str) -> set[str] | None:
    """The kwarg names ``kyttar.<shim>(...)`` accepts, parsed statically (ast) from
    the package source. Returns None when the class cannot be found, or the full
    set of __init__ arg names; a **kwargs catch-all yields the sentinel {'**'}."""
    import ast

    for py in _SHIM_DIR.glob("*.py"):
        try:
            tree = ast.parse(py.read_text())
        except SyntaxError:
            continue
        for node in ast.walk(tree):
            if not (isinstance(node, ast.ClassDef) and node.name == shim):
                continue
            for fn in node.body:
                if isinstance(fn, ast.FunctionDef) and fn.name == "__init__":
                    if fn.args.kwarg is not None:
                        return {"**"}
                    args = fn.args
                    return {a.arg for a in (args.args + args.kwonlyargs)} - {"self"}
            return None    # class found but no own __init__ (inherited) — unknown
    return None


@pytest.mark.skipif(CAT is None, reason="catalog unavailable")
@pytest.mark.parametrize("block", _DONE)
def test_done_block_yaml_make_kwargs_accepted_by_shim(block):
    """Every kwarg the YAML ``make:`` template passes must be a parameter of the
    shim's __init__ — a stale shim raises TypeError the moment GRC Run
    instantiates the flowgraph (the dc_blocker bug: yml passed length/long_form,
    the marker still took the long-dead 'alpha'). Parsed statically, no GR needed."""
    yml = _yaml_for_class(block, CAT, RESOLVE)
    if yml is None:
        pytest.skip("no binding (covered by the resolvable-binding test)")
    text = yml.read_text()
    mk = re.search(r"make:\s*kyttar\.([a-zA-Z0-9_]+)\s*\(", text)
    if not mk:
        pytest.skip("binding has no kyttar.<shim> make: (device-only / templated)")
    shim = mk.group(1)
    call = _call_args_from(text, mk.end() - 1)
    if call is None:
        pytest.skip("make: call not parseable")
    passed = set(re.findall(r"(?:^|,)\s*([a-zA-Z_][a-zA-Z0-9_]*)\s*=", call))
    accepted = _shim_init_params(shim)
    if accepted is None or accepted == {"**"}:
        pytest.skip(f"shim {shim} signature not statically determinable")
    extra = passed - accepted
    assert not extra, (
        f"{block}: {yml.name} make: passes kwargs {sorted(extra)} that "
        f"kyttar.{shim}.__init__ does not accept ({sorted(accepted)}) — GRC Run "
        f"would raise TypeError. Update the shim or the yml.")


def _call_args_from(text: str, open_paren: int) -> str | None:
    """The balanced-paren argument substring starting at ``text[open_paren]=='('``."""
    depth = 0
    for i in range(open_paren, len(text)):
        if text[i] == "(":
            depth += 1
        elif text[i] == ")":
            depth -= 1
            if depth == 0:
                return text[open_paren + 1:i]
    return None


def test_grc_binding_coverage_summary():
    """A single roll-up so the count is visible even when parametrized cases are many."""
    missing = [b for b in _DONE if _yaml_for_class(b, CAT, RESOLVE) is None]
    assert not missing, (
        f"{len(missing)}/{len(_DONE)} done blocks have NO resolvable GRC binding "
        f"(INV-22): {missing}")
