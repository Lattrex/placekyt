# SPDX-License-Identifier: GPL-3.0-or-later
"""Deterministic reproducer: ComplexMixerBlock produces ZERO output at 180-family
orientations (cw+cw, cw+cw+cw, mirror_v+cw+cw) when wired straight from the chip
input port. In-process, no pytest needed. A correct block is D4-invariant."""
import os, sys, random
os.environ.setdefault("QT_QPA_PLATFORM","offscreen")
sys.path.insert(0,"/home/system/placekyt/placekyt")
sys.path.insert(0,"/home/system/placekyt/verification")
sys.path.insert(0,"/home/system/placekyt/runtime/python")
from kyttar_verify import run_block_dut_complex, D4_ORIENTATIONS
CHIP="/home/system/placekyt/placekyt/resources/chips/kyttar_10x12.yaml"
def stim():
    r=random.Random(3); return [complex(r.uniform(-.5,.5),r.uniform(-.5,.5)) for _ in range(24)]
def m(o):
    return run_block_dut_complex("ComplexMixerBlock",stim(),params={},chip_yaml=CHIP,
        in_ports=("xi","xq"),out_port="yi",words_per_sample=2,orient=list(o))
base=tuple(m([]).i_q15[:8])
print("identity i[:8]:",base)
for o in D4_ORIENTATIONS[1:]:
    r=tuple(m(o).i_q15[:8])
    tag="+".join(o) or "id"
    z = all(v in (0,None) for v in r)
    print(f"  {tag:20s}: {'ZERO-OUTPUT (BUG)' if z else ('OK' if r==base else 'MISMATCH')}  {r}")
