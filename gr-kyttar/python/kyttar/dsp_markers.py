# SPDX-License-Identifier: GPL-3.0-or-later
"""Pass-through GR marker blocks for the real coherent-RX DSP blocks.

In the GRC-first workflow a flowgraph is built from the REAL DSP blocks
(ComplexCostasLoop, Gardner, BPSKSlicer) so that placeKYT can IMPORT it and place
+ route them on the chip. When that same flowgraph RUNS (linked to a placeKYT-hosted
chip in server-batch mode), the actual DSP happens ON the chip — these GR blocks are
pure pass-through MARKERS that exist only to (a) appear in the GRC graph for import
and (b) type-check the float stream between kyttar_source and kyttar_sink.

These mirror the existing ``costas_loop`` marker; they're separated here because the
GRC ``.block.yml`` files call ``kyttar.complex_costas_loop`` / ``gardner_timing_
recovery`` / ``bpsk_slicer`` factory names that previously had no implementation —
which is why a real-block GRC flowgraph could not generate/run.
"""

from gnuradio import gr
import math
import numpy as np


def _io_dtype(io_type):
    """Normalise a GRC ``io_type`` value to a numpy dtype. GRC substitutes an enum
    option (``float`` / ``complex``) as a BARE identifier, so the generated make call
    passes the Python BUILTIN ``float`` / ``complex`` — NOT the string ``"float"``.
    Accept the string, the builtin type, and numpy dtypes; default COMPLEX."""
    if io_type in ("float", float, np.float32, "real"):
        return np.float32
    if isinstance(io_type, str) and io_type.strip().strip("'\"").lower() in (
            "float", "real", "f"):
        return np.float32
    return np.complex64


class _PassThrough(gr.sync_block):
    """A float-stream pass-through GR block — a placeable-DSP MARKER. The real DSP
    runs on the placeKYT chip; this only carries the graph so it imports + runs.

    ``n_in`` / ``n_out`` give the number of stream ports; ``in_dtype`` /
    ``out_dtype`` give the per-side item type. A COMPLEX-baseband DSP block
    (matched filter, Costas) carries its I/Q as a SINGLE gr_complex stream so the
    .grc has one complex wire per hop (no dtype-mismatch warning, no unconnected
    port). The physical chip route is still ONE time-multiplexed bus carrying both
    rails; the GR stream is purely the logical graph. Each output mirrors input 0
    (markers don't compute); a complex input copied into a float output takes the
    real part (so a complex->float marker like Costas/IQUpconvert never raises a
    numpy cast warning)."""

    def __init__(self, name, n_in=1, n_out=1,
                 in_dtype=np.float32, out_dtype=np.float32):
        gr.sync_block.__init__(self, name=name,
                               in_sig=[in_dtype] * n_in,
                               out_sig=[out_dtype] * n_out)

    def _advertise_grc_params(self, device_id, placekyt_type, params,
                              block_name=""):
        """Record this marker's params for GRC↔placeKYT sync advertising.

        Markers call this in ``__init__`` to declare their placeKYT TYPE (e.g.
        ``"GainBlock"``) and current params. The ACTUAL registration into the
        shared per-device BatchSession happens in :meth:`start` (every flowgraph
        run), so the params reach the session fresh each run alongside the source's
        batch dispatch — which sends them to placeKYT for drift detection. Minimal
        and never crashy: a marker that can't determine its type simply records
        nothing; advertising is best-effort telemetry, not on the data path.

        ``block_name``: the placeKYT block name, VERBATIM — the robust keying for
        designs with several instances of one type (see register_params). Empty ⇒
        the construction-order fallback."""
        self._grc_advert = (str(device_id), str(placekyt_type), dict(params or {}),
                            str(block_name or ""))

    def start(self) -> bool:
        # Register the recorded advertisement into the per-device BatchSession
        # each run, so the source's batch dispatch ships current params to placeKYT
        # (GRC↔placeKYT sync indicator). Best-effort: never break the flowgraph.
        # The session + assigned placeKYT block name are KEPT so a GRC slider
        # callback (e.g. gain.set_gain) can LIVE-update this block's advertised
        # params mid-run — the next burst carries the new value and the server
        # retunes the running fabric (see _update_grc_param).
        advert = getattr(self, "_grc_advert", None)
        if advert is not None:
            try:
                from ._batch_session import get_session
                device_id, placekyt_type, params, block_name = advert
                session = get_session(device_id)
                self._pk_session = session
                self._pk_name = session.register_params(
                    placekyt_type, params, explicit_name=block_name or None)
            except Exception:  # noqa: BLE001 — advertising is best-effort
                pass
        return True

    def _update_grc_param(self, key, value):
        """LIVE param update from a GRC callback (slider/entry change mid-run).

        Updates the recorded advertisement (so a later run re-registers the
        current value) AND the live session entry (so the NEXT burst dispatch
        ships it — the placeKYT server turns a registered live-tunable param
        into a coefficient WRITE on the running fabric). Best-effort: before
        ``start`` there is no session yet and only the advertisement updates."""
        advert = getattr(self, "_grc_advert", None)
        if advert is not None:
            advert[2][key] = value
        session = getattr(self, "_pk_session", None)
        name = getattr(self, "_pk_name", None)
        if session is not None and name is not None:
            try:
                session.update_param(name, key, value)
                # LIVE push: retune the persistently-hosted fabric NOW (server
                # applies a coefficient WRITE), not just on the next burst.
                from ._batch_session import push_params_live
                push_params_live(session.device_id, {name: {key: value}})
            except Exception:  # noqa: BLE001 — advertising is best-effort
                pass

    def work(self, input_items, output_items):
        n = len(input_items[0])
        src = input_items[0][:n]
        for o in output_items:
            if np.iscomplexobj(src) and not np.iscomplexobj(o):
                # complex input copied into a real output port -> take real part
                # (astype(float) on complex would drop imag with a numpy warning)
                o[:] = np.asarray(src).real
            else:
                o[:] = src
        return n


class complex_rrc_matched_filter(_PassThrough):
    """Complex RRC matched filter — GR marker (maps to ComplexRRCMatchedFilterBlock).

    The RX matched filter front end: a SINGLE complex baseband stream in, the
    matched-filtered complex stream out (one gr_complex port each side). The real
    DSP runs on the chip; this only carries the graph so it imports into placeKYT
    and runs in server-batch mode."""

    def __init__(self, device_id="kyttar_0", gain=0.7105, samp_rate=2.0,
                 sym_rate=1.0, alpha=0.35, ntaps=17, decimation=1):
        super().__init__("Kyttar Complex RRC Matched Filter", n_in=1, n_out=1,
                         in_dtype=np.complex64, out_dtype=np.complex64)
        self.device_id = device_id
        self.gain = gain
        self.samp_rate = samp_rate
        self.sym_rate = sym_rate
        self.alpha = alpha
        self.ntaps = int(ntaps)
        self.decimation = int(decimation)
        # GR-verbatim params (firdes.root_raised_cosine): gain/samp_rate/sym_rate/
        # alpha/ntaps. The placeKYT block designs the SAME firdes RRC taps and runs
        # them on both rails (GR fir_filter_ccf).
        self._advertise_grc_params(device_id, "ComplexRRCMatchedFilterBlock",
                                   {"gain": gain, "samp_rate": samp_rate,
                                    "sym_rate": sym_rate, "alpha": alpha,
                                    "ntaps": int(ntaps),
                                    "decimation": int(decimation)})


class complex_costas_loop(_PassThrough):
    """Complex Costas carrier recovery — GR marker (maps to ComplexCostasLoopBlock).

    A SINGLE complex baseband stream in → recovered tap out. At ``order=2`` (BPSK) the
    tap is the recovered I only (yi_tap, real/float); at ``order=4`` (QPSK) it is the
    recovered (I, Q) pair carried as ONE gr_complex stream (yi_tap) — placeKYT's
    importer splits that single complex net into the on-chip yi_tap/yq_tap rails. One
    wire per complex link keeps the flowgraph clean (no dangling second-rail port)."""

    def __init__(self, device_id="kyttar_0", loop_bw=0.05, damping=1.0, order=2):
        # complex in; out is complex at order 4 (I/Q tap) else float (I tap only),
        # matching the .block.yml's ${ 'complex' if order == 4 else 'float' } out dtype.
        out_dt = np.complex64 if int(order) == 4 else np.float32
        super().__init__("Kyttar Complex Costas Loop", n_in=1, n_out=1,
                         in_dtype=np.complex64, out_dtype=out_dt)
        self.device_id = device_id
        self.loop_bw = loop_bw
        self.damping = damping
        self.order = int(order)
        self._advertise_grc_params(device_id, "ComplexCostasLoopBlock",
                                   {"loop_bw": loop_bw, "damping": damping,
                                    "order": int(order)})


class gardner_timing_recovery(_PassThrough):
    """Gardner timing recovery — GR marker (maps to GardnerTimingRecovery).

    ``complex=True`` selects 2-rail (I/Q) timing recovery carried as ONE gr_complex
    stream in/out (the on-chip xi/xq in + yi_e/yq_e out pairs; placeKYT's importer
    splits the single complex net into the two rails), feeding a downstream QPSK
    slicer. ``complex=False`` (default) is the real BPSK timing loop (a single float
    stream in, recovered center ``out``). One wire per link — no dangling rail port."""

    def __init__(self, device_id="kyttar_0", kp=3, ki=1, complex=False):
        dt = np.complex64 if bool(complex) else np.float32
        super().__init__("Kyttar Gardner Timing Recovery",
                         in_dtype=dt, out_dtype=dt)
        self.device_id = device_id
        self.kp = kp
        self.ki = ki
        self.complex = bool(complex)
        self._advertise_grc_params(device_id, "GardnerTimingRecovery",
                                   {"kp": kp, "ki": ki, "complex": bool(complex)})


class lms_equalizer(_PassThrough):
    """Decision-directed complex LMS adaptive equalizer — GR marker (maps to
    LMSEqualizerBlock; GR counterpart digital.linear_equalizer with
    adaptive_algorithm_lms). Always COMPLEX in/out — the recovered (yi, yq)
    pair rides one gr_complex stream (placeKYT's importer splits the rails).

    HW-DEVIATIONS (documented on the block + proven scale-covariant in
    verification/tests/test_lms_equalizer.py): decisions at the unit-circle
    QPSK constellation (alpha = 1/2 of GR's +-1.414-component points — the
    whole trajectory scales by alpha), DD-only spike cold start (no on-chip
    training memory), taps stored halved (envelope sum|w_eff| <= 2). A pure
    pass-through marker; the real DSP runs on the placeKYT-hosted chip."""

    def __init__(self, device_id="kyttar_0", num_taps=5, step_size=0.03,
                 sps=1, block_name=""):
        super().__init__("Kyttar LMS Equalizer",
                         in_dtype=np.complex64, out_dtype=np.complex64)
        self.device_id = device_id
        self.num_taps = int(num_taps)
        self.step_size = float(step_size)
        self.sps = int(sps)
        self._advertise_grc_params(device_id, "LMSEqualizerBlock",
                                   {"num_taps": int(num_taps),
                                    "step_size": float(step_size),
                                    "sps": int(sps)},
                                   block_name=block_name)


class complex_to_mag(_PassThrough):
    """True complex magnitude |x+jy| — GR marker (maps to ComplexToMagBlock,
    CORDIC vectoring; GR counterpart blocks.complex_to_mag). Complex in, float
    out. The chip emits Q15 (kyttar.sink rescales q15/32768). |v|>1 saturates
    to 0.9999695 by design. Stateless feed-forward — saturation-safe. A pure
    pass-through marker; the real DSP runs on the placeKYT-hosted chip."""

    def __init__(self, device_id="kyttar_0", block_name=""):
        super().__init__("Kyttar Complex To Mag",
                         in_dtype=np.complex64, out_dtype=np.float32)
        self.device_id = device_id
        self._advertise_grc_params(device_id, "ComplexToMagBlock", {},
                                   block_name=block_name)


class complex_to_arg(_PassThrough):
    """Complex argument atan2(y,x) — GR marker (maps to ComplexToArgBlock,
    CORDIC vectoring; GR counterpart blocks.complex_to_arg). Complex in, float
    out. HW-DEVIATION: the chip emits HALF-TURN Q15 angle (word/32768 * pi
    radians) — multiply the sink's float by pi for radians; GR emits radians
    directly. Stateless feed-forward — saturation-safe. A pure pass-through
    marker; the real DSP runs on the placeKYT-hosted chip."""

    def __init__(self, device_id="kyttar_0", block_name=""):
        super().__init__("Kyttar Complex To Arg",
                         in_dtype=np.complex64, out_dtype=np.float32)
        self.device_id = device_id
        self._advertise_grc_params(device_id, "ComplexToArgBlock", {},
                                   block_name=block_name)


class mm_timing_recovery(_PassThrough):
    """Mueller & Müller timing recovery — GR marker (maps to MMTimingRecoveryBlock).

    Decision-directed symbol-timing recovery for MULTILEVEL QAM (16-QAM) — the GR
    ``digital.symbol_sync_cc`` M&M path. Always COMPLEX (I/Q): the recovered (yi, yq)
    center pair is carried as ONE gr_complex stream in/out (the on-chip xi/xq in +
    yi_e/yq_e out pairs; placeKYT's importer splits the single complex net into the
    two rails), feeding a downstream QAM16 Costas + slicer. Unlike Gardner (a
    BPSK/QPSK non-decision-directed TED that leaves ~3% jitter on 16-QAM's 4-level
    axes), M&M locks 16-QAM cleanly at 2 sps. A pure pass-through marker."""

    def __init__(self, device_id="kyttar_0", sps=2, loop_bw=0.02, damping=1.0):
        super().__init__("Kyttar M&M Timing Recovery",
                         in_dtype=np.complex64, out_dtype=np.complex64)
        self.device_id = device_id
        self.sps = int(sps)
        self.loop_bw = float(loop_bw)
        self.damping = float(damping)
        self._advertise_grc_params(device_id, "MMTimingRecoveryBlock",
                                   {"sps": int(sps), "loop_bw": float(loop_bw),
                                    "damping": float(damping)})


class fll_band_edge(_PassThrough):
    """FLL band-edge coarse frequency recovery — GR marker (maps to
    FLLBandEdgeBlock = digital.fll_band_edge_cc).

    The coarse carrier stage of the industry RX cascade (MF -> FLL -> timing ->
    fine DD carrier): pulls a large offset (beyond Costas pull-in) to a residual
    the downstream Costas captures. Always COMPLEX: the corrected I/Q pair rides
    as ONE gr_complex stream in/out (the on-chip xi/xq in + yi_tap/yq_tap out
    pairs; placeKYT's importer splits the single complex net into the two
    rails). A pure pass-through marker; the real DSP runs on the hosted chip."""

    def __init__(self, device_id="kyttar_0", samps_per_sym=2.0, rolloff=0.35,
                 filter_size=17, bandwidth=0.06):
        super().__init__("Kyttar FLL Band-Edge",
                         in_dtype=np.complex64, out_dtype=np.complex64)
        self.device_id = device_id
        self.samps_per_sym = float(samps_per_sym)
        self.rolloff = float(rolloff)
        self.filter_size = int(filter_size)
        self.bandwidth = float(bandwidth)
        self._advertise_grc_params(
            device_id, "FLLBandEdgeBlock",
            {"samps_per_sym": float(samps_per_sym), "rolloff": float(rolloff),
             "filter_size": int(filter_size), "bandwidth": float(bandwidth)})


class bpsk_slicer(_PassThrough):
    """BPSK slicer — GR marker (maps to BPSKSlicerBlock).

    Mirrors GNU Radio ``digital.binary_slicer_fb``: a recovered real sample ->
    hard bit (``sample >= 0 -> 1``, ``sample < 0 -> 0``, tie at 0 -> 1). GR emits
    one byte per sample; ``out_mode="bit"`` reproduces that 1:1 (the GR-equivalent
    default for imports). ``out_mode="byte"``/``"word"`` are a Kyttar-only ergonomic
    packing extension (8/16 bits MSB-first per output word — no GR counterpart) to
    cut output-port pressure in a long receiver chain. A pure pass-through marker;
    the real DSP runs on the placeKYT chip."""

    def __init__(self, device_id="kyttar_0", out_mode="bit"):
        # digital.binary_slicer_fb is float-in / BYTE-out (the recovered hard bit) —
        # declare a float input but a uint8 output so GRC stream connections match.
        super().__init__("Kyttar BPSK Slicer",
                         in_dtype=np.float32, out_dtype=np.uint8)
        self.device_id = device_id
        self.out_mode = str(out_mode)
        self._advertise_grc_params(device_id, "BPSKSlicerBlock",
                                   {"out_mode": str(out_mode)})


class qpsk_slicer(_PassThrough):
    """QPSK slicer — GR marker (maps to QPSKSlicerBlock).

    Final decision stage of the coherent QPSK RX: a recovered (I, Q) symbol-center
    pair -> the 2-bit Gray symbol index (0..3), mirroring GNU Radio
    ``digital.constellation_decoder_cb(constellation_qpsk())``. The recovered pair
    arrives as ONE gr_complex stream (matching the complex Gardner's complex output);
    placeKYT's importer splits it into the on-chip in_i (I@R0)/in_q (Q@R1) rails. A
    pure pass-through marker (carries the graph for import + server-batch run)."""

    def __init__(self, device_id="kyttar_0"):
        super().__init__("Kyttar QPSK Slicer", n_in=1, n_out=1,
                         in_dtype=np.complex64, out_dtype=np.float32)
        self.device_id = device_id
        self._advertise_grc_params(device_id, "QPSKSlicerBlock", {})


class psk_symbol_mapper(_PassThrough):
    """PSK symbol mapper — GR marker (maps to PSKSymbolMapperBlock).

    TX front end: input bit(s) -> COMPLEX PSK constellation symbol, matching GNU
    Radio's ``digital.chunks_to_symbols_bc`` (byte/float in, gr_complex out). BPSK
    constellation points are ``(-1+0j, 1+0j)`` — COMPLEX values with Q=0, so the
    output stream is complex (8 bytes), NOT float; QPSK/8-PSK are complex too. The
    whole PSK TX chain (mapper -> upsampler -> RRC -> I/Q upconvert) is therefore
    COMPLEX end-to-end until the final complex_to_real inside the upconvert — the
    GR-idiomatic BPSK/QPSK transmit."""

    def __init__(self, device_id="kyttar_0", modulation="bpsk",
                 symbol_table=None, dimension=1, bpsk_bit0_positive=True):
        # bit in (float) -> COMPLEX symbol out (chunks_to_symbols_bc equivalent).
        super().__init__("Kyttar PSK Symbol Mapper", n_in=1, n_out=1,
                         in_dtype=np.float32, out_dtype=np.complex64)
        self.device_id = device_id
        self.modulation = modulation
        # GR-native chunks_to_symbols params: an arbitrary complex symbol_table
        # (index-in) and its dimension D (D=1 on chip). When symbol_table is empty
        # the modulation preset is used (Kyttar bit-packing extension).
        self.symbol_table = symbol_table
        self.dimension = int(dimension)
        # BPSK sign convention: True=bit0->+1 (chunks_to_symbols_bf([1,-1])),
        # False=bit0->-1 (constellation_bpsk; pairs with binary_slicer_fb).
        self.bpsk_bit0_positive = bool(bpsk_bit0_positive)
        params = {"modulation": modulation, "dimension": int(dimension),
                  "bpsk_bit0_positive": bool(bpsk_bit0_positive)}
        if symbol_table:
            params["symbol_table"] = list(symbol_table)
        self._advertise_grc_params(device_id, "PSKSymbolMapperBlock", params)


class qam16_symbol_mapper(_PassThrough):
    """16-QAM symbol mapper — GR marker (maps to QAM16SymbolMapperBlock).

    TX front end: 4 input bits (MSB-first) -> the COMPLEX ``digital.constellation_16qam()``
    point, mirroring ``digital.chunks_to_symbols_bc(constellation_16qam().points(), 1)``.
    Bit in (float) -> gr_complex symbol out; the on-chip block emits the (I, Q) pair
    which placeKYT carries as one complex net into the downstream ComplexUpsampler."""

    def __init__(self, device_id="kyttar_0"):
        super().__init__("Kyttar 16-QAM Symbol Mapper", n_in=1, n_out=1,
                         in_dtype=np.float32, out_dtype=np.complex64)
        self.device_id = device_id
        self._advertise_grc_params(device_id, "QAM16SymbolMapperBlock", {})


class qam16_slicer(_PassThrough):
    """16-QAM slicer — GR marker (maps to QAM16SlicerBlock).

    Final decision stage of the coherent 16-QAM RX: a recovered (I, Q) symbol-center
    pair -> the 4-bit symbol index (0..15), mirroring GNU Radio
    ``digital.constellation_decoder_cb(constellation_16qam())``. The recovered pair
    arrives as ONE gr_complex stream; placeKYT's importer splits it into the on-chip
    in_i (I@R0)/in_q (Q@R1) rails. A pure pass-through marker."""

    def __init__(self, device_id="kyttar_0"):
        super().__init__("Kyttar 16-QAM Slicer", n_in=1, n_out=1,
                         in_dtype=np.complex64, out_dtype=np.float32)
        self.device_id = device_id
        self._advertise_grc_params(device_id, "QAM16SlicerBlock", {})


class qam16_costas_loop(_PassThrough):
    """16-QAM decision-directed carrier recovery — GR marker (maps to
    QAM16ComplexCostasLoopBlock).

    A SINGLE complex baseband stream in -> recovered (I, Q) pair out (ONE gr_complex
    stream; placeKYT splits it into the on-chip yi/yq rails). 16-QAM is not
    constant-modulus, so the order-2/4 PSK phase detectors fail — this runs a
    DECISION-DIRECTED loop (slice to the nearest constellation grid point, form the
    error from the decision), the standard ``digital.constellation_receiver_cb(
    constellation_16qam())`` carrier-recovery path."""

    def __init__(self, device_id="kyttar_0", alpha_q15=0x0800, beta_q15=0x0040):
        super().__init__("Kyttar 16-QAM Costas Loop", n_in=1, n_out=1,
                         in_dtype=np.complex64, out_dtype=np.complex64)
        self.device_id = device_id
        self.alpha_q15 = int(alpha_q15)
        self.beta_q15 = int(beta_q15)
        self._advertise_grc_params(device_id, "QAM16ComplexCostasLoopBlock",
                                   {"alpha_q15": int(alpha_q15),
                                    "beta_q15": int(beta_q15)})


class fsk4_symbol_mapper(_PassThrough):
    """M17 4FSK symbol mapper — GR marker (maps to FSK4SymbolMapperBlock).

    TX front end of an M17 4-level FSK (C4FM) modem: a real bit stream (0/1) in ->
    one signed PAM deviation LEVEL per DIBIT out (two LSB-first bits -> one level).
    The four normalised M17 levels are {+1, +1/3, -1/3, -1} (== {+3,+1,-1,-3}·1/3),
    Gray-mapped LSB-first ((b0,b1): (1,0)->+3, (0,0)->+1, (0,1)->-1, (1,1)->-3). Feed
    the level stream to a FrequencyModulator (sensitivity 2*pi*2400/fs) to get the
    M17 +-2400/+-800 Hz deviations. The real DSP runs on the chip; this carries the
    graph (float in -> float out, one level per two input bits)."""

    def __init__(self, device_id="kyttar_0"):
        super().__init__("Kyttar 4FSK Symbol Mapper", n_in=1, n_out=1,
                         in_dtype=np.float32, out_dtype=np.float32)
        self.device_id = device_id
        self._advertise_grc_params(device_id, "FSK4SymbolMapperBlock", {})

    def work(self, input_items, output_items):
        # Faithful M17 4FSK mapping so a GR run shows the PAM level stream: accumulate
        # two bits LSB-first (d = b0 + 2*b1), emit levels[d] on the SECOND bit. The
        # marker is 1:1 (sync_block), so a level per input sample; the intermediate
        # (first-bit) sample repeats the held level (visual only — the chip halves
        # the rate). d=0->+1/3, d=1->+1, d=2->-1/3, d=3->-1.
        levels = [1.0 / 3.0, 1.0, -1.0 / 3.0, -1.0]
        x = input_items[0]
        n = len(x)
        out = output_items[0]
        bidx = getattr(self, "_bidx", 0)
        dacc = getattr(self, "_dacc", 0)
        lvl = getattr(self, "_lvl", 0.0)
        for k in range(n):
            b = int(round(float(x[k]))) & 1
            if bidx == 0:
                dacc = b
                bidx = 1
            else:
                dacc = dacc + 2 * b
                lvl = levels[dacc]
                bidx = 0
                dacc = 0
            out[k] = lvl
        self._bidx, self._dacc, self._lvl = bidx, dacc, lvl
        return n


class fsk4_slicer(_PassThrough):
    """M17 4FSK hard-decision slicer — GR marker (maps to FSK4SlicerBlock).

    RX final stage of an M17 4-level FSK modem: a recovered FM-discriminator LEVEL
    (real) in -> the recovered DIBIT (0..3) it came from, ONE word per input symbol
    (like the QPSK slicer). The exact inverse of :class:`fsk4_symbol_mapper` (thresholds
    at 0 and +-2/3 -> nearest of {+3,+1,-1,-3} -> inverse LSB-first Gray map):
    ``d = b0 + 2*b1`` with ``b0 = (|y| >= 2/3)`` (LSB) and ``b1 = (y < 0)`` (MSB). The
    real DSP runs on the chip; this carries the graph (float level in -> float dibit
    out, 1:1). A downstream that wants the raw 9600-bps bit stream unpacks the dibit."""

    def __init__(self, device_id="kyttar_0"):
        super().__init__("Kyttar 4FSK Slicer", n_in=1, n_out=1,
                         in_dtype=np.float32, out_dtype=np.float32)
        self.device_id = device_id
        self._advertise_grc_params(device_id, "FSK4SlicerBlock", {})

    def work(self, input_items, output_items):
        # Faithful slice so a GR run shows clean 0..3 dibit levels (matches the chip's
        # single-word-per-symbol emit): d = b0 + 2*b1.
        x = input_items[0]
        n = len(x)
        out = output_items[0]
        thr = 2.0 / 3.0
        for k in range(n):
            y = float(x[k])
            b0 = 1 if abs(y) >= thr else 0
            b1 = 1 if y < 0 else 0
            out[k] = float(b0 + 2 * b1)
        return n


class fsk4_sync_timing_recovery(_PassThrough):
    """M17 4FSK sync-word timing recovery — GR marker (maps to
    FSK4SyncTimingRecoveryBlock).

    RX symbol-timing stage of an M17 4-level FSK modem. Gardner (any decision-feedback
    loop) does NOT lock a 4-level FSK signal; real M17 receivers recover timing by
    cross-correlating the known sync word. This block slides the M17 LSF sync word's
    +-1 template over the RX matched-filter stream (2 sps), locks on the first
    correlation peak above a threshold, and decimates 2:1 at the locked symbol phase ->
    one recovered symbol-center value per symbol, feeding the FSK4Slicer. A real float
    stream in/out marker; the DSP runs on the chip. The RX signal must be scaled so the
    outer symbols reach ~full-scale (the fixed correlation + slicer thresholds assume
    outer ~= +-1.0)."""

    def __init__(self, device_id="kyttar_0", threshold=None):
        super().__init__("Kyttar 4FSK Sync Timing Recovery", n_in=1, n_out=1,
                         in_dtype=np.float32, out_dtype=np.float32)
        self.device_id = device_id
        self.threshold = threshold
        params = {} if threshold is None else {"threshold": int(threshold)}
        self._advertise_grc_params(device_id, "FSK4SyncTimingRecoveryBlock", params)


class map_bb(_PassThrough):
    """Per-symbol LUT remap — GR marker (maps to MapBBBlock).

    Byte in -> ``map[in]`` byte out, mirroring GNU Radio ``digital.map_bb``: an
    internal 256-entry table is seeded to the identity then overwritten with the
    user's ``map`` (bytes), so ``out = map[in]`` for ``in < len(map)`` and identity
    pass-through above. The real remap runs ON the chip (a single LOAD-indirect
    table); this marker carries the byte graph so a GRC flowgraph imports + runs. The
    ``map`` parameter mirrors GR's ``map`` verbatim (a vector of ints, default
    ``[0, 1]``)."""

    def __init__(self, device_id="kyttar_0", map=[0, 1]):
        super().__init__("Kyttar Map (map_bb)", n_in=1, n_out=1,
                         in_dtype=np.uint8, out_dtype=np.uint8)
        self.device_id = device_id
        self.map = list(map)
        # GR's d_map: 256-entry identity, first len(map) overwritten (bytes).
        self._d_map = np.arange(256, dtype=np.uint8)
        for i, v in enumerate(self.map):
            self._d_map[i] = int(v) & 0xFF
        self._advertise_grc_params(device_id, "MapBBBlock", {"map": list(self.map)})

    def work(self, input_items, output_items):
        # Faithful remap so a native GR run of the markers shows the real map_bb output.
        x = input_items[0]
        out = output_items[0]
        n = len(x)
        out[:n] = self._d_map[np.asarray(x, dtype=np.uint8)]
        return n


class upsampler(_PassThrough):
    """Upsampler — GR marker (maps to UpsamplerBlock).

    Zero-stuffing rate expander: one input sample -> ``sps`` outputs (the sample,
    then sps-1 zeros). In the modem TX chain it carries the COMPLEX baseband symbol
    (one gr_complex stream in/out) between the mapper and the RRC pulse shaper."""

    def __init__(self, device_id="kyttar_0", sps=4, io_type="complex"):
        # dtype-AGNOSTIC zero-stuffer; io_type selects the stream dtype and MUST equal
        # the .block.yml ``io_type`` default + ${io_type} port dtype. Default COMPLEX:
        # the PSK TX chain carries a COMPLEX baseband symbol (the mapper emits
        # gr_complex). A real-only stream sets io_type=float.
        dt = _io_dtype(io_type)
        super().__init__("Kyttar Upsampler", n_in=1, n_out=1,
                         in_dtype=dt, out_dtype=dt)
        self.device_id = device_id
        self.sps = sps
        self.io_type = io_type
        self._advertise_grc_params(device_id, "UpsamplerBlock", {"sps": sps})


class repeat(_PassThrough):
    """Repeat (hold-upsampler) — GR marker (maps to RepeatBlock).

    Each input sample emitted ``interp`` times — GNU Radio ``blocks.repeat``.
    The symbol-HOLD between a mapper and an amplitude-envelope stage (the PSK31
    RaisedCosineEnvelope consumes a HELD ±A stream, where the zero-stuffing
    upsampler would feed it zeros). ``io_type`` selects the stream dtype exactly
    like the upsampler marker (default float — the PSK31 chain holds a REAL ±A
    symbol rail)."""

    def __init__(self, device_id="kyttar_0", interp=4, io_type="float"):
        dt = _io_dtype(io_type)
        super().__init__("Kyttar Repeat", n_in=1, n_out=1,
                         in_dtype=dt, out_dtype=dt)
        self.device_id = device_id
        self.interp = int(interp)
        self.io_type = io_type
        self._advertise_grc_params(device_id, "RepeatBlock",
                                   {"interp": int(interp)})

    def work(self, input_items, output_items):
        # Faithful hold so a native GR run of the markers shows the held stream.
        # The marker is 1:1 (sync_block): each output mirrors its input sample —
        # the true rate change happens on the chip; this is visual/graph-carrying.
        n = min(len(output_items[0]), len(input_items[0]))
        output_items[0][:n] = input_items[0][:n]
        return n


class unpack_k_bits(_PassThrough):
    """Unpack-k-bits — GR marker (maps to UnpackKBitsBlock).

    Mirrors GNU Radio ``blocks.unpack_k_bits_bb(k)``: one input BYTE -> its low
    ``k`` bits emitted MSB-first as ``k`` output bytes (each 0 or 1). The exact
    inverse of ``blocks.pack_k_bits_bb``. Rate-EXPANDING (1 in -> k out); the real
    DSP runs on the placeKYT chip. Byte stream in/out (this is a bit-manipulation
    block, not a sample block). ``k`` mirrors GR verbatim (1..8)."""

    def __init__(self, device_id="kyttar_0", k=8):
        super().__init__("Kyttar Unpack K Bits", n_in=1, n_out=1,
                         in_dtype=np.uint8, out_dtype=np.uint8)
        self.device_id = device_id
        self.k = int(k)
        self._advertise_grc_params(device_id, "UnpackKBitsBlock", {"k": int(k)})


class complex_upsampler(_PassThrough):
    """Complex Upsampler — GR marker (maps to ComplexUpsamplerBlock).

    The 2-rail (I/Q) twin of :class:`upsampler`: one complex input sample ->
    ``sps`` complex outputs (the sample, then sps-1 (0,0) pairs). In the QPSK modem
    TX chain it carries the COMPLEX baseband symbol (one gr_complex stream in/out)
    between the QPSK mapper and the complex RRC pulse shaper. Unlike the real
    Upsampler (which the BPSK modem uses for its real symbol stream), this maps to
    the on-chip ComplexUpsamplerBlock so BOTH I and Q rails are genuinely
    zero-stuffed on the array."""

    def __init__(self, device_id="kyttar_0", sps=2):
        super().__init__("Kyttar Complex Upsampler", n_in=1, n_out=1,
                         in_dtype=np.complex64, out_dtype=np.complex64)
        self.device_id = device_id
        self.sps = sps
        self._advertise_grc_params(device_id, "ComplexUpsamplerBlock", {"sps": sps})


class complex_gain(_PassThrough):
    """Complex fixed-gain scaler — GR marker (maps to ComplexGainBlock).

    The complex twin of :class:`gain`: multiplies a complex (I, Q) stream by the
    SAME real constant ``gain`` on BOTH rails (out = gain * in), scaling the
    constellation WITHOUT rotation — mirrors GNU Radio ``blocks.multiply_const_cc(
    gain)``. In the 16-QAM RX it gain-stages the matched-filter output up to the
    nominal 0.949 outer level the decision-directed M&M timing recovery + QAM16
    Costas need (both are scale-sensitive). ``gain`` may exceed 1 (a receiver
    amplifies): the on-chip block applies it as an integer-part doubling plus a
    Q15 fractional MULQ, so any ``gain`` in (0, 4) is exact. A pass-through marker;
    the real DSP runs on the placeKYT chip."""

    def __init__(self, device_id="kyttar_0", gain=1.0):
        super().__init__("Kyttar Complex Gain", n_in=1, n_out=1,
                         in_dtype=np.complex64, out_dtype=np.complex64)
        self.device_id = device_id
        self.gain = float(gain)
        self._advertise_grc_params(device_id, "ComplexGainBlock",
                                   {"gain": float(gain)})


class multiply_const_complex(_PassThrough):
    """TRUE complex-constant multiply — GR marker (maps to MultiplyConstComplex).

    The genuine ``blocks.multiply_const_cc(k)`` with a COMPLEX constant
    ``k = re + im·j``: multiplies the complex (I, Q) stream by ``k``, which SCALES
    **and** ROTATES the constellation (yi = xi·re − xq·im, yq = xi·im + xq·re).
    Unlike :class:`complex_gain` (the same real gain on both rails, NO rotation),
    this carries the cross-terms that rotate. ``|re|, |im| < 2`` (a Q15 headroom
    range; the datapath stores re/4, im/4 and restores with a SATURATING <<2, so it
    CLIPS on overload exactly like GR). A pass-through marker; the real DSP runs on
    the placeKYT chip."""

    def __init__(self, device_id="kyttar_0", re=0.7, im=0.5):
        super().__init__("Kyttar Multiply Const Complex", n_in=1, n_out=1,
                         in_dtype=np.complex64, out_dtype=np.complex64)
        self.device_id = device_id
        self.re = float(re)
        self.im = float(im)
        self._advertise_grc_params(device_id, "MultiplyConstComplex",
                                   {"re": float(re), "im": float(im)})


class rrc_pulse_shaper(_PassThrough):
    """RRC pulse shaper — GR marker (maps to RRCPulseShaperBlock).

    TX pulse shaper for the COMPLEX baseband: one gr_complex stream in/out (it
    pulse-shapes the upsampled complex symbol before the I/Q upconvert). The real
    DSP runs on the chip; this only carries the graph."""

    def __init__(self, device_id="kyttar_0", gain=1.0, sampling_freq=None,
                 symbol_rate=1.0, alpha=0.35, ntaps=None, io_type="complex",
                 # --- back-compat aliases (older .grc / .kyt authored span/sps) ----
                 span=None, sps=None):
        # GR-VERBATIM firdes.root_raised_cosine params (gain, sampling_freq,
        # symbol_rate, alpha, ntaps) — the RRCPulseShaperBlock constructor's params
        # (INV-0). span/sps are back-compat ALIASES: an older .grc authored span=8,
        # sps=4; map them onto the GR params (sampling_freq=sps, symbol_rate=1,
        # ntaps=span*sps+1 per firdes' length convention) so those flowgraphs keep
        # loading. When both are absent the GR-verbatim defaults apply (33 taps @ 4 sps).
        if sps is not None:
            sampling_freq = float(sps)
            symbol_rate = 1.0
            if ntaps is None:
                _span = span if span is not None else 8
                ntaps = int(_span) * int(sps) + 1
        if sampling_freq is None:
            sampling_freq = 4.0
        if ntaps is None:
            ntaps = 33
        # io_type selects the stream dtype and MUST equal the .block.yml ``io_type``
        # default + ${io_type} port dtype. Default COMPLEX: the PSK TX chain carries a
        # COMPLEX baseband symbol (GR's interp_fir_filter_ccf). A real-only stream
        # sets io_type=float.
        dt = _io_dtype(io_type)
        super().__init__("Kyttar RRC Pulse Shaper", n_in=1, n_out=1,
                         in_dtype=dt, out_dtype=dt)
        self.device_id = device_id
        self.gain = float(gain)
        self.sampling_freq = float(sampling_freq)
        self.symbol_rate = float(symbol_rate)
        self.alpha = alpha
        self.ntaps = int(ntaps)
        self.io_type = io_type
        # Advertise the placeKYT block's REAL (GR-verbatim) params so the importer
        # sets them: ports/taps come from gain/sampling_freq/symbol_rate/alpha/ntaps.
        self._advertise_grc_params(device_id, "RRCPulseShaperBlock",
                                   {"gain": self.gain,
                                    "sampling_freq": self.sampling_freq,
                                    "symbol_rate": self.symbol_rate,
                                    "alpha": alpha,
                                    "ntaps": self.ntaps})


class iq_upconvert(_PassThrough):
    """I/Q upconvert — GR marker (maps to IQUpconvertBlock).

    ONE COMPLEX baseband in -> REAL passband out: ``s = I*cos(wt) - Q*sin(wt)``,
    free-running NCO. This is exactly GNU Radio's ``multiply_cc(baseband,
    sig_source_c(cos)) -> complex_to_real`` (complex -> float). On-chip the block
    reads the I/Q as a complex PACKET (I@R0, Q@R1). To upconvert a REAL signal, put
    a float->complex block in front (blocks_float_to_complex) — the block itself is
    COMPLEX-ONLY, never a dual-purpose real/complex rail (that dual role was the
    source of every dtype/broker bug)."""

    def __init__(self, device_id="kyttar_0", sample_rate=32000.0,
                 frequency=4000.0, block_name=""):
        # ONE complex baseband in -> real passband out (multiply_cc -> complex_to_real).
        super().__init__("Kyttar I/Q Upconvert", n_in=1, n_out=1,
                         in_dtype=np.complex64, out_dtype=np.float32)
        self.device_id = device_id
        self.sample_rate = sample_rate
        self.frequency = frequency
        # block_name pins the placeKYT block this instance advertises for
        # (multi-instance flowgraphs — see complex_mixer).
        self._advertise_grc_params(device_id, "IQUpconvertBlock",
                                   {"sample_rate": sample_rate,
                                    "frequency": frequency},
                                   block_name=block_name)


class not_bb(_PassThrough):
    """Bitwise NOT of a byte stream — GR marker (maps to NotBlock).

    Drop-in for GNU Radio ``blocks.not_bb``: ONE byte (uint8) input -> ONE byte
    output, ``out = (~in) & 0xFF`` over the FULL 8-bit width (``0x00 -> 0xFF``,
    ``0x0F -> 0xF0``, ``0xAA -> 0x55``). No parameters (not_bb takes none). The real
    DSP runs on the placeKYT chip; this marker carries the graph for import + a
    faithful host-side preview."""

    def __init__(self, device_id="kyttar_0"):
        super().__init__("Kyttar Not", n_in=1, n_out=1,
                         in_dtype=np.uint8, out_dtype=np.uint8)
        self.device_id = device_id
        self._advertise_grc_params(device_id, "NotBlock", {})

    def work(self, input_items, output_items):
        # Faithful (~in)&0xFF byte NOT so a GR run shows the complemented stream
        # (the chip computes the identical full-width byte complement).
        x = input_items[0]
        n = min(len(output_items[0]), len(x))
        output_items[0][:n] = (~x[:n].astype(np.int32) & 0xFF).astype(np.uint8)
        return n


class complex_to_float(_PassThrough):
    """Complex -> Float split — GR marker (maps to ComplexToFloatBlock).

    ONE complex baseband stream in -> TWO real streams out (out_re = I, out_im = Q).
    On the chip this is the identity datapath that de-interleaves the I/Q pair onto
    two rails so downstream REAL blocks (e.g. two Low Pass Filters) can process each
    axis. GR marker only; the real DSP runs on the placeKYT chip."""

    def __init__(self, device_id="kyttar_0"):
        super().__init__("Kyttar Complex To Float", n_in=1, n_out=2,
                         in_dtype=np.complex64, out_dtype=np.float32)
        self.device_id = device_id
        self._advertise_grc_params(device_id, "ComplexToFloatBlock", {})

    def work(self, input_items, output_items):
        # out0 = real(in), out1 = imag(in) — a faithful complex->2xfloat split so
        # a GR run of the flowgraph shows the two rails (the chip does the same).
        x = input_items[0]
        n = min(len(output_items[0]), len(output_items[1]), len(x))
        output_items[0][:n] = x[:n].real
        output_items[1][:n] = x[:n].imag
        return n


class char_to_float(_PassThrough):
    """Char -> Float type convert — GR marker (maps to CharToFloatBlock).

    Drop-in for GNU Radio ``blocks.char_to_float`` (``out = in / scale``). ONE int8
    (byte) stream in -> ONE float stream out.

    HW-DEVIATION (Q15): a Kyttar 'float' is a Q15 value in [-1, 1), so the byte's
    value ``in/scale`` only fits when ``scale >= 128``. GR's default ``scale = 1``
    is NOT representable on the fabric — the placed CharToFloatBlock RAISES on any
    ``scale < 128`` (128 maps the int8 range onto the full [-1, 1) span). GR marker
    only; the real DSP runs on the placeKYT chip."""

    def __init__(self, device_id="kyttar_0", scale=128.0):
        super().__init__("Kyttar Char To Float", n_in=1, n_out=1,
                         in_dtype=np.int8, out_dtype=np.float32)
        self.device_id = device_id
        self._scale = scale
        self._advertise_grc_params(device_id, "CharToFloatBlock",
                                   {"scale": scale})

    def set_scale(self, scale):
        self._scale = scale

    def get_scale(self):
        return self._scale

    def work(self, input_items, output_items):
        # out = in / scale (a faithful char->float convert so a GR run of the
        # flowgraph matches the chip).
        x = input_items[0]
        n = min(len(output_items[0]), len(x))
        output_items[0][:n] = x[:n].astype(np.float32) / self._scale
        return n


class complex_to_real(_PassThrough):
    """Complex -> Real (take the real part) — GR marker (maps to ComplexToRealBlock).

    ONE complex stream in -> ONE real stream out (``out = Re(in)``). On the chip this
    is a placed 1-cell block that reads the I/Q pair and emits ONLY the real rail --
    used to turn a complex passband (e.g. an FM modulator's ``exp(jφ)``) into a REAL
    passband for viewing/output. Unlike GNU Radio's ``blocks_complex_to_real`` (which
    the placeKYT importer DISSOLVES, relying on the upstream cell to shape one rail --
    which the FM emit cell cannot do, it always emits BOTH rails), this ``kyttar_``
    binding is PLACED as a real block that genuinely drops the Q rail."""

    def __init__(self, device_id="kyttar_0"):
        super().__init__("Kyttar Complex To Real", n_in=1, n_out=1,
                         in_dtype=np.complex64, out_dtype=np.float32)
        self.device_id = device_id
        self._advertise_grc_params(device_id, "ComplexToRealBlock", {})

    def work(self, input_items, output_items):
        x = input_items[0]
        n = min(len(output_items[0]), len(x))
        output_items[0][:n] = x[:n].real
        return n


class frequency_modulator(_PassThrough):
    """FM modulator (VCO) — GR marker (maps to FrequencyModulatorBlock).

    Drop-in for GNU Radio ``analog.frequency_modulator_fc(sensitivity)``: ONE real
    input (the message) -> ONE COMPLEX output ``exp(j·phi)``, ``phi += sensitivity·x``.
    float in -> complex out, matching the verified block (see
    verification/tests/test_frequency_modulator.py). The real DSP runs on the chip;
    this carries the graph."""

    def __init__(self, device_id="kyttar_0", sensitivity=1.0, pipeline_lock=False):
        super().__init__("Kyttar Frequency Modulator", n_in=1, n_out=1,
                         in_dtype=np.float32, out_dtype=np.complex64)
        self.device_id = device_id
        self.sensitivity = sensitivity
        # SATURATION serialize-LOCK (INV-20): required True for a pipelined/saturated
        # TX (e.g. the 2-sps 4FSK modem) or the reconvergent fan-in drops every other
        # sample; advertised so the importer builds the locked FrequencyModulatorBlock.
        self.pipeline_lock = bool(pipeline_lock)
        self._advertise_grc_params(device_id, "FrequencyModulatorBlock",
                                   {"sensitivity": sensitivity,
                                    "pipeline_lock": bool(pipeline_lock)})

    def work(self, input_items, output_items):
        # A faithful float->complex FM so a GR run of the flowgraph shows the passband
        # (the chip does the same). Integrate phase, emit exp(j*phi).
        x = input_items[0]
        n = min(len(output_items[0]), len(x))
        phi = getattr(self, "_phi", 0.0)
        dphi = float(self.sensitivity) * x[:n].astype(np.float64)
        phase = phi + np.cumsum(dphi)
        output_items[0][:n] = np.exp(1j * phase).astype(np.complex64)
        self._phi = float(phase[-1]) if n else phi
        return n


class quadrature_demod(_PassThrough):
    """FM demodulator (quadrature discriminator) — GR marker (maps to
    QuadratureDemodBlock).

    Drop-in for GNU Radio ``analog.quadrature_demod_cf(gain)``: ONE COMPLEX input ->
    ONE real output ``gain·arg(x[n]·conj(x[n-1]))`` (the FM discriminator). complex in
    -> float out, matching the verified block (see
    verification/tests/test_quadrature_demod.py)."""

    def __init__(self, device_id="kyttar_0", gain=1.0):
        super().__init__("Kyttar Quadrature Demod", n_in=1, n_out=1,
                         in_dtype=np.complex64, out_dtype=np.float32)
        self.device_id = device_id
        self.gain = gain
        self._advertise_grc_params(device_id, "QuadratureDemodBlock",
                                   {"gain": gain})

    def work(self, input_items, output_items):
        # Faithful complex->float FM discriminator: gain*arg(x[n]*conj(x[n-1])).
        x = input_items[0]
        n = min(len(output_items[0]), len(x))
        prev = getattr(self, "_prev", np.complex64(0))
        xp = np.empty(n, dtype=np.complex64)
        if n:
            xp[0] = prev
            xp[1:] = x[:n - 1]
            prod = x[:n] * np.conj(xp)
            output_items[0][:n] = (float(self.gain) * np.angle(prod)).astype(np.float32)
            self._prev = x[n - 1]
        return n


class splitter(_PassThrough):
    """Stream splitter — GR marker (maps to SplitterBlock).

    A GNU Radio port fans out natively, so GR-side this is a plain copy; on the
    chip it is an explicit 1-cell fan-out relay authored with a reserved exit
    tail (up to 8 arms — one WRITE+JUMP pair per arm). The importer also
    auto-splices one wherever a .grc fans a single-rail block output to ≥2
    different blocks (or ≥3 inputs) — place it explicitly to control WHERE the
    fan-out cell sits, or to tree still-wider fan-outs. GR marker only; the
    real relay runs on the placeKYT chip."""

    def __init__(self, device_id="kyttar_0"):
        super().__init__("Kyttar Splitter", n_in=1, n_out=1,
                         in_dtype=np.float32, out_dtype=np.float32)
        self.device_id = device_id
        self._advertise_grc_params(device_id, "StreamSplitterBlock", {})


class abs_bb(_PassThrough):
    """Absolute value — GR marker (maps to AbsBlock).

    Drop-in for GNU Radio ``blocks.abs_ff`` (``out = |in|``): ONE real stream in ->
    ONE real stream out. Single-cell conditional negate on the chip. GR marker only;
    the real DSP runs on the placeKYT chip."""

    def __init__(self, device_id="kyttar_0"):
        super().__init__("Kyttar Abs", n_in=1, n_out=1,
                         in_dtype=np.float32, out_dtype=np.float32)
        self.device_id = device_id
        self._advertise_grc_params(device_id, "AbsBlock", {})

    def work(self, input_items, output_items):
        x = input_items[0]
        n = min(len(output_items[0]), len(x))
        output_items[0][:n] = np.abs(x[:n])
        return n


class conjugate(_PassThrough):
    """Complex conjugate — GR marker (maps to ConjugateBlock).

    Drop-in for GNU Radio ``blocks.conjugate_cc`` (``out = conj(in) = re - j*im``):
    ONE complex stream in -> ONE complex stream out. Single cell: pass I, negate Q.
    GR marker only; the real DSP runs on the placeKYT chip."""

    def __init__(self, device_id="kyttar_0"):
        super().__init__("Kyttar Conjugate", n_in=1, n_out=1,
                         in_dtype=np.complex64, out_dtype=np.complex64)
        self.device_id = device_id
        self._advertise_grc_params(device_id, "ConjugateBlock", {})

    def work(self, input_items, output_items):
        x = input_items[0]
        n = min(len(output_items[0]), len(x))
        output_items[0][:n] = np.conj(x[:n])
        return n


class complex_to_imag(_PassThrough):
    """Complex -> Imag (take the imaginary part) — GR marker (maps to
    ComplexToImagBlock).

    Drop-in for GNU Radio ``blocks.complex_to_imag`` (``out = Im(in)``): ONE complex
    stream in -> ONE real stream out. Single-cell selector on the chip that forwards
    the Q rail. GR marker only; the real DSP runs on the placeKYT chip."""

    def __init__(self, device_id="kyttar_0"):
        super().__init__("Kyttar Complex To Imag", n_in=1, n_out=1,
                         in_dtype=np.complex64, out_dtype=np.float32)
        self.device_id = device_id
        self._advertise_grc_params(device_id, "ComplexToImagBlock", {})

    def work(self, input_items, output_items):
        x = input_items[0]
        n = min(len(output_items[0]), len(x))
        output_items[0][:n] = x[:n].imag
        return n


class complex_to_mag_squared(_PassThrough):
    """Complex -> Mag^2 (instantaneous power) — GR marker (maps to
    ComplexToMagSquaredBlock).

    Drop-in for GNU Radio ``blocks.complex_to_mag_squared`` (``out = re^2 + im^2``):
    ONE complex stream in -> ONE real stream out. Single cell (MULQ + MACQ) on the
    chip, saturating the [0,2) power range into Q15. GR marker only; the real DSP
    runs on the placeKYT chip."""

    def __init__(self, device_id="kyttar_0"):
        super().__init__("Kyttar Complex To Mag^2", n_in=1, n_out=1,
                         in_dtype=np.complex64, out_dtype=np.float32)
        self.device_id = device_id
        self._advertise_grc_params(device_id, "ComplexToMagSquaredBlock", {})

    def work(self, input_items, output_items):
        x = input_items[0]
        n = min(len(output_items[0]), len(x))
        output_items[0][:n] = (x[:n].real ** 2 + x[:n].imag ** 2).astype(np.float32)
        return n


class float_to_complex(_PassThrough):
    """Float -> Complex join — GR marker (maps to FloatToComplexBlock).

    Drop-in for GNU Radio ``blocks.float_to_complex`` (``out = re + j*im`` from TWO
    real streams): TWO real streams in (re @ port 0, im @ port 1) -> ONE complex
    stream out. The complement of complex_to_float and the SAME identity datapath on
    the chip. GR marker only; the real DSP runs on the placeKYT chip."""

    def __init__(self, device_id="kyttar_0"):
        super().__init__("Kyttar Float To Complex", n_in=2, n_out=1,
                         in_dtype=np.float32, out_dtype=np.complex64)
        self.device_id = device_id
        self._advertise_grc_params(device_id, "FloatToComplexBlock", {})

    def work(self, input_items, output_items):
        re = input_items[0]
        im = input_items[1]
        n = min(len(output_items[0]), len(re), len(im))
        output_items[0][:n] = (re[:n] + 1j * im[:n]).astype(np.complex64)
        return n


class dual_float_to_complex(_PassThrough):
    """Float -> Complex rendezvous — GR marker (maps to DualFloatToComplexBlock).

    The PHYSICAL ``blocks.float_to_complex`` for TWO INDEPENDENT, asynchronously-timed
    real producers (I from one source, Q from another) feeding a complex block. TWO
    real streams in (i @ port 0, q @ port 1) -> ONE complex stream out. On the chip a
    1-cell arbiter-LOCK pairs the two rails by arrival FACE. GR marker only; the real
    DSP runs on the placeKYT chip.

    The face_i/face_q/hop/dest_i/dest_q/entry placement knobs of the placed block are
    router-reconciled internals (see DualFloatToComplexBlock.GRC_UNSUPPORTED_PARAMS) and
    are intentionally NOT exposed to GRC — GNU Radio's float_to_complex has no such
    params, so exposing them would break the 1:1 drop-in contract."""

    def __init__(self, device_id="kyttar_0"):
        super().__init__("Kyttar Float To Complex", n_in=2, n_out=1,
                         in_dtype=np.float32, out_dtype=np.complex64)
        self.device_id = device_id
        self._advertise_grc_params(device_id, "DualFloatToComplexBlock", {})

    def work(self, input_items, output_items):
        i = input_items[0]
        q = input_items[1]
        n = min(len(output_items[0]), len(i), len(q))
        output_items[0][:n] = (i[:n] + 1j * q[:n]).astype(np.complex64)
        return n


class keep_one_in_n(_PassThrough):
    """Keep 1 in N — GR marker (maps to KeepOneInNBlock).

    Drop-in for GNU Radio ``blocks.keep_one_in_n`` (keep 1 sample of every ``n``, no
    filter): ONE real stream in -> ONE decimated real stream out. A modulo-``n`` emit
    gate on the chip (the decimator WITHOUT the FIR). GR marker only; the real DSP
    runs on the placeKYT chip."""

    def __init__(self, device_id="kyttar_0", n=2):
        super().__init__("Kyttar Keep 1 in N", n_in=1, n_out=1,
                         in_dtype=np.float32, out_dtype=np.float32)
        self.device_id = device_id
        self.n = int(n)
        # MARKER CONVENTION (matches the Decimator marker): plain 1:1
        # pass-through — the REAL decimation runs on the chip and the kyttar
        # SINK emits the recovered (already-decimated) stream. A sync_block
        # faking the rate change (set_relative_rate(1/n) + a partial-return
        # work) DEADLOCKS the client scheduler at flowgraph end: sync work's
        # return value is both produce AND consume, so the input tail is never
        # drained and tb.run() hangs (the effect_echo real-client hang).
        self._advertise_grc_params(device_id, "KeepOneInNBlock", {"n": int(n)})


class zero_crossing_rate(_PassThrough):
    """Zero-Crossing Rate — GR marker (maps to ZeroCrossingRateBlock).

    Windowed zero-crossing rate of a real stream (placeKYT-native; no stock GNU
    Radio streaming counterpart): per non-overlapping window of ``window_size``
    samples, the crossing count / window_size in [0, 1). Rate-REDUCING
    (window_size in -> 1 out) on the chip. GR marker only; the real DSP runs on
    the placeKYT chip — plain 1:1 pass-through here (the keep_one_in_n marker
    convention: a sync_block faking the rate change deadlocks the client
    scheduler at flowgraph end)."""

    def __init__(self, device_id="kyttar_0", window_size=64):
        super().__init__("Kyttar Zero-Crossing Rate", n_in=1, n_out=1,
                         in_dtype=np.float32, out_dtype=np.float32)
        self.device_id = device_id
        self.window_size = int(window_size)
        self._advertise_grc_params(device_id, "ZeroCrossingRateBlock",
                                   {"window_size": int(window_size)})


class bin_argmax(_PassThrough):
    """Framewise Argmax — GR marker (maps to BinArgmaxBlock).

    Framewise argmax of a real stream (placeKYT-native; no stock GNU Radio
    streaming counterpart): per non-overlapping frame of ``n`` samples, ONE raw
    integer word = the ZERO-BASED index (0..n-1) of the frame's maximum (ties:
    FIRST occurrence; comparison is SIGNED). Rate-REDUCING (n in -> 1 out) on
    the chip. The output is an INDEX word, not a Q15 sample (the crc16 raw-word
    convention — dtype short). GR marker only; the real DSP runs on the placeKYT
    chip — plain 1:1 pass-through here (the keep_one_in_n marker convention: a
    sync_block faking the rate change deadlocks the client scheduler at
    flowgraph end)."""

    def __init__(self, device_id="kyttar_0", n=128):
        super().__init__("Kyttar Framewise Argmax", n_in=1, n_out=1,
                         in_dtype=np.float32, out_dtype=np.int16)
        self.device_id = device_id
        self.n = int(n)
        self._advertise_grc_params(device_id, "BinArgmaxBlock", {"n": int(n)})


class sigmoid(_PassThrough):
    """Sigmoid — GR marker (maps to SigmoidBlock).

    Q15 logistic sigmoid ``out = 1/(1+exp(-a))`` (placeKYT-native; no stock
    GNU Radio counterpart). The input sample x in [-1, 1) is interpreted with
    a configurable binary point: ``a = x * 2**(3 + dshift)`` — the default
    ``dshift = 0`` maps full-scale input to +-8 (the canonical domain). An
    upstream dot product prescaled by ``2**-S`` for Q15 headroom is
    compensated with ``dshift = S - 3`` at zero on-chip instruction cost.
    On the chip: a two-cell 16-interval table + linear interpolation
    (max abs error 0.0030 vs float, exhaustive). GR marker only; the real
    DSP runs on the placeKYT chip."""

    def __init__(self, device_id="kyttar_0", dshift=0):
        super().__init__("Kyttar Sigmoid", n_in=1, n_out=1,
                         in_dtype=np.float32, out_dtype=np.float32)
        self.device_id = device_id
        self.dshift = int(dshift)
        self._advertise_grc_params(device_id, "SigmoidBlock",
                                   {"dshift": int(dshift)})

    def work(self, input_items, output_items):
        x = input_items[0].astype(np.float64)
        n = min(len(output_items[0]), len(x))
        a = x[:n] * float(2.0 ** (3 + self.dshift))
        output_items[0][:n] = (1.0 / (1.0 + np.exp(-a))).astype(np.float32)
        return n


class tanh(_PassThrough):
    """Tanh — GR marker (maps to TanhBlock).

    Q15 hyperbolic tangent ``out = tanh(a)`` (placeKYT-native; no stock
    GNU Radio counterpart). The input sample x in [-1, 1) is interpreted with
    a configurable binary point: ``a = x * 2**(2 + dshift)`` — the default
    ``dshift = 0`` maps full-scale input to +-4 (the canonical domain). An
    upstream dot product prescaled by ``2**-S`` is compensated with
    ``dshift = S - 2`` at zero on-chip instruction cost. On the chip: the
    same two-cell 16-interval table + linear interpolation engine as Sigmoid
    (max abs error 0.0060 vs float, exhaustive). GR marker only; the real
    DSP runs on the placeKYT chip."""

    def __init__(self, device_id="kyttar_0", dshift=0):
        super().__init__("Kyttar Tanh", n_in=1, n_out=1,
                         in_dtype=np.float32, out_dtype=np.float32)
        self.device_id = device_id
        self.dshift = int(dshift)
        self._advertise_grc_params(device_id, "TanhBlock",
                                   {"dshift": int(dshift)})

    def work(self, input_items, output_items):
        x = input_items[0].astype(np.float64)
        n = min(len(output_items[0]), len(x))
        a = x[:n] * float(2.0 ** (2 + self.dshift))
        output_items[0][:n] = np.tanh(a).astype(np.float32)
        return n


class moving_average(_PassThrough):
    """Moving Average — GR marker (maps to MovingAverageBlock).

    Drop-in for GNU Radio ``blocks.moving_average_ff`` (``out = scale * sum of the last
    ``length`` samples``): ONE real stream in -> ONE real stream out. On the chip this
    is a FIR whose ``length`` taps all equal ``scale`` (the box-car running average).
    GR marker only; the real DSP runs on the placeKYT chip."""

    def __init__(self, device_id="kyttar_0", length=4, scale=0.25):
        super().__init__("Kyttar Moving Average", n_in=1, n_out=1,
                         in_dtype=np.float32, out_dtype=np.float32)
        self.device_id = device_id
        self.length = int(length)
        self.scale = float(scale)
        self._advertise_grc_params(device_id, "MovingAverageBlock",
                                   {"length": int(length), "scale": float(scale)})

    def work(self, input_items, output_items):
        # Faithful box-car moving average: out[k] = scale * sum(x[k-length+1 .. k]).
        x = input_items[0].astype(np.float64)
        n = min(len(output_items[0]), len(x))
        L = max(1, int(self.length))
        prev = getattr(self, "_hist", np.zeros(L - 1, dtype=np.float64))
        buf = np.concatenate([prev, x[:n]])
        csum = np.cumsum(np.concatenate([[0.0], buf]))
        out = (csum[L:] - csum[:-L])[:n] * float(self.scale)
        output_items[0][:n] = out.astype(np.float32)
        self._hist = buf[-(L - 1):] if L > 1 else np.zeros(0, dtype=np.float64)
        return n


class rms(_PassThrough):
    """RMS (real) — GR marker (maps to RMSBlock).

    Drop-in for GNU Radio ``blocks.rms_ff``: single-pole IIR power average
    ``avg = (1-alpha)*avg + alpha*x^2`` then ``out = sqrt(avg)``. ONE real
    stream in -> ONE real stream out. On the chip: a 4-cell feed-forward chain
    (power+IIR with full-precision error feedback -> normalize -> quartic
    sqrt -> denormalize). GR marker only; the real DSP runs on the placeKYT
    chip. HW-DEVIATION: alpha is quantized to Q15 (the default 1e-4 runs as
    3/32768; the settled RMS is unchanged, only the time constant shifts ~8%)."""

    def __init__(self, device_id="kyttar_0", alpha=0.0001):
        super().__init__("Kyttar RMS", n_in=1, n_out=1,
                         in_dtype=np.float32, out_dtype=np.float32)
        self.device_id = device_id
        self.alpha = float(alpha)
        self._advertise_grc_params(device_id, "RMSBlock",
                                   {"alpha": float(alpha)})

    def work(self, input_items, output_items):
        x = input_items[0].astype(np.float64)
        n = min(len(output_items[0]), len(x))
        avg = float(getattr(self, "_avg", 0.0))
        a = float(self.alpha)
        out = np.empty(n, dtype=np.float64)
        for k in range(n):
            avg = (1.0 - a) * avg + a * x[k] * x[k]
            out[k] = math.sqrt(avg)
        self._avg = avg
        output_items[0][:n] = out.astype(np.float32)
        return n


class rms_cf(_PassThrough):
    """RMS (complex) — GR marker (maps to RMSCFBlock).

    Drop-in for GNU Radio ``blocks.rms_cf``: the same single-pole averager run
    on ``|z|^2 = re^2 + im^2`` with a REAL output. ONE complex stream in -> ONE
    real stream out. Same 4-cell chip datapath as ``rms`` with a
    ComplexToMagSquared front. GR marker only; the real DSP runs on the
    placeKYT chip. HW-DEVIATION: alpha quantized to Q15 (see ``rms``)."""

    def __init__(self, device_id="kyttar_0", alpha=0.0001):
        super().__init__("Kyttar RMS (Complex)", n_in=1, n_out=1,
                         in_dtype=np.complex64, out_dtype=np.float32)
        self.device_id = device_id
        self.alpha = float(alpha)
        self._advertise_grc_params(device_id, "RMSCFBlock",
                                   {"alpha": float(alpha)})

    def work(self, input_items, output_items):
        z = input_items[0]
        n = min(len(output_items[0]), len(z))
        avg = float(getattr(self, "_avg", 0.0))
        a = float(self.alpha)
        out = np.empty(n, dtype=np.float64)
        for k in range(n):
            p = float(z[k].real) ** 2 + float(z[k].imag) ** 2
            avg = (1.0 - a) * avg + a * p
            out[k] = math.sqrt(avg)
        self._avg = avg
        output_items[0][:n] = out.astype(np.float32)
        return n


class r2_butterfly(_PassThrough):
    """Radix-2 DIF butterfly — GR marker (maps to R2ButterflyBlock).

    Two complex streams in (a, b) -> two complex streams out::

        sum  = (a + b) / 2        diff = (a - b) / 2

    the streaming radix-2 FFT primitive with the pinned unconditional
    scale-by-2 (round-half-to-even on chip; this marker computes the float
    form for a faithful host-side preview). Output 0 is the SUM pair, output
    1 is the DIFFERENCE pair. The real DSP runs ON the chip (8-cell 2x4
    serpentine, both output pairs on their own cells); this carries the
    graph. Connect BOTH outputs (an unused chip output pair should be
    terminated, not left dangling)."""

    def __init__(self, device_id="kyttar_0"):
        super().__init__("Kyttar R2 Butterfly", n_in=2, n_out=2,
                         in_dtype=np.complex64, out_dtype=np.complex64)
        self.device_id = device_id
        self._advertise_grc_params(device_id, "R2ButterflyBlock", {})

    def work(self, input_items, output_items):
        a = input_items[0]
        b = input_items[1]
        n = min(len(a), len(b), len(output_items[0]), len(output_items[1]))
        output_items[0][:n] = (a[:n] + b[:n]) / 2.0
        output_items[1][:n] = (a[:n] - b[:n]) / 2.0
        return n


class twiddle_multiply(_PassThrough):
    """Per-sample table-selected twiddle multiply — GR marker (maps to
    TwiddleMultiplyBlock).

    ``y[n] = x[n] * twiddles[n mod P]`` with ``P = len(twiddles)`` — the
    streaming radix-2 DIF FFT stage's twiddle rotator. Twiddles are stored
    round(32768*x) Q15 on chip; exactly ``1`` and exactly ``-1j`` are
    structurally special-cased (pass-through / swap + saturating negate) and
    P is capped at 12 in-cell table entries (the block raises beyond). This
    marker applies the float table walk for a faithful host-side preview."""

    def __init__(self, device_id="kyttar_0", twiddles=(1,)):
        super().__init__("Kyttar Twiddle Multiply", n_in=1, n_out=1,
                         in_dtype=np.complex64, out_dtype=np.complex64)
        self.device_id = device_id
        self.twiddles = [complex(w) for w in twiddles]
        self._n = 0
        self._advertise_grc_params(device_id, "TwiddleMultiplyBlock",
                                   {"twiddles": list(self.twiddles)})

    def work(self, input_items, output_items):
        x = input_items[0]
        out = output_items[0]
        n = min(len(x), len(out))
        P = max(1, len(self.twiddles))
        w = np.array([self.twiddles[(self._n + k) % P] for k in range(n)],
                     dtype=np.complex64)
        out[:n] = x[:n] * w
        self._n = (self._n + n) % P
        return n


class chirp_symbol_mapper(_PassThrough):
    """CSS symbol mapper — GR marker (maps to ChirpSymbolMapperBlock).

    Packs k = log2(m) input bits (LSB of each item, GR pack_k_bits_bb
    semantics) MSB-first into one RAW symbol word 0..m-1 — GNU Radio
    ``blocks.pack_k_bits_bb(log2 m)`` re-parameterized for CSS with the uint8
    output cap lifted (raw 16-bit symbol word, m up to 32768). Rate-REDUCING
    (k in -> 1 out) on the chip; a trailing partial group is dropped. GR
    marker only; the real DSP runs on the placeKYT chip — plain 1:1
    pass-through here (the keep_one_in_n marker convention: a sync_block
    faking the rate change deadlocks the client scheduler at flowgraph end)."""

    def __init__(self, device_id="kyttar_0", m=128):
        super().__init__("Kyttar Chirp Symbol Mapper", n_in=1, n_out=1,
                         in_dtype=np.uint8, out_dtype=np.int16)
        self.device_id = device_id
        self.m = int(m)
        self._advertise_grc_params(device_id, "ChirpSymbolMapperBlock",
                                   {"m": int(m)})


class chirp_generator(_PassThrough):
    """CSS chirp generator — GR marker (maps to ChirpGeneratorBlock).

    For each RAW symbol word s (0..m-1) the chip emits n complex samples of
    the cyclic-shifted linear up-chirp starting at (s/m - 1/2)*BW and sweeping
    upward by BW with the mod-BW wrap (the 16-bit double phase accumulator;
    phase CARRIES across symbols). Rate-EXPANDING (1 in -> n complex out) on
    the chip. GR marker only; the real DSP runs on the placeKYT chip — plain
    1:1 pass-through here (the keep_one_in_n marker convention: a sync_block
    faking the rate change deadlocks the client scheduler at flowgraph end)."""

    def __init__(self, device_id="kyttar_0", n=128, m=128, amplitude=1.0):
        super().__init__("Kyttar Chirp Generator", n_in=1, n_out=1,
                         in_dtype=np.int16, out_dtype=np.complex64)
        self.device_id = device_id
        self.n = int(n)
        self.m = int(m)
        self.amplitude = float(amplitude)
        self._advertise_grc_params(device_id, "ChirpGeneratorBlock",
                                   {"n": int(n), "m": int(m),
                                    "amplitude": float(amplitude)})
