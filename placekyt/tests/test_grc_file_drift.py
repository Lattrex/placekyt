# SPDX-License-Identifier: GPL-3.0-or-later
"""GRC drift is detected on SAVE (re-reading the .grc), not only on Run.

The required UX: edit a parameter in GNU Radio, SAVE the .grc, and placeKYT flags
"out of sync" — so the user can resync BEFORE running, instead of run → resync →
run again. The .grc is a file on disk, so placeKYT watches it and re-diffs its
block params against the placed design on change; no bidirectional channel and no
run needed. This tests the detection path (controller.check_grc_file_drift); the
GUI QFileSystemWatcher just calls it.
"""
import os
import tempfile

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
import yaml

from ui.controller import AppController

GRC = "/home/system/placekyt/examples/gain/gain.grc"
pytestmark = pytest.mark.skipif(not os.path.exists(GRC), reason="gain.grc absent")


def _grc_with_changed_gain(value="0.99"):
    data = yaml.safe_load(open(GRC).read())
    changed = False
    for b in data.get("blocks", []):
        if "gain" in b.get("id", ""):
            ps = b.get("parameters", {})
            if "gain" in ps:
                ps["gain"] = value
                changed = True
    assert changed, "expected a gain param in the example .grc"
    tmp = tempfile.NamedTemporaryFile(suffix=".grc", delete=False, mode="w")
    yaml.safe_dump(data, tmp)
    tmp.close()
    return tmp.name


def test_import_records_grc_source_and_is_in_sync():
    ctrl = AppController()
    ctrl.import_grc(GRC)
    assert ctrl._grc_source_path is not None, "import must remember the .grc path"
    assert not ctrl.grc_sync.diffs, "freshly imported design is in sync"


def test_resaved_grc_flags_drift_without_running():
    """Editing + saving the .grc (a param change) flags drift on SAVE — no run."""
    ctrl = AppController()
    ctrl.import_grc(GRC)
    tmp = _grc_with_changed_gain("0.99")
    try:
        ctrl._grc_source_path = tmp           # the user "saved" the edited .grc
        diffs = ctrl.check_grc_file_drift()
        assert diffs, "a saved parameter change must flag out-of-sync on save"
        assert "gain" in diffs
        assert not diffs["gain"].resizes, "a gain value change is not a resize"
    finally:
        os.unlink(tmp)


def test_no_grc_source_is_a_noop():
    ctrl = AppController()
    # No import → no tracked .grc → drift check is a harmless no-op (returns None).
    assert ctrl.check_grc_file_drift() is None
