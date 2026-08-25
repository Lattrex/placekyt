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
                                            [block.port,block.port,...]
The placeKYT server must already be hosting the design on the port the .grc
names (58950 — the GUI's default bind).

The optional third argument names EXTRA blocks/ports to tap — the display glue
that actually feeds the scopes. Use it when the gate needs to assert the traces
the USER SEES rather than only the recovered sink stream.
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

# EXTRA DISPLAY TAPS (optional 3rd arg: "block.port,block.port,...").
#
# The kyttar sink carries the RECOVERED data, but it is NOT what the user
# looks at — the scopes are fed by the display glue downstream of it. A gate
# that asserts only the sink stream can pass while the plotted traces are
# unusable (measured on this example: a separate free-running reference source
# ran 27.9% fast and slid the reference off a bit-exact decode). Naming the
# blocks that FEED the scopes lets a gate assert the plotted traces themselves.
#
# A VECTOR port (a spectrum display block emits one N-point vector per frame)
# is tapped with a matching-vlen vector_sink_f: connecting a vlen-1 sink to an
# N-float port is an itemsize mismatch and the flowgraph would refuse to
# connect. ``vector_sink_f(vlen).data()`` returns the frames FLATTENED, which is
# what the SINK line protocol below carries — the reading gate reshapes by vlen.
for spec_str in (sys.argv[3].split(",") if len(sys.argv) > 3 and sys.argv[3]
                 else []):
    spec_str = spec_str.strip()
    if not spec_str:
        continue
    attr, _, port = spec_str.partition(".")
    blk = getattr(tb, attr, None)
    assert blk is not None, f"no block {attr!r} in the generated flowgraph"
    p = int(port or 0)
    itemsize = blk.output_signature().sizeof_stream_item(p)
    vlen, rem = divmod(itemsize, gr.sizeof_float)
    assert rem == 0 and vlen >= 1, (
        f"{spec_str}: port itemsize {itemsize} is not a whole number of "
        "floats — this tap only handles float / float-vector ports")
    vs = blocks.vector_sink_f(vlen)
    tb.connect((blk, p), (vs, 0))
    taps[spec_str] = vs

tb.start()
t0 = time.time()
while time.time() - t0 < run_secs:
    time.sleep(0.25)
tb.stop()
tb.wait()

for attr, vs in taps.items():
    data = list(vs.data())
    print("SINK", attr, " ".join(repr(float(v)) for v in data), flush=True)
