# SPDX-License-Identifier: GPL-3.0-or-later
"""examples/foc_motor — the FULL-LOOP FLOWGRAPH gate.

The sibling ``test_foc_motor_example.py`` gates the shipped ``.kyt`` (the
command half on one array). THIS file gates the thing that file does not
cover: ``foc_motor.grc``, the whole loop — measurement half, host-side error
former, command half, and the motor that closes it.

WHAT IS PROVEN HERE

  * the ``.grc`` is STRUCTURALLY complete: every on-chip block of the full
    loop is present with the right parameters, every connection of the loop
    is present, theta is delivered as TWO independent arms (one per rotation),
    every ingress arm has its own relay and its own distinct stream id, and
    ``server_port`` is 58950 everywhere — a 0 there makes ``kyttar_source``
    silently no-op, which is the single most common "GRC does nothing" cause;
  * it VALIDATES under GNU Radio's own flowgraph validator with zero errors,
    and CODEGENS through ``grcc`` into Python that parses (INV-42: the flags
    are asserted on the GENERATED Python, not the .grc text);
  * the CLOSED LOOP settles in pure host simulation against the blocks' own
    pinned integer models: i_d -> 0 and i_q -> its reference, which proves the
    control law and the plant are consistent;
  * ``StatefulPI`` — the sample-at-a-time integrator the closed loop needs —
    is bit-identical to ``PIControllerBlock``'s batch model;
  * the full loop RUNS ACROSS TWO ARRAYS, closed around the plant, with the
    measurement half bit-exact against its host golden every iteration and
    every run settling ``QueueEmpty`` (INV-56) — and the measured per-sample
    interval is the serial sum of the two halves.

WHAT IS NOT CLAIMED: the whole loop on ONE array. It does not route — the
limit is corridor/arm budget, not cells (INV-71) — and nothing here pretends
otherwise. The two-array gate is the honest whole-loop measurement.

Run::

    QT_QPA_PLATFORM=offscreen \\
      .venv/bin/python -m pytest verification/tests/test_foc_motor_grc.py -q
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

_ROOT = Path(__file__).resolve().parents[2]
_EX = _ROOT / "examples" / "foc_motor"
_GRC = _EX / "foc_motor.grc"
for _p in (_ROOT / "placekyt", _ROOT / "runtime" / "python", _ROOT / "verification",
           _ROOT / "verification" / "tests", _EX):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

pytestmark = pytest.mark.skipif(not _GRC.exists(), reason="foc_motor.grc absent")

SERVER_PORT = "58950"

# The five on-chip blocks of the FULL loop, and the parameters that make each
# the right one. A missing entry here is a hole in the flowgraph.
EXPECTED_CHIP_BLOCKS = {
    "clarke": ("kyttar_clarke_transform", {}),
    "park": ("kyttar_cordic_rotate", {"sign": "-1"}),      # FORWARD Park
    "ipark": ("kyttar_cordic_rotate", {"sign": "1"}),      # INVERSE Park
    "pi_d": ("kyttar_pi_controller", {"kp": "0.25", "ki": "0.01", "limit": "1.0"}),
    "pi_q": ("kyttar_pi_controller", {"kp": "0.25", "ki": "0.01", "limit": "1.0"}),
    "svpwm": ("kyttar_svpwm", {}),
}

# Every ingress arm: its source, its relay, and the on-chip port it feeds.
# THETA APPEARS TWICE, on purpose — it fans out to both rotations, and on-chip
# fan-out to two rendezvous arms is the hard part, so it is delivered as two
# independent arms rather than split on the fabric.
EXPECTED_ARMS = [
    ("src_ia", "ia", "relay_ia", "clarke", "0"),
    ("src_ib", "ib", "relay_ib", "clarke", "1"),
    ("src_th_park", "th_park", "relay_th_park", "park", "2"),
    ("src_ed", "e_d", "relay_ed", "pi_d", "0"),
    ("src_eq", "e_q", "relay_eq", "pi_q", "0"),
    ("src_th_ipark", "th_ipark", "relay_th_ipark", "ipark", "2"),
]

# The loop's signal path, as (src, src_port, dst, dst_port) edges that MUST be
# present. This is the connectivity the user will hand-place from.
EXPECTED_EDGES = [
    # measurement half
    ("clarke", "0", "ab_split", "0"),
    ("ab_split", "0", "park", "0"),          # i_alpha -> x
    ("ab_split", "1", "park", "1"),          # i_beta  -> y
    ("park", "0", "sink_idq", "0"),
    ("sink_idq", "0", "idq_split", "0"),
    # error formation, on the host — inside foc_host, which also emits it
    ("foc_host", "3", "src_ed", "0"),        # e_d
    ("foc_host", "4", "src_eq", "0"),        # e_q
    # command half
    ("pi_d", "0", "ipark", "0"),             # v_d -> x
    ("pi_q", "0", "ipark", "1"),             # v_q -> y
    ("ipark", "0", "v_split", "0"),
    ("v_split", "0", "svpwm", "0"),          # v_alpha
    ("v_split", "1", "svpwm", "1"),          # v_beta
    ("svpwm", "0", "sink_duty", "0"),
    ("sink_duty", "0", "duty_split", "0"),
    # the loop CLOSES INSIDE foc_host — the feedback is an assignment, not a
    # wire, because GNU Radio refuses to start a stream cycle. What the
    # flowgraph shows is foc_host driving both halves with the live sensed
    # quantities and the live errors of the period it is running.
    ("foc_host", "0", "src_ia", "0"),        # sensed ia
    ("foc_host", "1", "src_ib", "0"),        # sensed ib
    ("foc_host", "2", "src_th_park", "0"),   # theta, arm 1 of 2
    ("foc_host", "5", "src_th_ipark", "0"),  # theta, arm 2 of 2
]


@pytest.fixture(scope="module")
def fg():
    return yaml.safe_load(_GRC.read_text())


@pytest.fixture(scope="module")
def blocks(fg):
    return {b["name"]: b for b in fg["blocks"] if "name" in b}


@pytest.fixture(scope="module")
def edges(fg):
    return {(c[0], str(c[1]), c[2], str(c[3])) for c in fg["connections"]}


# --------------------------------------------------------------------------- #
#  The flowgraph is STRUCTURALLY the whole loop                                #
# --------------------------------------------------------------------------- #

def test_every_on_chip_block_of_the_full_loop_is_present(blocks):
    """All five stages, with the parameters that make each the right one.

    In particular the two CORDIC rotations must carry OPPOSITE signs: sign=-1
    is the forward Park (measure), sign=+1 the inverse Park (command). Getting
    both the same would be a flowgraph that looks complete and is not a loop."""
    for name, (gid, params) in EXPECTED_CHIP_BLOCKS.items():
        assert name in blocks, f"block '{name}' missing from the flowgraph"
        assert blocks[name]["id"] == gid, (
            f"{name} is a {blocks[name]['id']}, expected {gid}")
        for k, v in params.items():
            got = str(blocks[name]["parameters"].get(k))
            assert got == v, f"{name}.{k} = {got}, expected {v}"


def test_the_two_rotations_have_opposite_signs(blocks):
    """Stated separately because it is the single easiest thing to get wrong
    and the hardest to see: a loop with two forward Parks still validates,
    codegens, and places."""
    assert blocks["park"]["parameters"]["sign"] == "-1"
    assert blocks["ipark"]["parameters"]["sign"] == "1"


def test_every_loop_connection_is_present(edges):
    missing = [e for e in EXPECTED_EDGES if e not in edges]
    assert not missing, f"the flowgraph is missing {len(missing)} loop edges: {missing}"


def test_the_loop_actually_closes(blocks, edges):
    """The loop must really be CLOSED — the duties must drive the motor and
    the motor's sensed outputs must drive the next control period.

    It closes INSIDE ``foc_host`` rather than through a wire, because GNU
    Radio's stream scheduler refuses a cycle outright. So the proof is in two
    parts: the block feeds every ingress arm of both halves (below), and its
    source really does step the motor with the duties it just computed and
    re-read what the motor then sensed (asserted here on the source text, and
    behaviourally by the settle gates further down).
    """
    for out_port, arm_src in ((0, "src_ia"), (1, "src_ib"),
                              (2, "src_th_park"), (5, "src_th_ipark")):
        assert ("foc_host", str(out_port), arm_src, "0") in edges, (
            f"foc_host output {out_port} does not reach {arm_src}")
    for out_port, arm_src in ((3, "src_ed"), (4, "src_eq")):
        assert ("foc_host", str(out_port), arm_src, "0") in edges, (
            f"foc_host output {out_port} does not reach {arm_src}")

    src = blocks["foc_host"]["parameters"]["_source_code"]
    # The feedback assignment itself: this period's duties step the motor, and
    # what it senses becomes the NEXT period's input. Note this is a text
    # check and therefore the weaker half of the proof — dropping the
    # assignment while keeping the call would slip past it. The gate that
    # actually catches that is the behavioural one,
    # ``test_the_shipped_block_closes_the_loop_and_settles``: with the
    # feedback removed i_q sits at 0.00000 instead of reaching its reference.
    assert "self._sensed = self.plant.step(da, db, dc)" in src, (
        "foc_host never feeds the stepped motor back into the next control "
        "period — the loop is OPEN and this example would be replaying a "
        "stimulus rather than regulating")


def test_theta_is_delivered_as_two_independent_arms(blocks, edges):
    """Theta feeds BOTH rotations. On-chip fan-out to two rendezvous arms is
    the hard part, so it is delivered as two independent ingress arms, each
    with its own source, its own stream id and its own relay."""
    for src, relay, blk in (("src_th_park", "relay_th_park", "park"),
                            ("src_th_ipark", "relay_th_ipark", "ipark")):
        assert src in blocks and relay in blocks
        assert (src, "0", relay, "0") in edges
        assert (relay, "0", blk, "2") in edges
    a = blocks["src_th_park"]["parameters"]["stream_id"]
    b = blocks["src_th_ipark"]["parameters"]["stream_id"]
    assert a != b, f"theta's two arms share a stream id ({a}) — they are not independent"


def test_every_ingress_arm_has_its_own_relay(blocks, edges):
    """INV-71, in the flowgraph. A net fanned straight off the chip input port
    into a face-locking block's arm lands every word on the PORT CELL, hence
    on ONE face, which the rendezvous LOCK bars: the chain then routes, builds
    and emits nothing. Each arm therefore gets its own relay so the arms land
    on distinct hops."""
    for src, _sid, relay, blk, port in EXPECTED_ARMS:
        assert src in blocks, f"{src} missing"
        assert relay in blocks, f"{relay} missing"
        assert blocks[relay]["id"] == "kyttar_splitter", (
            f"{relay} is not a relay block")
        assert (src, "0", relay, "0") in edges, f"{src} -> {relay} missing"
        assert (relay, "0", blk, port) in edges, f"{relay} -> {blk}.{port} missing"


def test_every_arm_has_a_distinct_stream_id(blocks):
    """Independent arms share one input port and are told apart by stream id:
    the importer copies it onto the x16_in -> block net so the server resolves
    each burst to that block's own entry/hop/data registers. Two arms with one
    id are indistinguishable on the wire."""
    ids = {}
    for src, sid, _relay, _blk, _port in EXPECTED_ARMS:
        got = str(blocks[src]["parameters"]["stream_id"]).strip('"')
        assert got == sid, f"{src} stream_id is {got!r}, expected {sid!r}"
        assert got, f"{src} has an EMPTY stream id"
        ids.setdefault(got, []).append(src)
    dupes = {k: v for k, v in ids.items() if len(v) > 1}
    assert not dupes, f"arms share stream ids: {dupes}"


def test_server_port_is_set_on_every_kyttar_block(blocks):
    """A server_port of 0 makes kyttar_source silently no-op — it never
    connects, the flowgraph runs and does nothing, and there is no error. It
    is the single most common 'GRC does nothing' cause in this repo."""
    checked = 0
    for name, b in blocks.items():
        if b["id"] in ("kyttar_source", "kyttar_sink"):
            got = str(b["parameters"].get("server_port"))
            assert got == SERVER_PORT, (
                f"{name}.server_port = {got}, expected {SERVER_PORT}")
            checked += 1
    assert checked >= 8, f"only {checked} kyttar source/sink blocks found"


def test_svpwm_output_is_split_three_ways(blocks, edges):
    """SVPWM emits THREE words per sample on ONE stream (duty a, b, c). Its
    sink must be set to q15 words and the packet split with a Deinterleave at
    3 — that is the block's documented output contract."""
    assert blocks["duty_split"]["id"] == "blocks_deinterleave"
    assert blocks["duty_split"]["parameters"]["num_streams"] == "3"
    for i in range(3):
        assert ("duty_split", str(i), "scope_duty", str(i)) in edges, (
            f"duty rail {i} off the array is not displayed")


def test_the_plant_is_an_embedded_python_block(blocks):
    """The motor is host-side and always will be — it is the physical machine,
    not a DSP stage. It lives inside ``foc_host`` together with the error
    former and the feedback path, which is what keeps the flowgraph acyclic."""
    assert blocks["foc_host"]["id"] == "epy_block"
    src = blocks["foc_host"]["parameters"]["_source_code"]
    for token in ("back", "i_alpha", "i_beta", "theta_e", "omega_e"):
        assert token in src, f"the plant source does not mention {token}"


def test_the_host_block_owns_the_error_former_too(blocks):
    """The error former moved INTO the same block. It used to be its own
    block, with the array between it and the motor — which is precisely what
    made the flowgraph a ring the scheduler would not start."""
    src = blocks["foc_host"]["parameters"]["_source_code"]
    assert "i_d_ref" in src and "i_q_ref" in src, (
        "foc_host does not form the error — the reference is missing")
    assert "foc_loop_golden" in src, (
        "foc_host does not run the loop's own pinned integer models")
    # and the old two-block shape is really gone
    assert "err" not in blocks, (
        "the standalone error former is still in the flowgraph — the ring is "
        "back")
    assert "plant" not in blocks, (
        "the standalone plant block is still in the flowgraph")


def test_scopes_buffer_a_full_burst(blocks):
    """BLANK-SCOPE DISPLAY CONTRACT: a QT time sink needs a FULL-size buffer.
    The batch arrives all at once, so a short buffer paints nothing and the
    chip gets blamed for a display bug."""
    scopes = [n for n, b in blocks.items() if b["id"] == "qtgui_time_sink_x"]
    assert scopes, "no scopes in the flowgraph"
    for n in scopes:
        assert blocks[n]["parameters"]["size"] == "burst_len", (
            f"scope {n} does not buffer a full burst")


# --------------------------------------------------------------------------- #
#  GRC validity and CODEGEN — INV-42                                           #
# --------------------------------------------------------------------------- #

def _gr_python():
    for cand in ("/usr/bin/python3",):
        if Path(cand).exists():
            r = subprocess.run([cand, "-c", "import gnuradio.grc"],
                               capture_output=True)
            if r.returncode == 0:
                return cand
    return None


GR_PY = _gr_python()
GRCC = shutil.which("grcc")
_GR_ENV = dict(os.environ,
               GRC_BLOCKS_PATH="/usr/share/gnuradio/grc/blocks:"
               + os.path.expanduser("~/.local/share/gnuradio/grc/blocks"))


@pytest.mark.skipif(GR_PY is None, reason="GNU Radio python not available")
def test_grc_validates_with_zero_errors():
    """GNU Radio's OWN flowgraph validator, headless — the same errors GRC
    would paint in the GUI."""
    r = subprocess.run([GR_PY, str(_ROOT / "examples" / "validate_grc.py"), str(_GRC)],
                       capture_output=True, text=True, env=_GR_ENV, timeout=600)
    tail = "\n".join(r.stdout.strip().splitlines()[-15:])
    assert r.returncode == 0, f"validate_grc reported errors:\n{tail}"
    assert "0 error(s)" in r.stdout, tail


@pytest.mark.skipif(GRCC is None or GR_PY is None, reason="grcc not available")
def test_grc_codegens_and_the_generated_python_parses(tmp_path):
    """INV-42: the flags are asserted on the GENERATED Python, not the .grc.

    A .grc can be structurally plausible and still generate Python that does
    not parse — an embedded python block whose constructor keyword has no
    matching GRC parameter emits a bare ``kw=`` and the file is invalid. That
    is exactly the defect this gate catches."""
    r = subprocess.run([GRCC, "-o", str(tmp_path), str(_GRC)],
                       capture_output=True, text=True, env=_GR_ENV, timeout=900)
    assert r.returncode == 0, f"grcc failed:\n{r.stdout[-2000:]}\n{r.stderr[-2000:]}"
    out = tmp_path / "foc_motor.py"
    assert out.exists(), f"grcc produced no foc_motor.py (in {list(tmp_path.iterdir())})"

    import ast
    src = out.read_text()
    ast.parse(src)                    # raises SyntaxError if codegen is broken

    # The flags, ON THE GENERATED PYTHON.
    assert f"server_port={SERVER_PORT}" in src
    assert "server_port=0" not in src, (
        "the generated Python has a server_port=0 — that source will silently "
        "never connect")
    for call in ("kyttar.clarke_transform(", "kyttar.cordic_rotate(",
                 "kyttar.pi_controller(", "kyttar.svpwm("):
        assert call in src, f"the generated Python never constructs {call}"
    assert src.count("kyttar.cordic_rotate(") == 2, "expected BOTH rotations"
    assert src.count("kyttar.pi_controller(") == 2, "expected BOTH PI axes"


# --------------------------------------------------------------------------- #
#  THE SCHEDULER ACCEPTS IT — the gate that codegen does NOT give you          #
# --------------------------------------------------------------------------- #

# Codegen and validation both PASS on a flowgraph GNU Radio will refuse to run.
# "flow graph has loops!" is raised by the runtime scheduler inside
# ``tb.start()``, downstream of the validator and downstream of grcc — so an
# earlier version of this flowgraph passed every structural gate here and still
# died instantly the moment a user pressed Run. Nothing short of STARTING it
# catches that, so this gate starts it.
#
# A control loop IS a cycle, and GNU Radio forbids stream cycles outright: no
# buffer sizing, no priming, no scheduler option makes a stream ring legal. The
# loop is therefore closed INSIDE ``foc_host`` — motor, error former and
# feedback path in one stateful block with no stream inputs — so what the
# scheduler sees is a tree rather than a ring.

_START_RUNNER = r"""
import sys, time, traceback
outdir = sys.argv[1]
hold = float(sys.argv[2])
try:
    from PyQt5 import Qt
    _qapp = Qt.QApplication(sys.argv[:1])
except Exception:
    _qapp = None
sys.path.insert(0, outdir)
try:
    import foc_motor as M
    tb = M.foc_motor()
    tb.start()          # <-- where "flow graph has loops!" is raised
    time.sleep(hold)
    tb.stop()
    tb.wait()
    print("START_OK")
except Exception as e:
    print("START_FAILED: %s: %s" % (type(e).__name__, e))
    traceback.print_exc(file=sys.stderr)
    sys.exit(1)
"""


def _codegen_and_start(grc_path, tmp_path, hold=2.0):
    """grcc ``grc_path``, then START the generated flowgraph in a subprocess.

    Returns ``(returncode, stdout, stderr)`` of the START step. Raises if
    codegen itself fails, so a codegen break is never mistaken for a topology
    that the scheduler rejected."""
    r = subprocess.run([GRCC, "-o", str(tmp_path), str(grc_path)],
                       capture_output=True, text=True, env=_GR_ENV, timeout=900)
    assert r.returncode == 0, f"grcc failed:\n{r.stdout[-2000:]}\n{r.stderr[-2000:]}"
    runner = tmp_path / "_start_runner.py"
    runner.write_text(_START_RUNNER)
    # A stale .pyc of a PREVIOUS foc_motor.py in this directory would be
    # imported instead of the one just generated (INV-4's stale-pyc trap).
    shutil.rmtree(tmp_path / "__pycache__", ignore_errors=True)
    env = dict(_GR_ENV, QT_QPA_PLATFORM="offscreen")
    p = subprocess.run([GR_PY, str(runner), str(tmp_path), str(hold)],
                       capture_output=True, text=True, env=env, timeout=600)
    return p.returncode, p.stdout, p.stderr


@pytest.mark.skipif(GRCC is None or GR_PY is None, reason="grcc not available")
def test_the_flowgraph_actually_starts(tmp_path):
    """THE GATE THAT MATTERS: GNU Radio's scheduler accepts this topology.

    MEASURED: ``tb.start()`` returns, the graph runs, ``tb.stop()``/``wait()``
    are clean. Before the loop was moved inside ``foc_host`` this same gate
    reported ``RuntimeError: flow graph has loops!`` — which is exactly the
    failure a user hit on Run while every structural gate here stayed green."""
    rc, out, err = _codegen_and_start(_GRC, tmp_path)
    assert "flow graph has loops!" not in (out + err), (
        "GNU Radio refused the topology — the flowgraph contains a stream "
        f"cycle:\n{out}\n{err[-2000:]}")
    assert "START_OK" in out, (
        f"the flowgraph did not start cleanly:\n{out}\n{err[-3000:]}")
    assert rc == 0, f"the start runner exited {rc}:\n{out}\n{err[-3000:]}"


@pytest.mark.skipif(GRCC is None or GR_PY is None, reason="grcc not available")
def test_the_start_gate_fails_on_a_looped_flowgraph(tmp_path):
    """INV-4: prove the start gate can FAIL.

    Reintroduce the defect — wire a chip return back into the host block's
    stimulus path, which is exactly the shape the flowgraph had before — and
    the scheduler must refuse it. A gate that cannot see the bug it exists for
    is not a gate.

    This mutates a COPY in tmp_path; the shipped .grc is never touched."""
    fg = yaml.safe_load(_GRC.read_text())

    # Give foc_host a stream input fed from the chip's own return path. Any
    # such edge closes the ring the scheduler walks.
    src = None
    for b in fg["blocks"]:
        if b.get("name") == "foc_host":
            src = b["parameters"]["_source_code"]
            break
    assert src is not None, "foc_host is not in the flowgraph"
    looped = src.replace("in_sig=[],", "in_sig=[np.float32],")
    assert looped != src, "could not inject an input port into foc_host"
    for b in fg["blocks"]:
        if b.get("name") == "foc_host":
            b["parameters"]["_source_code"] = looped
    fg["connections"].append(["idq_split", "0", "foc_host", "0"])

    mutant = tmp_path / "foc_motor.grc"
    mutant.write_text(yaml.safe_dump(fg, sort_keys=False,
                                     default_flow_style=False, width=100))
    rc, out, err = _codegen_and_start(mutant, tmp_path)
    assert "flow graph has loops!" in (out + err), (
        "a flowgraph with a stream cycle STARTED — the start gate has no "
        f"teeth:\n{out}\n{err[-3000:]}")
    assert rc != 0, "the looped flowgraph reported success"


def test_the_host_block_has_no_stream_inputs(blocks):
    """The CYCLE BREAK, asserted structurally so a future edit cannot quietly
    reintroduce the ring and rediscover 'flow graph has loops!' in the GUI.

    ``foc_host`` owns the motor, the error former and the feedback path. The
    feedback is an internal assignment, not a wire — so the block takes no
    stream input, and nothing in the flowgraph may connect INTO it."""
    src = blocks["foc_host"]["parameters"]["_source_code"]
    assert "in_sig=[]" in src, (
        "foc_host declares a stream input — the loop is a ring again")


def test_nothing_connects_into_the_host_block(edges):
    """The same break, from the connection list."""
    into = [e for e in edges if e[2] == "foc_host"]
    assert not into, (
        f"these connections feed foc_host and re-close the ring: {into}")


def test_the_flowgraph_is_acyclic(edges):
    """No stream cycle ANYWHERE, not just around the host block.

    This is the property GNU Radio's scheduler checks, checked here where the
    failure names the offending path instead of printing 'flow graph has
    loops!' with no further detail."""
    adj = {}
    for a, _ap, b, _bp in edges:
        adj.setdefault(a, set()).add(b)
    seen, stack, onstack, cycles = set(), [], set(), []

    def walk(n):
        seen.add(n)
        stack.append(n)
        onstack.add(n)
        for m in adj.get(n, ()):
            if m in onstack:
                cycles.append(stack[stack.index(m):] + [m])
            elif m not in seen:
                walk(m)
        stack.pop()
        onstack.discard(n)

    for n in list(adj):
        if n not in seen:
            walk(n)
    assert not cycles, ("the flowgraph contains stream cycles, which GNU "
                        "Radio refuses to start: "
                        + "; ".join(" -> ".join(c) for c in cycles))


# --------------------------------------------------------------------------- #
#  placeKYT IMPORTS it — the thing the flowgraph exists for                    #
# --------------------------------------------------------------------------- #

@pytest.fixture(scope="module")
def imported():
    """Import the flowgraph the way placeKYT does when the user opens it."""
    from PySide6.QtWidgets import QApplication
    QApplication.instance() or QApplication([])
    from engine.catalog import BlockCatalog
    from engine.grc_import import import_grc
    return import_grc(str(_GRC), BlockCatalog.from_gr_kyttar())


def test_placekyt_imports_the_flowgraph(imported):
    """The flowgraph is a PLACEMENT INPUT, so the gate that matters most is
    that placeKYT can read it: no unknown blocks, and a clean import."""
    assert imported.ok, f"import failed: {imported}"
    assert not imported.unknown, (
        f"placeKYT does not recognise these blocks: {imported.unknown}")


def test_import_yields_every_on_chip_block(imported):
    """Five functional stages plus a relay per ingress arm — and nothing else
    on the fabric. The plant, the error former and the scopes are host-side and
    must be DROPPED, not placed."""
    names = [b.name for b in imported.project.blocks]
    assert len(names) == 12, f"expected 12 on-chip blocks, got {len(names)}: {names}"
    assert sum(n.startswith("streamsplitter") for n in names) == 6, names
    for stage in ("clarketransform", "svpwm", "cordicrotate", "cordicrotate_2",
                  "picontroller", "picontroller_2"):
        assert stage in names, f"{stage} missing from the imported project"
    # the host-side blocks must NOT have been placed
    for host in ("epy_block", "qtgui_time_sink_x"):
        assert host in imported.dropped, f"{host} should be dropped, not placed"


def test_import_carries_each_arm_stream_id_onto_its_net(imported):
    """The stream id has to land on the x16_in -> block net, because that is
    what lets the server resolve each burst to its own block's entry/hop/data
    registers and demux the returned words by tag."""
    sids = {c.stream_id for c in imported.project.connections
            if getattr(c, "stream_id", None)}
    assert sids == {"ia", "ib", "th_park", "e_d", "e_q", "th_ipark"}, sids


def test_import_wires_the_complex_rails_to_the_right_arms(imported):
    """The complex_to_float glue is SPLICED OUT by the importer: Clarke's I/Q
    rails must land on the rotation's x and y, and the inverse rotation's rails
    on SVPWM's v_alpha and v_beta. If this resolved the other way round the
    flowgraph would place into a silently wrong chain."""
    got = {(c.source.block, c.source.port, c.target.block, c.target.port)
           for c in imported.project.connections
           if hasattr(c.source, "block") and hasattr(c.target, "block")}
    for edge in (("clarketransform", "yi", "cordicrotate", "x"),
                 ("clarketransform", "yq", "cordicrotate", "y"),
                 ("cordicrotate_2", "yi", "svpwm", "v_alpha"),
                 ("cordicrotate_2", "yq", "svpwm", "v_beta")):
        assert edge in got, f"missing rail wiring {edge}"


# --------------------------------------------------------------------------- #
#  The CLOSED LOOP, in pure host simulation                                    #
# --------------------------------------------------------------------------- #

def test_stateful_pi_matches_the_block_batch_model():
    """The closed loop CANNOT batch: sample k's error is not known until
    sample k-1's duties have moved the motor. So the integrator has to be
    stepped, and ``StatefulPI`` carries the accumulator across calls.

    This gate proves the transcription is faithful. It matters because the
    failure mode is SILENT: ``PIControllerBlock.process_reference_q15`` resets
    its accumulator on every call, so calling it one sample at a time deletes
    the integral action entirely — the loop runs proportional-only, settles
    short of its reference, and changing ki changes nothing at all."""
    from foc_loop_model import StatefulPI, DEFAULT_PI

    errs = [1000, 0x333, -1500 & 0xFFFF, 300, 0x700, -200 & 0xFFFF,
            20000, -30000 & 0xFFFF, 0, 5] * 5
    StatefulPI.assert_matches_block_model(errs, **DEFAULT_PI)


def test_stateful_pi_actually_integrates():
    """The specific bug the class exists to prevent: under a CONSTANT error
    the command must GROW (that is integral action). A reset-every-call
    integrator gives a constant, which is what makes the bug invisible."""
    from foc_loop_model import StatefulPI, DEFAULT_PI, from_q15

    pi = StatefulPI("q", **DEFAULT_PI)
    out = [from_q15(pi.step(0x1000)) for _ in range(200)]
    assert out[-1] > out[0], "the integrator is not integrating"
    assert len(set(out)) > 10, (
        f"the command barely moves ({len(set(out))} distinct values) — the "
        f"accumulator is being reset")


def test_closed_loop_settles_on_the_host():
    """THE CONTROL GATE: the whole loop, closed around the plant, entirely on
    the host, with every on-chip stage computed by the blocks' own pinned
    integer models.

    MEASURED: i_q reaches 2% of its reference by step 432 and holds; i_d --
    the flux axis, referenced to zero on a surface PMSM -- settles to within
    0.001 of zero. This is the honest verification of the CONTROL LAW that is
    available without the whole loop on an array."""
    from foc_loop_model import run_closed_loop, from_q15

    tr = run_closed_loop(600, 0.30)
    ref = from_q15(tr["ref_q"])
    i_q = [from_q15(w) for w in tr["i_q"]]
    i_d = [from_q15(w) for w in tr["i_d"]]

    assert abs(i_q[-1] - ref) <= 0.02 * abs(ref), (
        f"the q-axis current settled at {i_q[-1]:+.4f}, reference {ref:+.4f}")
    assert abs(i_d[-1]) <= 0.005, (
        f"the d-axis current settled at {i_d[-1]:+.5f}, should be ~0")
    # and it really did have to CONVERGE — a loop that starts at the answer
    # proves nothing about the regulator.
    assert abs(i_q[0] - ref) > 0.5 * abs(ref), (
        "the loop starts at its reference — this gate proves nothing")


def test_closed_loop_settles_against_back_emf():
    """The regulator must reject the back-EMF disturbance, which is what a
    current loop is FOR. At 200 rad/s electrical the machine generates 7 V
    against a 24 V bus, and the loop still lands on its reference."""
    from foc_loop_model import (run_closed_loop, PMSMPlant, MotorParams,
                                from_q15)

    for omega in (0.0, 200.0):
        plant = PMSMPlant(params=MotorParams(l_s=1.5e-3), omega_e=omega)
        tr = run_closed_loop(600, 0.30, plant=plant)
        ref = from_q15(tr["ref_q"])
        got = from_q15(tr["i_q"][-1])
        assert abs(got - ref) <= 0.02 * abs(ref), (
            f"at omega_e={omega} the loop settled at {got:+.4f}, "
            f"reference {ref:+.4f}")


def test_mutation_inverted_park_sign_breaks_the_settle():
    """INV-4: the settle gate must be able to FAIL.

    Flipping the FORWARD Park's sign turns the measurement into a rotation the
    wrong way, which is positive feedback. The loop must then NOT settle on
    its reference. If it still did, the settle gate would be proving nothing
    about the rotation being in the path."""
    import foc_loop_model as M
    from gr_kyttar.placement.blocks.cordic_rotate_block import cordic_rotate_word

    original = M.measurement_half

    def mutated(ia_words, ib_words, theta_words):
        from gr_kyttar.placement.blocks.clarke_transform_block import ClarkeTransformBlock
        ab = ClarkeTransformBlock.process_reference_words(list(ia_words), list(ib_words))
        out = []
        for k in range(len(ab) // 2):
            # sign flipped: +1 instead of the correct -1
            i_d, i_q = cordic_rotate_word(ab[2 * k], ab[2 * k + 1],
                                          theta_words[k], +1)
            out.append((i_d & 0xFFFF, i_q & 0xFFFF))
        return out

    M.measurement_half = mutated
    try:
        tr = M.run_closed_loop(600, 0.30)
        got = M.from_q15(tr["i_q"][-1])
    finally:
        M.measurement_half = original

    ref = M.from_q15(tr["ref_q"])
    assert abs(got - ref) > 0.02 * abs(ref), (
        f"an INVERTED Park rotation still settled on the reference "
        f"({got:+.4f} vs {ref:+.4f}) — the settle gate has no teeth")


# --------------------------------------------------------------------------- #
#  The SHIPPED BLOCK's own closed loop                                         #
# --------------------------------------------------------------------------- #

_BLOCK_LOOP_RUNNER = r"""
import sys
outdir = sys.argv[1]
try:
    from PyQt5 import Qt
    _qapp = Qt.QApplication(sys.argv[:1])
except Exception:
    _qapp = None
sys.path.insert(0, outdir)
import numpy as np
import foc_motor as M

tb = M.foc_motor()
blk = tb.foc_host
N = 600
outs = [np.zeros(N, dtype=np.float32) for _ in range(11)]
blk.work([], outs)
print("RESULT %.6f %.6f %.6f %.6f %d" % (
    outs[6][0], outs[6][-1], outs[7][0], outs[7][-1],
    1 if np.array_equal(outs[2], outs[5]) else 0))
"""


@pytest.mark.skipif(GRCC is None or GR_PY is None, reason="grcc not available")
def test_the_shipped_block_closes_the_loop_and_settles(tmp_path):
    """THE CONTROL GATE, on the block the flowgraph actually ships.

    The host-side settle gate above proves the loop MODEL regulates. This one
    proves the block generated into ``foc_motor.py`` regulates — that moving
    the motor, the error former and the feedback path into one block to break
    the scheduler cycle did not change the control law or the discretization.

    MEASURED, stepping it 600 control periods: i_q climbs from 0.0000 to
    +0.30139 against a 0.30 reference and i_d holds at -0.00073. Those are the
    host golden's own numbers, so the restructuring is behaviour-preserving."""
    r = subprocess.run([GRCC, "-o", str(tmp_path), str(_GRC)],
                       capture_output=True, text=True, env=_GR_ENV, timeout=900)
    assert r.returncode == 0, f"grcc failed:\n{r.stdout[-2000:]}\n{r.stderr[-2000:]}"
    runner = tmp_path / "_loop_runner.py"
    runner.write_text(_BLOCK_LOOP_RUNNER)
    shutil.rmtree(tmp_path / "__pycache__", ignore_errors=True)
    env = dict(_GR_ENV, QT_QPA_PLATFORM="offscreen")
    pr = subprocess.run([GR_PY, str(runner), str(tmp_path)],
                        capture_output=True, text=True, env=env, timeout=600)
    line = [l for l in pr.stdout.splitlines() if l.startswith("RESULT ")]
    assert line, f"the block runner produced no result:\n{pr.stdout}\n{pr.stderr[-3000:]}"
    d0, d1, q0, q1, th_same = line[0].split()[1:]
    d0, d1, q0, q1 = float(d0), float(d1), float(q0), float(q1)
    ref = 0.30

    assert abs(q1 - ref) <= 0.02 * ref, (
        f"the shipped block's q-axis current settled at {q1:+.5f}, "
        f"reference {ref:+.4f}")
    assert abs(d1) <= 0.005, (
        f"the shipped block's d-axis current settled at {d1:+.5f}, should be ~0")
    # It really had to CONVERGE. A block replaying a canned stimulus, or one
    # whose feedback assignment was dropped, would start at the answer or
    # never move — this is the gate that tells those apart.
    assert abs(q0 - ref) > 0.5 * ref, (
        f"the loop starts at its reference (i_q[0]={q0:+.5f}) — it is not "
        f"regulating, it is replaying")
    assert th_same == "1", (
        "theta's two arms differ — they must be the SAME angle delivered "
        "twice, one per rotation")


# --------------------------------------------------------------------------- #
#  The whole loop ON TWO ARRAYS                                                #
# --------------------------------------------------------------------------- #

@pytest.fixture(scope="module")
def two_chip():
    from foc_loop_twochip import run_two_chip
    return run_two_chip(8, verbose=False)


def test_full_loop_runs_across_two_arrays(two_chip):
    """THE WHOLE-LOOP GATE. Measurement half on one array, command half on the
    other, closed around the plant with the host-side error former between.

    This is what the flowgraph describes, actually running."""
    assert two_chip["n_done"] == two_chip["n_want"], (
        f"only {two_chip['n_done']} of {two_chip['n_want']} closed-loop "
        f"iterations completed")


def test_two_array_measurement_half_is_bit_exact(two_chip):
    """Every iteration's (i_d, i_q) off the real chip equals the host golden
    composed from the blocks' own integer models — so the measurement half of
    the flowgraph is PROVEN, not asserted."""
    bad = [r[0] for r in two_chip["rows"] if not r[4]]
    assert not bad, f"measurement half diverged from the golden at iterations {bad}"


def test_two_array_command_half_is_bit_exact(two_chip):
    """Every iteration's duty packet off chip 1 equals the host golden built
    from the same integer models with the SAME live integrators.

    This is separate from the rate gate on purpose: a corrupted arithmetic
    constant changes the DUTIES while leaving the timing untouched, so a
    timing gate alone would not notice it."""
    bad = [r[0] for r in two_chip["rows"] if not r[7]]
    assert not bad, f"command half diverged from the golden at iterations {bad}"


def test_two_array_runs_all_settle_queue_empty(two_chip):
    """INV-56: read stop_reason for EVERY run, on BOTH arrays."""
    assert two_chip["meas_stops"] == {"QueueEmpty"}, two_chip["meas_stops"]
    assert two_chip["cmd_stops"] == {"QueueEmpty"}, two_chip["cmd_stops"]


def test_two_array_loop_rate(two_chip):
    """THE RATE ANSWER, measured rather than argued.

    MEASURED (simKYT's timing model): the measurement half sustains a
    13,142.7 ns interval and the command half 17,940.5 ns. The two are
    strictly SERIAL within a control period -- sample k's duties cannot be
    computed until sample k's currents have been measured and rotated -- so
    the full loop costs their SUM, 31,083.2 ns, i.e. 32.17 kHz. A chip
    crossing adds ~40 ns, negligible against a ~31 us period.

    Bands are wide enough not to flap on build-to-build jitter while still
    catching a real regression."""
    m = two_chip["meas_interval"]
    c = two_chip["cmd_interval"]
    loop = two_chip["loop_interval"]
    assert m is not None and c is not None
    assert 9_000.0 <= m <= 18_000.0, (
        f"measurement-half interval {m:,.1f} ns outside the band "
        f"(measured 13,142.7 ns)")
    assert 12_000.0 <= c <= 22_500.0, (
        f"command-half interval {c:,.1f} ns outside the band "
        f"(measured 17,940.5 ns)")
    assert 22_000.0 <= loop <= 40_000.0, (
        f"full-loop interval {loop:,.1f} ns outside the band "
        f"(measured 31,083.2 ns = 32.17 kHz)")
    # the serial-sum claim itself
    assert abs(loop - (m + c)) < 1e-6


def test_two_array_halves_fit_their_arrays(two_chip):
    """Each half routes and builds comfortably on its own array — 75 and 87
    cells of 120. It is the WHOLE loop on ONE array that does not fit, and
    that limit is corridor/arm budget rather than cells (INV-71)."""
    assert two_chip["meas_cells"] <= 120, two_chip["meas_cells"]
    assert two_chip["cmd_cells"] <= 120, two_chip["cmd_cells"]
    assert two_chip["meas_cells"] + two_chip["cmd_cells"] > 120, (
        "the two halves would fit one array by CELL count — the documented "
        "limit is arms, not cells; re-read INV-71 before changing this")
