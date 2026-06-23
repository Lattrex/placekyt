"""qam16_demo - generate the 16-QAM modem demo's stimulus + golden .kbs (#201).

The demo project (``tests/data/demo/qam16_demo.kyt``) is a symbol-level 16-QAM
loopback on one chip, as ONE composed transceiver block::

    x16_in(bits) -> QAM16TransceiverBlock(bits -> I/Q -> 4-bit sym) -> x16_out

16-QAM is separable Gray 4-PAM per axis. The block composes the proven mapper
(accumulates 4 input bits per symbol, MSB first, -> I/Q levels) and slicer
(hard-decides each axis back to the 4-bit Gray symbol index). It is ONE block
because the mapper's dual I/Q output must fan to the slicer's two inputs via the
block's ``internal_connections`` — a pair of separate project connections does not
route the dual handoff correctly. On a clean channel this is the identity (proven
16/16 in the internal reference implementation and end-to-end via
this demo): 4 bits -> (I,Q) -> the same 4-bit symbol index.

This module emits the two bitstreams the Run/--test path consumes:

  * ``qam16_stimulus.kbs`` — a WRITE+DATA+JUMP burst per input BIT, each JUMP
    targeting the mapper's resolved entry one hop east of x16_in (the mapper
    accumulates 4 bits, then emits one symbol).
  * ``qam16_golden.kbs``   — the output symbols, captured by actually running the
    built chip through simkyt (so golden == realized hardware behavior).

Run::

    cd placekyt && .venv/bin/python -m engine.qam16_demo
"""

from __future__ import annotations

from pathlib import Path

# 6 symbols (24 bits), MSB-first per symbol. A recognizable spread across the
# constellation (corners, inner points, and a repeat) that reads at a glance.
DEMO_SYMBOLS = [0b0000, 0b1010, 0b0110, 0b1111, 0b1001, 0b0011]
DEMO_BITS = [bit
             for sym in DEMO_SYMBOLS
             for bit in ((sym >> 3) & 1, (sym >> 2) & 1, (sym >> 1) & 1, sym & 1)]

_OP_WRITE = 0x6
_OP_JUMP = 0x7


def _wr(hop_field: int, dest: int) -> int:
    return (_OP_WRITE << 12) | ((hop_field & 0x1F) << 5) | (dest & 0x1F)


def _jp(hop_field: int, entry: int) -> int:
    return (_OP_JUMP << 12) | ((hop_field & 0x1F) << 5) | (entry & 0x1F)


def _demo_path() -> Path:
    return (Path(__file__).resolve().parents[1] / "tests" / "data" / "demo"
            / "qam16_demo.kyt")


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
        raise RuntimeError(
            f"QAM16 demo build failed: {[str(e) for e in result.errors]}")
    type_name = project.chip(0).type_name or project.chip_type
    return project, result, reg.require(type_name).path


def _entry(result) -> int:
    """Entry address of the transceiver landing cell (map0) at (0,0)."""
    cells = result.chips[0].cells
    cell = cells.get((0, 0)) or cells.get("0,0")
    if cell is None:
        raise RuntimeError("transceiver landing cell (0,0) not found in build")
    return int(cell["entry"])


def build_stimulus(entry: int) -> list[int]:
    """One WRITE(R0)+DATA(bit)+JUMP(landing entry) burst per input bit. The
    transceiver's landing cell (map0) is one hop east of x16_in, so the hop FIELD
    is ``31 - 1 = 30`` (``@1``)."""
    hopf = 31 - 1
    stim: list[int] = []
    for b in DEMO_BITS:
        stim += [_wr(hopf, 0), b & 0xFFFF, _jp(hopf, entry)]
    return stim


def capture_golden(ct_path: str, words: list[int], stim: list[int]):
    """Run the built chip through simkyt and return the captured output.

    Returns ``(out_words, syms)`` — ``out_words`` is the full tagged output stream
    (``OutWord`` list) for the golden ``.kbs``, and ``syms`` is just the WRITE
    values (the recovered 4-bit symbol indices) for the identity check.
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
    syms = [w.value for w in out if not w.is_jump]
    return out, syms


def generate() -> tuple[Path, Path]:
    from engine.io.kbs import write_golden_kbs, write_stimulus_kbs
    from engine.io.output_bitstream import encode_output

    kyt = _demo_path()
    _project, result, ct_path = _build(kyt)
    entry = _entry(result)
    stim = build_stimulus(entry)
    out_words, _syms = capture_golden(ct_path, result.words(0), stim)

    golden_words = encode_output(out_words)

    stim_path = kyt.with_name("qam16_stimulus.kbs")
    golden_path = kyt.with_name("qam16_golden.kbs")
    write_stimulus_kbs(stim, stim_path, name="qam16")
    write_golden_kbs(golden_words, golden_path, name="qam16")
    return stim_path, golden_path


if __name__ == "__main__":
    sp, gp = generate()
    print(f"transceiver entry resolved; wrote:\n  {sp}\n  {gp}")
    from engine.io.kbs import read_golden_kbs
    from engine.io.output_bitstream import decode_output
    out = [w.value for w in decode_output(read_golden_kbs(gp)) if not w.is_jump]
    ok = (out == DEMO_SYMBOLS)
    print(f"  in (symbols): {DEMO_SYMBOLS}")
    print(f"  out          : {out}")
    print("  IDENTITY OK" if ok else "  IDENTITY FAIL")
    if not ok:
        raise SystemExit(1)
