"""Pytest config: make the placeKYT package root importable.

The flat layout (the architecture notes §6) puts ``model/`` directly under the
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
