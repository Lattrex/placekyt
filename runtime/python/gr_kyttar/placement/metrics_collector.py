"""
Metrics Collection During Simulation

Collects transaction statistics during simulation runs to inform
placement decisions. Attaches to the simulator and logs all data
flow events.

Usage:
    from placement.metrics_collector import MetricsCollector

    collector = MetricsCollector()
    collector.set_cell_mapping(cell_to_filament)

    # During simulation:
    collector.on_cell_receive(row, col, data)
    collector.on_cell_send(row, col, data)

    # After simulation:
    metrics = collector.compute_metrics()
    collector.export_yaml("metrics.yaml")
"""

from collections import defaultdict
from dataclasses import dataclass, field
from typing import Dict, List, Tuple, Optional, Any
from pathlib import Path
import yaml

from .block import FilamentMetrics


@dataclass
class TransactionRecord:
    """A single recorded transaction."""
    time_ns: int
    filament: str
    cell: Tuple[int, int]
    direction: str  # 'input' or 'output'
    data: int
    stream_id: int = 0


class MetricsCollector:
    """
    Collects transaction statistics during simulation.

    Tracks all data flow events and computes metrics like:
    - input_ratio: fraction of transactions that are inputs
    - output_ratio: fraction of transactions that are outputs
    - activity: this filament's share of total transactions
    """

    def __init__(self):
        # Transaction counters per filament
        self.transactions: Dict[str, Dict[str, int]] = defaultdict(
            lambda: {'input': 0, 'output': 0}
        )

        # Totals
        self.total_input = 0
        self.total_output = 0

        # Cell to filament mapping
        self.cell_to_filament: Dict[Tuple[int, int], str] = {}

        # Optional: detailed transaction log
        self.transaction_log: List[TransactionRecord] = []
        self.log_enabled = False

        # Simulation info
        self.start_time_ns = 0
        self.end_time_ns = 0
        self.sample_count = 0

    def reset(self):
        """Reset all counters."""
        self.transactions.clear()
        self.total_input = 0
        self.total_output = 0
        self.transaction_log.clear()
        self.start_time_ns = 0
        self.end_time_ns = 0
        self.sample_count = 0

    def set_cell_mapping(self, mapping: Dict[Tuple[int, int], str]):
        """
        Set the mapping from cell positions to filament names.

        Args:
            mapping: Dict from (row, col) to filament name
        """
        self.cell_to_filament = mapping.copy()

    def add_cell_to_filament(self, row: int, col: int, filament: str):
        """Add a single cell to filament mapping."""
        self.cell_to_filament[(row, col)] = filament

    def enable_logging(self, enabled: bool = True):
        """Enable/disable detailed transaction logging."""
        self.log_enabled = enabled

    def on_cell_receive(self, row: int, col: int, data: int,
                        time_ns: int = 0, stream_id: int = 0):
        """
        Called when a cell receives data.

        Args:
            row: Cell row
            col: Cell column
            data: Data value received
            time_ns: Simulation time in nanoseconds
            stream_id: Optional stream identifier
        """
        filament = self.cell_to_filament.get((row, col))
        if filament:
            self.transactions[filament]['input'] += 1
            self.total_input += 1

            if self.log_enabled:
                self.transaction_log.append(TransactionRecord(
                    time_ns=time_ns,
                    filament=filament,
                    cell=(row, col),
                    direction='input',
                    data=data,
                    stream_id=stream_id,
                ))

    def on_cell_send(self, row: int, col: int, data: int,
                     time_ns: int = 0, stream_id: int = 0):
        """
        Called when a cell sends data.

        Args:
            row: Cell row
            col: Cell column
            data: Data value sent
            time_ns: Simulation time in nanoseconds
            stream_id: Optional stream identifier
        """
        filament = self.cell_to_filament.get((row, col))
        if filament:
            self.transactions[filament]['output'] += 1
            self.total_output += 1

            if self.log_enabled:
                self.transaction_log.append(TransactionRecord(
                    time_ns=time_ns,
                    filament=filament,
                    cell=(row, col),
                    direction='output',
                    data=data,
                    stream_id=stream_id,
                ))

    def compute_metrics(self) -> Dict[str, FilamentMetrics]:
        """
        Compute metrics from collected data.

        Returns:
            Dict mapping filament name to FilamentMetrics
        """
        total_transactions = self.total_input + self.total_output
        if total_transactions == 0:
            return {}  # No data, use defaults

        metrics = {}
        for filament, txns in self.transactions.items():
            filament_total = txns['input'] + txns['output']

            if filament_total > 0:
                input_ratio = txns['input'] / filament_total
                output_ratio = txns['output'] / filament_total
            else:
                input_ratio = 0.5
                output_ratio = 0.5

            metrics[filament] = FilamentMetrics(
                input_ratio=round(input_ratio, 3),
                output_ratio=round(output_ratio, 3),
                activity=round(filament_total / total_transactions, 3),
                input_transactions=txns['input'],
                output_transactions=txns['output'],
            )

        return metrics

    def get_summary(self) -> Dict[str, Any]:
        """Get summary statistics."""
        return {
            'total_input_transactions': self.total_input,
            'total_output_transactions': self.total_output,
            'total_transactions': self.total_input + self.total_output,
            'filament_count': len(self.transactions),
            'duration_ns': self.end_time_ns - self.start_time_ns,
            'sample_count': self.sample_count,
        }

    def export_yaml(self, path: str):
        """
        Export metrics to YAML file.

        Format compatible with load_metrics_from_yaml() in block.py.
        """
        metrics = self.compute_metrics()

        data = {
            'simulation_info': {
                'duration_ns': self.end_time_ns - self.start_time_ns,
                'sample_count': self.sample_count,
                'total_input_transactions': self.total_input,
                'total_output_transactions': self.total_output,
            },
            'filament_metrics': {
                name: m.to_dict() for name, m in metrics.items()
            }
        }

        with open(path, 'w') as f:
            yaml.dump(data, f, default_flow_style=False, sort_keys=False)

    def export_transaction_log(self, path: str):
        """Export detailed transaction log to YAML."""
        if not self.log_enabled or not self.transaction_log:
            return

        data = {
            'transactions': [
                {
                    'time_ns': t.time_ns,
                    'filament': t.filament,
                    'cell': list(t.cell),
                    'direction': t.direction,
                    'data': t.data,
                    'stream_id': t.stream_id,
                }
                for t in self.transaction_log
            ]
        }

        with open(path, 'w') as f:
            yaml.dump(data, f, default_flow_style=False)

    @classmethod
    def from_placement(cls, placement) -> 'MetricsCollector':
        """
        Create a collector with cell mapping from a Placement object.

        Args:
            placement: Placement object with placed_blocks

        Returns:
            MetricsCollector with cell mapping configured
        """
        collector = cls()

        for block_name, placed_block in placement.placed_blocks.items():
            for cell in placed_block.cells:
                collector.add_cell_to_filament(cell[1], cell[0], block_name)

        return collector


def collect_metrics_from_simulation(
    chip,
    placement,
    input_data: List[int],
    duration_ns: int = 10000,
) -> Dict[str, FilamentMetrics]:
    """
    Run simulation and collect metrics.

    Convenience function that:
    1. Creates a MetricsCollector
    2. Runs simulation with the given input
    3. Returns computed metrics

    Args:
        chip: Kyttar Chip instance
        placement: Placement with block positions
        input_data: List of input samples to process
        duration_ns: Simulation duration

    Returns:
        Dict of filament metrics
    """
    collector = MetricsCollector.from_placement(placement)

    # Hook into chip callbacks (if available)
    # This depends on the Chip implementation providing hooks
    if hasattr(chip, 'set_receive_callback'):
        chip.set_receive_callback(collector.on_cell_receive)
    if hasattr(chip, 'set_send_callback'):
        chip.set_send_callback(collector.on_cell_send)

    collector.sample_count = len(input_data)
    collector.start_time_ns = chip.time_ns if hasattr(chip, 'time_ns') else 0

    # Run simulation
    for sample in input_data:
        chip.inject_sample(sample)
        chip.run_ns(duration_ns // len(input_data))

    collector.end_time_ns = chip.time_ns if hasattr(chip, 'time_ns') else duration_ns

    return collector.compute_metrics()


if __name__ == '__main__':
    # Test metrics collection
    collector = MetricsCollector()

    # Setup cell mapping
    collector.add_cell_to_filament(0, 0, "Filter1")
    collector.add_cell_to_filament(0, 1, "Filter1")
    collector.add_cell_to_filament(1, 0, "Filter2")

    # Simulate some transactions
    for i in range(100):
        collector.on_cell_receive(0, 0, i, time_ns=i*10)
        collector.on_cell_send(0, 1, i*2, time_ns=i*10+5)

    for i in range(50):
        collector.on_cell_receive(1, 0, i)
        collector.on_cell_send(1, 0, i)

    # Compute metrics
    metrics = collector.compute_metrics()
    print("Collected metrics:")
    for name, m in metrics.items():
        print(f"  {name}:")
        print(f"    input_ratio: {m.input_ratio}")
        print(f"    output_ratio: {m.output_ratio}")
        print(f"    activity: {m.activity}")

    print(f"\nSummary: {collector.get_summary()}")

    # Export
    collector.export_yaml("/tmp/test_metrics.yaml")
    print("\nExported to /tmp/test_metrics.yaml")
