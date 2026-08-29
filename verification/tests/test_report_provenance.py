# SPDX-License-Identifier: GPL-3.0-or-later
"""INV-38 — a verification report is an ARTIFACT of a verified session.

Two gates live here, and between them this defect class cannot come back:

  * **The MECHANISM gate** (§1). Runs a real report writer inside a real pytest
    session that also contains a synthetic FAILING test, and asserts that NO report
    file is produced and that the writer itself fails. Also proves the unlink-first
    property (a pre-existing green report does not survive a failing session), the
    ``-x`` and ``-p no:randomly`` behaviours, and the refusal to write outside a
    session. This is the INV-4 failing-mutation gate applied to the report writer:
    a writer never shown to REFUSE certifies nothing.

  * **The GUARD gate** (§2). AST-scans every file in ``verification/tests/`` for a
    writer that emits a report with a hardcoded pass, and FAILS if a new one
    appears. The mechanism above is only durable if nobody can hand-roll around it.

The origin of the rule: a block's ``test_zz_write_report`` hardcoded
``"passed": true`` in its payload, so a session whose saturated-drive gate FAILED
still wrote a green ``verification/reports/<Block>.json``. The dashboard would have
read a pass that never happened.

Neither gate here asserts anything about any DUT, and neither weakens any existing
gate — they constrain only WHETHER and WHEN a report file is written."""

from __future__ import annotations

import ast
import json
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

_VERIFY = Path(__file__).resolve().parents[1]
_TESTS = _VERIFY / "tests"
_ROOT = _VERIFY.parent
if str(_VERIFY) not in sys.path:
    sys.path.insert(0, str(_VERIFY))

from kyttar_verify import session_report as sr  # noqa: E402


# =============================================================================
# 1. THE MECHANISM GATE — prove the writer REFUSES on a failing session
# =============================================================================
#
# These run pytest as a CHILD PROCESS on a temporary suite. A child process is the
# only honest way to test "what does the writer do when the session fails": the
# failing test has to be real, and it has to fail in the session the writer reads.

_SUB_CONFTEST = """
import sys
from pathlib import Path
_VERIFY = Path(r"{verify}")
if str(_VERIFY) not in sys.path:
    sys.path.insert(0, str(_VERIFY))
from kyttar_verify import session_report as _sr

def pytest_configure(config):
    _sr._set_live_config(config)

def pytest_unconfigure(config):
    _sr._clear_live_config()

def pytest_runtest_logreport(report):
    config = _sr._live_config()
    if config is None:
        return
    if report.failed:
        _sr.record_outcome(config, report.nodeid,
                           "failed" if report.when == "call" else "error")
"""

_SUB_SUITE = """
import sys
from pathlib import Path
_VERIFY = Path(r"{verify}")
if str(_VERIFY) not in sys.path:
    sys.path.insert(0, str(_VERIFY))
from kyttar_verify import session_report as sr

REPORTS = Path(r"{reports}")

def test_a_a_synthetic_gate():
    # The synthetic FAILING gate. Stands in for a real gate (a mutation gate, a
    # saturated-drive gate) that catches a genuine defect.
    assert {gate_passes}, "synthetic gate failed on purpose"

def test_zz_write_report():
    sr.write_session_report("ProvenanceProbeBlock",
                            {{"metric": "exact", "n_compared": 8}},
                            reports_dir=str(REPORTS))
"""


def _run_subsession(tmp_path, *, gate_passes: bool, extra_args=(),
                    preseed: bool = False):
    """Run a two-test suite in a child pytest and report what it left on disk."""
    # Unique per call so one test can run several sub-sessions.
    base = tmp_path / f"run{len(list(tmp_path.iterdir()))}"
    suite = base / "suite"
    suite.mkdir(parents=True)
    reports = base / "reports"
    reports.mkdir()
    out = reports / "ProvenanceProbeBlock.json"
    if preseed:
        # A green report left behind by an EARLIER, passing session. The unlink-first
        # rule says a failing session must not let it survive.
        out.write_text(json.dumps({"kyttar_block": "ProvenanceProbeBlock",
                                   "passed": True, "provenance": "stale"}) + "\n")

    (suite / "conftest.py").write_text(_SUB_CONFTEST.format(verify=_VERIFY))
    (suite / "test_probe.py").write_text(_SUB_SUITE.format(
        verify=_VERIFY, reports=reports, gate_passes=bool(gate_passes)))

    proc = subprocess.run(
        [sys.executable, "-m", "pytest", str(suite), "-p", "no:cacheprovider",
         "-q", *extra_args],
        capture_output=True, text=True, cwd=str(base))
    return proc, out


def test_clean_session_writes_the_report(tmp_path):
    """CONTROL. With every gate green, the writer DOES produce the report.

    Without this, "no file" would be trivially achievable by a writer that never
    writes at all — the refusal below has to be discriminating."""
    proc, out = _run_subsession(tmp_path, gate_passes=True)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert out.exists(), "a clean session must produce its report"
    rec = json.loads(out.read_text())
    assert rec["passed"] is True
    assert rec["provenance"] == "session"
    assert rec["kyttar_block"] == "ProvenanceProbeBlock"


def test_failing_session_writes_no_report_and_the_writer_fails(tmp_path):
    """THE GATE. A session containing a FAILING test leaves NO report behind, and
    the writer itself fails rather than skipping quietly.

    This is the exact scenario that produced the defect: pytest continues past a
    failure by default, so the later writer test still runs. It must refuse."""
    proc, out = _run_subsession(tmp_path, gate_passes=False)
    assert proc.returncode != 0, "the session must not report success"
    assert not out.exists(), (
        "A FAILING session produced a report file — this is the false-green "
        "defect INV-38 exists to prevent.\n" + proc.stdout + proc.stderr)
    # And the refusal is loud: the writer failed, naming the gate that failed.
    assert "2 failed" in proc.stdout, proc.stdout
    assert "NOT writing" in proc.stdout, proc.stdout
    assert "test_a_a_synthetic_gate" in proc.stdout, proc.stdout


def test_failing_session_deletes_a_pre_existing_green_report(tmp_path):
    """UNLINK-FIRST. Absence is the safe state: a green report left by an earlier
    passing session does NOT survive a session that fails."""
    proc, out = _run_subsession(tmp_path, gate_passes=False, preseed=True)
    assert proc.returncode != 0
    assert not out.exists(), (
        "a stale green report survived a failing session — the dashboard would "
        "still read a pass that this run disproved")


def test_writer_is_robust_under_no_randomly(tmp_path):
    """The gate reads accumulated outcomes, so test ORDER cannot defeat it."""
    proc, out = _run_subsession(tmp_path, gate_passes=False,
                                extra_args=("-p", "no:randomly"))
    assert proc.returncode != 0
    assert not out.exists()

    proc, out = _run_subsession(tmp_path, gate_passes=True,
                                extra_args=("-p", "no:randomly"))
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert out.exists()


def test_writer_is_robust_under_dash_x(tmp_path):
    """Under ``-x`` the session stops at the first failure, so the writer never runs
    — and having never run, it produces no NEW file."""
    proc, out = _run_subsession(tmp_path, gate_passes=False, extra_args=("-x",))
    assert proc.returncode != 0
    assert not out.exists(), "an aborted session must not produce a report"


def test_dash_x_leaves_a_pre_existing_report_UNTOUCHED(tmp_path):
    """A DOCUMENTED LIMIT, gated so it cannot be forgotten.

    Unlink-first happens INSIDE the writer. Under ``-x`` the session aborts at the
    first failure, so the writer never runs and therefore never unlinks — a green
    report from an EARLIER session survives a ``-x`` run that failed.

    This is not a hole in the mechanism; it is the boundary of what a
    single-process mechanism can promise, and it is exactly why the provenance
    audit treats a report as evidence only when its own suite has been re-run to
    completion. A full run (no ``-x``) DOES clear it — see
    ``test_failing_session_deletes_a_pre_existing_green_report``.

    Stated plainly rather than papered over: with ``-x``, absence is still the safe
    state for anything this session would have WRITTEN, but a stale file from a
    previous session is outside this session's reach."""
    proc, out = _run_subsession(tmp_path, gate_passes=False, preseed=True,
                                extra_args=("-x",))
    assert proc.returncode != 0
    assert out.exists(), (
        "behaviour changed: if -x now clears a pre-existing report, that is an "
        "improvement — update this gate and the INV-38 note rather than deleting it")
    assert json.loads(out.read_text())["provenance"] == "stale", \
        "the surviving file must be the ORIGINAL, not one this session wrote"


def test_parallel_invocations_do_not_share_state(tmp_path):
    """Two concurrent sessions in separate processes each see only their own
    outcomes — the record lives on the per-process pytest Config, never in a
    module global that one process could poison for another."""
    import concurrent.futures as cf

    def run(passes, sub):
        d = tmp_path / sub
        d.mkdir()
        return _run_subsession(d, gate_passes=passes)

    with cf.ThreadPoolExecutor(max_workers=2) as ex:
        fut_ok = ex.submit(run, True, "ok")
        fut_bad = ex.submit(run, False, "bad")
        proc_ok, out_ok = fut_ok.result()
        proc_bad, out_bad = fut_bad.result()

    assert proc_ok.returncode == 0 and out_ok.exists(), \
        "the clean parallel session was poisoned by the failing one"
    assert proc_bad.returncode != 0 and not out_bad.exists(), \
        "the failing parallel session wrote a report"


def test_writer_refuses_outside_a_session(tmp_path):
    """No live session means no evidence, and no evidence means no write. There is
    deliberately no 'assume it passed' path."""
    prev = list(sr._RECORDING_CONFIG)
    sr._RECORDING_CONFIG.clear()
    try:
        with pytest.raises(AssertionError, match="no live pytest session"):
            sr.write_session_report("ProvenanceProbeBlock", {"metric": "exact"},
                                    reports_dir=str(tmp_path))
    finally:
        sr._RECORDING_CONFIG[:] = prev
    assert not (tmp_path / "ProvenanceProbeBlock.json").exists()


def test_write_report_refuses_a_failed_comparison(tmp_path):
    """The shared ``write_report`` refuses a CompareResult that did not pass, and
    deletes any earlier report for that block — a measured failure invalidates the
    previous artifact."""
    from kyttar_verify import CompareResult, Metric, write_report

    stale = tmp_path / "ProvenanceProbeBlock.json"
    stale.write_text(json.dumps({"passed": True}) + "\n")
    bad = CompareResult(passed=False, metric=Metric.EXACT, n_compared=4,
                        reason="synthetic mismatch")
    with pytest.raises(AssertionError, match="did not pass"):
        write_report("ProvenanceProbeBlock", bad, reports_dir=str(tmp_path))
    assert not stale.exists(), "a failed comparison left its old report in place"


# =============================================================================
# 2. THE GUARD GATE — this defect class cannot be reintroduced
# =============================================================================
#
# The mechanism above is only durable if nobody hand-rolls around it. This scanner
# reads every file in verification/tests/ and fails on either shape of the defect:
#
#   (i)  a HAND-ROLLED WRITE: a function that writes a file into verification/
#        reports/ without going through write_report / write_session_report. Such a
#        writer bypasses the unlink-first rule and the session gate entirely.
#   (ii) a HARDCODED VERDICT: a report payload that carries a literal
#        ``"passed": True``. The verdict belongs to the session, not the author.
#
# ``test_report_provenance.py`` itself is exempt: its literals are deliberate
# fixtures (a stale-report seed and a synthetic sub-suite), which is precisely what
# the scanner is built to recognise as NOT a real writer.

_GUARD_EXEMPT = {"test_report_provenance.py"}

#: The only sanctioned ways to produce a report file.
_SANCTIONED = {"write_report", "write_session_report"}


def _writes_a_report_path(func: ast.AST, src: str) -> bool:
    """True if this function builds a path under ``reports/`` AND writes to it."""
    seg = ast.get_source_segment(src, func) or ""
    if "reports" not in seg:
        return False
    for node in ast.walk(func):
        if not isinstance(node, ast.Call):
            continue
        fn = node.func
        if isinstance(fn, ast.Attribute) and fn.attr in ("write_text", "write_bytes"):
            return True
        if (isinstance(fn, ast.Attribute) and fn.attr == "dump"
                and isinstance(fn.value, ast.Name) and fn.value.id == "json"):
            return True
    return False


def _hardcoded_pass_literal(func: ast.AST) -> bool:
    """True if a dict literal in this function maps a "passed" key to True.

    Matching the AST rather than the text means a reformat, a rename of the dict, or
    a different quote style cannot slip past."""
    for node in ast.walk(func):
        if not isinstance(node, ast.Dict):
            continue
        for key, val in zip(node.keys, node.values):
            if (isinstance(key, ast.Constant) and key.value == "passed"
                    and isinstance(val, ast.Constant) and val.value is True):
                return True
    return False


def _fabricated_compare_result(func: ast.AST) -> bool:
    """True if this function builds ``CompareResult(passed=True, ...)`` as a literal.

    Same defect wearing the harness's own type: the verdict a report carries must be
    MEASURED (``res.passed`` from a real comparison, or a variable derived from one),
    never typed in by the author. This is how the fabricated-result shape hides —
    ``write_report`` looks correct at the call site while the result it is handed was
    invented.

    A ``CompareResult(passed=<expression>)`` — e.g. ``passed=(agreement == 1.0)`` —
    is fine: that verdict is derived from a measurement."""
    for node in ast.walk(func):
        if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
                and node.func.id == "CompareResult"):
            continue
        for kw in node.keywords:
            if (kw.arg == "passed" and isinstance(kw.value, ast.Constant)
                    and kw.value.value is True):
                return True
        # positional first arg is `passed`
        if node.args and isinstance(node.args[0], ast.Constant) \
                and node.args[0].value is True:
            return True
    return False


def _report_writer_functions(path: Path):
    """Yield (funcname, node, tree_src) for every function that emits a report."""
    src = path.read_text()
    tree = ast.parse(src)
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        # Sanctioned either as a bare name (`write_report(...)`) or through a module
        # alias (`sr.write_session_report(...)`).
        calls_sanctioned = any(
            isinstance(c, ast.Call)
            and ((isinstance(c.func, ast.Name) and c.func.id in _SANCTIONED)
                 or (isinstance(c.func, ast.Attribute) and c.func.attr in _SANCTIONED))
            for c in ast.walk(node))
        hand_rolled = _writes_a_report_path(node, src)
        if calls_sanctioned or hand_rolled:
            yield node.name, node, src, calls_sanctioned, hand_rolled


def scan_for_hardcoded_report_writers(tests_dir: Path) -> list[str]:
    """The guard, as a plain function so a test can point it at a fixture tree."""
    findings = []
    for path in sorted(tests_dir.glob("test_*.py")):
        if path.name in _GUARD_EXEMPT:
            continue
        for name, node, src, sanctioned, hand_rolled in _report_writer_functions(path):
            if hand_rolled and not sanctioned:
                findings.append(
                    f"{path.name}::{name} (line {node.lineno}) writes a report file "
                    f"directly — it must go through write_session_report, which "
                    f"unlinks first and writes only on a clean session")
            if _hardcoded_pass_literal(node):
                findings.append(
                    f"{path.name}::{name} (line {node.lineno}) hardcodes "
                    f'\'"passed": True\' in a report payload — the verdict must come '
                    f"from the session, never from a literal")
            if _fabricated_compare_result(node):
                findings.append(
                    f"{path.name}::{name} (line {node.lineno}) builds "
                    f"CompareResult(passed=True) as a literal — the verdict must be "
                    f"MEASURED (res.passed from a real comparison, or a variable "
                    f"derived from one), never typed in")
    return findings


def test_no_hardcoded_pass_report_writers_in_the_suite():
    """THE GUARD. No test in verification/tests/ may hand-roll a report write or
    hardcode its own pass verdict.

    A failure here names the file and function. The fix is never to silence this
    test — it is to route the writer through ``write_session_report``."""
    findings = scan_for_hardcoded_report_writers(_TESTS)
    assert not findings, (
        "hardcoded-pass / hand-rolled report writers found (INV-38):\n  - "
        + "\n  - ".join(findings))


def test_the_guard_itself_detects_a_reintroduced_instance(tmp_path):
    """INV-4 TEETH. Deliberately reintroduce both shapes of the defect in a fixture
    tree and prove the guard FAILS on each. A guard never shown to fire certifies
    nothing — that is the whole lesson of this invariant."""
    fixture = tmp_path / "tests"
    fixture.mkdir()

    # Shape (i): a hand-rolled write straight into reports/.
    (fixture / "test_handrolled.py").write_text(textwrap.dedent("""
        import json
        from pathlib import Path
        _VERIFY = Path(".")

        def test_emit_report():
            report = {"metric": "exact", "n_compared": 8}
            (_VERIFY / "reports").mkdir(exist_ok=True)
            (_VERIFY / "reports" / "SneakyBlock.json").write_text(json.dumps(report))
    """))
    findings = scan_for_hardcoded_report_writers(fixture)
    assert any("test_handrolled.py::test_emit_report" in f for f in findings), \
        f"the guard MISSED a hand-rolled report write: {findings}"

    # Shape (ii): the sanctioned helper, but with a hardcoded verdict smuggled in.
    (fixture / "test_handrolled.py").unlink()
    (fixture / "test_literal.py").write_text(textwrap.dedent("""
        from kyttar_verify import write_session_report

        def test_emit_report():
            report = {"passed": True, "metric": "exact"}
            write_session_report("SneakyBlock", report)
    """))
    findings = scan_for_hardcoded_report_writers(fixture)
    assert any("test_literal.py::test_emit_report" in f for f in findings), \
        f"the guard MISSED a hardcoded pass literal: {findings}"

    # Shape (iii): the verdict fabricated inside the harness's own result type.
    (fixture / "test_literal.py").unlink()
    (fixture / "test_fabricated.py").write_text(textwrap.dedent("""
        from kyttar_verify import write_report, CompareResult, Metric

        def test_emit_report():
            res = CompareResult(passed=True, metric=Metric.EXACT, n_compared=8)
            write_report("SneakyBlock", res, coverage={"edge": True})
    """))
    findings = scan_for_hardcoded_report_writers(fixture)
    assert any("test_fabricated.py::test_emit_report" in f for f in findings), \
        f"the guard MISSED a fabricated CompareResult verdict: {findings}"

    # NEGATIVE CONTROL: the correct shape must NOT trip the guard, or it would be
    # a guard that fails on everything and therefore proves nothing. Both a plain
    # payload AND a DERIVED CompareResult verdict must pass clean.
    (fixture / "test_fabricated.py").unlink()
    (fixture / "test_derived.py").write_text(textwrap.dedent("""
        from kyttar_verify import write_report, CompareResult, Metric

        def test_emit_report():
            mismatches = _measure()
            res = CompareResult(passed=(mismatches == 0), metric=Metric.EXACT,
                                n_compared=8, bit_errors=mismatches)
            write_report("HonestBlock", res, coverage={"edge": True})
    """))
    assert scan_for_hardcoded_report_writers(fixture) == [], \
        "the guard fires on a DERIVED verdict — it must allow the correct shape"
    (fixture / "test_derived.py").unlink()
    (fixture / "test_correct.py").write_text(textwrap.dedent("""
        from kyttar_verify import write_session_report

        def test_emit_report():
            report = {"metric": "exact", "n_compared": 8}
            write_session_report("HonestBlock", report)
    """))
    assert scan_for_hardcoded_report_writers(fixture) == [], \
        "the guard fires on a CORRECT writer — it would be un-actionable noise"


# ---------------------------------------------------------------------------
# The failure SCOPE is the writer's own suite, not the whole session.
# ---------------------------------------------------------------------------
def test_failure_scope_is_the_writers_own_file_not_the_session():
    """``session_failures(scope_file=...)`` must report ONLY that file's failures.

    INV-38 originally asked "did ANYTHING fail in this session?" before writing.
    Combined with its unlink-first rule that made one failing gate destroy the
    evidence for every block whose writer sorted after it — measured on this repo
    at ~57 of 118 reports lost in a single full-suite run, with recovery costing
    ~56 individual suite re-runs. It also made the suite NON-IDEMPOTENT: two
    identical invocations reported 14 and 60 failures, because the count depended
    on how many reports happened to exist when the run started.

    The guarantee that matters is unchanged and is asserted by the sibling tests
    above: a report may not claim a pass its OWN tests did not earn. This test
    pins the other half — that a DIFFERENT block's failure is not evidence about
    this block and must not be treated as such.
    """
    from kyttar_verify import session_report as sr

    class _Cfg:
        pass

    cfg = _Cfg()
    setattr(cfg, sr.SESSION_RECORD_ATTR, {
        "verification/tests/test_mine.py::test_a": "failed",
        "verification/tests/test_other.py::test_b": "failed",
        "/abs/path/verification/tests/test_other.py::test_c": "error",
    })
    sr._RECORDING_CONFIG[:] = [cfg]
    try:
        mine = sr.session_failures("test_mine.py")
        other = sr.session_failures("test_other.py")
        every = sr.session_failures()

        assert len(mine) == 1 and mine[0].endswith("::test_a"), \
            f"scoping to test_mine.py must see ONLY its own failure, got {mine}"
        # Absolute vs relative node-id paths must not widen the scope back out.
        assert len(other) == 2, \
            f"basename matching must catch both path forms, got {other}"
        assert len(every) == 3, "unscoped must still see the whole session"
    finally:
        sr._RECORDING_CONFIG.clear()
