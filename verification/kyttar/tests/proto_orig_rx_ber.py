# SPDX-License-Identifier: GPL-3.0-or-later
"""RX-recovery PROOF on the user's hand-built qpsk_modem.orig.kyt — NO auto-route.

Drives a REAL RRC-shaped QPSK burst through the RX stream of the hosted chip and
computes symbol BER. This proves the shared-port broker fix delivers RX to the matched
filter well enough to RECOVER symbols, not merely emit words. TX is driven too (must
stay alive).

STIMULUS = the RX chain's actual operating point. The hosted RX is a BASEBAND coherent
receiver (port -> ComplexRRCMatchedFilter -> ComplexCostasLoop carrier recovery ->
GardnerTimingRecovery -> QPSKSlicer); there is NO downconvert in front of it, so it
runs at baseband with only a SMALL residual carrier offset the Costas loop can pull in.
We use the EXACT stimulus + BER convention of the KNOWN-GOOD straight-placement test
(placekyt/tests/test_qpsk_modem_ber.py::test_qpsk_rx_ber_zero): foff=0.008, toff=0.45,
160 symbols, amp=0.7 — apples-to-apples with the proven build, but through the
SHARED-PORT BROKER path of the hosted orig chip.

NOTE: an earlier revision drove a 0.125 (=4000/32000) carrier here — the TX iqupconvert
frequency. That is ~16x the Costas pull-in range and yields BER ~0.66 on ANY correct RX
(verified: it breaks even the known-good straight-placement build), so it was masking a
CORRECT delivery as a failure. Fixed to the baseband operating point above.
"""
from __future__ import annotations
import os, sys, random, socket
from pathlib import Path
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
_ROOT = Path(__file__).resolve().parents[3]
for p in (str(_ROOT / "placekyt"), str(_ROOT / "runtime" / "python"),
          str(_ROOT / "examples" / "qpsk_modem")):
    if p not in sys.path:
        sys.path.insert(0, p)
import numpy as np                                       # noqa: E402
from PySide6.QtWidgets import QApplication               # noqa: E402
QApplication.instance() or QApplication([])
import simkyt                                            # noqa: E402
import batch_check as BC                                 # noqa: E402
from engine.io.project_io import load_project            # noqa: E402
from engine.build import BuildEngine                     # noqa: E402
from engine.catalog import BlockCatalog                  # noqa: E402
from engine.io.chip_type_io import load_chip_type        # noqa: E402
from engine.registry import ChipTypeRegistry             # noqa: E402
from engine.port_config import stream_targets            # noqa: E402
from engine.sim_bridge import SimServer, recv_message, send_message  # noqa: E402

CHIP = str(_ROOT / "placekyt" / "resources" / "chips" / "kyttar_10x12.yaml")
KYT = str(_ROOT / "examples" / "qpsk_modem" / "qpsk_modem.orig.kyt")


def _batch(c, *, stream_id, payload, complex_):
    send_message(c, {"op": "process_batch", "port": "x16_out", "in_port": "x16_in",
                     "stream_id": stream_id, "complex": bool(complex_), "raw": True},
                 np.asarray(payload, dtype=np.float32))
    return recv_message(c)


def main():
    proj = load_project(KYT)
    ct = load_chip_type(CHIP); key = getattr(ct, "name", None) or "kyttar_10x12"
    cat = BlockCatalog.from_gr_kyttar()
    bres = BuildEngine(cat, CHIP).build(proj, {key: ct})
    print("build ok:", bres.ok)
    reg = ChipTypeRegistry(); reg.register_file(CHIP)
    targets = stream_targets(proj, reg, cat, 0, build_result=bres)

    # Baseband QPSK burst at the RX chain's operating point (KNOWN-GOOD convention):
    # small carrier OFFSET foff=0.008 (within Costas pull-in), fractional timing 0.45.
    random.seed(5)
    n_syms = 160
    foff, toff = 0.008, 0.45
    syms = [(random.randint(0, 1), random.randint(0, 1)) for _ in range(n_syms)]
    xi, xq, tx_ref = BC._tx_signal(syms, sps=2, beta=0.35, span=8, toff=toff, amp=0.7)
    kk = np.arange(len(xi))
    base = np.asarray(xi) + 1j * np.asarray(xq)
    iq = base * np.exp(1j * 2 * np.pi * foff * kk)
    rx_payload = np.empty(2 * len(iq), dtype=np.float32)
    rx_payload[0::2] = iq.real
    rx_payload[1::2] = iq.imag

    chip = simkyt.Chip.from_yaml(CHIP)
    chip.load_bitstream_physical(bres.words(0))
    srv = SimServer(chip, stream_targets=targets)
    port = srv.start()
    try:
        c = socket.socket(); c.connect(("127.0.0.1", port))
        rx_h, rx_out = _batch(c, stream_id="rx", payload=rx_payload, complex_=True)
        tx_h, tx_out = _batch(c, stream_id="tx",
                              payload=np.asarray([0, 1, 1, 0, 1, 0, 0, 1],
                                                 dtype=np.float32), complex_=False)
        c.close()
    finally:
        srv.stop()

    rx = [int(v) & 0xFFFF for v in (rx_out if rx_out is not None else [])]
    # RX slicer emits a symbol index per recovered symbol.
    rx_syms = [v & 0x3 for v in rx]
    e, m, rot, lag = BC._qpsk_ber(rx_syms, tx_ref)
    print(f"[RX] {len(rx)} words out; symbol BER = {e}/{m}"
          + (f" = {e/m:.4f}" if m else "") + f"  (rot={rot}, lag={lag})")
    print(f"[TX] {len(tx_out) if tx_out is not None else 0} words out")
    if m and e == 0:
        print("RESULT: RX recovers BER 0 on the orig file. PASS.")
    elif m:
        print(f"RESULT: RX emits + partially recovers (BER {e/m:.3f}). "
              "output alive but not BER0.")
    else:
        print("RESULT: RX did not emit enough to score. FAIL.")


if __name__ == "__main__":
    main()
