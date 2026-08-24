# SPDX-License-Identifier: GPL-3.0-or-later
"""Session-outcome recorder for the verification suites (INV-36).

A `verification/reports/<Block>.json` file means "this block was verified". That
claim is only true if the session that wrote it actually passed, so the writers in
`kyttar_verify.session_report` refuse to write unless they can READ the session's
real outcomes. This plugin is what makes those outcomes readable.

It records, on the pytest ``Config`` of the running process, the node id of every
test whose call/setup/teardown phase FAILED or ERRORED. Config is per-process and
per-session: nothing here is a module-level mutable that a crashed run could leave
stale, and a parallel invocation sees only its own worker's outcomes.

This plugin asserts nothing and changes no gate — it only observes."""

from __future__ import annotations

import sys
from pathlib import Path

_VERIFY = Path(__file__).resolve().parents[1]
if str(_VERIFY) not in sys.path:
    sys.path.insert(0, str(_VERIFY))

from kyttar_verify import session_report as _sr  # noqa: E402


def pytest_configure(config):
    """Open a fresh, empty outcome record for this session."""
    _sr._set_live_config(config)


def pytest_unconfigure(config):
    """Close it. Nothing survives the process."""
    _sr._clear_live_config()


def pytest_runtest_logreport(report):
    """Record any non-passing terminal outcome, in any phase."""
    config = _sr._live_config()
    if config is None:
        return
    if report.failed:
        # A failure in setup/teardown is pytest's "error"; in the call phase it is
        # a "failure". Both mean the session did not cleanly pass.
        outcome = "failed" if report.when == "call" else "error"
        _sr.record_outcome(config, report.nodeid, outcome)
