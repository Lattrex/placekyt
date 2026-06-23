"""bpsk_demo - generate the BPSK modem demo's stimulus + golden .kbs (#199).

The demo project (``tests/data/demo/bpsk_demo.kyt``) is a symbol-level BPSK
loopback on one chip::

    x16_in(bits) -> PSK Mapper(bpsk) -> SoftDemodulator -> BPSKSlicer -> x16_out(bits)

For a clean channel this is the identity: bit -> +-1.0 -> +-LLR -> bit. This
module emits the two bitstreams the Run/--test path consumes:

  * ``bpsk_stimulus.kbs`` — a WRITE+DATA+JUMP burst per input bit, each JUMP
    targeting the mapper's resolved entry one hop east of x16_in.
  * ``bpsk_golden.kbs``   — the output bits, captured by actually running the
    built chip through simkyt (so golden == realized hardware behavior).

Run::

    cd placekyt && .venv/bin/python -m engine.bpsk_demo
"""

from __future__ import annotations

from pathlib import Path

# 24 bits — a recognizable pattern (alternating, runs, edges) that any DSP
# engineer can eyeball against the output.
DEMO_BITS = [0, 1, 0, 1, 1, 0, 0, 1, 1, 1, 0, 0,
             1, 0, 1, 0, 0, 1, 1, 0, 1, 1, 0, 1]

_OP_WRITE = 0x6
_OP_JUMP = 0x7


def _wr(hop_field: int, dest: int) -> int:
    return (_OP_WRITE << 12) | ((hop_field & 0x1F) << 5) | (dest & 0x1F)


def _jp(hop_field: int, entry: int) -> int:
    return (_OP_JUMP << 12) | ((hop_field & 0x1F) << 5) | (entry & 0x1F)


def _demo_path() -> Path:
    return Path(__file__).resolve().parents[1] / "tests" / "data" / "demo" / "bpsk_demo.kyt"


def _build(kyt_path: Path):
    from engine.build import BuildEngine
    from engine.catalog import BlockCatalog
    from engine.io.project_io import load_project
    from engine.registry import ChipTypeRegistry

    project = load_project(str(kyt_path))
    chips_dir = Path(__file__).resolve().parents[1] / "resources" / "chips"
    reg = ChipTypeRegistry.from_dirs([chips_dir])
    catalog = BlockCatalog.from_gr_kyttar()
    result = BuildEngine(catalog, reg.paths()).build(project, reg.chip_types())
    if not result.ok:
        raise RuntimeError(f"BPSK demo build failed: {[str(e) for e in result.errors]}")
    type_name = project.chip(0).type_name or project.chip_type
    return project, result, reg.require(type_name).path


def _mapper_entry(result) -> int:
    """Entry address of the mapper cell at (0,0) from the build result."""
    cells = result.chips[0].cells
    cell = cells.get((0, 0)) or cells.get("0,0")
    if cell is None:
        raise RuntimeError("mapper cell (0,0) not found in build result")
    return int(cell["entry"])


def build_stimulus(entry: int) -> list[int]:
    """One WRITE(R0)+DATA(bit)+JUMP(mapper entry) burst per input bit.

    The mapper is one hop east of x16_in, so the hop FIELD is ``31 - 1 = 30``
    (``@1``) — the burst is consumed at the mapper cell.
    """
    hopf = 31 - 1
    stim: list[int] = []
    for b in DEMO_BITS:
        stim += [_wr(hopf, 0), b & 0xFFFF, _jp(hopf, entry)]
    return stim


def capture_golden(ct_path: str, words: list[int], stim: list[int]):
    """Run the built chip through simkyt and return the captured output.

    Returns ``(out_words, bits)`` — ``out_words`` is the full tagged output
    stream (``OutWord`` list, WRITE+JUMP) for the golden ``.kbs``, and ``bits``
    is just the WRITE values for the human-readable identity check.
    """
    from engine.simulator import SimulationEngine

    sim = SimulationEngine(ct_path)
    sim.load(words)
    sim.inject_words(stim, port="x16_in")
    for _ in range(20000):
        info = sim.chip.run(max_events=64)
        if (isinstance(info, dict)
                and info.get("stop_reason") == "QueueEmpty"
                and sim.chip.run(max_events=0).get("stop_reason") == "QueueEmpty"):
            break
    out = sim.capture_output_words("x16_out")
    bits = [w.value for w in out if not w.is_jump]
    return out, bits


def generate() -> tuple[Path, Path]:
    from engine.io.kbs import write_golden_kbs, write_stimulus_kbs

    from engine.io.output_bitstream import encode_output

    kyt = _demo_path()
    project, result, ct_path = _build(kyt)
    entry = _mapper_entry(result)
    stim = build_stimulus(entry)
    out_words, _bits = capture_golden(ct_path, result.words(0), stim)

    # Golden is the FULL realized output stream (WRITE+JUMP, with tags) so the
    # --test compare matches kind+tag+value exactly.
    golden_words = encode_output(out_words)

    stim_path = kyt.with_name("bpsk_stimulus.kbs")
    golden_path = kyt.with_name("bpsk_golden.kbs")
    write_stimulus_kbs(stim, stim_path, name="bpsk")
    write_golden_kbs(golden_words, golden_path, name="bpsk")
    return stim_path, golden_path


if __name__ == "__main__":
    sp, gp = generate()
    print(f"mapper entry resolved; wrote:\n  {sp}\n  {gp}")
    # Quick self-check: golden WRITE values should equal DEMO_BITS (clean channel).
    from engine.io.kbs import read_golden_kbs
    from engine.io.output_bitstream import decode_output
    out = [w.value for w in decode_output(read_golden_kbs(gp)) if not w.is_jump]
    ok = (out == DEMO_BITS)
    print(f"  in : {DEMO_BITS}")
    print(f"  out: {out}")
    print("  IDENTITY OK" if ok else "  IDENTITY FAIL")
    if not ok:
        raise SystemExit(1)
