"""Waveform signal-list persistence (YAML) — Qt-free.

Saves/loads the waveform viewer's SIGNAL LIST (what to plot + how) so a debug
view can be restored in a new session. Only the signal IDENTITY (port or
register) + display settings are stored — NOT the sample data, which is
re-resolved from the current trace on load.

Schema (``.wsig.yaml``)::

    version: 1
    signals:
      - source: {type: port, chip: 0, port: x16_in}
        label: chip0.x16_in
        radix: Analog          # Analog | Hex | Dec | Bin
        color: "#5ac8ff"
        height: 56
        amp_scale: 1.0
        group: 0               # traces sharing a group id render overlaid
      - source: {type: register, chip: 0, x: 1, y: 1, addr: 5}
        label: "c0:(1,1).R5"
        radix: Hex
        ...
"""

from __future__ import annotations

from pathlib import Path

from .safe_yaml import dump_yaml_str, load_yaml_str
from .errors import ProjectFileError

FORMAT_VERSION = 1


def save_signal_list(signals: list[dict], path: str | Path) -> None:
    """Write a signal list (from ``WaveformView.to_signal_list``) to YAML."""
    doc = {"version": FORMAT_VERSION, "signals": list(signals)}
    Path(path).write_text(dump_yaml_str(doc))


def load_signal_list(path: str | Path) -> list[dict]:
    """Read a signal list YAML → the ``signals`` list (for
    ``WaveformView.from_signal_list``). Raises on a bad/incompatible file."""
    try:
        doc = load_yaml_str(Path(path).read_text(), source=str(path))
    except OSError as exc:
        raise ProjectFileError(f"{path}: {exc}") from exc
    if not isinstance(doc, dict):
        raise ProjectFileError(f"{path}: not a signal-list file")
    version = doc.get("version", 1)
    if int(version) > FORMAT_VERSION:
        raise ProjectFileError(
            f"{path}: signal-list version {version} newer than supported "
            f"({FORMAT_VERSION})")
    signals = doc.get("signals", [])
    if not isinstance(signals, list):
        raise ProjectFileError(f"{path}: 'signals' must be a list")
    return signals
