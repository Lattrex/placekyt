# SPDX-License-Identifier: GPL-3.0-or-later
"""END-TO-END on the USER's hand-built qpsk_modem.orig.kyt — NO auto-route.

Mirrors exactly how the user runs it: load the .kyt, build, resolve stream_targets
from the built project (input_landings), host on SimServer, drive BOTH streams
(rx + tx) over the same hosted chip, and report what each stream emits.

The bug under investigation: TX (stream 'tx') produces NO output because the
shared input-port cell (0,0) forwards ONE way only; the fix is to make (0,0) a
broker that relays each stream down its corridor. This driver is the oracle:
if 'tx' emits nothing, the bug reproduces; when fixed, 'tx' emits passband.
"""
from __future__ import annotations
import os, sys, random
from pathlib import Path
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
_ROOT = Path(__file__).resolve().parents[3]
for p in (str(_ROOT / "placekyt"), str(_ROOT / "runtime" / "python")):
    if p not in sys.path:
        sys.path.insert(0, p)
import numpy as np                                       # noqa: E402
import socket                                            # noqa: E402
from PySide6.QtWidgets import QApplication               # noqa: E402
QApplication.instance() or QApplication([])
import simkyt                                            # noqa: E402
from engine.io.project_io import load_project            # noqa: E402
from engine.build import BuildEngine                     # noqa: E402
from engine.catalog import BlockCatalog                  # noqa: E402
from engine.io.chip_type_io import load_chip_type        # noqa: E402
from engine.registry import ChipTypeRegistry             # noqa: E402
from engine.port_config import stream_targets            # noqa: E402
from engine.sim_bridge import SimServer, recv_message, send_message  # noqa: E402

CHIP = str(_ROOT / "placekyt" / "resources" / "chips" / "kyttar_10x12.yaml")
KYT = str(_ROOT / "examples" / "qpsk_modem" / "qpsk_modem.orig.kyt")


def _batch(c, *, stream_id, payload, complex_, raw=True):
    send_message(c, {"op": "process_batch", "port": "x16_out",
                     "in_port": "x16_in", "stream_id": stream_id,
                     "complex": bool(complex_), "raw": bool(raw)},
                 np.asarray(payload, dtype=np.float32))
    return recv_message(c)


def main():
    proj = load_project(KYT)
    ct = load_chip_type(CHIP)
    key = getattr(ct, "name", None) or "kyttar_10x12"
    cat = BlockCatalog.from_gr_kyttar()
    bres = BuildEngine(cat, CHIP).build(proj, {key: ct})
    print("build ok:", bres.ok, "" if bres.ok else bres.errors[:3])

    reg = ChipTypeRegistry()
    reg.register_file(CHIP)
    targets = stream_targets(proj, reg, cat, 0, build_result=bres)
    print("stream_targets:")
    for k, v in targets.items():
        print(f"  {k}: {v}")

    chip = simkyt.Chip.from_yaml(CHIP)
    chip.load_bitstream_physical(bres.words(0))
    srv = SimServer(chip, stream_targets=targets)
    port = srv.start()
    try:
        c = socket.socket(); c.connect(("127.0.0.1", port))
        # RX: a few complex samples (real recovery is gated elsewhere; here we just
        # want to see the stream produce ANY output vs none).
        random.seed(5)
        rx_iq = np.zeros(2 * 40, dtype=np.float32)
        for i in range(40):
            rx_iq[2 * i] = random.uniform(-0.4, 0.4)
            rx_iq[2 * i + 1] = random.uniform(-0.4, 0.4)
        rx_h, rx_out = _batch(c, stream_id="rx", payload=rx_iq, complex_=True)
        # TX: a bit burst.
        tx_bits = np.asarray([0, 1, 1, 0, 1, 0, 0, 1, 1, 1, 0, 0], dtype=np.float32)
        tx_h, tx_out = _batch(c, stream_id="tx", payload=tx_bits, complex_=False)
        c.close()
    finally:
        srv.stop()

    rxn = len(rx_out) if rx_out is not None else 0
    txn = len(tx_out) if tx_out is not None else 0
    print(f"\n[RX] header={rx_h}")
    print(f"[RX] {rxn} words out")
    print(f"[TX] header={tx_h}")
    print(f"[TX] {txn} words out  {'<-- BUG: TX SILENT' if txn == 0 else ''}")


if __name__ == "__main__":
    main()
