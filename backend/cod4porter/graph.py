from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum, IntEnum
import struct
from typing import Any, Dict, Protocol, Sequence

from .zone_writer import (
    ASSET_ALIAS_BLOCK,
    FOLLOWING,
    INSERT,
    PackedOffset,
    RelocatingZoneWriter,
    SerializationOptions,
)


class AssetType(IntEnum):
    XMODELPIECES=0x00; PHYSPRESET=0x01; XANIM=0x02; XMODEL=0x03; MATERIAL=0x04
    PIXELSHADER=0x05; VERTEXSHADER=0x06; TECHSET=0x07; IMAGE=0x08; SOUND=0x09
    SNDCURVE=0x0A; LOADED_SOUND=0x0B; CLIPMAP_SP=0x0C; CLIPMAP_MP=0x0D; COMWORLD=0x0E
    GAMEWORLD_SP=0x0F; GAMEWORLD_MP=0x10; MAP_ENTS=0x11; GFXWORLD=0x12; LIGHTDEF=0x13
    UI_MAP=0x14; FONT=0x15; MENUFILE=0x16; MENU=0x17; LOCALIZE=0x18; WEAPON=0x19
    SNDDRIVERGLOBALS=0x1A; FX=0x1B; IMPACTFX=0x1C; AITYPE=0x1D; MPTYPE=0x1E
    CHARACTER=0x1F; XMODELALIAS=0x20; RAWFILE=0x21; STRINGTABLE=0x22


@dataclass(frozen=True)
class Dependency:
    target_symbol: str
    expected_type: AssetType
    description: str


class AssetNode(Protocol):
    type: AssetType
    symbol: str
    root_block: int
    dependencies: Sequence[Dependency]
    def write(self, writer: RelocatingZoneWriter, context: 'GraphContext') -> None: ...


class OwnedPointerKind(str, Enum):
    FOLLOWING = 'following'
    INSERT = 'insert'

    @property
    def marker(self) -> int:
        return INSERT if self is OwnedPointerKind.INSERT else FOLLOWING


@dataclass(frozen=True)
class OwnershipEdge:
    """One concrete inline ownership site in the destination loader graph.

    A support node is serialized exactly once by its owner.  The first pointer at ``site``
    contains FOLLOWING/INSERT; every later reference is a packed pointer to the persistent
    owner pointer field (FOLLOWING) or DB alias cell (INSERT). ``site`` is deliberately opaque to the generic
    graph writer and is interpreted by the owning node (for example ``material[3]`` or
    ``attenuation``).
    """

    owner_symbol: str
    child_symbol: str
    site: str
    pointer_kind: OwnedPointerKind = OwnedPointerKind.INSERT
    description: str = ''
    order: int = 0

    def __post_init__(self) -> None:
        if isinstance(self.pointer_kind, str):
            object.__setattr__(self, 'pointer_kind', OwnedPointerKind(self.pointer_kind))
        if not self.owner_symbol or not self.child_symbol or not self.site:
            raise ValueError('ownership edge requires owner, child and site')
        if self.owner_symbol == self.child_symbol:
            raise ValueError('asset cannot own itself')


def header_symbol(symbol: str) -> str:
    return symbol + '::$xasset_header'


def owned_alias_symbol(symbol: str) -> str:
    return symbol + '::$owned_insert_alias'


def nested_symbol(symbol: str, name: str) -> str:
    return symbol + '::' + name


@dataclass
class GraphContext:
    nodes: Dict[str, AssetNode] = field(default_factory=dict)
    top_level_symbols: tuple[str, ...] = ()
    ownership_edges: tuple[OwnershipEdge, ...] = ()
    root_physical_offsets: Dict[str, int] = field(default_factory=dict)
    header_physical_offsets: Dict[str, int] = field(default_factory=dict)
    header_locations: Dict[str, PackedOffset] = field(default_factory=dict)
    asset_types: Dict[str, AssetType] = field(default_factory=dict)
    results: Dict[str, Any] = field(default_factory=dict)
    _edge_by_site: Dict[tuple[str, str, str], OwnershipEdge] = field(init=False, default_factory=dict)
    _edge_by_child: Dict[str, OwnershipEdge] = field(init=False, default_factory=dict)
    _written_nodes: set[str] = field(init=False, default_factory=set)
    _active_stack: list[str] = field(init=False, default_factory=list)

    def __post_init__(self) -> None:
        top = set(self.top_level_symbols)
        for e in self.ownership_edges:
            key = (e.owner_symbol, e.child_symbol, e.site)
            if key in self._edge_by_site:
                raise ValueError(f'duplicate ownership site {key}')
            if e.child_symbol in self._edge_by_child:
                old = self._edge_by_child[e.child_symbol]
                raise ValueError(
                    f'owned child {e.child_symbol} has multiple owners/sites: '
                    f'{old.owner_symbol}:{old.site} and {e.owner_symbol}:{e.site}'
                )
            if e.child_symbol in top:
                raise ValueError(f'top-level XAsset {e.child_symbol} cannot also be inline-owned')
            self._edge_by_site[key] = e
            self._edge_by_child[e.child_symbol] = e

    @property
    def written_nodes(self) -> frozenset[str]:
        return frozenset(self._written_nodes)

    @property
    def active_owner(self) -> str | None:
        return self._active_stack[-1] if self._active_stack else None

    def register_header(self, symbol: str, typ: AssetType, physical: int, logical: PackedOffset) -> None:
        if symbol in self.header_physical_offsets:
            raise ValueError(f'duplicate header {symbol}')
        if symbol not in self.top_level_symbols:
            raise ValueError(f'non-top-level node {symbol} cannot register an XAssetList header')
        self.header_physical_offsets[symbol] = physical
        self.header_locations[symbol] = logical
        self.asset_types[symbol] = typ

    def register_root(self, symbol: str, physical: int) -> None:
        if symbol in self.root_physical_offsets:
            raise ValueError(f'duplicate root {symbol}')
        if symbol not in self.nodes:
            raise ValueError(f'unregistered graph node root {symbol}')
        self.root_physical_offsets[symbol] = physical

    def register_result(self, key: str, value: Any) -> None:
        if key in self.results:
            raise ValueError(f'duplicate result {key}')
        self.results[key] = value

    def ownership_edge(self, owner_symbol: str, child_symbol: str, site: str) -> OwnershipEdge | None:
        return self._edge_by_site.get((owner_symbol, child_symbol, site))

    def child_owner(self, child_symbol: str) -> OwnershipEdge | None:
        return self._edge_by_child.get(child_symbol)

    def owns_at(self, owner_symbol: str, child_symbol: str, site: str) -> bool:
        return self.ownership_edge(owner_symbol, child_symbol, site) is not None

    def owned_marker(self, owner_symbol: str, child_symbol: str, site: str) -> int | None:
        edge = self.ownership_edge(owner_symbol, child_symbol, site)
        return None if edge is None else edge.pointer_kind.marker

    def bind_owned_pointer_field(
        self,
        writer: RelocatingZoneWriter,
        owner_symbol: str,
        child_symbol: str,
        site: str,
        location: PackedOffset,
    ) -> None:
        """Name the persistent owner pointer cell for FOLLOWING reuse.

        A nested asset root in TEMP is stack memory and can never be the target
        of a later packed reference. INSERT ownership obtains a separate DB
        alias cell in :meth:`write_owned_child` instead.
        """
        edge=self.ownership_edge(owner_symbol,child_symbol,site)
        if edge is None:raise ValueError(f'{owner_symbol}:{site} is not an ownership site for {child_symbol}')
        if edge.pointer_kind is OwnedPointerKind.FOLLOWING:
            writer.define_symbol_at(owned_alias_symbol(child_symbol),location)

    def reference_symbol(self, symbol: str) -> str:
        """Return the packed-reference target for a graph asset identity.

        Top-level references target the XAssetList header pointer cell. Owned children target
        either the persistent owner pointer field (FOLLOWING) or DB alias cell (INSERT), never
        their reusable TEMP root. The symbolic relocation may be registered before the
        target is physically serialized; the writer resolves it at finalization.
        """
        if symbol not in self.nodes:
            raise ValueError(f'unknown graph reference {symbol}')
        if symbol in self.top_level_symbols:
            return header_symbol(symbol)
        edge = self._edge_by_child.get(symbol)
        if edge is None:
            raise ValueError(f'support node {symbol} has no owner/reference policy')
        return owned_alias_symbol(symbol)

    def serialize_node(self, writer: RelocatingZoneWriter, symbol: str) -> None:
        if symbol in self._written_nodes:
            raise ValueError(f'graph node {symbol} was serialized more than once')
        if symbol in self._active_stack:
            cycle = ' -> '.join(self._active_stack + [symbol])
            raise ValueError(f'ownership cycle during serialization: {cycle}')
        node = self.nodes.get(symbol)
        if node is None:
            raise ValueError(f'graph node {symbol} is not registered')
        self._active_stack.append(symbol)
        writer.push_block(node.root_block)
        try:
            # Every IW3 PS3 XAsset root is at least uint32-aligned in its
            # destination allocator.  FastFile bytes remain physically adjacent;
            # only the active block cursor advances across loader padding.
            writer.align_logical_only(4)
            node.write(writer, self)
        finally:
            popped = writer.pop_block()
            self._active_stack.pop()
        if popped != node.root_block:
            raise ValueError(f'{symbol} changed block stack')
        if symbol not in self.root_physical_offsets:
            raise ValueError(f'{symbol} did not register its root')
        self._written_nodes.add(symbol)

    def write_owned_child(
        self,
        writer: RelocatingZoneWriter,
        owner_symbol: str,
        child_symbol: str,
        site: str,
    ) -> None:
        edge = self.ownership_edge(owner_symbol, child_symbol, site)
        if edge is None:
            raise ValueError(f'{owner_symbol}:{site} does not own {child_symbol}')
        if self.active_owner != owner_symbol:
            raise ValueError(
                f'owned child {child_symbol} requested by inactive owner {owner_symbol}; '
                f'active={self.active_owner}'
            )
        if child_symbol in self._written_nodes:
            raise ValueError(f'owned child {child_symbol} already serialized')
        if edge.pointer_kind is OwnedPointerKind.INSERT:
            writer.define_insert_pointer_alias(
                owned_alias_symbol(child_symbol),
                canonical_identity='owned-xasset:' + child_symbol,
                description=edge.description or f'{owner_symbol}:{site} INSERT alias',
            )
        elif owned_alias_symbol(child_symbol) not in writer.symbols:
            raise ValueError(f'{owner_symbol}:{site} FOLLOWING owner pointer field was not bound')
        self.serialize_node(writer, child_symbol)
        self.results[f'graph.owned:{child_symbol}'] = {
            'owner_symbol': owner_symbol,
            'site': site,
            'pointer_kind': edge.pointer_kind.value,
            'reference_symbol': self.reference_symbol(child_symbol),
            'order': edge.order,
            'description': edge.description,
        }

    def finalize(self) -> None:
        missing = sorted(set(self.nodes) - self._written_nodes)
        if missing:
            raise ValueError(f'graph nodes were never serialized: {missing[:20]}')
        self.results['graph.placement'] = {
            'top_level_symbols': list(self.top_level_symbols),
            'owned_nodes': [
                {
                    'owner_symbol': e.owner_symbol,
                    'child_symbol': e.child_symbol,
                    'site': e.site,
                    'pointer_kind': e.pointer_kind.value,
                    'order': e.order,
                    'description': e.description,
                }
                for e in sorted(self.ownership_edges, key=lambda x: (x.order, x.owner_symbol, x.site, x.child_symbol))
            ],
            'written_nodes': len(self._written_nodes),
        }


@dataclass
class GraphWriteResult:
    zone: Any
    context: GraphContext
    script_strings: tuple[str | None, ...]
    asset_types: tuple[AssetType, ...]
    all_asset_types: tuple[AssetType, ...]


def _dependency_map(assets: Sequence[AssetNode]) -> dict[str, tuple[Dependency, ...]]:
    return {a.symbol: tuple(getattr(a, 'dependencies', ())) for a in assets}


def validate_graph(
    assets: Sequence[AssetNode],
    top_level_assets: Sequence[AssetNode] | None = None,
    ownership_edges: Sequence[OwnershipEdge] = (),
) -> None:
    if not assets:
        raise ValueError('asset graph cannot be empty')
    by: dict[str, AssetNode] = {}
    for a in assets:
        if not a.symbol or a.symbol in by:
            raise ValueError(f'empty/duplicate asset symbol {a.symbol!r}')
        by[a.symbol] = a
    top = tuple(top_level_assets or assets)
    if not top:
        raise ValueError('asset graph needs at least one top-level XAsset')
    top_symbols: list[str] = []
    for a in top:
        if a.symbol not in by:
            raise ValueError(f'top-level node {a.symbol} is not in graph registry')
        if a.symbol in top_symbols:
            raise ValueError(f'duplicate top-level node {a.symbol}')
        if by[a.symbol].type != a.type:
            raise ValueError(f'top-level node type drift for {a.symbol}')
        top_symbols.append(a.symbol)

    deps = _dependency_map(assets)
    for a in assets:
        seen: set[str] = set()
        for d in deps[a.symbol]:
            if d.target_symbol in seen:
                continue
            seen.add(d.target_symbol)
            if d.target_symbol not in by:
                raise ValueError(f'{a.symbol} references missing {d.target_symbol}: {d.description}')
            if by[d.target_symbol].type != d.expected_type:
                raise ValueError(f'{a.symbol} dependency type mismatch for {d.target_symbol}')

    child_edges: dict[str, OwnershipEdge] = {}
    site_edges: set[tuple[str, str, str]] = set()
    for raw in ownership_edges:
        e = raw if isinstance(raw, OwnershipEdge) else OwnershipEdge(*raw)
        if e.owner_symbol not in by or e.child_symbol not in by:
            raise ValueError(f'ownership edge references missing node: {e.owner_symbol}->{e.child_symbol}')
        if e.child_symbol in top_symbols:
            raise ValueError(f'top-level XAsset {e.child_symbol} cannot be inline-owned')
        if e.child_symbol in child_edges:
            raise ValueError(f'owned child {e.child_symbol} has more than one ownership edge')
        key = (e.owner_symbol, e.child_symbol, e.site)
        if key in site_edges:
            raise ValueError(f'duplicate ownership site {key}')
        site_edges.add(key)
        child_edges[e.child_symbol] = e
        if not any(d.target_symbol == e.child_symbol for d in deps[e.owner_symbol]):
            raise ValueError(
                f'ownership edge {e.owner_symbol}:{e.site}->{e.child_symbol} is not declared as a dependency'
            )

    support = set(by) - set(top_symbols)
    missing_owner = sorted(support - set(child_edges))
    extra_owner = sorted(set(child_edges) - support)
    if missing_owner:
        raise ValueError(f'support nodes have no explicit owner: {missing_owner[:20]}')
    if extra_owner:
        raise ValueError(f'ownership edges target non-support nodes: {extra_owner[:20]}')

    # Ownership must form a forest rooted in the global XAssetList.
    children_by_owner: dict[str, list[str]] = {}
    for e in child_edges.values():
        children_by_owner.setdefault(e.owner_symbol, []).append(e.child_symbol)
    visiting: set[str] = set()
    visited: set[str] = set()

    def walk(symbol: str) -> None:
        if symbol in visiting:
            raise ValueError(f'ownership cycle detected at {symbol}')
        if symbol in visited:
            return
        visiting.add(symbol)
        for child in children_by_owner.get(symbol, ()):
            walk(child)
        visiting.remove(symbol)
        visited.add(symbol)

    for symbol in top_symbols:
        walk(symbol)
    unreachable = sorted(set(by) - visited)
    if unreachable:
        raise ValueError(f'graph nodes are not reachable from a top-level owner: {unreachable[:20]}')


def write_graph(
    assets: Sequence[AssetNode],
    script_strings: Sequence[str | None] = (),
    options: SerializationOptions | None = None,
    *,
    top_level_assets: Sequence[AssetNode] | None = None,
    ownership_edges: Sequence[OwnershipEdge] = (),
) -> GraphWriteResult:
    all_assets = tuple(assets)
    top = tuple(top_level_assets or all_assets)
    ownership = tuple(ownership_edges)
    validate_graph(all_assets, top, ownership)
    by = {a.symbol: a for a in all_assets}
    c = GraphContext(by, tuple(a.symbol for a in top), ownership)
    w = RelocatingZoneWriter(options)
    # XAssetList contains only true global roots. Inline-owned support nodes are loaded by
    # their parent and never receive a synthetic global XAssetHeader entry.
    # The 0x10-byte XAssetList root is raw/fill data and is not charged to any
    # loader block. Only its script-string members and XAssetHeader array enter B4.
    w.write_raw(
        struct.pack('>4I',len(script_strings),FOLLOWING if script_strings else 0,len(top),FOLLOWING),
        'raw XAssetList root',
    )
    w.push_block(ASSET_ALIAS_BLOCK)
    for i, s in enumerate(script_strings):
        w.write_u32(FOLLOWING if s is not None else 0, f'script string pointer #{i}')
    for i, s in enumerate(script_strings):
        if s is not None:
            w.write_latin1z(s, f'script string #{i}')
    w.align_logical_only(4)
    for a in top:
        w.write_u32(int(a.type), f'XAsset type {a.type.name} ({a.symbol})')
        physical = w.physical_position
        logical = w.define_symbol(header_symbol(a.symbol))
        c.register_header(a.symbol, a.type, physical, logical)
        w.write_following_pointer(f'XAsset header {a.symbol}')
    w.pop_block()

    for a in top:
        c.serialize_node(w, a.symbol)
    c.finalize()
    result = w.build()

    # Independent structural check through the vendored backend parser.
    from .backend.v4_ffio import parse_xasset_list
    al = parse_xasset_list(result.zone_bytes, 'ps3')
    if len(al.assets) != len(top) or len(al.script_strings) != len(script_strings):
        raise ValueError('graph readback count mismatch')
    if any(x.serialized_pointer != FOLLOWING for x in al.assets):
        raise ValueError('top-level graph root marker mismatch')
    return GraphWriteResult(
        result,
        c,
        tuple(script_strings),
        tuple(a.type for a in top),
        tuple(a.type for a in all_assets),
    )
