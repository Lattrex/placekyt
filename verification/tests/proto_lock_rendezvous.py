# SPDX-License-Identifier: GPL-3.0-or-later
"""PROTO / substrate probe: the LOCK/LOCK_FACE rendezvous primitives.

Confirms (mechanically, on simKYT) the ISA facts the DualFloatToComplex block
is built on:

  * `MOVE [LOCK_FACE], Rn` / `MOVE [LOCK], Rn` assemble to CONFIG-register writes
    (CONFIG addr 3 = LOCK_FACE at memory-mapped dest 35; addr 4 = LOCK at dest 36).
    Verified via disassembly.
  * `MOVE [FACE]`=CONFIG 1 (dest 33), `[IN_FACE]`=CONFIG 2 (dest 34),
    `[FLAGS]`=CONFIG 0 (dest 32) — the config space is memory addresses 32..36.

The FULL rendezvous behaviour (a cell locked to face A ignores face B, flips on
LOCK_FACE rewrite, and pairs two independent I/Q producers under adversarial
interleaving) is proven by the DualFloatToComplex block-DUT test rather than raw
injection here: triggering an interior cell requires the placeKYT place/route
flow to configure the intervening cells' FWD_FACE (raw inject_jump_physical to an
unrouted interior cell does not reach it). See the block's verification test.

Run:
    cd /home/system/placekyt
    QT_QPA_PLATFORM=offscreen .venv/bin/python verification/tests/proto_lock_rendezvous.py
"""
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_ROOT / "runtime" / "python"))

import simkyt  # noqa: E402


def _config_dest(reg: str) -> int:
    """Assemble `MOVE [reg], R1` and return the decoded destination address."""
    import re
    prog = simkyt.Program.from_source("t", f"t:\n    MOVE [{reg}], R1\n    HALT\n", 0)
    line = prog.disassemble().splitlines()[1]
    m = re.search(r"dest: (\d+)", line)
    return int(m.group(1)) if m else -1


CONFIG_BASE = 32
EXPECT = {"FLAGS": 32, "FACE": 33, "IN_FACE": 34, "LOCK_FACE": 35, "LOCK": 36}


def probe_config_encoding() -> bool:
    ok = True
    for reg, want in EXPECT.items():
        got = _config_dest(reg)
        status = "OK" if got == want else "MISMATCH"
        if got != want:
            ok = False
        print(f"[probe] MOVE [{reg:9s}] -> dest {got:2d}  (CONFIG {got - CONFIG_BASE}) "
              f"want {want} — {status}")
    return ok


if __name__ == "__main__":
    ok = probe_config_encoding()
    print("\nLOCK/CONFIG instruction encoding:", "CONFIRMED" if ok else "FAILED")
    sys.exit(0 if ok else 1)
