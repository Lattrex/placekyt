# SPDX-License-Identifier: GPL-3.0-or-later
"""session_report — a verification report is EVIDENCE, never a literal.

INV-38. A `verification/reports/<Block>.json` file is the artifact the dashboard
reads as "this block was verified against GNU Radio". It must therefore be a
FUNCTION OF THE SESSION THAT PRODUCED IT, and its ABSENCE must be the safe state.

Two failure modes this module exists to make impossible:

1. **The literal.** A writer that emits ``"passed": True`` regardless of what the
   session actually did. A run with a FAILING gate still leaves a green report and
   the dashboard reads a pass that never happened. This is the single worst defect
   class this project recognizes — it is the "make the gate look green" failure the
   whole verification harness exists to eliminate.

2. **The stale green.** A writer that emits a report derived from ONE comparison
   while OTHER gates in the same file (mutation / orientation / saturation) failed.
   pytest continues past a failure by default, so a later writer test still runs and
   still writes. Worse: a report written by an earlier, passing session survives on
   disk when a later session crashes, is killed, or fails — so the file says
   "verified" about a state of the code that was never verified.

The mechanism, applied uniformly by :func:`write_session_report`:

  * **Unlink first.** Any existing report for the block is deleted at writer entry,
    BEFORE the verdict is decided. A session that dies at any point after that
    leaves NO file, and the dashboard reads absence as "not verified" — which is
    true — rather than a stale green.
  * **Write only on a clean session.** The write happens only when pytest's own
    per-test outcome record for this session shows zero failures AND zero errors.
    The verdict is read from the test runner, never from bookkeeping the writing
    module keeps about itself.
  * **Fail loudly otherwise.** If anything failed, the writer itself FAILS and names
    the offending gates, so a report-less run is never mistaken for a silent skip.

Robustness requirements (all covered by ``test_report_provenance.py``):

  * Works under ``-p no:randomly`` (ordering is irrelevant — the gate reads the
    outcomes accumulated so far, and the writer is named to sort last).
  * Works under ``-x`` (the session stops at the first failure, so the writer never
    runs at all and, having never unlinked-then-written, leaves no new file; a
    PREVIOUS file is not refreshed, and the provenance audit treats a report older
    than its suite as unproven).
  * Works under parallel invocation: the session record lives on the pytest
    ``Config`` object of the process that is running, not in a module-level global
    that a crashed run could leave stale. Each worker sees only its own outcomes.
  * Never depends on a global mutable that survives a process: the record is created
    fresh per session by the conftest plugin and is empty if the plugin is absent
    (in which case the writer refuses, rather than assuming success).

This module changes only WHETHER and WHEN a report is written. It asserts nothing
about the DUT and weakens no gate.
"""

from __future__ import annotations

import json
from pathlib import Path

#: Attribute name under which the conftest plugin stashes this session's record on
#: the pytest ``Config`` object. Config is per-process and per-session, so this is
#: safe under xdist / parallel invocation and cannot go stale across processes.
SESSION_RECORD_ATTR = "_kyttar_session_outcomes"


def reports_dir(explicit: str | Path | None = None) -> Path:
    """The directory reports live in (``verification/reports`` by default)."""
    if explicit is not None:
        return Path(explicit)
    return Path(__file__).resolve().parents[1] / "reports"


def report_path(kyttar_block: str, explicit: str | Path | None = None) -> Path:
    """Absolute path of one block's report JSON."""
    return reports_dir(explicit) / f"{kyttar_block}.json"


class NoSessionError(RuntimeError):
    """Raised when the session's outcome record cannot be found.

    A writer that cannot see what the session did MUST NOT write. There is no
    "assume it passed" path — that assumption is exactly the defect."""


#: Single-slot holder for the pytest ``Config`` of the session running in THIS
#: process. Set by the conftest plugin at session start (``pytest_configure``) and
#: CLEARED at session finish (``pytest_unconfigure``).
#:
#: It deliberately holds ONLY the live Config and never a cache of results: every
#: read of the outcomes goes through that Config, whose lifetime is the session's.
#: Between sessions this list is empty and :func:`session_failures` raises — so a
#: writer running outside a session, or after one ended, has nothing to mistake for
#: evidence. Being process-local, it is also invisible to a parallel invocation.
_RECORDING_CONFIG: list = []


def _live_config():
    """The pytest ``Config`` of the session running in this process, or ``None``."""
    return _RECORDING_CONFIG[0] if _RECORDING_CONFIG else None


def _set_live_config(config) -> None:
    """Called by the conftest plugin at session start."""
    _RECORDING_CONFIG[:] = [config]
    setattr(config, SESSION_RECORD_ATTR, {})


def _clear_live_config() -> None:
    """Called by the conftest plugin at session finish."""
    _RECORDING_CONFIG.clear()


def record_outcome(config, nodeid: str, outcome: str) -> None:
    """Record one test's terminal outcome on the session's Config.

    ``outcome`` is pytest's own report outcome for a non-passing call/setup/
    teardown phase: ``"failed"`` (assertion or exception in the test) or
    ``"error"`` (setup/teardown blew up)."""
    rec = getattr(config, SESSION_RECORD_ATTR, None)
    if rec is None:
        rec = {}
        setattr(config, SESSION_RECORD_ATTR, rec)
    rec[nodeid] = outcome


def session_failures(scope_file: str | None = None) -> list[str]:
    """Node ids that FAILED or ERRORED in this session, so far.

    ``scope_file`` restricts the answer to failures in ONE test file (matched
    against the node id's path part). That is what a report writer asks: "did
    MY OWN suite fail?" — see :func:`write_session_report` for why the
    session-wide question is the wrong one.

    Raises :class:`NoSessionError` if there is no live session to ask — a writer
    invoked outside pytest, or with the conftest plugin missing, has no evidence
    and must refuse."""
    config = _live_config()
    if config is None:
        raise NoSessionError(
            "no live pytest session — a verification report may only be written "
            "as an artifact of a session whose outcomes can be read")
    rec = getattr(config, SESSION_RECORD_ATTR, None)
    if rec is None:
        raise NoSessionError(
            "the session outcome record is missing (verification/tests/conftest.py "
            "not loaded?) — refusing to write a report on unknown evidence")
    if scope_file is None:
        return sorted(rec)
    # Node ids are "path/to/test_x.py::test_name[param]" — compare the file part
    # by BASENAME so an absolute/relative path difference cannot silently widen
    # the scope back to the whole session.
    import os

    want = os.path.basename(str(scope_file))
    return sorted(n for n in rec
                  if os.path.basename(str(n).split("::", 1)[0]) == want)


def _caller_test_file() -> str | None:
    """Basename of the nearest ``test_*.py`` up the call stack, or ``None``.

    Walks OUT from this module to find the test file that invoked the writer, so
    the failure scope is the caller's own suite. Returns ``None`` when no test
    file is on the stack (a non-pytest caller), in which case the writer falls
    back to the session-wide question — strictly the safer direction."""
    import inspect
    import os

    for frame in inspect.stack():
        base = os.path.basename(frame.filename)
        if base.startswith("test_") and base.endswith(".py"):
            return base
    return None


def write_session_report(kyttar_block: str, payload: dict, *,
                         verdict: bool = True,
                         reports_dir: str | Path | None = None) -> Path:
    """Write ``verification/reports/<kyttar_block>.json`` — IF the session earned it.

    ``payload`` is the report body the caller assembled from MEASURED results. The
    caller supplies the metrics; this function supplies the PROVENANCE, and it is
    the only thing that decides whether a file appears on disk.

    Order of operations is load-bearing:

      1. Unlink any existing report for this block. From here on, absence is the
         state of the world unless this call completes — a crash, a kill, or a
         later failure all leave no file.
      2. Ask the session what happened. No live session, or a session with any
         failure or error, means NO WRITE.
      3. Only then write, stamping the payload with the provenance that lets an
         auditor tell a real artifact from a literal.

    ``verdict=False`` records a QUARANTINE: a block that does NOT work, whose suite
    nevertheless passes because it asserts the documented failure. The session gate
    still applies in full — a quarantine record is only written by a clean session,
    so ``verdict`` can only make the record worse than the session, never better.

    Raises ``AssertionError`` (so pytest reports it as a normal test failure) when
    the session did not earn a report. Never returns without having written."""
    out = report_path(kyttar_block, reports_dir)

    # (1) ABSENCE IS THE SAFE STATE. Delete first, unconditionally, before the
    #     verdict is even known — nothing downstream can resurrect a stale green.
    if out.exists():
        out.unlink()

    # (2) The verdict comes from the test runner, not from this module — and the
    #     question is "did MY OWN suite fail?", NOT "did anything anywhere fail?".
    #
    #     THE SESSION-WIDE QUESTION WAS WRONG, and destructively so. A writer runs
    #     `unlink` first (step 1) and then refuses to rewrite if the session has
    #     ANY failure. So one failing gate — in a DIFFERENT block, possibly hours
    #     earlier — deleted the evidence for every block whose writer sorted after
    #     it. Measured repeatedly on this repo: a single early failure destroyed
    #     ~57 of 118 reports in one full-suite run, and recovery was ~56 individual
    #     suite re-runs. It also made the suite NON-IDEMPOTENT (two identical runs
    #     gave 14 and 60 failures) because the failure count depended on how many
    #     reports happened to exist when the run started, which made diagnosing
    #     anything downstream unreliable.
    #
    #     Scoping to the caller's own file keeps INV-38's guarantee EXACTLY: a
    #     report still cannot claim a pass its OWN tests did not earn, which is the
    #     property that matters (the hardcoded `"passed": True` class of defect is
    #     still impossible). What it drops is the part that was never evidence
    #     about this block at all — another block's failure.
    caller_file = _caller_test_file()
    try:
        failures = session_failures(caller_file)
    except NoSessionError as exc:
        raise AssertionError(
            f"NOT writing verification/reports/{kyttar_block}.json — {exc}") from None
    if failures:
        raise AssertionError(
            f"NOT writing verification/reports/{kyttar_block}.json — "
            f"{len(failures)} gate(s) failed in "
            f"{caller_file or 'this suite'}: "
            + ", ".join(f.split("::")[-1] for f in failures))

    # A payload that asserts its own pass without a measurement is the literal this
    # module exists to forbid. The verdict below is the session's, not the caller's.
    body = {"kyttar_block": kyttar_block,
            # ``verdict`` may only make the record WORSE than the session, never
            # better: reaching this line already proves the session was clean, and
            # a False verdict is a deliberate QUARANTINE record (the block does not
            # work; the suite passes because it asserts the documented failure).
            "passed": bool(verdict)}
    body.update(payload)
    body.pop("provenance", None)
    # Marks this file as an artifact of a session, distinguishable by an auditor
    # from a report a hardcoding writer could have produced.
    body["provenance"] = "session"

    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(body, indent=2) + "\n")
    return out
