"""placeKYT command-line interface (the architecture notes §7.1, §11.4).

Headless modes for CI and scripting — no Qt required:

    placekyt --drc   PROJECT.kyt              run DRC only
    placekyt --build PROJECT.kyt -o OUT.kbs   DRC + build + write bitstream
    placekyt --info  BITSTREAM.kbs            print .kbs metadata
    placekyt --test  PROJECT.kyt              build + sim.compare vs golden

Exit codes (§11.4):
    0  success (DRC clean / build ok / test passed)
    1  DRC errors (build not possible) / test failed
    2  DRC warnings only (build succeeded with warnings)
    3  file error (not found, parse error, permission denied)
    4  internal error (unexpected exception / simkyt failure)

Chip-type YAML files are discovered via --chip-dir (repeatable); a project's
chip_type name is resolved against those directories.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

EXIT_OK = 0
EXIT_DRC_ERRORS = 1
EXIT_WARNINGS = 2
EXIT_FILE_ERROR = 3
EXIT_INTERNAL = 4


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="placekyt", description="placeKYT CLI")
    mode = p.add_mutually_exclusive_group(required=True)
    mode.add_argument("--drc", metavar="PROJECT", help="run DRC on a .kyt project")
    mode.add_argument("--build", metavar="PROJECT", help="build a .kyt to a .kbs")
    mode.add_argument("--info", metavar="BITSTREAM", help="print .kbs metadata")
    mode.add_argument("--disasm", metavar="BITSTREAM",
                      help="disassemble a .kbs bitstream to a mnemonic listing")
    mode.add_argument("--test", metavar="PROJECT", help="build + compare vs golden")
    mode.add_argument("--replay", metavar="TRACE",
                      help="replay a command trace (.py or .kytrace) headlessly: "
                           "re-run a captured session to reproduce a bug, then "
                           "build + DRC-report the result")
    p.add_argument("-o", "--output", metavar="OUT.kbs", help="output path for --build")
    p.add_argument(
        "--chip-dir", action="append", default=[], metavar="DIR",
        help="directory of chip-type YAMLs (repeatable)",
    )
    p.add_argument(
        "--chip-type", metavar="FILE.yaml", action="append", default=[],
        help="explicit chip-type YAML file to register (repeatable)",
    )
    p.add_argument("--tolerance", type=int, default=2,
                   help="--test compare tolerance (default 2)")
    p.add_argument("--chip", type=int, default=0, metavar="N",
                   help="--disasm: which chip's bitstream in the .kbs (default 0)")
    p.add_argument("--flat", action="store_true",
                   help="--disasm: decode every word independently (do NOT track "
                        "WRITE->DATA payloads)")
    return p


def _registry(args):
    from engine.registry import ChipTypeRegistry

    reg = ChipTypeRegistry.from_dirs(args.chip_dir)
    for f in args.chip_type:
        reg.register_file(f)
    return reg


def _print_findings(findings) -> None:
    for f in findings:
        print(str(f), file=sys.stderr)


# --------------------------------------------------------------------------- #
# Modes
# --------------------------------------------------------------------------- #


def cmd_drc(args) -> int:
    from engine.drc import check_project
    from engine.io.project_io import load_project

    project = load_project(args.drc)
    reg = _registry(args)
    board = None  # board loading is wired when boards/.kdb resolution lands
    result = check_project(project, reg.chip_types(), board)
    _print_findings(result.findings)
    if result.errors:
        print(f"DRC: {len(result.errors)} error(s), "
              f"{len(result.warnings)} warning(s).", file=sys.stderr)
        return EXIT_DRC_ERRORS
    if result.warnings:
        print(f"DRC: clean (with {len(result.warnings)} warning(s)).")
        return EXIT_WARNINGS
    print("DRC: clean.")
    return EXIT_OK


def cmd_build(args) -> int:
    from engine.build import BuildEngine
    from engine.catalog import BlockCatalog
    from engine.io.kbs import Kbs, KbsChip, chip_type_hash, write_kbs
    from engine.io.project_io import load_project

    project = load_project(args.build)
    reg = _registry(args)
    catalog = BlockCatalog.from_gr_kyttar()
    engine = BuildEngine(catalog, reg.paths())
    result = engine.build(project, reg.chip_types())

    _print_findings(result.errors + result.warnings)
    if not result.ok:
        print(f"build failed: {len(result.errors)} DRC error(s).", file=sys.stderr)
        return EXIT_DRC_ERRORS

    output = args.output
    if output:
        # Resolve each chip's type name → hash for the .kbs per-chip header.
        chips = []
        for cid in sorted(result.chips):
            chip = project.chip(cid)
            type_name = (chip.type_name if chip and chip.type_name
                         else project.chip_type)
            chips.append(KbsChip(chip_type_hash(type_name), result.words(cid)))
        # Host-side port config so a headless consumer (the GNURadio bridge)
        # can drive the design's I/O ports without re-deriving routing — see
        # engine.port_config. Embedded in the .kbs metadata so the bitstream is
        # self-contained (words + how to talk to its ports).
        from engine.port_config import input_port_config, output_port_target

        io_cfg = {}
        in_cfg = input_port_config(project, reg, catalog, chip_id=0)
        if in_cfg is not None:
            port, kw = in_cfg
            io_cfg["input_port"] = port
            io_cfg.update(kw)  # entry_addr, hop_count, data_addr
        out_tgt = output_port_target(project)
        if out_tgt is not None:
            io_cfg["output_chip"], io_cfg["output_port"] = out_tgt

        kbs = Kbs(chips=chips, metadata={
            "project_name": project.metadata.name,
            "ide_version": "1.0.0",
            "blocks": [b.name for b in project.blocks],
            "io": io_cfg,
        })
        write_kbs(kbs, output)

    summary = {
        "status": "ok",
        "chips": len(result.chips),
        "cells_used": [result.chips[c].cell_count for c in sorted(result.chips)],
        "warnings": len(result.warnings),
        "output": output or None,
    }
    print(json.dumps(summary))
    return EXIT_WARNINGS if result.warnings else EXIT_OK


def cmd_info(args) -> int:
    from engine.io.kbs import read_kbs

    kbs = read_kbs(args.info)
    out = {
        "format_version": kbs.format_version,
        "chips": len(kbs.chips),
        "words_per_chip": [len(c.words) for c in kbs.chips],
        "metadata": kbs.metadata,
    }
    print(json.dumps(out, indent=2))
    return EXIT_OK


def cmd_disasm(args) -> int:
    from engine.disasm import disassemble_bitstream
    from engine.io.kbs import STIMULUS_KIND, read_kbs

    kbs = read_kbs(args.disasm)
    if not kbs.chips:
        print("disasm: .kbs has no bitstream.", file=sys.stderr)
        return EXIT_INTERNAL
    if args.chip < 0 or args.chip >= len(kbs.chips):
        print(f"disasm: chip {args.chip} out of range "
              f"(0..{len(kbs.chips) - 1}).", file=sys.stderr)
        return EXIT_INTERNAL
    words = kbs.chips[args.chip].words
    # A stimulus bitstream is WRITE+DATA+JUMP bursts → track the data payloads
    # (the word after a WRITE is a literal, not an instruction). ``--flat`` forces
    # independent per-word decode (e.g. to inspect a flat program image).
    is_stimulus = (kbs.metadata or {}).get("kind") == STIMULUS_KIND
    stateful = not args.flat
    kind = "stimulus" if is_stimulus else "bitstream"
    print(f"; {args.disasm} — chip {args.chip}, {len(words)} words ({kind})")
    print(disassemble_bitstream(words, stateful=stateful))
    return EXIT_OK


def cmd_test(args) -> int:
    from engine.build import BuildEngine
    from engine.catalog import BlockCatalog
    from engine.io.project_io import load_project
    from engine.simulator import SimulationEngine

    project = load_project(args.test)
    if not project.simulation.golden_output:
        print("--test: project has no simulation.golden_output configured.",
              file=sys.stderr)
        return EXIT_FILE_ERROR

    reg = _registry(args)
    catalog = BlockCatalog.from_gr_kyttar()
    result = BuildEngine(catalog, reg.paths()).build(project, reg.chip_types())
    _print_findings(result.errors)
    if not result.ok:
        return EXIT_DRC_ERRORS

    # Single-chip test path (§4.3). Resolve the chip's type → YAML path.
    chip0 = project.chip(0)
    type_name = (chip0.type_name if chip0 and chip0.type_name
                 else project.chip_type)
    ct_path = reg.require(type_name).path

    from engine.io.kbs import read_stimulus_kbs

    proj_dir = Path(args.test).resolve().parent
    golden = proj_dir / project.simulation.golden_output  # .kbs golden bitstream
    stim_words = None
    if project.simulation.default_stimulus:
        stim_words = read_stimulus_kbs(
            proj_dir / project.simulation.default_stimulus)

    sim = SimulationEngine(ct_path)
    sim.load(result.words(0))
    cmp = sim.compare_bitstream(
        "x16_out", golden, in_port="x16_in",
        stimulus_words=stim_words, tolerance=args.tolerance,
    )
    if cmp.passed:
        print(f"test PASSED: {cmp.compared} output words match (tolerance "
              f"{args.tolerance}).")
        return EXIT_OK
    print(f"test FAILED: {cmp.mismatches} mismatch(es), first at word "
          f"{cmp.first_mismatch}.", file=sys.stderr)
    return EXIT_DRC_ERRORS  # exit 1 = test failed (§11.4)


def cmd_replay(args) -> int:
    """Replay a command trace headlessly, then build + DRC-report the result.

    A trace captured in the GUI (File -> Export Command Trace) is the exact,
    replayable sequence of operations a user performed. Re-running it on another
    machine reproduces the session — the basis for deterministic bug reports.
    Supports a .py replay script (run as code with ``controller`` in scope) or a
    .kytrace JSON (structured events). After replay it BUILDS the resulting design
    and prints DRC findings, so a 'phantom cells / no output' bug surfaces as
    concrete DRC errors here.

    NOTE: a trace captures COMMANDS (place/move/rotate/face/param/connect/route/
    rename/delete). Operations that are NOT commands — importing a .grc, starting
    the GNU Radio server, running the simulation — are NOT in the trace. A .py
    trace whose first lines are ``# (manual) ...`` flags these gaps; reproduce
    those steps yourself (the .py is editable) before/around the replay.
    """
    import os
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

    from pathlib import Path

    from engine.build import BuildEngine
    from ui.controller import AppController

    trace_path = Path(args.replay)
    ctrl = AppController()
    reg = _registry(args)
    # Seed a default project so a trace that starts with edits (its import was a
    # non-command 'manual' step) still has a chip to act on.
    ctrl.registry = reg
    try:
        ctrl.new_project("replay", "kyttar_10x12")
    except Exception:  # noqa: BLE001 — chip type may come from the trace itself
        pass

    if trace_path.suffix == ".py":
        # Run the .py replay script with `controller`/`ctrl` in scope.
        ns = {"controller": ctrl, "ctrl": ctrl}
        code = trace_path.read_text()
        print(f"replaying {trace_path} (.py script)…")
        exec(compile(code, str(trace_path), "exec"), ns)  # noqa: S102
    else:
        print(f"replaying {trace_path} (.kytrace)…")
        ctrl.replay_trace(str(trace_path))

    n = len(ctrl.trace.events())
    print(f"replayed {n} command(s); "
          f"{len(ctrl.project.blocks)} block(s), "
          f"{len(ctrl.project.connections)} connection(s).")

    catalog = ctrl.catalog
    result = BuildEngine(catalog, reg.paths()).build(
        ctrl.project, reg.chip_types())
    _print_findings(result.errors)
    if result.ok:
        print("build OK (no DRC errors).")
        return EXIT_OK
    print(f"build has {len(result.errors)} DRC finding(s) — "
          f"the replayed design does not build cleanly.", file=sys.stderr)
    return EXIT_DRC_ERRORS


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    # Import errors lazily so --help etc. work without the engine deps.
    from engine.errors import EngineError
    from engine.io.errors import ProjectFileError

    try:
        if args.drc is not None:
            return cmd_drc(args)
        if args.build is not None:
            return cmd_build(args)
        if args.info is not None:
            return cmd_info(args)
        if args.disasm is not None:
            return cmd_disasm(args)
        if args.test is not None:
            return cmd_test(args)
        if args.replay is not None:
            return cmd_replay(args)
    except FileNotFoundError as exc:
        print(f"file error: {exc}", file=sys.stderr)
        return EXIT_FILE_ERROR
    except ProjectFileError as exc:
        print(f"file error: {exc}", file=sys.stderr)
        return EXIT_FILE_ERROR
    except EngineError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_INTERNAL
    except Exception as exc:  # noqa: BLE001 — last-resort internal error
        print(f"internal error: {exc}", file=sys.stderr)
        return EXIT_INTERNAL
    return EXIT_INTERNAL  # unreachable (argparse requires a mode)


if __name__ == "__main__":
    sys.exit(main())
