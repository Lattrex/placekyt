# SPDX-License-Identifier: GPL-3.0-or-later
"""GRC OPEN-AND-BUILD checker — run under the SYSTEM GNU Radio interpreter.

Loads a .grc with GRC's OWN compiler (gnuradio.grc.core.platform.Platform)
against the stock block tree + the REPO's gr-kyttar/grc ymls, generates the
Python flowgraph, and INSTANTIATES the generated top block with the REPO's
kyttar markers on sys.path. This is exactly what happens when the user opens
the flowgraph in GRC and presses Run (minus the QApplication event loop), so
it catches everything that class of "I opened it and it broke" bug can be:

  * a yml that fails GRC's schema (e.g. a missing ``file_format`` — the block
    silently becomes "Missing Block" and its connections drop);
  * invalid params (``None`` for a vector dtype);
  * connections GRC drops (nonexistent ports) → "Port is not connected";
  * runtime ITEMSIZE mismatches between MARKERS (``hier_block2.connect``) —
    the yml may claim any dtype; the marker's ``io_signature`` is the truth
    (the byte-out BPSK slicer vs its float-out yml crashed three examples).

Usage:  /usr/bin/python3 grc_instantiate_check.py <file.grc> [...]
Prints one ``OK <name>`` / ``FAIL <name>: <reason>`` line per file; exit 1 on
any FAIL. Kept as a standalone script (not pytest) because it must run under
the GNU Radio interpreter, not the placeKYT venv.
"""
import os
import sys
import tempfile

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(REPO, "gr-kyttar", "python"))

# REPO-COHERENT gate: the yml templates import ``from gnuradio import kyttar``,
# which resolves to the INSTALLED dist-packages OOT even with the repo path on
# sys.path (namespace package) — so a repo yml gaining a param would fail here
# against a stale installed module (split-brain: repo templates, installed
# python). Alias the REPO kyttar into the gnuradio namespace so this gate tests
# the repo's yml contract against the repo's OWN markers; the installed-stack
# staleness signal stays with the grcc-smoke gate's named skip.
import kyttar as _repo_kyttar  # noqa: E402
sys.modules["gnuradio.kyttar"] = _repo_kyttar

from gnuradio import gr  # noqa: E402
from gnuradio.grc.core.platform import Platform  # noqa: E402

_platform = Platform(
    name="placeKYT GRC gate",
    prefs=gr.prefs(),
    version=gr.version(),
    version_parts=(gr.major_version(), gr.api_version(), gr.minor_version()),
)
# Stock GR blocks + the REPO ymls LAST (they shadow any installed kyttar copy,
# so the gate always tests the repo's contract, installed-OOT staleness aside).
_platform.build_library([
    "/usr/share/gnuradio/grc/blocks",
    os.path.join(REPO, "gr-kyttar", "grc"),
])

_qapp = None


def check(grc_path: str):
    """None if the flowgraph opens, generates and instantiates; else a reason."""
    global _qapp
    out = tempfile.mkdtemp(prefix="grcgate_")
    try:
        fg, file_path = _platform.load_and_generate_flow_graph(
            os.path.abspath(grc_path), os.path.abspath(out))
    except Exception as e:  # noqa: BLE001
        return f"GENERATE-EXC: {e}"
    if not file_path:
        errs = []
        try:
            for elem, msg in (fg.iter_error_messages() if fg else []):
                errs.append(f"{elem}: {msg}")
        except Exception:  # noqa: BLE001
            pass
        return "GENERATE-FAILED: " + ("; ".join(errs) or
                                      "flowgraph invalid (see GRC output)")
    errs = []
    try:
        for elem, msg in fg.iter_error_messages():
            errs.append(f"{elem}: {msg}")
    except Exception:  # noqa: BLE001
        pass
    if errs:
        return "VALIDATE: " + "; ".join(errs)

    import importlib.util
    name = os.path.splitext(os.path.basename(file_path))[0]
    spec = importlib.util.spec_from_file_location(name, file_path)
    mod = importlib.util.module_from_spec(spec)
    # The generated script's own directory is sys.path[0] when GRC runs it —
    # epy_block companion modules (<flowgraph>_<block>.py) import from there.
    gen_dir = os.path.dirname(os.path.abspath(file_path))
    sys.path.insert(0, gen_dir)
    try:
        spec.loader.exec_module(mod)
    except SystemExit:
        return "IMPORT-EXIT"
    except Exception as e:  # noqa: BLE001
        return f"IMPORT-EXC: {e}"
    finally:
        if gen_dir in sys.path:
            sys.path.remove(gen_dir)
    cls = getattr(mod, name, None)
    if cls is None:
        for v in vars(mod).values():
            if isinstance(v, type) and getattr(v, "__module__", "") == name:
                cls = v
                break
    if cls is None:
        return "NO-TOPBLOCK-CLASS"
    try:
        from PyQt5 import Qt
        if _qapp is None:
            _qapp = Qt.QApplication(["gate"])
    except Exception:  # noqa: BLE001
        pass
    try:
        tb = cls()
    except Exception as e:  # noqa: BLE001
        return f"INSTANTIATE: {type(e).__name__}: {e}"
    try:
        tb.stop()
    except Exception:  # noqa: BLE001
        pass
    return None


if __name__ == "__main__":
    rc = 0
    for grc in sys.argv[1:]:
        err = check(grc)
        tag = os.path.basename(os.path.dirname(grc)) + "/" + os.path.basename(grc)
        if err:
            rc = 1
            print(f"FAIL {tag}: {err}", flush=True)
        else:
            print(f"OK   {tag}", flush=True)
    sys.exit(rc)
