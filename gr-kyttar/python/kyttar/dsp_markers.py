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

    def _advertise_grc_params(self, device_id, placekyt_type, params):
        """Record this marker's params for GRC↔placeKYT sync advertising.

        Markers call this in ``__init__`` to declare their placeKYT TYPE (e.g.
        ``"GainBlock"``) and current params. The ACTUAL registration into the
        shared per-device BatchSession happens in :meth:`start` (every flowgraph
        run), so the params reach the session fresh each run alongside the source's
        batch dispatch — which sends them to placeKYT for drift detection. Minimal
        and never crashy: a marker that can't determine its type simply records
        nothing; advertising is best-effort telemetry, not on the data path."""
        self._grc_advert = (str(device_id), str(placekyt_type), dict(params or {}))

    def start(self) -> bool:
        # Register the recorded advertisement into the per-device BatchSession
        # each run, so the source's batch dispatch ships current params to placeKYT
        # (GRC↔placeKYT sync indicator). Best-effort: never break the flowgraph.
        advert = getattr(self, "_grc_advert", None)
        if advert is not None:
            try:
                from ._batch_session import get_session
                device_id, placekyt_type, params = advert
                get_session(device_id).register_params(placekyt_type, params)
            except Exception:  # noqa: BLE001 — advertising is best-effort
                pass
        return True

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

    def __init__(self, device_id="kyttar_0", alpha=0.35, span=8, decimation=1):
        super().__init__("Kyttar Complex RRC Matched Filter", n_in=1, n_out=1,
                         in_dtype=np.complex64, out_dtype=np.complex64)
        self.device_id = device_id
        self.alpha = alpha
        self.span = span
        self.decimation = int(decimation)
        # placeKYT uses `beta` for the roll-off (GRC marker calls it `alpha`).
        self._advertise_grc_params(device_id, "ComplexRRCMatchedFilterBlock",
                                   {"beta": alpha, "span": span,
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


class bpsk_slicer(_PassThrough):
    """BPSK slicer — GR marker (maps to BPSKSlicerBlock)."""

    def __init__(self, device_id="kyttar_0"):
        super().__init__("Kyttar BPSK Slicer")
        self.device_id = device_id


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

    def __init__(self, device_id="kyttar_0", modulation="bpsk"):
        # bit in (float) -> COMPLEX symbol out (chunks_to_symbols_bc equivalent).
        super().__init__("Kyttar PSK Symbol Mapper", n_in=1, n_out=1,
                         in_dtype=np.float32, out_dtype=np.complex64)
        self.device_id = device_id
        self.modulation = modulation
        self._advertise_grc_params(device_id, "PSKSymbolMapperBlock",
                                   {"modulation": modulation})


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
    (real) in -> the 2-bit DIBIT it came from out (b0 LSB then b1 MSB). The exact
    inverse of :class:`fsk4_symbol_mapper` (thresholds at 0 and +-2/3 -> nearest of
    {+3,+1,-1,-3} -> inverse LSB-first Gray map): b0 = (|y| >= 2/3), b1 = (y < 0).
    The real DSP runs on the chip; this carries the graph (float level in -> float
    bit out, two bits per input level)."""

    def __init__(self, device_id="kyttar_0"):
        super().__init__("Kyttar 4FSK Slicer", n_in=1, n_out=1,
                         in_dtype=np.float32, out_dtype=np.float32)
        self.device_id = device_id
        self._advertise_grc_params(device_id, "FSK4SlicerBlock", {})

    def work(self, input_items, output_items):
        # Faithful slice so a GR run shows a bit stream. The chip emits TWO bits per
        # input level; the 1:1 marker can only show one word per sample, so emit b0
        # (the magnitude bit) — enough to visualise the decision. (The chip's full
        # 2-bit output is drained by the server-batch path.)
        x = input_items[0]
        n = len(x)
        out = output_items[0]
        thr = 2.0 / 3.0
        for k in range(n):
            y = float(x[k])
            out[k] = 1.0 if abs(y) >= thr else 0.0
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


class rrc_pulse_shaper(_PassThrough):
    """RRC pulse shaper — GR marker (maps to RRCPulseShaperBlock).

    TX pulse shaper for the COMPLEX baseband: one gr_complex stream in/out (it
    pulse-shapes the upsampled complex symbol before the I/Q upconvert). The real
    DSP runs on the chip; this only carries the graph."""

    def __init__(self, device_id="kyttar_0", alpha=0.35, span=8, sps=4,
                 io_type="complex"):
        # io_type selects the stream dtype and MUST equal the .block.yml ``io_type``
        # default + ${io_type} port dtype. Default COMPLEX: the PSK TX chain carries a
        # COMPLEX baseband symbol (GR's interp_fir_filter_ccf). A real-only stream
        # sets io_type=float.
        dt = _io_dtype(io_type)
        super().__init__("Kyttar RRC Pulse Shaper", n_in=1, n_out=1,
                         in_dtype=dt, out_dtype=dt)
        self.device_id = device_id
        self.alpha = alpha
        self.span = span
        self.sps = int(sps)
        self.io_type = io_type
        # Advertise the placeKYT block's REAL params so the importer sets them (its
        # ports/taps come from sampling_freq/symbol_rate/alpha/ntaps, NOT beta/span).
        # RRCPulseShaperBlock derives sps = sampling_freq/symbol_rate; ntaps = span·sps+1
        # (GNU Radio firdes.root_raised_cosine convention). So sampling_freq = sps and
        # symbol_rate = 1 gives the intended samples/symbol, and ntaps = span·sps+1
        # matches the on-chip filter length. Without this, the block kept its default
        # (sps=4, ntaps=33) even in a 2-sps chain -> a mismatched matched filter.
        self._advertise_grc_params(device_id, "RRCPulseShaperBlock",
                                   {"sampling_freq": float(self.sps),
                                    "symbol_rate": 1.0,
                                    "alpha": alpha,
                                    "ntaps": span * self.sps + 1})


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
                 frequency=4000.0):
        # ONE complex baseband in -> real passband out (multiply_cc -> complex_to_real).
        super().__init__("Kyttar I/Q Upconvert", n_in=1, n_out=1,
                         in_dtype=np.complex64, out_dtype=np.float32)
        self.device_id = device_id
        self.sample_rate = sample_rate
        self.frequency = frequency
        self._advertise_grc_params(device_id, "IQUpconvertBlock",
                                   {"sample_rate": sample_rate,
                                    "frequency": frequency})


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
