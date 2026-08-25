# SPDX-License-Identifier: GPL-3.0-or-later
"""EVERY shipped example .grc must be a VALID GNU Radio flowgraph (AGENTS.md
honesty: 'done' means the user can OPEN it and it works).

The user-hit failure class this gates (2026-08-10): flowgraphs that imported
fine into placeKYT (the importer reads only what it needs) but ERRORED the
moment they were opened in GRC — an undefined ``samp_rate`` variable in both
transceiver scopes, NCO connections GRC cannot realize (the marker yml
declared no input), a nonexistent stock id (``blocks_float_to_uchar``; the
real GRC id is ``blocks_float_uchar``), and byte streams wired through ymls
declaring float.

Checks per .grc, mirroring what GRC itself validates:

  * every block id resolves to a block yml — looked up by the yml's ``id:``
    FIELD across the repo gr-kyttar/grc tree and the installed GNU Radio
    tree (file names are not authoritative: variable_qtgui_range lives in
    qtgui_range.block.yml);
  * every EVALUATED parameter (yml dtype int/float/real/complex/raw/expr —
    the dtypes GRC runs through eval) evaluates in the flowgraph's variable
    namespace + the involved blocks' template imports (missing third-party
    modules are mocked — the gate checks NAME RESOLUTION, not module
    presence);
  * every connection endpoint exists and its port INDEX is within the yml's
    declared ports (multiplicity expressions evaluated; unresolvable →
    tolerated);
  * where both endpoint dtypes are CONCRETE (not ${templated}), they match.

Plus a grcc smoke compile — skipped with a NAMED reason when the installed
kyttar ymls differ from the repo's (grcc reads the INSTALLED tree, which
shadows GRC_BLOCKS_PATH; re-run gr-kyttar/install.sh to arm it).
"""
from __future__ import annotations

import math
import os
import re
import shutil
import subprocess
import tempfile
from pathlib import Path
from unittest.mock import MagicMock

import pytest
import yaml

_ROOT = Path(__file__).resolve().parents[2]
# The GNU-Radio interpreter (a separate process — its NumPy must never clash
# with the venv's); the same env knob the rest of the harness uses.
_GR_PYTHON = os.environ.get("KYTTAR_GR_PYTHON", "/usr/bin/python3")
_KYTTAR_YMLS = _ROOT / "gr-kyttar" / "grc"
_STOCK_DIRS = [Path("/usr/share/gnuradio/grc/blocks"),
               Path("/usr/local/share/gnuradio/grc/blocks")]

EXAMPLE_GRCS = sorted((_ROOT / "examples").glob("*/*.grc"))
assert EXAMPLE_GRCS, "no example .grc files found"

# GRC evaluates these param dtypes through eval() in the flowgraph namespace.
_EVAL_DTYPES = {"int", "float", "real", "complex", "raw", "expr",
                "int_vector", "real_vector", "float_vector", "complex_vector"}

# Known open findings in PRE-EXISTING examples, tracked explicitly (an xfail
# is a visible debt, not a silent pass). Remove entries as they are fixed.
# (coherent_bpsk_rx's hand-wired dual mf rails were restructured to single
# complex wires on 2026-08-10 — the importer synthesizes the Q rail.)
_KNOWN_OPEN: dict[str, str] = {}


def _build_index() -> dict:
    """{block id: parsed yml} over the repo kyttar tree + installed stock
    trees, indexed by the yml's ``id:`` field (the authoritative key)."""
    idx: dict = {}
    dirs = [d for d in _STOCK_DIRS if d.is_dir()] + [_KYTTAR_YMLS]
    for d in dirs:                      # kyttar last → repo wins
        for f in d.glob("*.block.yml"):
            try:
                data = yaml.safe_load(f.read_text())
            except Exception:  # noqa: BLE001
                continue
            bid = (data or {}).get("id")
            if bid:
                idx[str(bid)] = data
    return idx


_INDEX = _build_index()


class _MockImporter:
    """exec() helper: run import statements, substituting a MagicMock for any
    module that is not importable here — the lint checks NAMES, not modules."""

    @staticmethod
    def run(code: str, ns: dict) -> None:
        for line in (code or "").splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            try:
                exec(line, ns)  # noqa: S102
            except ImportError:
                m = re.match(
                    r"from\s+([\w.]+)\s+import\s+(.+)|import\s+([\w.]+)"
                    r"(?:\s+as\s+(\w+))?", line)
                if not m:
                    continue
                if m.group(1):          # from X import a, b as c
                    for part in m.group(2).split(","):
                        bits = part.strip().split(" as ")
                        name = (bits[1] if len(bits) > 1 else bits[0]).strip()
                        if name.isidentifier():
                            ns[name] = MagicMock(name=name)
                else:                   # import X [as y]
                    name = m.group(4) or m.group(3).split(".")[0]
                    ns[name] = MagicMock(name=name)
            except Exception:  # noqa: BLE001
                pass


def _ports(yml, direction: str, params: dict):
    """The yml's declared STREAM ports for ``direction``, multiplicity
    expanded. Returns None (= tolerate, skip index checks) when a
    multiplicity expression cannot be resolved."""
    out = []
    pns = {k: str(v).strip("'\"") for k, v in (params or {}).items()}
    for k, v in list(pns.items()):
        try:
            pns[k] = eval(v, {"__builtins__": {}})  # noqa: S307
        except Exception:  # noqa: BLE001
            pass
    for p in (yml or {}).get(direction, []) or []:
        if str(p.get("domain", "stream")) == "message":
            continue
        mult = p.get("multiplicity", 1)
        if isinstance(mult, str):
            expr = mult.strip()
            if expr.startswith("${"):
                expr = expr[2:-1]
            try:
                mult = int(eval(expr, {"__builtins__": {}}, dict(pns)))  # noqa: S307
            except Exception:  # noqa: BLE001
                return None
        out.extend([p] * max(0, int(mult)))
    return out


def _lint(grc_path: Path) -> list[str]:
    findings: list[str] = []
    doc = yaml.safe_load(grc_path.read_text())
    blocks = {b["name"]: b for b in doc.get("blocks", [])}

    # ---- the evaluation namespace, GRC-style --------------------------------
    ns: dict = {"math": math}
    for b in doc.get("blocks", []):
        if b.get("id") == "import":
            _MockImporter.run(b["parameters"].get("imports", ""), ns)
    # every involved block's template imports join the namespace too (GRC
    # evaluates params with the block's own imports available — e.g.
    # ``analog.GR_SIN_WAVE`` in a stock sig_source param).
    for b in doc.get("blocks", []):
        yml = _INDEX.get(b.get("id"))
        imp = ((yml or {}).get("templates") or {}).get("imports", "")
        _MockImporter.run(imp, ns)
    # variable-defining blocks (variable, variable_qtgui_range, …)
    pending = {}
    for b in doc.get("blocks", []):
        if str(b.get("id", "")).startswith("variable"):
            pending[b["name"]] = (b.get("parameters") or {}).get("value", "0")
    for _ in range(len(pending) + 1):
        for name, val in list(pending.items()):
            try:
                ns[name] = eval(str(val), ns)  # noqa: S307
                del pending[name]
            except Exception:  # noqa: BLE001
                pass
    for name, val in pending.items():
        try:
            eval(str(val), ns)  # noqa: S307
        except Exception as e:  # noqa: BLE001
            findings.append(f"variable {name} = {val!r}: {e}")

    # ---- per-block: yml resolvable + evaluated params evaluate --------------
    # epy_block / epy_module are GRC CORE blocks defined in code, not ymls
    # (their ports/params come from the embedded source); the instantiate gate
    # covers them under the real Platform.
    skip_ids = {"import", "options", "note", "snippet", "epy_block",
                "epy_module"}
    for name, b in blocks.items():
        bid = b.get("id")
        if bid in skip_ids or str(bid).startswith("variable"):
            continue
        yml = _INDEX.get(bid)
        if yml is None:
            findings.append(f"block {name}: no yml for id {bid!r} "
                            "(red 'Missing Block' in GRC)")
            continue
        dtypes = {p.get("id"): str(p.get("dtype", ""))
                  for p in yml.get("parameters", []) or []}
        for pid, val in (b.get("parameters") or {}).items():
            if dtypes.get(pid) not in _EVAL_DTYPES:
                continue
            try:
                eval(str(val), dict(ns))  # noqa: S307
            except Exception as e:  # noqa: BLE001
                findings.append(
                    f"block {name}.{pid} = {val!r} (dtype "
                    f"{dtypes.get(pid)}): {e}")

    # ---- connections: endpoints + port indices + concrete dtypes ------------
    for conn in doc.get("connections", []) or []:
        if len(conn) < 4:
            findings.append(f"malformed connection {conn}")
            continue
        sname, sp, dname, dp = conn[0], conn[1], conn[2], conn[3]
        missing = False
        for endname, role in ((sname, "source"), (dname, "target")):
            if endname not in blocks:
                findings.append(f"connection {conn}: {role} block "
                                f"{endname!r} does not exist")
                missing = True
        if missing:
            continue
        sy = _INDEX.get(blocks[sname].get("id"))
        dy = _INDEX.get(blocks[dname].get("id"))
        souts = _ports(sy, "outputs", blocks[sname].get("parameters")) \
            if sy else None
        dins = _ports(dy, "inputs", blocks[dname].get("parameters")) \
            if dy else None
        if souts is not None and str(sp).isdigit() and int(sp) >= len(souts):
            findings.append(
                f"connection {conn}: source {sname} has no output port {sp} "
                f"({len(souts)} declared)")
            continue
        if dins is not None and str(dp).isdigit() and int(dp) >= len(dins):
            findings.append(
                f"connection {conn}: target {dname} has no input port {dp} "
                f"({len(dins)} declared)")
            continue
        if souts and dins and str(sp).isdigit() and str(dp).isdigit():
            sdt = str(souts[int(sp)].get("dtype", ""))
            ddt = str(dins[int(dp)].get("dtype", ""))
            if ("$" not in sdt and "$" not in ddt and sdt and ddt
                    and sdt != ddt):
                findings.append(
                    f"connection {conn}: dtype mismatch {sdt} -> {ddt}")
    return findings


def _rel(p: Path) -> str:
    return p.parent.name + "/" + p.name


@pytest.mark.parametrize("grc", EXAMPLE_GRCS, ids=_rel)
def test_grc_lints_clean(grc):
    if _rel(grc) in _KNOWN_OPEN:
        pytest.xfail(_KNOWN_OPEN[_rel(grc)])
    findings = _lint(grc)
    assert not findings, "\n".join(findings)


def _installed_kyttar_py_stale() -> str | None:
    """Non-None (a reason) when the installed kyttar PYTHON package is missing
    a repo module a flowgraph may import.

    A ``.grc``'s ``import`` block runs under the GNU Radio interpreter, which
    resolves ``gnuradio.kyttar`` to the INSTALLED dist-packages OOT — never the
    repo tree. A demo-stimulus module added to ``gr-kyttar/python/kyttar/`` is
    therefore invisible to ``grcc`` until ``install.sh`` re-syncs it, and every
    variable/vector that evaluates through it fails to compile. That is an
    install-staleness artifact, not a defect in the flowgraph — the same
    condition the yml check below already names, so it gets the same named
    skip rather than a bogus red.
    """
    try:
        r = subprocess.run(
            [_GR_PYTHON, "-c",
             "import os, gnuradio.kyttar as k; print(os.path.dirname(k.__file__))"],
            capture_output=True, text=True, timeout=120)
    except Exception:  # noqa: BLE001
        return None                      # no GR interpreter — nothing to check
    if r.returncode != 0 or not r.stdout.strip():
        return None
    inst_dir = Path(r.stdout.strip())
    for repo_py in (_ROOT / "gr-kyttar" / "python" / "kyttar").glob("*.py"):
        if not (inst_dir / repo_py.name).exists():
            return (f"{repo_py.name} not installed in {inst_dir} — "
                    "run gr-kyttar/install.sh")
    return None


def _installed_kyttar_stale() -> str | None:
    """Non-None (a reason) when the installed kyttar OOT differs from the
    repo's — grcc would then validate against the WRONG interface."""
    for d in _STOCK_DIRS:
        if any(d.glob("kyttar_*.block.yml")):
            for repo_yml in _KYTTAR_YMLS.glob("*.block.yml"):
                inst = d / repo_yml.name
                if not inst.exists():
                    return f"{repo_yml.name} not installed in {d}"
                if inst.read_text() != repo_yml.read_text():
                    return (f"{repo_yml.name} installed copy differs — "
                            "run gr-kyttar/install.sh")
            return _installed_kyttar_py_stale()
    return "kyttar block ymls not installed"


@pytest.mark.skipif(shutil.which("grcc") is None, reason="grcc not available")
@pytest.mark.parametrize("grc", EXAMPLE_GRCS, ids=_rel)
def test_grcc_compiles(grc):
    if _rel(grc) in _KNOWN_OPEN:
        pytest.xfail(_KNOWN_OPEN[_rel(grc)])
    stale = _installed_kyttar_stale()
    if stale:
        pytest.skip(f"installed OOT stale ({stale}) — grcc reads the "
                    "INSTALLED ymls; re-run gr-kyttar/install.sh to arm this "
                    "gate")
    out = tempfile.mkdtemp(prefix="ex_grcc_")
    r = subprocess.run(["grcc", str(grc), "-o", out],
                       capture_output=True, text=True, timeout=300)
    pys = list(Path(out).glob("*.py"))
    assert r.returncode == 0 and pys, (
        f"grcc failed for {grc.name}:\n{r.stderr[-1500:]}")
    compile(pys[0].read_text(), str(pys[0]), "exec")


# ---------------------------------------------------------------------------
# INV-43: GRC's Generate must never land on a hand-written module.
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("grc", EXAMPLE_GRCS, ids=_rel)
def test_grc_generate_target_is_not_a_hand_written_module(grc):
    """No ``.grc`` may generate ON TOP OF a hand-written source file.

    GRC's "Generate" writes ``<flowgraph id>.py`` beside the ``.grc``. If a
    hand-written module already occupies that name, pressing Generate destroys
    it silently, and the loss reads as an ordinary edit in ``git status``.

    This is a REAL loss, not a hypothetical one: ``gru_classifier.py`` — the
    534-line design module the example's builder, demo and gates all import —
    was overwritten by its own generated flowgraph, and the deletion rode into
    an unrelated commit. 28 shipped examples share the id/filename shape; the
    other 27 survived only because their ``<id>.py`` IS the generated flowgraph.

    A file counts as hand-written here if it lacks GRC's generated-file banner.
    That banner is what Generate itself stamps, so this asks the only question
    that matters: would Generate overwrite something it did not write?
    """
    text = grc.read_text()
    m = re.search(r"^    id: (\S+)$", text, re.MULTILINE)
    if not m:
        pytest.skip(f"{_rel(grc)} declares no flowgraph id")
    target = grc.parent / f"{m.group(1)}.py"
    if not target.exists():
        return  # Generate would create a new file — nothing to destroy.

    head = target.read_text(errors="replace")[:2000]
    is_generated = ("GNU Radio Python Flow Graph" in head
                    or "GNU Radio version" in head)
    assert is_generated, (
        f"{_rel(grc)} has flowgraph id {m.group(1)!r}, so GRC's Generate writes "
        f"{target.name} — but that file is HAND-WRITTEN (no generated banner). "
        f"Pressing Generate would destroy it. Rename the flowgraph id; the repo "
        f"convention is <name>_demo (see fft128_2p2s, gain, gain_2p2s).")
