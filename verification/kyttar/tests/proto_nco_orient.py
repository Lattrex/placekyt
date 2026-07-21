# SPDX-License-Identifier: GPL-3.0-or-later
"""Deterministic reproducer: NCOBlock (real, single-rail) D4 orientation output.
A correct block is D4-invariant: same on-chip output in all 8 orientations."""
import os, sys, random
os.environ.setdefault("QT_QPA_PLATFORM","offscreen")
sys.path.insert(0,"/home/system/placekyt/placekyt")
sys.path.insert(0,"/home/system/placekyt/verification")
sys.path.insert(0,"/home/system/placekyt/runtime/python")
from kyttar_verify import run_block_dut, D4_ORIENTATIONS
CHIP="/home/system/placekyt/placekyt/resources/chips/kyttar_10x12.yaml"
def _fq(v):
    q=int(round(v*32768.0)); return max(-32768,min(32767,q))&0xFFFF
def stim():
    r=random.Random(3); return [_fq(r.uniform(-0.6,0.6)) for _ in range(16)]
def n(o):
    return run_block_dut("NCOBlock",stim(),params={},chip_yaml=CHIP,
        in_port="sample",out_port="yi",orient=list(o))
base=n([])
b=tuple(base.outputs_q15[:8])
print("identity out[:8]:",b, "ok=",getattr(base,'ok',True))
for o in D4_ORIENTATIONS[1:]:
    r=n(o); v=tuple(r.outputs_q15[:8]); tag="+".join(o) or "id"
    z=all(x in (0,None) for x in v)
    print(f"  {tag:20s}: {'ZERO/NONE(BUG)' if z else ('OK' if v==b else 'MISMATCH')}  {v}  ok={getattr(r,'ok',True)} {getattr(r,'reason','')}")
