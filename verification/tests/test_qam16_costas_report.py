# SPDX-License-Identifier: GPL-3.0-or-later
"""QAM16ComplexCostasLoopBlock — metrics REPORT for the dashboard.

The block's verification is the PRODUCTION whole-RX-chain BER-0 gate
(``placekyt/tests/test_qam16_modem_ber.py``: MF → ComplexGain → M&M timing →
decision-directed 16-QAM Costas → slicer, driven by a random RRC-shaped
``constellation_16qam()`` burst through a carrier offset, recovering at
symbol BER 0 modulo the inherent four-fold phase ambiguity — the
industry-standard cascade per the QAM16 research notes). A decision-directed
carrier loop has no sample-exact GR twin to diff against (loop trajectories
differ implementation-to-implementation); the DECISION-level BER-0 recovery
on the GR-golden constellation is the honest equivalence claim, exactly as
the other recovery-class blocks are gated. This module re-runs that proven
drive and emits ``verification/reports/QAM16ComplexCostasLoopBlock.json`` so
the block carries measured metrics in the dashboard like every other done
block (it was the one row with an empty quality column).
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

_ROOT = Path(__file__).resolve().parents[2]
for p in (str(_ROOT / "placekyt"), str(_ROOT / "verification"),
          str(_ROOT / "runtime" / "python"), str(_ROOT / "placekyt" / "tests")):
    if p not in sys.path:
        sys.path.insert(0, p)

from kyttar_verify import write_report, CompareResult, Metric  # noqa: E402

_CT = _ROOT / "placekyt" / "resources" / "chips" / "kyttar_10x12.yaml"
pytestmark = pytest.mark.skipif(not _CT.exists(), reason="chip yaml absent")


def test_qam16_costas_chain_ber_zero_and_report():
    from PySide6.QtWidgets import QApplication
    QApplication.instance() or QApplication([])
    import test_qam16_modem_ber as M
    from engine.catalog import BlockCatalog
    from engine.io.chip_type_io import load_chip_type

    catalog = BlockCatalog.from_gr_kyttar()
    chip_type = load_chip_type(str(M.CT_PATH))
    bres, entry = M._build_rx(catalog, chip_type)
    (ber, rot, lag), n_out, tx = M._drive_ber(bres, entry, n=400, seed=5)
    n_comp = max(0, n_out - 60)
    assert n_out >= len(tx) - 10, f"too few recovered symbols: {n_out}"
    assert ber == 0.0, f"QAM16 RX chain BER {ber:.4f} (rot={rot}, lag={lag})"

    # DERIVED from the BER measured above, not asserted. (The assert already gates
    # it; expressing the verdict as the measurement keeps the report an artifact.)
    res = CompareResult(passed=(ber == 0.0), metric=Metric.DECISION,
                        n_compared=n_comp, bit_errors=int(round(ber * n_comp)),
                        delay_used=int(lag))
    write_report("QAM16ComplexCostasLoopBlock", res, coverage={
        "gr_equiv": "digital.constellation_receiver_cb(constellation_16qam) "
                    "(decision-level equivalence)",
        "patterns": "400-symbol random RRC constellation_16qam burst, carrier "
                    "offset, full MF->gain->MMTiming->Costas->slicer chain",
        "mutation": True,
        "note": "whole-RX-chain BER-0 gate (placekyt/tests/"
                "test_qam16_modem_ber.py owns the mutations); DD loops have no "
                "sample-exact GR twin — decision-level recovery is the claim",
    })
