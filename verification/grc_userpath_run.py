# SPDX-License-Identifier: GPL-3.0-or-later
"""Run a GENERATED example flowgraph against a live placeKYT server — the
exact "open the .grc in GRC and press Run" path — and capture every
kyttar_sink's recovered stream.

GRC-generates the .grc (repo ymls, repo markers — see grc_instantiate_check),
instantiates the top block, attaches a ``blocks.vector_sink_f`` to every
kyttar sink's OUTPUT port (a plain GR fan-out; the flowgraph itself is
untouched), runs the graph to completion of one batch, and prints one
``SINK <block_name> <v0> <v1> ...`` line per sink (floats).

Usage: /usr/bin/python3 grc_userpath_run.py <file.grc> <run_seconds>
The placeKYT server must already be hosting the design on the port the .grc
names (58950 — the GUI's default bind).
"""
import os
import sys
import tempfile
import time

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(REPO, "gr-kyttar", "python"))

# REPO-COHERENT gate: the generated flowgraph does ``import kyttar`` AND the
# yml templates ``from gnuradio import kyttar`` — the latter resolves to the
# INSTALLED dist-packages OOT even with the repo path first on sys.path
# (namespace package). This gate compiles with the REPO ymls (they shadow the
# installed dir below), so the runtime must be the REPO markers too, or a repo
# yml gaining a param fails here against a stale installed module (the
# ``repeat`` kwarg). Same alias as grc_instantiate_check.py; the user-facing
# staleness signal stays with the grcc-smoke gate's named skip.
import kyttar as _repo_kyttar  # noqa: E402
sys.modules["gnuradio.kyttar"] = _repo_kyttar

from gnuradio import blocks, gr  # noqa: E402
from gnuradio.grc.core.platform import Platform  # noqa: E402

grc_path, run_secs = sys.argv[1], float(sys.argv[2])

platform = Platform(
    name="placeKYT GRC user-path gate",
    prefs=gr.prefs(),
    version=gr.version(),
    version_parts=(gr.major_version(), gr.api_version(), gr.minor_version()),
)
platform.build_library([
    "/usr/share/gnuradio/grc/blocks",
    os.path.join(REPO, "gr-kyttar", "grc"),
])
out = tempfile.mkdtemp(prefix="grcuser_")
fg, file_path = platform.load_and_generate_flow_graph(
    os.path.abspath(grc_path), os.path.abspath(out))
assert file_path, "generation failed"

import importlib.util  # noqa: E402

name = os.path.splitext(os.path.basename(file_path))[0]
spec = importlib.util.spec_from_file_location(name, file_path)
mod = importlib.util.module_from_spec(spec)
# The generated script's own directory is sys.path[0] when GRC runs it —
# epy_block companion modules (<flowgraph>_<block>.py) import from there.
sys.path.insert(0, os.path.dirname(os.path.abspath(file_path)))
spec.loader.exec_module(mod)
cls = getattr(mod, name)

try:
    from PyQt5 import Qt
    qapp = Qt.QApplication(["gate"])
except Exception:  # noqa: BLE001
    qapp = None

tb = cls()

# Tap every kyttar sink's recovered output with a vector sink (GR fan-out).
taps = {}
for attr, val in vars(tb).items():
    if val.__class__.__name__ == "sink" and "kyttar" in type(val).__module__:
        vs = blocks.vector_sink_f()
        tb.connect((val, 0), (vs, 0))
        taps[attr] = vs
assert taps, "no kyttar sinks found in the generated flowgraph"

tb.start()
t0 = time.time()
while time.time() - t0 < run_secs:
    time.sleep(0.25)
tb.stop()
tb.wait()

for attr, vs in taps.items():
    data = list(vs.data())
    print("SINK", attr, " ".join(repr(float(v)) for v in data), flush=True)
