"""bpsk_duplex_demo - generate the full-duplex BPSK demo's stimulus + golden (#206).

The project (`tests/data/demo/bpsk_duplex_demo.kyt`) is a full-duplex BPSK
transceiver sharing ONE input and ONE output port. A SplitterBlock landing cell
at (0,0) steers each incoming WRITE+DATA+JUMP burst by its JUMP entry tag:

  * JUMP -> splitter rx_entry : burst flows EAST into the RX chain
            (SoftDemod -> Slicer -> x16_out, output dest tag = RX_TAG)
  * JUMP -> splitter tx_entry : burst flows SOUTH into the TX chain
            (PSKMapper -> ...row 1... -> x16_out, output dest tag = TX_TAG)

Both chains emit on the shared x16_out; the RX and TX outputs are distinguished
by their WRITE `dest` tag (the captured OutWord.tag), so no join block is needed.

This module:
  1. builds the project and resolves the splitter's rx/tx entries + the
     downstream block entries (asserting the .kyt's hardcoded chain entries are
     correct),
  2. emits a single interleaved stimulus .kbs: per step, a TX bit burst (JUMP->
     tx_entry) and an RX symbol burst (JUMP-> rx_entry),
  3. runs the built chip through simkyt and captures the tagged output as
     golden (split by tag into RX bits and TX symbols).

Run:
    cd placekyt && .venv/bin/python -m engine.bpsk_duplex_demo
"""

from __future__ import annotations

from pathlib import Path

# Output dest tags (must match the .kyt out_tag values).
RX_TAG = 5    # recovered RX bits
TX_TAG = 10   # TX baseband symbols

# A recognizable pattern fed to BOTH chains.
RX_SYMBOLS = [0x7FFF, 0x8000, 0x7FFF, 0x7FFF, 0x8000, 0x7FFF, 0x8000, 0x8000]  # +-1.0
TX_BITS = [0, 1, 1, 0, 1, 0, 0, 1]

_OP_WRITE = 0x6
_OP_JUMP = 0x7
Q15_PLUS_ONE = 0x7FFF
Q15_MINUS_ONE = 0x8000


def _wr(hop_field: int, dest: int) -> int:
    return (_OP_WRITE << 12) | ((hop_field & 0x1F) << 5) | (dest & 0x1F)


def _jp(hop_field: int, entry: int) -> int:
    return (_OP_JUMP << 12) | ((hop_field & 0x1F) << 5) | (entry & 0x1F)


def _demo_path() -> Path:
    return Path(__file__).resolve().parents[1] / "tests" / "data" / "demo" / "bpsk_duplex_demo.kyt"


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
        raise RuntimeError(f"duplex build failed: {[str(e) for e in result.errors]}")
    type_name = project.chip(0).type_name or project.chip_type
    return project, result, reg.require(type_name).path


def _entry_at(result, xy) -> int:
    cell = result.chips[0].cells.get(xy)
    if cell is None:
        raise RuntimeError(f"no programmed cell at {xy}")
    return int(cell["entry"])


def _splitter_entries(result):
    """Splitter rx_entry = its resolved entry_addr; tx_entry = +5 (rx arm is 5
    words: MOVE [FACE], MOVE R0, WRITE, JUMP, HALT)."""
    rx = _entry_at(result, (0, 0))
    return rx, rx + 5


def build_stimulus(rx_entry: int, tx_entry: int) -> list[int]:
    """Interleave one TX-bit burst and one RX-symbol burst per step.

    Each burst is WRITE(R0)+DATA+JUMP(splitter entry); the splitter steers it.
    The input port is 1 hop from the splitter, so the hop FIELD is 31-1 = 30.
    """
    hopf = 31 - 1
    stim: list[int] = []
    n = max(len(TX_BITS), len(RX_SYMBOLS))
    for i in range(n):
        if i < len(TX_BITS):
            stim += [_wr(hopf, 0), TX_BITS[i] & 0xFFFF, _jp(hopf, tx_entry)]
        if i < len(RX_SYMBOLS):
            stim += [_wr(hopf, 0), RX_SYMBOLS[i] & 0xFFFF, _jp(hopf, rx_entry)]
    return stim


def capture(ct_path: str, words: list[int], stim: list[int]):
    """Run the chip, return the full tagged output stream + per-tag value lists."""
    from engine.simulator import SimulationEngine

    sim = SimulationEngine(ct_path)
    sim.load(words)
    sim.inject_words(stim, port="x16_in")
    for _ in range(40000):
        info = sim.chip.run(max_events=64)
        if (isinstance(info, dict)
                and info.get("stop_reason") == "QueueEmpty"
                and sim.chip.run(max_events=0).get("stop_reason") == "QueueEmpty"):
            break
    out = sim.capture_output_words("x16_out")
    rx = [w.value for w in out if (not w.is_jump) and w.dest == RX_TAG]
    tx = [w.value for w in out if (not w.is_jump) and w.dest == TX_TAG]
    return out, rx, tx


def generate() -> tuple[Path, Path]:
    from engine.io.kbs import write_golden_kbs, write_stimulus_kbs
    from engine.io.output_bitstream import encode_output

    kyt = _demo_path()
    project, result, ct_path = _build(kyt)

    # Verify the .kyt's hardcoded chain entries match the resolved downstream
    # block entries (a wrong entry would silently misroute).
    demod_entry = _entry_at(result, (1, 0))
    mapper_entry = _entry_at(result, (0, 1))
    split_block = next(b for b in project.blocks if b.name == "split")
    assert int(split_block.params["rx_chain_entry"]) == demod_entry, (
        f"rx_chain_entry={split_block.params['rx_chain_entry']} != demod entry {demod_entry}")
    assert int(split_block.params["tx_chain_entry"]) == mapper_entry, (
        f"tx_chain_entry={split_block.params['tx_chain_entry']} != mapper entry {mapper_entry}")

    rx_entry, tx_entry = _splitter_entries(result)
    stim = build_stimulus(rx_entry, tx_entry)
    out_words, rx_bits, tx_syms = capture(ct_path, result.words(0), stim)

    golden = encode_output(out_words)
    stim_path = kyt.with_name("bpsk_duplex_stimulus.kbs")
    golden_path = kyt.with_name("bpsk_duplex_golden.kbs")
    write_stimulus_kbs(stim, stim_path, name="bpsk_duplex")
    write_golden_kbs(golden, golden_path, name="bpsk_duplex")
    return stim_path, golden_path


if __name__ == "__main__":
    sp, gp = generate()
    print(f"wrote:\n  {sp}\n  {gp}")

    # Self-check: RX demod->slice round-trips the symbol sign to a bit; TX maps
    # the bit to +-1.0. Recompute the expected demuxed outputs.
    _project, result, ct_path = _build(_demo_path())
    rx_entry, tx_entry = _splitter_entries(result)
    _out, rx_bits, tx_syms = capture(ct_path, result.words(0),
                                     build_stimulus(rx_entry, tx_entry))

    exp_rx = [0 if (s if s < 0x8000 else s - 0x10000) >= 0 else 1 for s in RX_SYMBOLS]
    exp_tx = [Q15_PLUS_ONE if b == 0 else Q15_MINUS_ONE for b in TX_BITS]
    rx_ok = (rx_bits == exp_rx)
    tx_ok = (tx_syms == exp_tx)
    print(f"  RX bits  (tag {RX_TAG}): {rx_bits}  expect {exp_rx}  {'OK' if rx_ok else 'FAIL'}")
    print(f"  TX syms  (tag {TX_TAG}): {[hex(v) for v in tx_syms]}  "
          f"expect {[hex(v) for v in exp_tx]}  {'OK' if tx_ok else 'FAIL'}")
    print("  DUPLEX OK" if (rx_ok and tx_ok) else "  DUPLEX FAIL")
    if not (rx_ok and tx_ok):
        raise SystemExit(1)
