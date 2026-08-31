# SPDX-License-Identifier: GPL-3.0-or-later
"""LZ4-stream example — WHOLE-CHAIN end-to-end gate (AGENTS.md §5b).

TWO SRAM-panel-backed blocks on ONE 10x12 array — the panel design limit:

  raw : 1 KB payload (512 repetitive + 512 random bytes) + the end-of-block
        sentinel -> LZ4EncoderBlock -> the compressed stream on x16_out
  cmp : the compressed bytes (re-injected by the client) -> LZ4DecoderBlock
        -> the recovered payload on x16_out

Gated here, all on the real hand-placed + built chip:

  * the encoder output is MODEL-EXACT (the pinned ``encode_model``, itself
    gated against the independent reference C decoder in the block's suite)
    and the full 1 KB round trip is byte-exact.
  * the RATIO gate: the mixed payload compresses well below the all-literals
    length — proven non-vacuous by the dead-hash-insert mutant (INV-4),
    whose output still round-trips.
  * the PANEL-ALIASING gate (INV-61): the decoder recovers a stream whose
    content DISAGREES with what the encoder left in the shared panel — its
    reads come from its own writes, never encoder leftovers.
  * INV-56: every settle stop_reason is "QueueEmpty".
  * INV-42: window/hash/addr params are pinned on the IMPORTED project and
    the shipped flags on the GENERATED Python (raw words, port 58950,
    server_repeat, the worst-case scope buffer, the payload literal).
  * the USER PATH: the shipped .kyt hosted on port 58950, the shipped .grc
    GRC-generated and run under the real GNU Radio interpreter, both sinks
    byte-exact with clean repetition.
"""
from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

_ROOT = Path(__file__).resolve().parents[2]
_EX = _ROOT / "examples" / "lz4_stream"
for _p in (str(_ROOT / "runtime" / "python"), str(_ROOT / "placekyt"),
           str(_EX)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from lz4_stream_demo import (  # noqa: E402
    EOB, GRC_PATH, KYT_PATH, PAYLOAD, PAYLOAD_REP, PAYLOAD_RND, goldens,
    import_and_pnr, load_and_build, run_roundtrip)

GR_PYTHON = os.environ.get("KYTTAR_GR_PYTHON", "/usr/bin/python3")

#: The LZ4 worst case for 1024 input bytes (the .grc's scope buffer).
WORST_1K = 1024 + 1024 // 255 + 16


def _need_chip():
    pytest.importorskip("simkyt")


@pytest.fixture(scope="module")
def built():
    return import_and_pnr()


# ----------------------------------------------------------- BUILD + IMPORT
def test_import_place_build_ok(built):
    project, bres, cat, ct, tags = built
    assert bres.ok
    used = sum(c.cell_count for c in bres.chips.values())
    assert used <= 120
    enc = next(b for b in project.blocks if b.type == "LZ4EncoderBlock")
    dec = next(b for b in project.blocks if b.type == "LZ4DecoderBlock")
    assert len(enc.placement.cells) == 15
    assert len(dec.placement.cells) == 8
    xos = [b for b in project.blocks if b.type == "CrossoverBlock"]
    assert len(xos) == 4
    # no cell is shared between blocks (self- and cross-block overlap)
    cells = [(c.x, c.y) for b in project.blocks for c in b.placement.cells]
    assert len(cells) == len(set(cells)) == 27


def test_INV42_imported_params_are_the_shipped_literals(built):
    """The importer literal-coerces marker params; an expression silently
    becomes the block default. Pin the values the example depends on."""
    project = built[0]
    enc = next(b for b in project.blocks if b.type == "LZ4EncoderBlock")
    dec = next(b for b in project.blocks if b.type == "LZ4DecoderBlock")
    assert int(enc.params["window_words"]) == 32768
    assert int(enc.params["hash_bits"]) == 12
    assert int(enc.params.get("addr_base", 0)) == 0
    assert int(dec.params["window_words"]) == 65536
    # measured: SramControllerBlock.addr_base offsets ONLY the lookup path
    # (writes auto-increment from 0), so a based read-write client reads a
    # region it never wrote. The decoder MUST stay at its proven addr_base 0.
    assert int(dec.params.get("addr_base", 0)) == 0
    # the panel-backed block order is load-bearing: refresh_panel_params
    # re-derives descriptors for backed[0] only, and its direct-landing
    # formula matches the DECODER's return corridor
    panel_backed = [b for b in project.blocks
                    if b.type in ("LZ4EncoderBlock", "LZ4DecoderBlock")]
    assert panel_backed[0].type == "LZ4DecoderBlock"


# ------------------------------------------------------------- ON-CHIP E2E
def test_full_1k_roundtrip_and_ratio_on_chip(built):
    """THE FLAGSHIP GATE: the whole 1 KB through both panel clients on the
    built chip — encoder model-exact, round trip byte-exact, output length
    within the LZ4 bound and well under all-literals, settle reasons clean,
    and the pass-2 emission timeline non-degenerate (the variable rate)."""
    _need_chip()
    project, bres = built[0], built[1]
    exp_cmp, rep_len, rnd_len = goldens()
    cmp_bytes, dec_bytes, info = run_roundtrip(project, bres, PAYLOAD)
    assert cmp_bytes == exp_cmp, (
        f"encoder diverged from the model ({len(cmp_bytes)} vs "
        f"{len(exp_cmp)} bytes)")
    assert dec_bytes == PAYLOAD, "round trip not byte-exact"
    assert info["settle"] == {"QueueEmpty"}, sorted(info["settle"])
    # THE RATIO GATE (mutation-proven below): the mixed payload must compress
    # well below the all-literals floor (~1030 bytes for 1 KB). A dead hash
    # insert makes the output pure literals and MUST fail this bound.
    assert len(cmp_bytes) <= WORST_1K
    assert len(cmp_bytes) < 800, (
        f"{len(cmp_bytes)} bytes for 1 KB — the repetitive half did not "
        f"compress (dead hash table?)")
    # the per-half model split the demo advertises
    assert rep_len < 64, f"repetitive half: {rep_len} bytes"
    assert 512 <= rnd_len <= 512 + 512 // 255 + 16
    # the emission timeline exists and is front-back split (data-dependent
    # rate: sparse early tokens, literal flood at the tail)
    tl = info["timeline"]
    assert len(tl) == len(cmp_bytes)
    t0, t1 = tl[0][0], tl[-1][0]
    mid = t0 + (t1 - t0) // 2
    first_half = sum(1 for t, _n in tl if t <= mid)
    assert first_half < len(tl) // 4, (
        "the emission timeline is not data-dependent: half the compressed "
        "stream arrived in the first half of the scan")


def test_shipped_kyt_parity():
    """The SHIPPED .kyt (the file the GUI hosts) round-trips the full 1 KB."""
    _need_chip()
    project, bres, cat, ct = load_and_build()
    exp_cmp, _rep, _rnd = goldens()
    cmp_bytes, dec_bytes, info = run_roundtrip(project, bres, PAYLOAD)
    assert cmp_bytes == exp_cmp
    assert dec_bytes == PAYLOAD
    assert info["settle"] == {"QueueEmpty"}


# ------------------------------------------------- PANEL ALIASING (INV-61)
def test_decoder_reads_its_own_writes_not_encoder_leftovers(built):
    """INV-61: an alias returns a wrong answer of the RIGHT length. The two
    clients share panel addresses [0, len) sequentially, and for the
    encoder's OWN stream the leftovers coincide with the decoded bytes — a
    decoder reading encoder leftovers would still pass the round trip.
    Decode a stream whose content DISAGREES with the panel leftovers: encode
    PAYLOAD (filling the panel with it), then decode the compressed form of
    the REVERSED payload. Only a decoder whose match reads come from its own
    appended bytes recovers it."""
    _need_chip()
    from gr_kyttar.placement.blocks.lz4_encoder_block import encode_model

    project, bres = built[0], built[1]
    alt = PAYLOAD[::-1]
    assert alt != PAYLOAD
    alt_cmp, _ = encode_model(alt, 32768, 12)
    cmp_bytes, dec_bytes, info = run_roundtrip(project, bres, PAYLOAD,
                                               cmp_override=alt_cmp)
    assert dec_bytes == alt, (
        "the decoder did not recover the independent stream — its panel "
        "reads alias the encoder's leftovers")
    assert info["settle"] == {"QueueEmpty"}


# --------------------------------------------------------- MUTATIONS (INV-4)
def test_MUTATION_dead_hash_insert_fails_the_ratio_gate():
    """Suppress the encoder INS cell's commit trigger (the exact mutant class
    that once shipped): the hash table stays empty, the output is pure
    literals — STILL round-tripping, which is why the round-trip gate alone
    certifies nothing. The ratio gate must fire."""
    _need_chip()
    import contextlib

    import gr_kyttar.placement.blocks.lz4_encoder_block as mod

    real = mod.LZ4EncoderBlock.build_cell_programs

    def mutate(progs):
        cp = progs[mod.C_INS]
        lines = cp.assembly_template.splitlines(keepends=True)
        for k, ln in enumerate(lines):
            if ln.strip().startswith("JUMP") and ln.strip().endswith(", 0"):
                del lines[k]
                break
        else:
            raise AssertionError("the commit JUMP was not found — the "
                                 "mutant no longer mutates the real block")
        cp.assembly_template = "".join(lines)

    @contextlib.contextmanager
    def ctx():
        def mutated(self):
            progs = real(self)
            mutate(progs)
            return progs
        mod.LZ4EncoderBlock.build_cell_programs = mutated
        try:
            yield
        finally:
            mod.LZ4EncoderBlock.build_cell_programs = real

    from gr_kyttar.placement.blocks.lz4_decoder_block import decode_model
    with ctx():
        project, bres, cat, ct, tags = import_and_pnr()
        cmp_bytes, _dec, info = run_roundtrip(project, bres, PAYLOAD_REP,
                                              decode=False)
    assert cmp_bytes, "the mutant emitted nothing (a different defect)"
    # still format-legal, still round-tripping under the golden decoder...
    got, _ = decode_model(cmp_bytes, 65536)
    assert list(got) == PAYLOAD_REP
    # ...and the ratio gate is the one with teeth: all-literals >> compressed
    assert len(cmp_bytes) > len(PAYLOAD_REP) * 0.5, (
        "the no-insert mutant still compressed — the mutation missed")
    exp_rep = goldens()[1]
    assert len(cmp_bytes) > 8 * exp_rep, (
        "the ratio gate cannot tell a dead hash table from the real encoder")


def test_MUTATION_corrupted_compressed_byte_breaks_the_roundtrip(built):
    """Flip one literal byte in the compressed stream before re-injection:
    the recovered payload must differ — the decode half of the gate is not
    satisfied by stream length alone."""
    _need_chip()
    from gr_kyttar.placement.blocks.lz4_encoder_block import encode_model

    project, bres = built[0], built[1]
    exp_cmp, _rep, _rnd = goldens()
    bad = list(exp_cmp)
    bad[-3] ^= 0xFF          # inside the final literal run (format-safe)
    cmp_bytes, dec_bytes, info = run_roundtrip(project, bres, PAYLOAD,
                                               cmp_override=bad)
    assert len(dec_bytes) == len(PAYLOAD), (
        "a one-byte literal corruption changed the LENGTH — pick a "
        "different mutant byte")
    assert dec_bytes != PAYLOAD, (
        "the round-trip gate accepted a corrupted compressed stream")


# --------------------------------------------------- GENERATED PYTHON (INV-42)
_GEN_SCRIPT = r"""
import os, sys, tempfile
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
from gnuradio import gr
from gnuradio.grc.core.platform import Platform
platform = Platform(name="lz4 gen check", prefs=gr.prefs(), version=gr.version(),
                    version_parts=(gr.major_version(), gr.api_version(),
                                   gr.minor_version()))
platform.build_library(["/usr/share/gnuradio/grc/blocks", sys.argv[2]])
out = tempfile.mkdtemp(prefix="lz4gen_")
fg, file_path = platform.load_and_generate_flow_graph(
    os.path.abspath(sys.argv[1]), os.path.abspath(out))
assert file_path, "generation failed"
sys.stdout.write(open(file_path).read())
"""


@pytest.mark.skipif(not os.path.exists(GR_PYTHON),
                    reason="GNU Radio interpreter absent")
def test_generated_python_carries_the_shipped_flags():
    """INV-42: assert the flags on the GENERATED Python, never the .grc
    text: raw output words on BOTH sources, port 58950 on all four markers,
    looped display batches on both sinks, the worst-case scope buffer, the
    literal window/hash params, and the exact payload vector."""
    script = Path(os.environ.get("TMPDIR", "/tmp")) / "lz4_gen_check.py"
    script.write_text(_GEN_SCRIPT)
    r = subprocess.run(
        [GR_PYTHON, str(script), str(GRC_PATH),
         str(_ROOT / "gr-kyttar" / "grc")],
        capture_output=True, text=True, timeout=300,
        env=dict(os.environ, QT_QPA_PLATFORM="offscreen"))
    assert r.returncode == 0, r.stderr[-800:]
    py = r.stdout
    assert py.count('output_words="raw"') == 2, \
        "both kyttar sources must set output_words='raw' EXPLICITLY"
    assert py.count("server_port=58950") == 4, \
        "all four kyttar markers must bind the GUI's default port 58950"
    assert py.count("server_repeat=True") == 2, \
        "both sinks must loop the display batch"
    assert 'stream_id="raw"' in py and 'stream_id="cmp"' in py
    # the pinned lengths: the raw burst (payload + sentinel), the measured
    # compressed batch, and the WORST-CASE encoder-output scope buffer
    assert "payload_len = 1025" in py
    assert "cmp_len = 540" in py
    assert f"cmp_worst = {WORST_1K}" in py
    # marker params generate as literals
    assert re.search(r"lz4_encoder\([^)]*32768", py)
    assert re.search(r"lz4_decoder\([^)]*65536", py)
    # the payload vector is EXACTLY the demo payload + the sentinel
    m = re.search(r"vector_source_f\(\s*[\[\(](.*?)[\]\)]\s*,", py, re.S)
    assert m, "payload vector_source not found in the generated python"
    vec = [int(float(v)) for v in m.group(1).replace("\n", " ").split(",")
           if v.strip()]
    assert vec == PAYLOAD + [EOB], "the .grc payload diverged from the demo"


# ------------------------------------------------------------ USER PATH (§5b)
@pytest.mark.skipif(not os.path.exists(GR_PYTHON),
                    reason="GNU Radio interpreter absent")
def test_shipped_grc_user_path():
    """Host the SHIPPED .kyt exactly as the GUI's "Run as GNURadio Server"
    does (port 58950), GRC-generate and run the SHIPPED .grc under the real
    GNU Radio interpreter, and assert what the kyttar sinks recovered: the
    compressed stream (model-exact) on raw_sink and the byte-exact payload
    on cmp_sink, both as RAW word floats, with clean server_repeat
    repetition."""
    _need_chip()
    from PySide6.QtWidgets import QApplication
    QApplication.instance() or QApplication([])
    from engine.catalog import BlockCatalog
    from engine.io.project_io import load_project
    from ui.controller import AppController
    from ui.sim_controller import SimController

    cat = BlockCatalog.from_gr_kyttar()
    ctrl = AppController(catalog=cat)
    ctrl.set_project(load_project(str(KYT_PATH)))
    sim = SimController(ctrl)
    bound = sim.start_gnuradio_server(port=58950)
    assert bound == 58950, f"port 58950 busy (bound {bound})"
    try:
        runner = _ROOT / "verification" / "grc_userpath_run.py"
        r = subprocess.run(
            [GR_PYTHON, str(runner), str(GRC_PATH), "240"],
            capture_output=True, text=True, timeout=420,
            env=dict(os.environ, QT_QPA_PLATFORM="offscreen"))
        sinks = {}
        for line in r.stdout.splitlines():
            if line.startswith("SINK "):
                parts = line.split()
                sinks[parts[1]] = [float(x) for x in parts[2:]]
        assert r.returncode == 0 and sinks, (
            f"generated flowgraph failed (rc={r.returncode}):\n"
            f"{r.stdout[-800:]}\n{r.stderr[-800:]}")
    finally:
        sim.stop_gnuradio_server()
    # output_words="raw": the recovered floats ARE the words, no q15 rescale.
    cmp_got = [int(round(v)) & 0xFFFF for v in sinks.get("raw_sink", [])]
    rec_got = [int(round(v)) & 0xFFFF for v in sinks.get("cmp_sink", [])]
    exp_cmp, _rep, _rnd = goldens()
    assert len(cmp_got) >= len(exp_cmp), (
        f"raw_sink recovered only {len(cmp_got)}/{len(exp_cmp)} words")
    assert cmp_got[:len(exp_cmp)] == exp_cmp, \
        "user-path compressed stream diverges from the model"
    assert len(rec_got) >= len(PAYLOAD), (
        f"cmp_sink recovered only {len(rec_got)}/{len(PAYLOAD)} words")
    assert rec_got[:len(PAYLOAD)] == PAYLOAD, \
        "user-path recovered payload diverges"
    # server_repeat integrity: every full repetition is the SAME batch.
    for name, got, exp in (("raw", cmp_got, exp_cmp),
                           ("cmp", rec_got, PAYLOAD)):
        for rep in range(1, len(got) // len(exp)):
            assert got[rep * len(exp):(rep + 1) * len(exp)] == exp, \
                f"{name}: repetition {rep} diverges (display loop corrupt)"
