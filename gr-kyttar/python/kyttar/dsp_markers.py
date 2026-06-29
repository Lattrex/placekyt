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

    def __init__(self, device_id="kyttar_0", alpha=0.35, span=8):
        super().__init__("Kyttar Complex RRC Matched Filter", n_in=1, n_out=1,
                         in_dtype=np.complex64, out_dtype=np.complex64)
        self.device_id = device_id
        self.alpha = alpha
        self.span = span
        # placeKYT uses `beta` for the roll-off (GRC marker calls it `alpha`).
        self._advertise_grc_params(device_id, "ComplexRRCMatchedFilterBlock",
                                   {"beta": alpha, "span": span})


class complex_costas_loop(_PassThrough):
    """Complex Costas carrier recovery — GR marker (maps to ComplexCostasLoopBlock).

    A SINGLE complex baseband stream in → recovered-I tap out (yi_tap, real)."""

    def __init__(self, device_id="kyttar_0", loop_bw=0.05, damping=1.0):
        # complex in, real yi_tap out
        super().__init__("Kyttar Complex Costas Loop", n_in=1, n_out=1,
                         in_dtype=np.complex64, out_dtype=np.float32)
        self.device_id = device_id
        self.loop_bw = loop_bw
        self.damping = damping
        self._advertise_grc_params(device_id, "ComplexCostasLoopBlock",
                                   {"loop_bw": loop_bw, "damping": damping})


class gardner_timing_recovery(_PassThrough):
    """Gardner timing recovery — GR marker (maps to GardnerTimingRecovery)."""

    def __init__(self, device_id="kyttar_0", kp=3, ki=1):
        super().__init__("Kyttar Gardner Timing Recovery")
        self.device_id = device_id
        self.kp = kp
        self.ki = ki
        self._advertise_grc_params(device_id, "GardnerTimingRecovery",
                                   {"kp": kp, "ki": ki})


class bpsk_slicer(_PassThrough):
    """BPSK slicer — GR marker (maps to BPSKSlicerBlock)."""

    def __init__(self, device_id="kyttar_0"):
        super().__init__("Kyttar BPSK Slicer")
        self.device_id = device_id


class psk_symbol_mapper(_PassThrough):
    """PSK symbol mapper — GR marker (maps to PSKSymbolMapperBlock).

    TX front end: input bit(s) -> complex PSK constellation symbol. One float in
    (the bit), a SINGLE complex out (the I/Q symbol). The real DSP runs on the
    chip."""

    def __init__(self, device_id="kyttar_0", modulation="bpsk"):
        # bit in (float), complex symbol out
        super().__init__("Kyttar PSK Symbol Mapper", n_in=1, n_out=1,
                         in_dtype=np.float32, out_dtype=np.complex64)
        self.device_id = device_id
        self.modulation = modulation
        self._advertise_grc_params(device_id, "PSKSymbolMapperBlock",
                                   {"modulation": modulation})


class upsampler(_PassThrough):
    """Upsampler — GR marker (maps to UpsamplerBlock).

    Zero-stuffing rate expander: one input sample -> ``sps`` outputs (the sample,
    then sps-1 zeros). In the modem TX chain it carries the COMPLEX baseband symbol
    (one gr_complex stream in/out) between the mapper and the RRC pulse shaper."""

    def __init__(self, device_id="kyttar_0", sps=4):
        super().__init__("Kyttar Upsampler", n_in=1, n_out=1,
                         in_dtype=np.complex64, out_dtype=np.complex64)
        self.device_id = device_id
        self.sps = sps
        self._advertise_grc_params(device_id, "UpsamplerBlock", {"sps": sps})


class rrc_pulse_shaper(_PassThrough):
    """RRC pulse shaper — GR marker (maps to RRCPulseShaperBlock).

    TX pulse shaper for the COMPLEX baseband: one gr_complex stream in/out (it
    pulse-shapes the upsampled complex symbol before the I/Q upconvert). The real
    DSP runs on the chip; this only carries the graph."""

    def __init__(self, device_id="kyttar_0", alpha=0.35, span=8):
        super().__init__("Kyttar RRC Pulse Shaper", n_in=1, n_out=1,
                         in_dtype=np.complex64, out_dtype=np.complex64)
        self.device_id = device_id
        self.alpha = alpha
        self.span = span
        # placeKYT uses `beta` for the roll-off (GRC marker calls it `alpha`).
        self._advertise_grc_params(device_id, "RRCPulseShaperBlock",
                                   {"beta": alpha, "span": span})


class iq_upconvert(_PassThrough):
    """I/Q upconvert — GR marker (maps to IQUpconvertBlock).

    A SINGLE complex baseband stream -> real passband sample (out).
    s = I*cos(phase) - Q*sin(phase), free-running NCO."""

    def __init__(self, device_id="kyttar_0", sample_rate=32000.0,
                 frequency=4000.0):
        # complex in, real passband out
        super().__init__("Kyttar I/Q Upconvert", n_in=1, n_out=1,
                         in_dtype=np.complex64, out_dtype=np.float32)
        self.device_id = device_id
        self.sample_rate = sample_rate
        self.frequency = frequency
        self._advertise_grc_params(device_id, "IQUpconvertBlock",
                                   {"sample_rate": sample_rate,
                                    "frequency": frequency})
