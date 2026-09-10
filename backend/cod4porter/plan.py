from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Sequence

from .graph import AssetNode, AssetType, OwnershipEdge, validate_graph, write_graph
from .main_ff import build_main_fastfile, MainBuildResult
from .backend.v4_ffio import XAssetList
from .zone_writer import SerializationOptions


class DispositionKind(str, Enum):
    EMITTED = 'emitted'
    MERGED = 'merged'
    EXTERNAL_NATIVE = 'external_native'


@dataclass(frozen=True)
class SourceDisposition:
    source_index: int
    source_type_id: int
    kind: DispositionKind
    target_symbol: str | None = None
    target_type: AssetType | None = None
    reason: str = ''


@dataclass
class PortPlan:
    """A complete destination registry plus an explicit global/owned placement plan.

    ``assets`` is the symbol registry, not the XAssetList.  Only symbols selected by an
    ``EMITTED`` source disposition are global XAssets.  Every reachable non-global symbol must
    have exactly one :class:`OwnershipEdge`; its owner writes that child at the corresponding
    FOLLOWING/INSERT pointer site.

    Keeping these two concepts separate fixes the CP17 failure where support Material/Image
    nodes validated as graph dependencies but were absent during serialization.  It also avoids
    the inverse CP11 bug that promoted every support node into the global XAssetList.
    """

    assets: Sequence[AssetNode]
    script_strings: Sequence[str | None]
    dispositions: Sequence[SourceDisposition]
    map_name: str
    findings: list[str] = field(default_factory=list)
    ownership_edges: Sequence[OwnershipEdge] = field(default_factory=tuple)
    # PC custom-map IWDs are separate source containers.  When the destination contract is a
    # FastFile-only PS3 package, archive images which have no Material owner still need explicit
    # global XAsset roots (for example compass/loadscreen images reached by name at runtime).
    # They are destination-only additions, so they deliberately do not manufacture fake source
    # dispositions and do not weaken the exact PC XAsset classification gate.
    supplemental_top_level_symbols: Sequence[str] = field(default_factory=tuple)

    def _registry(self) -> dict[str, AssetNode]:
        ordered = tuple(self.assets)
        symbols = {a.symbol: a for a in ordered}
        if len(symbols) != len(ordered):
            raise ValueError('PortPlan duplicate destination symbol')
        return symbols

    def _source_top_level_assets(self) -> tuple[AssetNode, ...]:
        """Collect destination XAssets in first source-occurrence order.

        A destination asset is global only when a source disposition explicitly EMITs it.
        Multiple source roots may MERGE into one destination root; that root appears exactly
        once at the first emitted source slot.
        """
        bysym = self._registry()
        seen: set[str] = set()
        out: list[AssetNode] = []
        for d in sorted(self.dispositions, key=lambda x: x.source_index):
            if d.kind != DispositionKind.EMITTED or not d.target_symbol:
                continue
            if d.target_symbol in seen:
                continue
            if d.target_symbol not in bysym:
                raise ValueError(f'top-level target missing: {d.target_symbol}')
            out.append(bysym[d.target_symbol])
            seen.add(d.target_symbol)
        for symbol in self.supplemental_top_level_symbols:
            if symbol in seen:
                raise ValueError(f'supplemental top-level node already emitted from source: {symbol}')
            if symbol not in bysym:
                raise ValueError(f'supplemental top-level target missing: {symbol}')
            out.append(bysym[symbol])
            seen.add(symbol)
        return tuple(out)

    def top_level_assets(self) -> tuple[AssetNode, ...]:
        """Return a stable loader-safe topological order of global XAssets.

        Source order is retained whenever dependencies permit it. A packed
        reference to a top-level XAssetHeader can only be consumed after that
        header cell has been replaced by the loaded asset address. Dependencies
        reached through inline-owned support nodes therefore constrain their
        top-level owner as well.
        """
        source_top=self._source_top_level_assets()
        if not source_top:return ()
        bysym=self._registry();top_symbols={a.symbol for a in source_top}
        owner_by_child={e.child_symbol:e.owner_symbol for e in self.ownership_edges}

        def top_owner(symbol:str)->str:
            seen=set()
            while symbol not in top_symbols:
                if symbol in seen:raise ValueError(f'ownership cycle while ordering {symbol}')
                seen.add(symbol)
                owner=owner_by_child.get(symbol)
                if owner is None:raise ValueError(f'active support dependency {symbol} has no top-level owner')
                symbol=owner
            return symbol

        reachable=set();stack=[a.symbol for a in reversed(source_top)]
        while stack:
            symbol=stack.pop()
            if symbol in reachable:continue
            node=bysym.get(symbol)
            if node is None:raise ValueError(f'graph closure references missing node {symbol}')
            reachable.add(symbol)
            for dep in reversed(tuple(getattr(node,'dependencies',()))):stack.append(dep.target_symbol)

        prerequisites={a.symbol:set() for a in source_top}
        for symbol in reachable:
            consumer=top_owner(symbol)
            for dep in getattr(bysym[symbol],'dependencies',()):
                required=top_owner(dep.target_symbol)
                if required!=consumer:prerequisites[consumer].add(required)

        emitted=set();ordered=[]
        while len(ordered)<len(source_top):
            ready=next((a for a in source_top if a.symbol not in emitted and prerequisites[a.symbol]<=emitted),None)
            if ready is None:
                pending={a.symbol:sorted(prerequisites[a.symbol]-emitted) for a in source_top if a.symbol not in emitted}
                raise ValueError(f'top-level XAsset dependency cycle: {pending}')
            ordered.append(ready);emitted.add(ready.symbol)
        return tuple(ordered)

    def with_external_xmodels(self) -> tuple['PortPlan', dict]:
        """Return a copy whose XModels are name-reference shells instead of owned geometry.

        Diagnostic only.  The XAssetList keeps every XModel slot, so the ClipMap static-model
        array and the GfxWorld smodel draws - which reference models through their XAssetList
        alias cell, never through the model's bytes - stay valid.  Dropping the ownership edges
        the models held makes their Materials, GfxImages and PhysPresets unreachable, and
        ``serialized_assets`` then leaves them dormant on its own.
        """
        from .assets.external import ExternalMaterialNode, ExternalTechniqueSetNode, ExternalXModelNode
        from .assets.image import ExternalImageNode  # local imports: avoid an import cycle
        from .xmodel import XModelNode

        def shell(node: AssetNode) -> AssetNode | None:
            name = getattr(node, 'name', None) or getattr(getattr(node, 'model', None), 'name', None) \
                or getattr(getattr(node, 'material', None), 'name', None) \
                or getattr(getattr(node, 'image', None), 'name', None)
            if not name:
                return None
            if node.type is AssetType.XMODEL:
                return ExternalXModelNode(name, node.symbol)
            if node.type is AssetType.MATERIAL:
                return ExternalMaterialNode(name, node.symbol)
            if node.type is AssetType.IMAGE:
                return ExternalImageNode(name, node.symbol)
            if node.type is AssetType.TECHSET:
                return ExternalTechniqueSetNode(name, node.symbol)
            return None

        assets = {a.symbol: a for a in self.assets}
        order = [a.symbol for a in self.assets]
        replaced: dict[str, str] = {}
        kept_physics = 0
        for symbol, node in list(assets.items()):
            if not isinstance(node, XModelNode):
                continue
            model = node.model
            # A model that carries physics is left whole.  The ClipMap's dynamic entities and
            # other models reach its PhysPreset through an INSERT alias that only the owning
            # XModelNode defines, so shelling it would leave that relocation unresolvable.
            if (model.inline_phys_preset is not None or model.inline_phys_geoms is not None
                    or model.resolved_phys_preset_asset_index is not None):
                kept_physics += 1
                continue
            new = shell(node)
            if new is None:
                raise ValueError(f'no reference shell for XModel {symbol}')
            assets[symbol] = new
            replaced[symbol] = model.name
        edges = [e for e in self.ownership_edges if e.owner_symbol not in replaced]
        # Dropping an owner orphans its children.  A child nobody else reaches simply goes
        # dormant, but one that another asset still references by packed alias needs a writer,
        # so it is promoted to a top-level XAsset of its own.  That is legal - Retail zones
        # carry standalone Materials and GfxImages - and it keeps every pointer resolvable.
        supplemental = list(self.supplemental_top_level_symbols)
        for _ in range(16):
            plan = PortPlan(
                tuple(assets[s] for s in order), self.script_strings, self.dispositions,
                self.map_name, list(self.findings), tuple(edges), tuple(supplemental),
            )
            reachable = {a.symbol for a in plan.serialized_assets()}
            top = {a.symbol for a in plan._source_top_level_assets()}
            owner_by_child = {e.child_symbol: e.owner_symbol for e in edges}

            def rootless(symbol: str) -> bool:
                """True when the ownership chain from ``symbol`` never reaches a global XAsset."""
                seen: set[str] = set()
                while symbol not in top:
                    if symbol in seen:
                        return True
                    seen.add(symbol)
                    symbol = owner_by_child.get(symbol)
                    if symbol is None:
                        return True
                return False

            orphans = sorted(s for s in reachable if s not in top and rootless(s))
            if not orphans:
                break
            supplemental.extend(orphans)
        else:
            raise ValueError('external-XModel reduction did not converge')
        dropped = len(self.ownership_edges) - len(edges)
        before = {a.symbol for a in self.serialized_assets()}
        after = {a.symbol for a in plan.serialized_assets()}
        return plan, {
            'xmodels_replaced_by_shells': len(replaced),
            'xmodels_kept_for_physics': kept_physics,
            'ownership_edges_dropped': dropped,
            'orphans_promoted_to_top_level': len(supplemental) - len(self.supplemental_top_level_symbols),
            'nodes_made_dormant': len(before - after),
        }

    def serialized_assets(self) -> tuple[AssetNode, ...]:
        """Return the dependency closure reachable from global XAssets.

        Candidate nodes that were discovered during planning but are not referenced by the final
        destination graph remain in the registry as *dormant* evidence.  They are deliberately
        excluded from serialization and therefore do not require an artificial owner.
        """
        bysym = self._registry()
        # Reachability is independent of final loader order and can be computed
        # before ownership placement is resolved during assembly.
        top = self._source_top_level_assets()
        if not top:
            raise ValueError('PortPlan has no emitted top-level destination assets')
        reachable: set[str] = set()
        stack = [a.symbol for a in reversed(top)]
        while stack:
            symbol = stack.pop()
            if symbol in reachable:
                continue
            node = bysym.get(symbol)
            if node is None:
                raise ValueError(f'graph closure references missing node {symbol}')
            reachable.add(symbol)
            for dep in reversed(tuple(getattr(node, 'dependencies', ()))):
                if dep.target_symbol not in bysym:
                    raise ValueError(
                        f'{symbol} references missing {dep.target_symbol}: {dep.description}'
                    )
                stack.append(dep.target_symbol)
        # Preserve registry order for stable diagnostics.  Physical ordering is driven by the
        # global XAsset order and each owner's explicit child-write sites.
        return tuple(a for a in self.assets if a.symbol in reachable)

    def active_ownership_edges(self) -> tuple[OwnershipEdge, ...]:
        active = {a.symbol for a in self.serialized_assets()}
        return tuple(
            e for e in self.ownership_edges
            if e.owner_symbol in active and e.child_symbol in active
        )

    def validate(self, source_assets: XAssetList | None = None) -> dict:
        symbols = self._registry()
        supplemental=tuple(self.supplemental_top_level_symbols)
        if len(set(supplemental))!=len(supplemental):
            raise ValueError('duplicate supplemental top-level symbol')
        missing_supplemental=[symbol for symbol in supplemental if symbol not in symbols]
        if missing_supplemental:
            raise ValueError(f'supplemental top-level targets missing: {missing_supplemental[:20]}')
        byidx: dict[int, SourceDisposition] = {}
        for d in self.dispositions:
            if d.source_index in byidx:
                raise ValueError(f'duplicate source disposition #{d.source_index}')
            byidx[d.source_index] = d
            if d.kind == DispositionKind.EMITTED:
                if not d.target_symbol or d.target_symbol not in symbols:
                    raise ValueError(
                        f'source #{d.source_index} emitted target missing: {d.target_symbol}'
                    )
                if d.target_type is not None and symbols[d.target_symbol].type != d.target_type:
                    raise ValueError(f'source #{d.source_index} destination type mismatch')
            elif d.kind == DispositionKind.MERGED:
                if not d.target_symbol or d.target_symbol not in symbols:
                    raise ValueError(
                        f'source #{d.source_index} merged parent missing: {d.target_symbol}'
                    )
            elif d.kind == DispositionKind.EXTERNAL_NATIVE:
                if not d.reason:
                    raise ValueError(
                        f'source #{d.source_index} external-native classification needs evidence/reason'
                    )

        if source_assets is not None:
            expected = {a.index for a in source_assets.assets}
            actual = set(byidx)
            if expected != actual:
                miss = sorted(expected - actual)
                extra = sorted(actual - expected)
                raise ValueError(
                    f'XAsset closure incomplete: missing={miss[:20]} extra={extra[:20]}'
                )
            for a in source_assets.assets:
                if byidx[a.index].source_type_id != a.type_id:
                    raise ValueError(f'source disposition type drift at asset #{a.index}')

        top = self.top_level_assets()
        active = self.serialized_assets()
        edges = self.active_ownership_edges()
        validate_graph(active, top_level_assets=top, ownership_edges=edges)

        kinds = {
            k.value: sum(d.kind == k for d in self.dispositions)
            for k in DispositionKind
        }
        active_symbols = {a.symbol for a in active}
        return {
            'passed': True,
            'source_assets': len(byidx),
            'destination_assets': len(top),
            'registry_nodes': len(symbols),
            'serialized_nodes': len(active),
            'support_nodes': len(active) - len(top),
            'dormant_nodes': len(symbols) - len(active_symbols),
            'ownership_edges': len(edges),
            'dispositions': kinds,
            'asset_types': self.destination_counts(),
        }

    def destination_counts(self) -> dict[str, int]:
        out: dict[str, int] = {}
        for a in self.top_level_assets():
            out[a.type.name] = out.get(a.type.name, 0) + 1
        return out

    def build(self, source_assets: XAssetList | None = None) -> MainBuildResult:
        self.validate(source_assets)
        top = self.top_level_assets()
        active = self.serialized_assets()
        return build_main_fastfile(
            active,
            self.script_strings,
            preserve_order=True,
            top_level_assets=top,
            ownership_edges=self.active_ownership_edges(),
        )

    def measure_block_sizes(self, source_assets: XAssetList | None = None) -> tuple[int, ...]:
        """Run one uncompressed layout pass and return the exact loader high-water marks.

        This is intentionally lighter than :meth:`build`: policy planning needs the fixed
        B3/B6 baseline, not a compressed FastFile or the two-pass determinism proof.
        """
        self.validate(source_assets)
        top=self.top_level_assets();active=self.serialized_assets()
        measured=write_graph(
            active,self.script_strings,SerializationOptions(apply_retail_block_floors=True),top_level_assets=top,
            ownership_edges=self.active_ownership_edges(),
        )
        return tuple(measured.zone.block_sizes)
