"""
Placement Engine for Kyttar Fabric

Two-phase placement algorithm:
1. Coarse placement: Assign blocks to regions based on I/O ratio and activity
2. Fine placement: Fit shapes within regions, optimizing wire length

Uses simulation-driven metrics (FilamentMetrics) to inform placement decisions.
"""

from .shapes import Shape, enumerate_shapes, enumerate_self_avoiding_walks
from .block import (
    BlockDefinition, Connection, FilamentMetrics, CellProgram,
    Port, EntryPoint, StateVar, DataWord,
)
from .resolver import CellProgramResolver, ResolvedTargets, WriteTarget, JumpTarget, ResolverError
from .placer import Placer, Placement, PlacementError
from .region import Region, ArrayConfig, PortConfig, PortDirection, Face
from .cell_map import CellMap, CellConfig, Face
from .router import Router, route_placement
from .bitstream import (
    Bitstream, BitstreamGenerator,
    MycWriter, HexWriter,
    generate_bitstream, write_myc, write_hex,
)
from .annealing import (
    SimulatedAnnealing, AnnealingConfig, AnnealingResult,
    anneal_placement,
)
from .metrics_collector import (
    MetricsCollector, TransactionRecord,
    collect_metrics_from_simulation,
)
from .kyttar_block import (
    KyttarBlock,
    BlockInterface,
    GainBlock,
    FIRFilterBlock,
    DCBlockerBlock,
    AGCBlock,
    NCOBlock,
    ComplexMixerBlock,
    IQUpconvertBlock,
    DecimatorBlock,
    SquelchBlock,
    IIRBiquadBlock,
    CostasLoopBlock,
    ComplexCostasLoopBlock,
    CoherentBPSKRxBlock,
    CoherentRXBlock,
    QAM16ComplexCostasLoopBlock,
    QAM16TransceiverBlock,
    GardnerTimingRecovery,
    SoftDemodulatorBlock,
    BPSKSlicerBlock,
    ViterbiBranchMetricBlock,
    ViterbiK7DecoderBlock,
    # STANAG 5066 / MIL-STD-188-110B blocks (Phase 3)
    LMSEqualizerBlock,
    BlockInterleaverBlock,
    LFSRScramblerBlock,
    ConvEncoderK7Block,
    PSKSymbolMapperBlock,
    # MIL-STD-188-110B Frame Structure blocks (Phase 4-5)
    DFEEqualizerBlock,
    PreambleCorrelatorBlock,
    FrameSyncBlock,
    MiniProbeDetectorBlock,
    EOMDetectorBlock,
    RRCPulseShaperBlock,
    ComplexRRCMatchedFilterBlock,
    PreambleGeneratorBlock,
    MiniProbeInserterBlock,
    float_to_q15,
    q15_to_float,
    assemble_to_words,
    build_block_chain,
    get_block_metrics,
)
from .demux_block import (
    DemuxBlock,
    DemuxInterface,
    create_iq_demux,
    CHANNEL_ENTRY_ADDRESSES,
)
from .mux_block import (
    MuxBlock,
    MuxInterface,
    ChannelInterface,
    create_iq_mux,
)
from .graph import (
    ConnectionGraph,
    BlockGraph,
    BlockEdge,
    parse_edge_list,
    parse_block_port,
    build_block_graph,
    is_demux_block,
    is_mux_block,
    get_face_direction,
    manhattan_distance,
    FACE_SOUTH,
    FACE_EAST,
    FACE_WEST,
    FACE_NORTH,
    OPPOSITE_FACE,
)
from .face_assignment import (
    FaceAssignment,
    FaceAssignmentError,
    assign_faces,
    configure_demux_block,
    configure_mux_block,
)
from .route_map import (
    Route,
    RouteMap,
    RoutingError,
    compute_routes,
    compute_manhattan_path,
    path_to_routing_cells,
)

__all__ = [
    'Shape',
    'enumerate_shapes',
    'enumerate_self_avoiding_walks',
    'BlockDefinition',
    'Connection',
    'FilamentMetrics',
    'CellProgram',
    'Port',
    'EntryPoint',
    'StateVar',
    'DataWord',
    # Resolver
    'CellProgramResolver',
    'ResolvedTargets',
    'WriteTarget',
    'JumpTarget',
    'ResolverError',
    'Placer',
    'Placement',
    'PlacementError',
    'Region',
    'ArrayConfig',
    'CellMap',
    'CellConfig',
    'Face',
    'Router',
    'route_placement',
    'Bitstream',
    'BitstreamGenerator',
    'MycWriter',
    'HexWriter',
    'generate_bitstream',
    'write_myc',
    'write_hex',
    'SimulatedAnnealing',
    'AnnealingConfig',
    'AnnealingResult',
    'anneal_placement',
    'MetricsCollector',
    'TransactionRecord',
    'collect_metrics_from_simulation',
    # Block base class and implementations
    'KyttarBlock',
    'BlockInterface',
    'GainBlock',
    'FIRFilterBlock',
    'DCBlockerBlock',
    'AGCBlock',
    'NCOBlock',
    'ComplexMixerBlock',
    'IQUpconvertBlock',
    'DecimatorBlock',
    'SquelchBlock',
    'IIRBiquadBlock',
    'CostasLoopBlock',
    'ComplexCostasLoopBlock',
    'CoherentBPSKRxBlock',
    'CoherentRXBlock',
    'QAM16ComplexCostasLoopBlock',
    'QAM16TransceiverBlock',
    'GardnerTimingRecovery',
    'SoftDemodulatorBlock',
    'BPSKSlicerBlock',
    'ViterbiBranchMetricBlock',
    'ViterbiK7DecoderBlock',
    # STANAG 5066 / MIL-STD-188-110B blocks
    'LMSEqualizerBlock',
    'BlockInterleaverBlock',
    'LFSRScramblerBlock',
    'ConvEncoderK7Block',
    'PSKSymbolMapperBlock',
    # MIL-STD-188-110B Frame Structure blocks
    'DFEEqualizerBlock',
    'PreambleCorrelatorBlock',
    'FrameSyncBlock',
    'MiniProbeDetectorBlock',
    'EOMDetectorBlock',
    'RRCPulseShaperBlock',
    'ComplexRRCMatchedFilterBlock',
    'PreambleGeneratorBlock',
    'MiniProbeInserterBlock',
    # Q15 helpers and assembly
    'float_to_q15',
    'q15_to_float',
    'assemble_to_words',
    # Flowgraph helpers
    'build_block_chain',
    'get_block_metrics',
    # Demux/Mux routing primitives
    'DemuxBlock',
    'DemuxInterface',
    'create_iq_demux',
    'MuxBlock',
    'MuxInterface',
    'create_iq_mux',
    'CHANNEL_ENTRY_ADDRESSES',
    # Graph data structures for placement/routing
    'ConnectionGraph',
    'BlockGraph',
    'BlockEdge',
    'parse_edge_list',
    'parse_block_port',
    'build_block_graph',
    'is_demux_block',
    'is_mux_block',
    'get_face_direction',
    'manhattan_distance',
    'FACE_SOUTH',
    'FACE_EAST',
    'FACE_WEST',
    'FACE_NORTH',
    'OPPOSITE_FACE',
    # Face assignment for demux/mux routing
    'FaceAssignment',
    'FaceAssignmentError',
    'assign_faces',
    'configure_demux_block',
    'configure_mux_block',
    # Route computation
    'Route',
    'RouteMap',
    'RoutingError',
    'compute_routes',
    'compute_manhattan_path',
    'path_to_routing_cells',
]
