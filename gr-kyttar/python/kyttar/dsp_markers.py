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

    ``n_in`` / ``n_out`` give the number of float stream ports. A COMPLEX-baseband
    DSP block (matched filter, Costas) exposes its I/Q as TWO NAMED float ports
    (xi/xq, yi/yq) — the LOGICAL view of the I/Q pair — so the .grc wires them
    explicitly and GRC keeps both wires (a single indexed port let the I/Q wiring
    collapse to port-0, dropping the Q rail). The physical chip route is still ONE
    time-multiplexed bus carrying both; the port count here is purely the logical
    graph. Each output mirrors input 0 (markers don't compute)."""

    def __init__(self, name, n_in=1, n_out=1):
        gr.sync_block.__init__(self, name=name,
                               in_sig=[np.float32] * n_in,
                               out_sig=[np.float32] * n_out)

    def work(self, input_items, output_items):
        n = len(input_items[0])
        for o in output_items:
            o[:] = input_items[0][:n]
        return n


class complex_rrc_matched_filter(_PassThrough):
    """Complex RRC matched filter — GR marker (maps to ComplexRRCMatchedFilterBlock).

    The RX matched filter front end: complex baseband (xi/xq) in, matched-filtered
    (yi/yq) out — TWO named float ports each side (the logical I/Q pair), so the .grc
    wires xi/xq and yi/yq explicitly. The real DSP runs on the chip; this only carries
    the graph so it imports into placeKYT and runs in server-batch mode."""

    def __init__(self, device_id="kyttar_0", alpha=0.35, span=8):
        super().__init__("Kyttar Complex RRC Matched Filter", n_in=2, n_out=2)
        self.device_id = device_id
        self.alpha = alpha
        self.span = span


class complex_costas_loop(_PassThrough):
    """Complex Costas carrier recovery — GR marker (maps to ComplexCostasLoopBlock).

    Complex baseband in (xi/xq, two named float ports) → recovered-I tap out
    (yi_tap)."""

    def __init__(self, device_id="kyttar_0", loop_bw=0.05, damping=1.0):
        super().__init__("Kyttar Complex Costas Loop", n_in=2, n_out=1)
        self.device_id = device_id
        self.loop_bw = loop_bw
        self.damping = damping


class gardner_timing_recovery(_PassThrough):
    """Gardner timing recovery — GR marker (maps to GardnerTimingRecovery)."""

    def __init__(self, device_id="kyttar_0", kp=3, ki=1):
        super().__init__("Kyttar Gardner Timing Recovery")
        self.device_id = device_id
        self.kp = kp
        self.ki = ki


class bpsk_slicer(_PassThrough):
    """BPSK slicer — GR marker (maps to BPSKSlicerBlock)."""

    def __init__(self, device_id="kyttar_0"):
        super().__init__("Kyttar BPSK Slicer")
        self.device_id = device_id
