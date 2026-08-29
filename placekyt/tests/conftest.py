# SPDX-License-Identifier: GPL-3.0-or-later
"""Pytest config: make the placeKYT package root importable.

The flat layout puts ``model/`` directly under the
project root. Adding that root to ``sys.path`` lets tests do ``import model``
without an installed package, matching the §11.2 ``--cov=placekyt/model`` paths.
"""

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent          # the placekyt/ package root
REPO = ROOT.parent                                     # the repository root

if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# Run Qt tests headless by default so `pytest tests/` works without a display
# (CI / SSH). An explicit QT_QPA_PLATFORM still wins.
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")


# --- Shared resource locations (resolved from the repo, never hardcoded) -----
#
# Tests must run from a fresh clone in any directory, so these resolve to
# in-repo files (with an environment override each). The GNU Radio out-of-tree
# module sits beside placekyt/ as ``gr-kyttar/``.

# The demo chip type (10x12 array). Ships in the placeKYT resources.
CHIP_YAML = Path(os.environ.get(
    "KYTTAR_CHIP_YAML",
    ROOT / "resources" / "chips" / "kyttar_10x12.yaml",
))

# Bundled demo .kyt / golden / stimulus fixtures.
DEMO_DIR = Path(os.environ.get("KYTTAR_DEMO_DIR", ROOT / "tests" / "data" / "demo"))

# The GNU Radio out-of-tree module (the kyttar python package).
GR_KYTTAR = Path(os.environ.get("KYTTAR_GR_DIR", REPO / "gr-kyttar"))
GR_KYTTAR_PY = GR_KYTTAR / "python" / "kyttar"

# GRC flowgraph fixtures for the import/routing tests. These are test inputs, kept
# under tests/data/ so they're decoupled from the user-facing demos in examples/.
EXAMPLES_DIR = Path(os.environ.get("KYTTAR_GRC_FIXTURES", ROOT / "tests" / "data" / "grc"))


# --- The GNURadio server does NOT auto-start under test ----------------------
#
# placeKYT ships with "Run as GNURadio Server" ON by default (preference
# ``sim/gr_server_autostart``), so a project load hosts the chip immediately —
# that is the shipping workflow and it is deliberate.
#
# It must be OFF for the suite. A live server is not a passive listener: it does
# a per-batch rebuild and drives the chip itself, which races the tests that
# drive the stepper BY HAND (Step / Pause / instruction-step, the cell-inspector
# live-mode tests) and makes them non-deterministic. It also binds a real TCP
# port, so two tests constructing a MainWindow would contend for 58950.
#
# Autouse + session-scoped so no test can forget it, and it is set before any
# MainWindow is constructed.
import pytest  # noqa: E402


@pytest.fixture(scope="session", autouse=True)
def _no_gr_server_autostart():
    """Disable GNURadio-server autostart for the whole test session."""
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtCore import QCoreApplication, QSettings

    # QSettings needs the org/name main.py sets, or it writes to a stray file.
    QCoreApplication.setOrganizationName("Lattrex")
    QCoreApplication.setApplicationName("placeKYT-tests")
    QSettings().setValue("sim/gr_server_autostart", False)
    QSettings().sync()
    yield


@pytest.fixture(autouse=True)
def _reap_leaked_gr_servers():
    """Stop any GNURadio server a test left running.

    Port 58950 is a REAL bound TCP socket, so a test that starts a server and
    does not stop it poisons every LATER test that needs the port — and the
    victim's failure names the port, not the leaker, which makes it look like a
    defect in whatever ran second.

    Several suites predate this fixture and leak by construction (measured:
    ``test_batch_trace_retention``, ``test_modem_grc_import_duplex_e2e`` and
    ``test_persistent_chip_batch_reset`` each start a server and never stop it).
    Rather than edit each one — and hope the next author remembers — this reaps
    whatever is still bound after every test.

    Cheap: it walks the SimController instances that exist, so a test that
    cleaned up correctly costs one no-op call.
    """
    yield
    try:
        import gc

        from ui.sim_controller import SimController

        for obj in gc.get_objects():
            if isinstance(obj, SimController) and getattr(obj, "_gr_server", None):
                try:
                    obj.stop_gnuradio_server()
                except Exception:      # noqa: BLE001 — teardown must never fail a test
                    pass
    except Exception:                  # noqa: BLE001 — ui not importable in this run
        pass
