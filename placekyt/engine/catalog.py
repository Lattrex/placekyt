"""Block catalog — metadata adapter over the existing KyttarBlock classes.

placeKYT does NOT define its own block library (the architecture notes §0.1). The 25+
production DSP blocks already exist as ``KyttarBlock`` subclasses in
``gr_kyttar.placement.kyttar_block``. This adapter discovers them and
exposes their metadata to the UI (library panel §3.4), the build pipeline
(§5.1), and the project model — without re-implementing any DSP logic.

Metadata sources:
  * ``CATEGORY`` / ``TAGS`` — class attributes (added to the gr_kyttar
    classes; categories per §3.2's block→category mapping).
  * ``cell_count``, ``interface`` (entry / input / output registers) — read
    from a default-constructed instance.
  * constructor parameters (name, default, type) — via ``inspect.signature``.
  * ``description`` — the class docstring's first paragraph.

Block-type resolution follows §2.2 library precedence:
``project-local > user > lattrex.official > other``. For v1.0 every block is a
bundled ``lattrex.official`` Python class, so resolution is effectively a
name lookup; the precedence machinery is in place for when ``.kbl`` libraries
land post-v1.0.
"""

from __future__ import annotations

import inspect
from dataclasses import dataclass, field
from typing import Any

# Import the block module lazily-tolerant: the catalog is engine-layer and may
# be imported in contexts where gr_kyttar is present (it is, via the venv).
from gr_kyttar.placement import kyttar_block as _mb
from gr_kyttar.placement.kyttar_block import KyttarBlock

# Library name for the bundled production blocks (§2.2). Every discovered class
# belongs to this library in v1.0.
OFFICIAL_LIBRARY = "lattrex.official"

# Blocks excluded from the catalog: never completed, no usable in-array
# implementation — superseded by external-RAM offload (done off-array, not in
# the cell fabric). ``ViterbiK7DecoderBlock`` (trellis manipulation) and
# ``BlockInterleaverBlock`` (symbol interleaving storage) both offload to FPGA
# RAM rather than running on the array.
_EXCLUDED_BLOCKS = frozenset({
    "ViterbiK7DecoderBlock",
    "BlockInterleaverBlock",
})


def _palette_allowlist() -> frozenset[str] | None:
    """Block type names that may appear in the PALETTE — the curated set from the
    verification manifest (any status: verified ``done``, ``planned``, or
    proof-of-concept). A block NOT in the manifest is still resolvable (so designs
    referencing it load) but is HIDDEN from the palette, so users don't unknowingly
    build on the ~28 unverified leftover blocks.

    Returns ``None`` if the manifest can't be found (e.g. a packaged bundle without
    the verification tree) — in that case nothing is hidden and the full set shows,
    the safe fallback (never hide a block we can't confirm is uncurated).
    """
    import json
    import os

    here = os.path.dirname(os.path.abspath(__file__))
    # repo layout: <repo>/placekyt/engine/catalog.py and
    # <repo>/verification/manifest.json
    candidates = [
        os.path.join(here, "..", "..", "verification", "manifest.json"),
        os.path.join(here, "..", "verification", "manifest.json"),
    ]
    for path in candidates:
        path = os.path.normpath(path)
        if os.path.isfile(path):
            try:
                with open(path) as f:
                    m = json.load(f)
                return frozenset(
                    b["kyttar_block"] for b in m.get("blocks", [])
                    if "kyttar_block" in b)
            except Exception:  # noqa: BLE001 — malformed manifest ⇒ show all
                return None
    return None


@dataclass(frozen=True)
class ParamSpec:
    """A block constructor parameter: name, default, and (best-effort) type."""

    name: str
    default: Any
    type_name: str | None = None  # "float" / "int" / "list" / ... or None

    @property
    def required(self) -> bool:
        return self.default is _NO_DEFAULT


_NO_DEFAULT = object()  # sentinel for parameters without a default


def _placeholder_for(name: str, type_name: str | None) -> Any:
    """A safe placeholder for a required param with no default, so the block can
    be instantiated as a passthrough at placement time (user edits it later)."""
    lname = name.lower()
    if type_name in ("List", "list") or "coeff" in lname or "taps" in lname:
        # Biquad denominator/numerator need 3 elements (a0=1 passthrough);
        # a single-tap [1.0] is an identity FIR/decimator filter.
        if lname.startswith("a_") or "a_coeff" in lname:
            return [1.0, 0.0, 0.0]
        if lname.startswith("b_") or "b_coeff" in lname:
            return [1.0, 0.0, 0.0]
        return [1.0]
    if type_name in ("int",):
        return 1
    if type_name in ("float",):
        return 0.0
    return None


@dataclass(frozen=True)
class BlockSpec:
    """Static metadata describing one block TYPE (not an instance).

    ``cell_count`` is the count for a default-constructed instance; blocks with
    parameter-dependent geometry (FIR, DFE, Viterbi, ...) report a different
    count once instantiated with real params — use :meth:`BlockCatalog.cell_count`
    for the parameterized value.
    """

    type_name: str
    library: str
    category: str
    tags: tuple[str, ...]
    description: str
    default_cell_count: int
    entry_address: int
    input_registers: tuple[int, ...]
    output_registers: tuple[int, ...]
    params: tuple[ParamSpec, ...]
    cls: type = field(compare=False, repr=False)
    # Hidden from the block PALETTE (still fully resolvable so existing designs /
    # demos that reference it continue to load). A block is shown only if it is in
    # the curated verification manifest (verified, planned, or proof-of-concept);
    # the ~28 unverified HF-modem leftovers are hidden so users don't unknowingly
    # build on questionable blocks. See ``_palette_allowlist``.
    hidden: bool = field(default=False, compare=False)

    def param(self, name: str) -> ParamSpec | None:
        for p in self.params:
            if p.name == name:
                return p
        return None

    def default_params(self) -> dict[str, Any]:
        """Parameter values for a freshly-placed block.

        Includes each param's default, AND a sensible PLACEHOLDER for required
        params with no default (e.g. FIR/IIR/Decimator coefficients) so the
        block can be instantiated and placed — the user edits the real values in
        the Inspector afterward. Without this, required-coefficient blocks could
        not be dragged from the library at all.
        """
        out: dict[str, Any] = {}
        for p in self.params:
            if p.default is not _NO_DEFAULT:
                out[p.name] = p.default
            else:
                out[p.name] = _placeholder_for(p.name, p.type_name)
        return out


class BlockCatalog:
    """Registry of available block types, keyed by ``(library, type_name)``.

    Built once at startup by scanning ``gr_kyttar.placement.kyttar_block``.
    """

    def __init__(self, specs: dict[tuple[str, str], BlockSpec]):
        self._specs = specs

    # -- construction ---------------------------------------------------------

    @classmethod
    def from_gr_kyttar(cls) -> "BlockCatalog":
        """Discover every concrete ``KyttarBlock`` subclass and build specs.

        Blocks in ``_EXCLUDED_BLOCKS`` are skipped — they have no usable v2
        definition and would not build (placeKYT consumes only the v2/canonical
        path). See §0.1.
        """
        import dataclasses

        allow = _palette_allowlist()  # None ⇒ show everything (safe fallback)
        specs: dict[tuple[str, str], BlockSpec] = {}
        for obj in vars(_mb).values():
            if (
                inspect.isclass(obj)
                and issubclass(obj, KyttarBlock)
                and obj is not KyttarBlock
                and not inspect.isabstract(obj)
                and obj.__name__ not in _EXCLUDED_BLOCKS
            ):
                spec = _build_spec(obj)
                # A block NOT in the curated manifest is HIDDEN from the palette
                # (but still resolvable, so designs/demos referencing it load).
                if allow is not None and spec.type_name not in allow:
                    spec = dataclasses.replace(spec, hidden=True)
                specs[(spec.library, spec.type_name)] = spec
        return cls(specs)

    # -- queries --------------------------------------------------------------

    def all(self, include_hidden: bool = False) -> list[BlockSpec]:
        """Block specs sorted by category then type name (for the panel).

        By default only PALETTE-visible (non-hidden) blocks are returned — the
        curated manifest set. Pass ``include_hidden=True`` for the complete set
        (e.g. tooling that must see every resolvable block)."""
        specs = self._specs.values()
        if not include_hidden:
            specs = [s for s in specs if not s.hidden]
        return sorted(specs, key=lambda s: (s.category, s.type_name))

    def by_category(self, include_hidden: bool = False) -> dict[str, list[BlockSpec]]:
        """Specs grouped by category (drives the §3.4 categorized tree). Hidden
        (uncurated) blocks are excluded unless ``include_hidden=True``."""
        out: dict[str, list[BlockSpec]] = {}
        for spec in self.all(include_hidden=include_hidden):
            out.setdefault(spec.category, []).append(spec)
        return out

    def get(self, type_name: str, library: str | None = None) -> BlockSpec | None:
        """Resolve a block type, honoring §2.2 library precedence.

        If ``library`` is given, an exact ``(library, type_name)`` match is
        required. If omitted, search all libraries by precedence.
        """
        if library is not None:
            return self._specs.get((library, type_name))
        for lib in _LIBRARY_PRECEDENCE:
            spec = self._specs.get((lib, type_name))
            if spec is not None:
                return spec
        # Fall back to any library that has the name.
        for (lib, name), spec in self._specs.items():
            if name == type_name:
                return spec
        return None

    def search(self, query: str) -> list[BlockSpec]:
        """Case-insensitive search over name, description, category, and tags
        (§7.2 block search/filter)."""
        q = query.strip().lower()
        if not q:
            return self.all()
        results = []
        for spec in self.all():
            haystack = " ".join(
                (spec.type_name, spec.description, spec.category, *spec.tags)
            ).lower()
            if q in haystack:
                results.append(spec)
        return results

    # -- instantiation --------------------------------------------------------

    def instantiate(
        self, type_name: str, instance_name: str, params: dict[str, Any] | None = None,
        *, library: str | None = None,
    ) -> KyttarBlock:
        """Construct a live ``KyttarBlock`` from a project block's params.

        Used by the build pipeline to turn a project ``Block`` into the
        gr_kyttar object that ``Router`` / ``BitstreamGenerator`` consume.
        Unknown params raise ``TypeError`` from the constructor (surfaced as a
        build error by the caller).
        """
        spec = self.get(type_name, library)
        if spec is None:
            raise KeyError(f"unknown block type {type_name!r}")
        # No params given → use the spec's defaults (incl. placeholders for
        # required coefficient params) so required-param blocks still construct.
        kwargs = dict(params) if params is not None else spec.default_params()
        return spec.cls(name=instance_name, **kwargs)

    def cell_count(
        self, type_name: str, params: dict[str, Any] | None = None,
        *, library: str | None = None,
    ) -> int:
        """Parameterized cell count (instantiates with ``params``).

        For fixed-geometry blocks this equals ``spec.default_cell_count``; for
        parameter-dependent blocks (FIR taps, DFE taps, ...) it reflects the
        actual params.
        """
        block = self.instantiate(type_name, "__probe__", params, library=library)
        return block.cell_count

    def default_layout(
        self, type_name: str, params: dict[str, Any] | None = None,
        *, library: str | None = None,
    ) -> dict:
        """The block's hand-authored cell layout (§2.2 ``default_layout``).

        Returns ``{cell_id: (dx, dy, face_str)}`` relative offsets — the block
        author's tuned arrangement (e.g. the DFE serpentine), or a serpentine
        fallback that wraps within the array. Used by placement so multi-cell
        blocks land in a valid, repeatable shape rather than a flat row.
        Returns ``{}`` if the block doesn't expose a layout.
        """
        block = self.instantiate(type_name, "__probe__", params, library=library)
        layout = getattr(block, "default_layout", None)
        if layout is None:
            return {}
        try:
            return dict(layout() if callable(layout) else layout)
        except Exception:  # noqa: BLE001 — fall back to no layout
            return {}

    def port_map(
        self, type_name: str, params: dict[str, Any] | None = None,
        *, library: str | None = None,
    ):
        """The block's :class:`~engine.portmap.PortMap` — external I/O geometry
        (offset/face/reg/entry for input AND output cells) plus the bus-facing
        edge + I/O co-location hints (AUTO_PNR_DESIGN §4.1/§4.3). Used by the
        auto-P&R packer/router (Phase 3) and the schematic front-end (Phase 2)."""
        from engine.portmap import build_port_map
        return build_port_map(self, type_name, params, library=library)

    def resolved_io(
        self, type_name: str, params: dict[str, Any] | None = None,
        *, library: str | None = None,
    ) -> tuple[int, tuple[int, ...]]:
        """The RESOLVED (entry_addr, input_registers) of a block's landing cell.

        v2 blocks lay out memory dynamically (data packed low, instructions
        high), so the entry address and input register are NOT the static
        ``BlockInterface`` defaults — they come from resolving the v2 program.
        This is the authoritative source for configuring a chip input port that
        JUMPs into the block. Falls back to the static interface for blocks with
        no resolvable v2 program.
        """
        spec = self.get(type_name, library)
        block = self.instantiate(type_name, "__probe__", params, library=library)
        # Static interface as the fallback.
        entry = spec.entry_address if spec else 1
        in_regs = spec.input_registers if spec else ()
        try:
            from gr_kyttar.placement.resolver import CellProgramResolver

            cell_programs = block.build_cell_programs()
            # The LANDING cell is where external WRITE/JUMP arrive — the first
            # cell that declares inputs (not necessarily index 0; multi-cell
            # blocks like the DFE key cell_programs by string ids and land on a
            # specific cell). Fall back to the first templated cell.
            cp = None
            for c in cell_programs.values():
                if getattr(c, "assembly_template", "") and getattr(c, "inputs", None):
                    cp = c
                    break
            if cp is None:
                cp = next((c for c in cell_programs.values()
                           if getattr(c, "assembly_template", "")), None)
            if cp is not None:
                resolver = CellProgramResolver()
                cls = resolver.classify_addresses(cp)
                inputs = tuple(sorted(
                    a for a, c in cls.items() if c["role"] == "input"))
                if inputs:
                    in_regs = inputs
                entries = resolver.compute_entry_addresses(cp)
                if entries:
                    # Default entry = first declared entry point's address.
                    default = (cp.entries[0].name if cp.entries else None)
                    entry = entries.get(default, min(entries.values()))
        except Exception:  # noqa: BLE001 — keep the static fallback
            pass
        return int(entry), tuple(in_regs)

    def editable_params(
        self, type_name: str, params: dict[str, Any] | None = None,
        *, library: str | None = None,
    ) -> set[str]:
        """Names of params that map to a DATA value (so editing them is safe).

        A param is *data-mapped* — and thus editable in the Inspector — when
        perturbing it keeps the block's geometry (same cell count) but changes a
        DataWord value. Params that change the cell count / program structure
        (topology params like the DFE's ``forward_taps``) or change nothing
        observable are NOT editable: they would require re-tiling the block,
        which is out of scope. Those are shown read-only (informational).

        Determined empirically by probing — there is no declared param→data map.
        """
        spec = self.get(type_name, library)
        if spec is None:
            return set()
        base_params = dict(params) if params is not None else spec.default_params()

        def signature(p: dict):
            block = self.instantiate(type_name, "__probe__", p, library=library)
            data: dict = {}
            for k, cp in block.build_cell_programs().items():
                for dw in getattr(cp, "data", []):
                    data[(k, dw.name)] = dw.value
            return block.cell_count, data

        try:
            cc0, data0 = signature(base_params)
        except Exception:  # noqa: BLE001 — can't probe → nothing editable
            return set()

        editable: set[str] = set()
        for ps in spec.params:
            cur = base_params.get(ps.name)
            if cur is None or isinstance(cur, bool) or not isinstance(cur, (int, float)):
                continue  # only scalar numeric params are probed/editable here
            # A small perturbation that stays a valid value of the same type.
            if isinstance(cur, float):
                nv = cur * 1.3 if cur else 0.1
            else:
                nv = cur + 1
            probe = dict(base_params)
            probe[ps.name] = type(cur)(nv)
            try:
                cc, data = signature(probe)
            except Exception:  # noqa: BLE001 — invalid perturbation → skip
                continue
            if cc == cc0 and any(data.get(k) != data0.get(k) for k in data0):
                editable.add(ps.name)
        return editable


# §2.2 precedence order (highest first). Only OFFICIAL exists in v1.0.
_LIBRARY_PRECEDENCE = ("project", "user", OFFICIAL_LIBRARY)


def _build_spec(cls: type) -> BlockSpec:
    category = getattr(cls, "CATEGORY", "uncategorized")
    tags = tuple(getattr(cls, "TAGS", ()))
    description = _first_paragraph(inspect.getdoc(cls) or "")
    params = _param_specs(cls)

    # cell_count / interface come from a default instance. Most blocks construct
    # with just a name; parameter-required blocks (e.g. FIR needs coefficients)
    # are probed with their declared defaults, and fall back to safe sentinels
    # if even that fails (geometry is then reported as 0 until parameterized).
    cell_count, entry, ins, outs = _probe_instance(cls, params)

    return BlockSpec(
        type_name=cls.__name__,
        library=OFFICIAL_LIBRARY,
        category=category,
        tags=tags,
        description=description,
        default_cell_count=cell_count,
        entry_address=entry,
        input_registers=ins,
        output_registers=outs,
        params=params,
        cls=cls,
    )


def _param_specs(cls: type) -> tuple[ParamSpec, ...]:
    sig = inspect.signature(cls.__init__)
    out = []
    for name, p in sig.parameters.items():
        if name in ("self", "name") or p.kind in (
            inspect.Parameter.VAR_POSITIONAL,
            inspect.Parameter.VAR_KEYWORD,
        ):
            continue
        default = _NO_DEFAULT if p.default is inspect.Parameter.empty else p.default
        type_name = None
        if p.annotation is not inspect.Parameter.empty:
            type_name = getattr(p.annotation, "__name__", str(p.annotation))
        out.append(ParamSpec(name=name, default=default, type_name=type_name))
    return tuple(out)


def _probe_instance(
    cls: type, params: tuple[ParamSpec, ...]
) -> tuple[int, int, tuple[int, ...], tuple[int, ...]]:
    """Construct a probe instance to read cell_count + interface.

    Uses declared parameter defaults. If the class cannot be constructed from
    defaults alone (a required param without a default), geometry is reported
    as 0 and the interface defaults are used — the real values are available
    once the block is instantiated with concrete params via the catalog.
    """
    kwargs = {p.name: p.default for p in params if p.default is not _NO_DEFAULT}
    try:
        inst = cls(name="__probe__", **kwargs)
        iface = inst.interface
        return (
            int(inst.cell_count),
            int(getattr(iface, "entry_address", 1)),
            tuple(getattr(iface, "input_registers", ()) or ()),
            tuple(getattr(iface, "output_registers", ()) or ()),
        )
    except Exception:
        return (0, 1, (), ())


def _first_paragraph(doc: str) -> str:
    """First non-empty paragraph of a docstring, whitespace-collapsed."""
    lines: list[str] = []
    for line in doc.strip().splitlines():
        if not line.strip():
            if lines:
                break
            continue
        lines.append(line.strip())
    return " ".join(lines)
