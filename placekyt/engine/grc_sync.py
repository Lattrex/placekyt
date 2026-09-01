# SPDX-License-Identifier: GPL-3.0-or-later
"""GRC↔placeKYT parameter-sync detection (Qt-free).

The GRC-first flow imports a GNURadio flowgraph into a placeKYT design (see
``engine/grc_import.py``) and then HOSTS that design's chip for the flowgraph to
drive over a socket (``engine/sim_bridge.py`` SimServer). A gap: once imported,
if a block PARAMETER changes in the GRC flowgraph (a FIR's ``num_taps`` 7→40,
a gain's value, …) there is no mechanism to propagate that into the placed
design. A param change that RESIZES a block (FIR 7→40 taps grows ``cell_count``
and footprint) can move neighbours and raise DRC violations, so the propagation
must be able to re-place + re-route the affected blocks.

This module is the DETECTION half — pure model logic, no Qt, no commands. It:

  * Holds the last-known GRC params per placeKYT block (``GrcSyncState``).
  * Computes the DIFF between those GRC params and the placed design's current
    params (``compute_param_diff``), keyed by placeKYT block name. The
    comparison COERCES the GRC values through the same path ``grc_import`` uses
    (``_coerce_params``) so a GRC string ``"40"`` compares equal to a placed
    int ``40`` (no false positives from representation).

The GUI layer turns a non-empty diff into the "out of sync — click to resync"
indicator, and the controller applies the diff as ONE undoable command (param
edit + re-place + re-route — see ``commands/edit_cmds.ResyncFromGrcCommand``).

Detection is fed two ways (both land here):
  * OVER THE WIRE — the GRC client sends its block params with a batch (or via a
    dedicated ``set_grc_params`` op); the SimServer forwards them to
    ``GrcSyncState.observe`` (see ``sim_bridge`` ``on_grc_params``).
  * ON DEMAND — comparing the params recorded at the last GRC import against the
    current design (``GrcSyncState.observe`` is also called by the importer).
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class BlockParamDiff:
    """One block whose GRC params differ from its placed-design params.

    ``changes`` maps each differing param name to ``(placed_value, grc_value)``.
    ``resizes`` is True when applying the GRC params changes the block's cell
    count / footprint (so a resync must re-place + re-route, not just re-anchor).
    """

    block_name: str
    grc_params: dict
    changes: dict          # param -> (current_value, grc_value)
    resizes: bool = False


@dataclass
class GrcSyncState:
    """Tracks the GRC-side params per placeKYT block and the current diff.

    One per project (the controller owns it). ``observe`` records the GRC params
    for a block (from the wire or an import); ``diff_against`` recomputes the
    out-of-sync set against a live project. ``in_sync`` is the cached "no diff"
    flag the GUI indicator reads.
    """

    # placeKYT block name -> the GRC params last seen for it (raw, GRC-side).
    grc_params: dict = field(default_factory=dict)
    # The most recently computed diff (block_name -> BlockParamDiff).
    diffs: dict = field(default_factory=dict)

    @property
    def in_sync(self) -> bool:
        return not self.diffs

    def observe(self, block_name: str, params: dict) -> None:
        """Record the GRC-side params for ``block_name`` (does not diff)."""
        self.grc_params[block_name] = dict(params or {})

    def observe_many(self, mapping: dict) -> None:
        """Record GRC params for several blocks at once ({name: params})."""
        for name, params in (mapping or {}).items():
            self.observe(name, params)

    def clear(self) -> None:
        self.grc_params.clear()
        self.diffs.clear()

    def forget(self, block_name: str) -> None:
        """Drop a block (e.g. it was deleted) from both maps."""
        self.grc_params.pop(block_name, None)
        self.diffs.pop(block_name, None)

    def diff_against(self, project, catalog) -> dict:
        """Recompute (and cache) the out-of-sync diff against ``project``.

        Returns ``{block_name: BlockParamDiff}`` for every block whose recorded
        GRC params differ from its current placed params. Empty ⇒ in sync."""
        self.diffs = compute_param_diff(project, catalog, self.grc_params)
        return self.diffs


def _coerce_grc_params(params: dict, catalog, btype: str, library=None) -> dict:
    """Coerce raw GRC param values to the placed block's value TYPES.

    Reuses ``grc_import._coerce_params`` so the wire/import values are normalised
    identically to how an import would store them — a GRC ``"40"`` becomes int
    ``40``, an un-coercible expression keeps the block default — so the diff
    compares like-for-like and does not flag representation-only differences.
    """
    from engine.grc_import import _coerce_params

    spec = catalog.get(btype, library) if catalog is not None else None
    if spec is None:
        # No spec to coerce against → compare the raw values as given.
        return dict(params or {})
    return _coerce_params(dict(params or {}), catalog, spec.type_name)


# Parameters whose value is a function of the PLACED GEOMETRY, not of the DSP.
#
# The panel-backed blocks (LZ4 encoder/decoder, Varicode encoder/decoder, the CW
# keyer/decoder, the SRAM controller) reach the SRAM panel and their egress port
# over hand- or auto-routed corridors. The descriptors/hop counts/dest tags that
# steer those corridors are DERIVED FROM THE ROUTES at build time — see
# ``panel_pnr.refresh_panel_params``, which re-authors exactly these keys from
# the current geometry every build, and the block .yml docs ("The remaining
# parameters are placement-derived: the auto-P&R panel template fills them in
# from the geometry it chooses").
#
# A GNURadio flowgraph has NO geometry, so a .grc can only ever advertise the
# constructor DEFAULTS for these keys (panel_hop 1, emit_hop 2, descriptors 0).
# Diffing those against a placed design's real corridor values flagged every
# panel-backed example as permanently "out of sync" — measured on the shipped
# lz4_stream, cw_transceiver and psk31_transceiver examples, all three reporting
# "2 block(s) out of sync" on a freshly-opened, untouched design, with every
# flagged key drawn from this set. Resyncing would have OVERWRITTEN the routed
# values with the defaults and broken the chip.
#
# So: a placement-derived key is not a user-visible parameter change. It is
# suppressed ONLY when the GRC side carries the block's own default (the "the
# flowgraph never authored geometry" case). A .grc that hand-sets one of these
# to a NON-default value is a deliberate hand-placed override and still flags.
_PLACEMENT_DERIVED = frozenset({
    "panel_hop", "emit_hop", "emit_dest", "emit_entry", "emit_jump_entry",
    "done_entry", "out_dest", "read_wr_desc", "read_jp_desc", "read_dest",
    "read_entry", "read_addr_hop", "run_dest", "run_entry",
    "dest_b", "dest_c", "entry_c", "face_c", "hop_b", "hop_c",
    "track_a", "track_b", "track_c", "restore_face",
})


def compute_param_diff(project, catalog, grc_params_by_block: dict) -> dict:
    """Diff each block's recorded GRC params against its placed-design params.

    ``grc_params_by_block`` is ``{placeKYT block name: raw GRC params}``. For
    each such block still present in ``project``, coerce the GRC params to the
    block's types and compare to the block's current params. A block whose GRC
    params are all equal (after coercion) is in sync and omitted.

    The comparison is over EFFECTIVE values, not raw dict keys (the
    out-of-sync-banner false-positive class):

      * a key ABSENT from the placed params means "the block default" — an
        older .kyt that never stored a later-added param is not out of sync
        when the GRC advertises exactly that default;
      * a ``None`` on either side means "unset/derived" (dual-namespace blocks
        like the RRC matched filter accept firdes-style AND friendly params,
        each defaulting the other to None) — never a real difference;
      * when a textual difference remains, the two param sets are
        INSTANTIATED and their resolved cell-program signatures compared —
        a representation-only difference (friendly ``sps=2`` vs firdes
        ``samp_rate=2.0``) yields the identical chip config and is
        suppressed. Only a diff that would actually change the built chip
        reaches the banner.

    Returns ``{block_name: BlockParamDiff}``.
    """
    out: dict = {}
    for block_name, grc_raw in (grc_params_by_block or {}).items():
        block = project.block(block_name)
        if block is None:
            continue  # block deleted since the GRC params were recorded
        coerced = _coerce_grc_params(grc_raw, catalog, block.type, block.library)
        spec = (catalog.get(block.type, block.library)
                if catalog is not None else None)
        defaults = {}
        if spec is not None:
            try:
                defaults = spec.default_params() or {}
            except Exception:  # noqa: BLE001
                defaults = {}
        cur_eff = {**defaults, **{k: v for k, v in (block.params or {}).items()
                                  if v is not None}}
        changes: dict = {}
        for key, grc_val in coerced.items():
            cur_val = cur_eff.get(key)
            if grc_val is None or cur_val is None:
                continue  # unset/derived on either side — not a difference
            if (key in _PLACEMENT_DERIVED
                    and _values_equal(defaults.get(key), grc_val)):
                # Geometry-derived key at its default: the flowgraph carries no
                # placement, so this is "unspecified", not a change.
                continue
            if not _values_equal(cur_val, grc_val):
                changes[key] = (block.params.get(key), grc_val)
        if not changes:
            continue
        if _same_resolved_config(catalog, block, coerced):
            continue  # representation-only difference — identical chip config
        resizes = _would_resize(project, catalog, block, coerced)
        out[block_name] = BlockParamDiff(
            block_name=block_name, grc_params=coerced,
            changes=changes, resizes=resizes)
    return out


def _program_signature(catalog, btype, library, params):
    """A canonical signature of the chip config a param set resolves to:
    per-cell (template, data words, state inits) + the cell count. Two param
    sets with equal signatures build the identical block."""
    blk = catalog.instantiate(btype, "__sync_probe__", dict(params),
                              library=library)
    cells = []
    for cid, cp in (blk.build_cell_programs() or {}).items():
        data = tuple((dw.name, int(dw.value) & 0xFFFF, dw.address)
                     for dw in (getattr(cp, "data", None) or []))
        state = tuple((sv.name, getattr(sv, "initial_value", 0),
                       getattr(sv, "reset_value", None))
                      for sv in (getattr(cp, "state", None) or []))
        cells.append((str(cid), getattr(cp, "assembly_template", ""),
                      data, state))
    return (blk.cell_count, tuple(sorted(cells)))


def _same_resolved_config(catalog, block, coerced_grc: dict) -> bool:
    """True when the placed params and the GRC-merged params instantiate to
    the SAME resolved block (cell programs + data + footprint) — i.e. the
    textual param difference is representation-only. Conservative: any
    instantiation/probing failure returns False (the diff stays visible)."""
    if catalog is None:
        return False
    try:
        cur = _program_signature(catalog, block.type, block.library,
                                 block.params or {})
        new = _program_signature(catalog, block.type, block.library,
                                 merged_params(block, coerced_grc))
    except Exception:  # noqa: BLE001
        return False
    return cur == new


def merged_params(block, grc_params: dict) -> dict:
    """The block's params with the GRC values applied on top (the resynced
    param dict). Keeps any placed-only params the GRC didn't mention."""
    out = dict(block.params)
    out.update(grc_params or {})
    return out


def _would_resize(project, catalog, block, coerced_grc: dict) -> bool:
    """True if applying ``coerced_grc`` changes the block's cell count.

    A resize means the block's footprint changes, so a resync must re-place +
    re-route rather than just re-anchor. Compares the catalog cell count for the
    current params vs the GRC-merged params; tolerant of catalog errors (treat
    an un-instantiable variant as 'might resize' so we don't under-react)."""
    if catalog is None:
        return False
    new_params = merged_params(block, coerced_grc)
    try:
        cur = catalog.cell_count(block.type, params=block.params,
                                 library=block.library)
        new = catalog.cell_count(block.type, params=new_params,
                                 library=block.library)
    except Exception:  # noqa: BLE001 — geometry probe failed → assume it resizes
        return True
    return cur != new


def _values_equal(a, b) -> bool:
    """Tolerant value comparison for param diffing.

    Lists/tuples compare element-wise (a GRC tuple vs a placed list of the same
    values is equal); floats compare with a small tolerance (Q15-derived values
    won't round-trip bit-exactly through a string); everything else is ``==``.
    """
    if isinstance(a, (list, tuple)) and isinstance(b, (list, tuple)):
        if len(a) != len(b):
            return False
        return all(_values_equal(x, y) for x, y in zip(a, b))
    if isinstance(a, float) or isinstance(b, float):
        try:
            return abs(float(a) - float(b)) <= 1e-9
        except (TypeError, ValueError):
            return a == b
    return a == b
