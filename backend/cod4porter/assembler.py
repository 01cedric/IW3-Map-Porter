from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Mapping,Sequence
import hashlib

from .backend.v4_ffio import decode_pc_pointer
from .graph import AssetType,OwnershipEdge,OwnedPointerKind,nested_symbol
from .plan import PortPlan,SourceDisposition,DispositionKind
from .source_plan import CoreSourcePlan, analyze_core_source
from .material_graph import build_material_graph,norm,sym,MaterialGraphPlan,PS3_DEFAULT_MATERIAL
from .material_planning import serialized_image_pointer
from .assets.external import ExternalTechniqueSetNode,ExternalMaterialNode,ExternalXModelNode
from .assets.image import ImageNode,ExternalImageNode,ImageResourcePolicy
from .pc_image import image_asset_from_pc,parse_image_at
from .assets.basic import RawFileNode,StringTableNode,LightDefNode,ExternalFxNode
from .assets.fx import OwnedFxNode,ImpactFxNode
from .assets.sound import SoundNode,SoundCurveAssetNode,LoadedSoundAssetNode,StandaloneLoadedPlan
from .assets.gfxworld import GfxWorldNode,GfxWorldResourcePolicy
from .assets.clipmap import ClipMapNode
from .physics import PhysPresetNode
from .xmodel import XModelNode
from .world_core import parse_comworld,ComWorldNode,GameWorldMpNode
from .sound_binding import stage_iwd_sounds,resolve_sound_bindings,stage_standalone_loaded_sound
from .pc_fx import parse_impact_fx,resolve_fx_runner_bindings,resolve_fx_material_bindings,VisualKind
from .pc_xmodel import xasset_header_alias_index
from .pc_clipmap import resolve_dynent_owned_phys_aliases
from .source_assets import top_level_rawfiles,top_level_stringtables,top_level_lightdefs,packed_xstring_evidence_from_tables

PC={
 'xmodelpieces':0x00,'physpreset':0x01,'xanimparts':0x02,'xmodel':0x03,'material':0x04,'techset':0x05,'image':0x06,'sound':0x07,
 'sndcurve':0x08,'loaded_sound':0x09,'clipmap':0x0A,'clipmap_pvs':0x0B,'comworld':0x0C,'gameworld_sp':0x0D,'gameworld_mp':0x0E,
 'map_ents':0x0F,'gfxworld':0x10,'lightdef':0x11,'ui_map':0x12,'font':0x13,'menulist':0x14,'menu':0x15,'localize':0x16,
 'weapon':0x17,'snddriverglobals':0x18,'fx':0x19,'impactfx':0x1A,'aitype':0x1B,'mptype':0x1C,'character':0x1D,'xmodelalias':0x1E,
 'rawfile':0x1F,'stringtable':0x20,
}

def _safe(s:str)->str:
    return ''.join(c if c.isalnum() or c in '_.-' else '_' for c in s)

def _short(s:str)->str:return hashlib.sha1(s.casefold().encode()).hexdigest()[:16]

def _script_strings(core:CoreSourcePlan)->tuple[str|None,...]:return tuple(x.value for x in core.asset_list.script_strings)

@dataclass(frozen=True)
class AssemblyResult:
    source:CoreSourcePlan
    plan:PortPlan
    material_graph:object
    sound_binding:object
    diagnostics:Mapping[str,object]

class _DispositionBuilder:
    def __init__(self,core:CoreSourcePlan):
        self.core=core;self.rows={};self.by_symbol={}
    def add(self,index:int,kind:DispositionKind,target_symbol:str|None=None,target_type:AssetType|None=None,reason:str=''):
        if index in self.rows:raise ValueError(f'source XAsset #{index} already classified')
        a=self.core.asset_list.assets[index]
        self.rows[index]=SourceDisposition(index,a.type_id,kind,target_symbol,target_type,reason)
    def emit(self,index:int,symbol:str,typ:AssetType,reason:str):self.add(index,DispositionKind.EMITTED,symbol,typ,reason)
    def merge(self,index:int,symbol:str,typ:AssetType,reason:str):self.add(index,DispositionKind.MERGED,symbol,typ,reason)
    def ext(self,index:int,reason:str):self.add(index,DispositionKind.EXTERNAL_NATIVE,None,None,reason)
    def finish(self):
        missing=[a for a in self.core.asset_list.assets if a.index not in self.rows]
        if missing:
            counts={}
            for a in missing:counts[a.type_name]=counts.get(a.type_name,0)+1
            raise ValueError(f'unclassified source XAssets ({len(missing)}): {counts}; sample={[x.index for x in missing[:20]]}')
        return tuple(self.rows[i] for i in range(len(self.core.asset_list.assets)))


def _clip_raw_map(core:CoreSourcePlan,expected_type:int,symbol_by_asset_index:Mapping[int,str],raw_values:Sequence[int],label:str)->dict[int,str]:
    out={}
    for raw in raw_values:
        if not raw:continue
        ai=xasset_header_alias_index(core.asset_list,raw,expected_type)
        if ai is None or ai not in symbol_by_asset_index:
            raise ValueError(f'{label} reference 0x{raw:08X} is not an exact top-level XAssetHeader alias')
        old=out.get(raw)
        if old is not None and old!=symbol_by_asset_index[ai]:raise ValueError(f'{label} raw pointer maps inconsistently')
        out[raw]=symbol_by_asset_index[ai]
    return out


def _resolve_clipmap_physpreset_symbols(
    core:CoreSourcePlan,
    clip_symbol:str,
    phys_symbols:Mapping[int,str],
    xmodel_symbols:Mapping[int,str],
)->tuple[list[int],dict[int,str],dict[int,str],dict[int,str]]:
    """Close every non-null ClipMap DynEnt PhysPreset reference with typed evidence.

    Getaway uses three distinct reference domains: top-level PhysPreset XAsset headers,
    XModel-owned INSERT cells, and earlier DynEnt ``physPreset`` pointer cells.  Keeping this
    resolution in one fail-closed helper makes the real-map regression exercise the exact path
    used by the assembler instead of a parallel approximation.
    """
    entries=core.clipmap.dyn_models+core.clipmap.dyn_brushes
    raw_values=[entry.phys_preset_raw for entry in entries]
    top_level:dict[int,str]={}
    xmodel_aliases:dict[int,str]={}
    nested_phys_owners={int(raw):int(owner) for raw,owner in core.phys_resolution.get('nested_target_owners',{}).items()}
    xmodel_by_source={x.model.source_asset_index:x for x in core.xmodels}
    for raw in sorted(set(value for value in raw_values if value)):
        ai=xasset_header_alias_index(core.asset_list,raw,PC['physpreset'])
        if ai is not None and ai in phys_symbols:
            top_level[raw]=phys_symbols[ai]
            continue
        owner=nested_phys_owners.get(raw)
        if owner is None:
            # A DynEnt can reuse the INSERT cell owned by the XModel it instantiates even when no
            # later XModel references that cell. Getaway's com_junktire -> tire relation proves the
            # otherwise single-use alias without guessing from the PhysPreset name.
            users=[entry for entry in entries if entry.phys_preset_raw==raw]
            model_owners=set()
            for entry in users:
                model_ai=xasset_header_alias_index(core.asset_list,entry.xmodel_raw,PC['xmodel']) if entry.xmodel_raw else None
                model=xmodel_by_source.get(model_ai)
                if (model is not None and model.model.inline_phys_preset is not None
                    and decode_pc_pointer(model.model.phys_preset_pointer).kind=='insert'):
                    model_owners.add(model_ai)
            if len(model_owners)==1:
                owner=next(iter(model_owners))
        if owner is not None and owner in xmodel_symbols:
            xmodel_aliases[raw]=nested_symbol(xmodel_symbols[owner],'insert.physPreset')

    unresolved_packed=sorted({
        raw for raw in raw_values
        if raw and decode_pc_pointer(raw).kind=='packed'
        and raw not in top_level and raw not in xmodel_aliases
    })
    dyn_owner_by_raw=resolve_dynent_owned_phys_aliases(core.clipmap,unresolved_packed) if unresolved_packed else {}
    dynent_aliases={
        raw:nested_symbol(clip_symbol,f'dynEnt.{owner.list_kind}.{owner.index}.physPreset.{"insert" if owner.pointer_kind=="insert" else "field"}')
        for raw,owner in dyn_owner_by_raw.items()
    }
    unresolved=sorted({
        raw for raw in raw_values
        if raw and decode_pc_pointer(raw).kind=='packed'
        and raw not in top_level and raw not in xmodel_aliases and raw not in dynent_aliases
    })
    if unresolved:
        raise ValueError(f'ClipMap PhysPreset closure unresolved: {[f"0x{x:08X}" for x in unresolved[:8]]}')
    owned_fields={(owner.list_kind,owner.index) for owner in core.clipmap.dyn_owned_phys_presets}
    missing_owned=[
        (entry.list_kind,entry.index,entry.phys_preset_raw)
        for entry in entries
        if decode_pc_pointer(entry.phys_preset_raw).kind in ('following','insert')
        and (entry.list_kind,entry.index) not in owned_fields
    ]
    if missing_owned:
        raise ValueError(f'ClipMap inline PhysPreset owner roots missing: {missing_owned[:8]}')
    return raw_values,top_level,xmodel_aliases,dynent_aliases


def assemble_core_source(
    core:CoreSourcePlan,*,ffmpeg:str='ffmpeg',runtime_compatible:bool=False,
    boot_isolation:bool|None=None,
    gfxworld_resource_policy:GfxWorldResourcePolicy|str=GfxWorldResourcePolicy.FASTFILE_DELAYED,
    material_image_resource_policy:ImageResourcePolicy|str=ImageResourcePolicy.FASTFILE_DELAYED,
    sound_policy:str='omit',
    unresolved_image_policy:str='reference',
    unresolved_material_policy:str='reference',
    primary_light_policy:str='source',
    vertex_layer_policy:str='source',
    portal_policy:str='source',
)->AssemblyResult:
    if primary_light_policy not in ('source','sun-only'):
        raise ValueError("primary_light_policy must be 'source' or 'sun-only'")
    zone=core.pc_document.zone;al=core.asset_list;nodes=[];disp=_DispositionBuilder(core);findings=[]
    supplemental_top_level_symbols=()
    if isinstance(gfxworld_resource_policy,str):gfxworld_resource_policy=GfxWorldResourcePolicy(gfxworld_resource_policy)
    if isinstance(material_image_resource_policy,str):material_image_resource_policy=ImageResourcePolicy(material_image_resource_policy)
    if sound_policy not in ('omit','map-owned'):raise ValueError(f'unsupported sound policy {sound_policy!r}')
    # Compatibility fallbacks and the historical CP12 boot-isolation graph are separate
    # choices.  Generic callers get the complete owner-aware visual graph unless they opt into
    # the diagnostic isolation mode explicitly.
    if boot_isolation is None:boot_isolation=False
    if boot_isolation and not runtime_compatible:
        raise ValueError('boot isolation requires runtime-compatible semantic fallbacks')
    ownership_candidates=[];ownership_sequence=0
    def ownership_candidate(owner_symbol:str,child_symbol:str,site:str,raw_pointer:int,source_order:int,description:str):
        nonlocal ownership_sequence
        pk=decode_pc_pointer(raw_pointer).kind
        pointer_kind=OwnedPointerKind.FOLLOWING if pk=='following' else OwnedPointerKind.INSERT
        priority=0 if pk in ('following','insert') else 1
        ownership_candidates.append((priority,int(source_order),ownership_sequence,OwnershipEdge(owner_symbol,child_symbol,site,pointer_kind,description,int(source_order))))
        ownership_sequence+=1

    # 1) Technique/Material/Image ownership.
    #
    # CP11 promoted every nested Material/Image support node to the global XAssetList and
    # family-sorted the result. Retail Shipment disproves that topology: XModels/GfxWorld own
    # their Material/Image children while the top-level list stays source/dependency ordered.
    # CP12-A therefore has an explicit boot-isolation path: retain source TechniqueSet roots
    # (merged by proven PS3 native identity), retain the one source top-level Material slot as
    # a real shared ,default Material root, and do not emit nested Material/Image resources as
    # top-level assets. Full nested visual fidelity is restored separately after loader boot.
    from dataclasses import replace as _dc_replace

    if boot_isolation:
        # Do not even instantiate the full MaterialGraph/ImageNodes in CP12-A.  Those nodes retain
        # ~170 MiB of converted IWD pixels in RAM despite not being emitted, which made the
        # deterministic second write thrash memory.  The boot-isolation graph needs semantic
        # identity only.
        default_material_symbol='material:cp12a:shared_default'
        default_material_node=ExternalMaterialNode(PS3_DEFAULT_MATERIAL,default_material_symbol,True)
        nodes.append(default_material_node)
        runtime_material_symbols={norm(m.name).casefold():default_material_symbol for m in core.materials.all_unique}
        runtime_material_symbols['default']=default_material_symbol
        material_symbols_by_source={t.source_asset_index:default_material_symbol for t in core.materials.top_level}
        image_symbols={}
        mg=MaterialGraphPlan((),(),(default_material_node,),{}, {},runtime_material_symbols,material_symbols_by_source,{
            'mode':'cp12a_boot_isolation','source_material_identities':len(core.materials.all_unique),
            'owned_materials':0,'external_materials':1,'owned_images':0,'external_images':0,
            'technique_nodes':0,'nested_iwd_pixels_emitted':0,'image_resource_policy':material_image_resource_policy.value,'declared_image_resource_bytes':0,'planned_embedded_image_resource_bytes':0,'planned_omitted_image_resource_bytes':0})
        findings.append(f'CP12-A boot isolation: {len(core.materials.all_unique):,} Material identities close to shared ,default; nested IWD Material/Image payload emission disabled.')
    else:
        top_image_semantics={r.name:r.semantic for r in core.image_catalog.records_by_asset_index.values() if r.name}
        mg=build_material_graph(
            core.materials,core.iwd_images,
            # The PS3 deliverable is FastFile-only.  Material-owned images remain inline-owned;
            # IWD members without a typed owner are promoted to explicit supplemental Image
            # XAssets below so runtime name lookups do not depend on an output IWD.
            include_orphan_iwds=True,
            extra_image_semantics=top_image_semantics,
            image_resource_policy=material_image_resource_policy,
            include_runtime_default_material=runtime_compatible,
            unresolved_image_policy=unresolved_image_policy,
            unresolved_material_policy=unresolved_material_policy)
        # Runtime visual fallbacks are not limited to XModel material handles.  Owned FX graphs
        # may contain packed Material visuals that cannot be proven against a top-level PC
        # XAssetHeader.  CP18 accidentally obtained the shared ,default node only because the
        # older XModel resolver still had unresolved slots.  Once CP19 closes every XModel slot,
        # that incidental node disappears and FX serialization fails.  Keep a dormant shared
        # default identity in every runtime-compatible registry; PortPlan serializes it only when
        # an actual FX/XModel ownership edge references it.
        if runtime_compatible and 'default' not in mg.material_symbol_by_name:
            default_symbol=sym('material.ext',PS3_DEFAULT_MATERIAL)
            default_node=ExternalMaterialNode(PS3_DEFAULT_MATERIAL,default_symbol,True)
            material_symbols=dict(mg.material_symbol_by_name);material_symbols['default']=default_symbol
            diag=dict(mg.diagnostics);diag['external_materials']=int(diag.get('external_materials',0))+1;diag['runtime_default_material_registered']=True
            mg=_dc_replace(mg,material_nodes=tuple(mg.material_nodes)+(default_node,),material_symbol_by_name=material_symbols,diagnostics=diag)
        # Full-fidelity path retained for regression/development. A top-level GfxImage that
        # physically owns its source resource is stronger evidence than the IWD mirror.
        image_nodes_by_symbol={n.symbol:n for n in mg.image_nodes}
        image_symbols=dict(mg.image_symbol_by_name)
        for ai,r in core.image_catalog.records_by_asset_index.items():
            if not r.name:continue
            k=norm(r.name).casefold();symbol=image_symbols.get(k)
            if symbol is None:
                symbol=sym('image',norm(r.name));image_symbols[k]=symbol
            if r.owns_resource:
                image_nodes_by_symbol[symbol]=ImageNode(image_asset_from_pc(r),symbol,material_image_resource_policy)
            elif symbol not in image_nodes_by_symbol:
                image_nodes_by_symbol[symbol]=ExternalImageNode(norm(r.name),symbol)
        for ai,name in core.image_catalog.exact_name_by_asset_index.items():
            k=norm(name).casefold();symbol=image_symbols.get(k)
            if symbol is None:
                symbol=sym('image',norm(name));image_symbols[k]=symbol;image_nodes_by_symbol[symbol]=ExternalImageNode(norm(name),symbol)
        mg=_dc_replace(mg,image_nodes=tuple(image_nodes_by_symbol.values()),image_symbol_by_name=image_symbols)
        material_image_names={norm(binding.image_name).casefold() for binding in core.materials.image_proof['bindings']}
        source_top_image_names={norm(name).casefold() for name in core.image_catalog.exact_name_by_asset_index.values()}
        orphan_iwd_names=sorted(
            norm(entry.image.name).casefold()
            for entry in core.iwd_images.values()
            if norm(entry.image.name).casefold() not in material_image_names
            and norm(entry.image.name).casefold() not in source_top_image_names
        )
        supplemental_top_level_symbols=tuple(mg.image_symbol_by_name[name] for name in orphan_iwd_names)
        nodes.extend(mg.technique_nodes);nodes.extend(mg.image_nodes);nodes.extend(mg.material_nodes)
        runtime_material_symbols=mg.material_symbol_by_name
        material_symbols_by_source=mg.material_symbol_by_source_asset_index
        material_by_root={m.root_offset:m for m in core.materials.all_unique}
        material_symbol_by_root={r:runtime_material_symbols[norm(m.name).casefold()] for r,m in material_by_root.items()}
        for binding in core.materials.image_proof['bindings']:
            owner_symbol=material_symbol_by_root.get(binding.material_root)
            child_symbol=mg.image_symbol_by_name.get(norm(binding.image_name).casefold())
            material=material_by_root.get(binding.material_root)
            if owner_symbol is None or child_symbol is None or material is None:continue
            ti=binding.texture_index;site=f'water[{ti}]' if ti in material.water_by_texture else f'texture[{ti}]'
            raw=serialized_image_pointer(material,ti)
            ownership_candidate(owner_symbol,child_symbol,site,raw,material.root_offset+ti*0x0c,f"Material '{material.name}' owns image '{binding.image_name}'")

    # TechniqueSets are top-level source assets but multiple PC identities normalize to the
    # same proven PS3 native/shared TechniqueSet. Emit the target only at its first source
    # occurrence; classify later source roots as MERGED instead of silently duplicating them.
    tech_symbol_by_source={}
    existing_tech={} if boot_isolation else {n.name.casefold():n for n in mg.technique_nodes}
    emitted_tech_symbols=set()
    for binding in sorted(core.technique_bindings,key=lambda b:b.source_asset_index):
        node=existing_tech.get(binding.candidate_name.casefold())
        if node is None:
            node=ExternalTechniqueSetNode(binding.candidate_name,sym('techset',binding.candidate_name),True)
            nodes.append(node);existing_tech[binding.candidate_name.casefold()]=node
        tech_symbol_by_source[binding.source_asset_index]=node.symbol
        if node.symbol in emitted_tech_symbols:
            disp.merge(binding.source_asset_index,node.symbol,AssetType.TECHSET,'duplicate source TechniqueSet merged to proven native/shared PS3 identity')
        else:
            disp.emit(binding.source_asset_index,node.symbol,AssetType.TECHSET,'first source occurrence of proven native/shared PS3 TechniqueSet identity')
            emitted_tech_symbols.add(node.symbol)
    if not boot_isolation:
        # Constant-compatible per-material replacements need not be the first
        # availability choice of any source TechniqueSet. Give those verified
        # native references an explicit global owner before ordering consumers.
        supplemental_top_level_symbols+=tuple(
            n.symbol for n in mg.technique_nodes if n.symbol not in emitted_tech_symbols)
    if boot_isolation:
        _td=dict(mg.diagnostics);_td['technique_nodes']=len(existing_tech)
        mg=_dc_replace(mg,technique_nodes=tuple(existing_tech.values()),
            technique_symbol_by_candidate={n.name.casefold():n.symbol for n in existing_tech.values()},diagnostics=_td)

    # Preserve source top-level Material cardinality. CP12-A deliberately maps that root to
    # ,default for loader isolation; full mode keeps the exact regenerated Material node.
    for t in core.materials.top_level:
        symbol=material_symbols_by_source[t.source_asset_index]
        disp.emit(t.source_asset_index,symbol,AssetType.MATERIAL,
            'boot-isolation shared ,default Material closure' if boot_isolation else 'exact top-level Material semantic identity')

    # mp_getaway declares no top-level Images. Keep the full-mode exact path for other maps,
    # but never synthesize/promote image roots in CP12-A.
    exact_images=core.image_catalog.exact_name_by_asset_index
    for a in (x for x in al.assets if x.type_id==PC['image']):
        if boot_isolation:
            raise ValueError(f'CP12-A source Image XAsset #{a.index} needs an explicit top-level PS3 image policy')
        name=exact_images.get(a.index)
        if name is None:
            raise ValueError(f'Image XAsset #{a.index} has no exact semantic identity; cannot claim full closure')
        symbol=mg.image_symbol_by_name.get(norm(name).casefold())
        if symbol is None:raise ValueError(f"Image XAsset #{a.index} identity '{name}' has no destination Image node")
        mode='independent inline GfxImage root' if a.index in core.image_catalog.records_by_asset_index else 'exact Material/Image alias evidence'
        disp.emit(a.index,symbol,AssetType.IMAGE,mode)

    # 2) PhysPreset.
    phys_symbols={p.source_asset_index:f'physpreset:{p.source_asset_index:04d}:{_safe(p.name)}' for p in core.phys_presets}
    for p in core.phys_presets:
        node=PhysPresetNode(p,phys_symbols[p.source_asset_index]);nodes.append(node);disp.emit(p.source_asset_index,node.symbol,AssetType.PHYSPRESET,'exact top-level PhysPreset')

    # 3) XModels.
    xmodel_symbols={x.model.source_asset_index:f'xmodel:{x.model.source_asset_index:04d}:{_safe(x.model.name)}' for x in core.xmodels}
    for x in core.xmodels:
        node=XModelNode(zone,x.model,runtime_material_symbols,xmodel_symbols[x.model.source_asset_index],phys_symbols,xmodel_symbols_by_asset_index=xmodel_symbols);nodes.append(node);disp.emit(x.model.source_asset_index,node.symbol,AssetType.XMODEL,'exact typed XModel graph')
        for i,(surface,raw) in enumerate(zip(x.model.surfaces,x.material_handle_pointers)):
            child=runtime_material_symbols[norm(surface.material_name).casefold()]
            ownership_candidate(node.symbol,child,f'material[{i}]',raw,x.material_handle_physical_offset+i*4,f"XModel '{x.model.name}' material slot {i}")

    # 4) FX + ImpactFX. Source-owned FX are emitted natively, name-only source FX remain explicit native/shared shells.
    fx_symbols={x.source_asset_index:f'fx:{x.source_asset_index:04d}:{_safe(x.name)}' for x in core.fx_references}
    fx_name_by_index={x.source_asset_index:x.name for x in core.fx_references}
    fx_runner_resolution=resolve_fx_runner_bindings(core.map_name,core.fx_references,zone=zone,technique_sets=core.techniques)
    fx_runner_target_by_raw=fx_runner_resolution.target_asset_indices_by_raw
    material_symbol_by_root={m.root_offset:runtime_material_symbols[norm(m.name).casefold()] for m in core.materials.fx_inline}
    fx_material_replay_raws=[]
    for x in core.fx_references:
        if not x.source_owned or x.graph is None:continue
        for e in x.graph.elements:
            if e.element_type>4:continue
            for v in e.visuals:
                if v.kind is not VisualKind.PACKED_UNRESOLVED:continue
                try:ai=xasset_header_alias_index(al,v.serialized_pointer,PC['material'])
                except ValueError:ai=None
                if ai is None:fx_material_replay_raws.append(v.serialized_pointer)
    fx_material_resolution=resolve_fx_material_bindings(
        zone,core.fx_references,fx_runner_resolution,core.techniques,fx_material_replay_raws,
    )
    fx_material_root_by_raw=fx_material_resolution.target_material_roots_by_raw
    fx_source_packed_visuals=sum(
        v.kind is VisualKind.PACKED_UNRESOLVED
        for ref in core.fx_references if ref.source_owned and ref.graph is not None
        for element in ref.graph.elements for v in element.visuals
    )
    fx_exact_packed_visuals=0;fx_exact_runner_references=0;fx_exact_material_references=0;fx_runtime_material_visuals=0;fx_runtime_null_visuals=0
    default_material_symbol=runtime_material_symbols.get('default')
    fx_visual_fallbacks=[]
    for x in core.fx_references:
        if x.source_owned:
            if x.graph is None:raise ValueError(f"owned FX '{x.name}' has no typed graph")
            packed_visual_symbols={};packed_visual_names={}
            for e in x.graph.elements:
                for v in e.visuals:
                    if v.kind.value!='packed_unresolved':continue
                    if e.element_type<=4:
                        try: ai=xasset_header_alias_index(al,v.serialized_pointer,PC['material'])
                        except ValueError: ai=None
                        if ai is not None:
                            symbol=material_symbols_by_source.get(ai)
                            if symbol is not None:packed_visual_symbols[v.serialized_pointer]=(AssetType.MATERIAL,symbol);fx_exact_packed_visuals+=1;continue
                        material_root=fx_material_root_by_raw.get(v.serialized_pointer)
                        if material_root is not None:
                            symbol=material_symbol_by_root.get(material_root)
                            if symbol is None:
                                raise ValueError(
                                    f"FX '{x.name}' exact replay Material root 0x{material_root:X} has no destination symbol"
                                )
                            packed_visual_symbols[v.serialized_pointer]=(AssetType.MATERIAL,symbol)
                            fx_exact_packed_visuals+=1;fx_exact_material_references+=1;continue
                        if runtime_compatible:fx_runtime_material_visuals+=1
                    elif e.element_type==5:
                        try: ai=xasset_header_alias_index(al,v.serialized_pointer,PC['xmodel'])
                        except ValueError: ai=None
                        if ai is not None and ai in xmodel_symbols:
                            packed_visual_symbols[v.serialized_pointer]=(AssetType.XMODEL,xmodel_symbols[ai]);fx_exact_packed_visuals+=1;continue
                    elif e.element_type==10:
                        target_ai=fx_runner_target_by_raw.get(v.serialized_pointer)
                        target_symbol=fx_symbols.get(target_ai) if target_ai is not None else None
                        if target_symbol is None:
                            raise ValueError(f"FX '{x.name}' type-10 runner 0x{v.serialized_pointer:08X} has no exact FxEffectDef target")
                        packed_visual_symbols[v.serialized_pointer]=(AssetType.FX,target_symbol)
                        packed_visual_names[v.serialized_pointer]=fx_name_by_index[target_ai]
                        fx_exact_packed_visuals+=1;fx_exact_runner_references+=1
                    elif e.element_type==8 and runtime_compatible:
                        fx_runtime_null_visuals+=1
            node=OwnedFxNode(x.graph,material_symbol_by_root,fx_symbols[x.source_asset_index],
                packed_visual_symbols_by_raw=packed_visual_symbols,
                packed_visual_names_by_raw=packed_visual_names,
                effect_names_by_raw={raw:fx_name_by_index[ai] for raw,ai in fx_runner_target_by_raw.items()},
                runtime_fallback_material_symbol=default_material_symbol,
                allow_runtime_visual_fallback=runtime_compatible)
            for e in x.graph.elements:
                for vi,v in enumerate(e.visuals):
                    if v.kind is VisualKind.PACKED_UNRESOLVED and v.serialized_pointer not in packed_visual_symbols:
                        fx_visual_fallbacks.append(dict(effect=x.name,element=e.index,visual=vi,
                            element_type=e.element_type,source_pointer=f'0x{v.serialized_pointer:08X}',
                            replacement='default_material' if e.element_type<=4 else 'null',
                            target_symbol=default_material_symbol if e.element_type<=4 else None))
            reason='native owned FxElemDef graph'
            if runtime_compatible and any(v.kind.value=='packed_unresolved' for e in x.graph.elements for v in e.visuals):reason+=' with explicitly reported runtime packed-visual safety closure'
            for e in x.graph.elements:
                for vi,v in enumerate(e.visuals):
                    child=None
                    if v.kind.value=='material':child=material_symbol_by_root.get(v.material_root)
                    elif v.kind.value=='packed_unresolved' and e.element_type!=10:   # runner refs are name strings, never owned children
                        exact=packed_visual_symbols.get(v.serialized_pointer)
                        if exact is not None:child=exact[1]
                        elif runtime_compatible and e.element_type<=4:child=default_material_symbol
                    if child is not None:
                        ownership_candidate(node.symbol,child,f'element[{e.index}].visual[{vi}]',v.serialized_pointer,x.graph.root_offset+e.index*0xFC+vi*4,f"FX '{x.name}' visual {e.index}:{vi}")
        else:
            node=ExternalFxNode(x.name,fx_symbols[x.source_asset_index]);reason='name-only/shared FX reference preserved as native PS3 shell'
        nodes.append(node);disp.emit(x.source_asset_index,node.symbol,AssetType.FX,reason)
    impacts=tuple(parse_impact_fx(zone,al))
    for x in impacts:
        node=ImpactFxNode(x,fx_symbols,f'impactfx:{x.source_asset_index:04d}:{_safe(x.name)}');nodes.append(node);disp.emit(x.source_asset_index,node.symbol,AssetType.IMPACTFX,'exact 396-entry ImpactFx graph')

    # 5) ComWorld boundary + LightDefs using exact Image identities.  In IW3 MP zones the
    # ComWorld semantic name is the BSP path, not GfxWorld.baseName.  Replaying it first gives the
    # exact physical end of source asset #409, which is the source-order root of LightDef #410.
    com_assets=[a for a in al.assets if a.type_id==PC['comworld']]
    if len(com_assets)!=1:raise ValueError(f'expected one ComWorld XAsset, got {len(com_assets)}')
    com_expected=f'maps/mp/{core.map_name}.d3dbsp'
    com=parse_comworld(zone,com_expected,core.gfxworld.primary_light_count)
    packed_light_name_evidence={}
    packed_def_raw={l.def_name_pointer for l in com.primary_lights if decode_pc_pointer(l.def_name_pointer).kind=='packed'}
    inline_def_names={l.def_name for l in com.primary_lights if l.def_name}
    if len(packed_def_raw)==1 and len(inline_def_names)==1:
        # Runtime/semantic reuse evidence: one first-owned inline light definition name followed by
        # one repeated packed identity for the remaining lights.  The top-level LightDef name uses
        # that same raw packed identity in mp_getaway.
        packed_light_name_evidence[next(iter(packed_def_raw))]=next(iter(inline_def_names))
    lights=top_level_lightdefs(zone,al,exact_images,root_hints=(com.physical_end_offset,),packed_name_evidence=packed_light_name_evidence,runtime_compatible=runtime_compatible);light_symbols={x.source_asset_index:f'lightdef:{x.source_asset_index:04d}:{_safe(x.name)}' for x in lights}
    for x in lights:
        image_symbol=None
        inline_external_image_name=None
        if x.attenuation_image_name is not None:
            key=norm(x.attenuation_image_name).casefold()
            if (
                x.attenuation_image_source_asset_index is None
                and x.attenuation_pointer_kind in ('following','insert')
                and x.attenuation_image_has_resource is False
            ):
                # The source owns a resource-less support GfxImage shell.  This is still a real
                # loader object, not a nullable visual fallback.  Shipment proves the PS3 form is
                # a zeroed 0x34-byte shell followed by its comma-prefixed XString.
                inline_external_image_name=norm(x.attenuation_image_name)
                findings.append(f"LightDef '{x.name}' preserves inline external attenuation image ',{inline_external_image_name}'")
            elif boot_isolation:
                raise ValueError(f"boot isolation cannot discard resource-bearing LightDef attenuation image '{x.attenuation_image_name}'")
            else:
                image_symbol=mg.image_symbol_by_name.get(key)
            if not boot_isolation and image_symbol is None and inline_external_image_name is None:
                # LightDef may own/reference a support image that no Material uses. Replay the exact
                # nested source GfxImage and add it to the graph instead of requiring an unrelated
                # Material to have introduced the identity first.
                nr=decode_pc_pointer(int.from_bytes(zone[x.root_offset:x.root_offset+4],'little'))
                cursor=x.root_offset+0x10
                if nr.kind in ('following','insert'):
                    end=zone.find(b'\0',cursor,min(len(zone),cursor+513))
                    if end<0:raise ValueError(f"LightDef '{x.name}' name is unterminated")
                    cursor=end+1
                rec=parse_image_at(zone,cursor)
                if norm(rec.name).casefold()!=key:raise ValueError(f"LightDef '{x.name}' nested image identity mismatch: {rec.name!r} vs {x.attenuation_image_name!r}")
                image_symbol=sym('image',norm(rec.name))
                inode=ImageNode(image_asset_from_pc(rec),image_symbol,material_image_resource_policy) if rec.owns_resource else ExternalImageNode(norm(rec.name),image_symbol)
                nodes.append(inode)
                enriched=dict(mg.image_symbol_by_name);enriched[key]=image_symbol
                mg=_dc_replace(mg,image_nodes=mg.image_nodes+(inode,),image_symbol_by_name=enriched)
        node=LightDefNode(
            x.name,x.sampler,x.lmap_lookup_start,image_symbol,light_symbols[x.source_asset_index],
            attenuation_external_image_name=inline_external_image_name,
            attenuation_external_pointer_kind=x.attenuation_pointer_kind if inline_external_image_name else 'insert',
        );nodes.append(node);disp.emit(x.source_asset_index,node.symbol,AssetType.LIGHTDEF,'exact typed LightDef with source-order nested attenuation image')
        if image_symbol is not None:
            raw=int.from_bytes(zone[x.root_offset+4:x.root_offset+8],'little')
            ownership_candidate(node.symbol,image_symbol,'attenuation',raw,x.root_offset+4,f"LightDef '{x.name}' attenuation image")

    # 6) World corridor.
    gfx_assets=[a for a in al.assets if a.type_id==PC['gfxworld']];game_assets=[a for a in al.assets if a.type_id==PC['gameworld_mp']]
    clip_sp_assets=[a for a in al.assets if a.type_id==PC['clipmap']];clip_mp_assets=[a for a in al.assets if a.type_id==PC['clipmap_pvs']]
    # IW3 PC multiplayer custom maps normally declare CLIPMAP_PVS as the top-level collision asset;
    # it is the source counterpart of PS3 col_map_mp. Some toolchains expose a CLIPMAP root instead.
    # Exactly one of the two families must own the graph; never emit both.
    if len(com_assets)!=1 or len(gfx_assets)!=1 or len(game_assets)!=1 or len(clip_sp_assets)+len(clip_mp_assets)!=1:
        raise ValueError(f'expected one MP world corridor: com={len(com_assets)} gfx={len(gfx_assets)} game={len(game_assets)} clip={len(clip_sp_assets)} clip_pvs={len(clip_mp_assets)}')
    clip_source_asset=(clip_mp_assets or clip_sp_assets)[0]
    com_sym='comworld:'+_safe(core.map_name);gfx_sym='gfxworld:'+_safe(core.map_name);game_sym='gameworldmp:'+_safe(core.map_name);clip_sym='clipmap:'+_safe(core.map_name)
    com_node=ComWorldNode(com,com_sym);nodes.append(com_node);disp.emit(com_assets[0].index,com_sym,AssetType.COMWORLD,'exact ComWorld graph')

    # Exact packed Material aliases recovered by XModel closure can be reused by GfxWorld.
    mat_raw={}
    aliases=core.materials.xmodel_material_resolution.get('aliases',{})
    for off,value in aliases.items():
        name=value[0] if isinstance(value,(tuple,list)) else value
        symbol=runtime_material_symbols.get(norm(name).casefold())
        if symbol is None:continue
        # GfxWorldNode keys direct map by raw word. Reconstruct PC packed word for block4 target.
        raw=((4<<28)|int(off))+1;mat_raw[raw&0xffffffff]=symbol

    # Exact ClipMap XAssetHeader refs also serve as proven packed XModel/Phys/FX raw mappings.
    xmodel_raw={}
    # Shared XModel shells can be owned by world draw pointers, outside XAssetList.
    # Preserve their names and recover only unique backward aliases to draw cells.
    gfx_inline_draw_symbols={}
    if core.gfxworld.full.inline_draw_xmodels:
        from .gfxworld_xmodels import resolve_draw_aliases, draw_array_base, PC_DRAW
        dp=next(b for b in core.gfxworld.full.branches if b.name=='DPVS serialized graph')
        draw_start=dp.start+(core.gfxworld.static_surface_count+core.gfxworld.static_surface_count_no_decal)*2+core.gfxworld.static_model_count*0x1c+core.gfxworld.surface_count*0x30+core.gfxworld.cull_group_count*0x20
        raw_draws=[int.from_bytes(zone[draw_start+i*PC_DRAW+0x38:draw_start+i*PC_DRAW+0x3c],'little') for i in range(core.gfxworld.static_model_count)]
        shared_by_name={}
        for index,name,source_root,source_end in core.gfxworld.full.inline_draw_xmodels:
            child=shared_by_name.get(name.casefold())
            if child is None:
                child=sym('xmodel.shared',name)
                nodes.append(ExternalXModelNode(name,child));shared_by_name[name.casefold()]=child
            gfx_inline_draw_symbols[index]=child
            ownership_candidate(gfx_sym,child,f'draw[{index}].xmodel',raw_draws[index],source_root,f'GfxWorld draw[{index}] shared XModel {name}')
        known=dict(xmodel_raw)
        for raw in raw_draws:
            if decode_pc_pointer(raw).kind!='packed':continue
            ai=xasset_header_alias_index(al,raw,PC['xmodel'])
            if ai in xmodel_symbols:known[raw]=xmodel_symbols[ai]
        draw_base=draw_array_base(zone,core.gfxworld,core.materials.image_proof['gfxworld_block4_owner_replay'])
        aliases,draw_base=resolve_draw_aliases(raw_draws,gfx_inline_draw_symbols,known,array_base=draw_base)
        xmodel_raw=dict(xmodel_raw);xmodel_raw.update(aliases)
        findings.append(f'GfxWorld retains {len(gfx_inline_draw_symbols)} shared XModel shells and {len(aliases)} exact draw-cell aliases; source draw base={draw_base}.')

    xraw=[m.xmodel_raw for m in core.clipmap.static_models]
    praw=[];fraw=[];pieces=[]
    for e in core.clipmap.dyn_models+core.clipmap.dyn_brushes:
        xraw.append(e.xmodel_raw);praw.append(e.phys_preset_raw);fraw.append(e.destroy_fx_raw);pieces.append(e.destroy_pieces_raw)
    xmodel_raw.update(_clip_raw_map(core,PC['xmodel'],xmodel_symbols,[raw for raw in xraw if raw not in xmodel_raw],'ClipMap XModel'))
    praw,phys_raw,xmodel_phys_alias_raw,dynent_phys_alias_raw=_resolve_clipmap_physpreset_symbols(
        core,clip_sym,phys_symbols,xmodel_symbols,
    )
    fx_raw=_clip_raw_map(core,PC['fx'],fx_symbols,fraw,'ClipMap FX') if any(fraw) else {}
    if any(pieces):raise ValueError('ClipMap XModelPieces references are present but Python XModelPieces writer is not implemented')

    # Bind GfxWorld-owned inline sunSprite/sunFlare Materials to their regenerated typed
    # destination Material XAssets.  The source pointer word is only an ownership marker
    # (FOLLOWING/INSERT), so identity comes from source-order replay of inline Material roots.
    gfx_inline_field_symbols={}
    gfx_inline_roots=list(core.gfxworld.full.inline_material_roots)
    mm_branch=next(b for b in core.gfxworld.full.branches if b.name=='brush-model/material-memory roots')
    mm_start=mm_branch.start+core.gfxworld.model_count*0x38
    inline_index=0
    gfx_inline_by_root={m.root_offset:m for m in core.materials.gfxworld_inline}
    gfx_inline_matmem_symbols={}
    for i in range(core.gfxworld.material_memory_count):
        raw=int.from_bytes(zone[mm_start+i*8:mm_start+i*8+4],'little')
        if decode_pc_pointer(raw).kind not in ('following','insert'): continue
        if inline_index>=len(gfx_inline_roots): raise ValueError(f'GfxWorld materialMemory[{i}] inline root sequence exhausted')
        mat_root=gfx_inline_roots[inline_index]; inline_index+=1
        mat=gfx_inline_by_root.get(mat_root)
        if mat is None: raise ValueError(f'GfxWorld materialMemory[{i}] root 0x{mat_root:X} missing from Material universe')
        msym=runtime_material_symbols.get(norm(mat.name).casefold())
        if msym is None: raise ValueError(f"GfxWorld materialMemory[{i}] Material '{mat.name}' has no destination symbol")
        gfx_inline_matmem_symbols[i]=msym
    for src_rel in (0x180,0x184):
        raw=int.from_bytes(zone[core.gfxworld.root_offset+src_rel:core.gfxworld.root_offset+src_rel+4],'little')
        if decode_pc_pointer(raw).kind not in ('following','insert'): continue
        if inline_index>=len(gfx_inline_roots): raise ValueError(f'GfxWorld inline Material root sequence exhausted at +0x{src_rel:X}')
        mat_root=gfx_inline_roots[inline_index]; inline_index+=1
        mat=gfx_inline_by_root.get(mat_root)
        if mat is None: raise ValueError(f'GfxWorld inline Material root 0x{mat_root:X} missing from Material universe')
        msym=runtime_material_symbols.get(norm(mat.name).casefold())
        if msym is None: raise ValueError(f"GfxWorld inline Material '{mat.name}' has no destination symbol")
        gfx_inline_field_symbols[src_rel]=msym
    if inline_index!=len(gfx_inline_roots):
        raise ValueError(f'GfxWorld inline Material replay mismatch: consumed={inline_index} parsed={len(gfx_inline_roots)}')

    # Every mp_getaway DPVS surface material pointer targets the Material* cell of the
    # GfxWorld materialMemory array.  Prove this from the source: exactly one unique block-4
    # target per materialMemory entry, in a contiguous 8-byte stride.  This gives a 161/161
    # identity mapping without semantic/name guessing.
    surface_targets=sorted({decode_pc_pointer(raw).offset for raw in core.gfxworld.full.surface_material_pointers
                            if decode_pc_pointer(raw).kind=='packed' and decode_pc_pointer(raw).block==4})
    if surface_targets:
        if len(surface_targets)!=core.gfxworld.material_memory_count:
            raise ValueError(f'GfxWorld packed surface Material target cardinality {len(surface_targets)} != materialMemory {core.gfxworld.material_memory_count}')
        if any(b-a!=8 for a,b in zip(surface_targets,surface_targets[1:])):
            raise ValueError('GfxWorld packed surface Material targets are not a contiguous 8-byte materialMemory pointer table')
        if len(gfx_inline_matmem_symbols)!=core.gfxworld.material_memory_count:
            raise ValueError('GfxWorld materialMemory target table contains non-inline entries without exact destination identity')
        for i,target in enumerate(surface_targets):
            mat_raw[(((4<<28)|int(target))+1)&0xffffffff]=gfx_inline_matmem_symbols[i]

    gfx_node=GfxWorldNode(zone,core.gfxworld,gfx_sym,material_symbols_by_raw=mat_raw,xmodel_symbols_by_raw=xmodel_raw,lightdef_symbols_by_raw={},pc_asset_list=al,material_symbols_by_asset_index=material_symbols_by_source,xmodel_symbols_by_asset_index=xmodel_symbols,lightdef_symbols_by_asset_index=light_symbols,inline_draw_xmodel_symbols=gfx_inline_draw_symbols,inline_material_symbols_by_root_field=gfx_inline_field_symbols,inline_material_symbols_by_matmem_index=gfx_inline_matmem_symbols,runtime_default_material_symbol=default_material_symbol if boot_isolation else None,image_resource_policy=gfxworld_resource_policy,primary_light_policy=primary_light_policy,vertex_layer_policy=vertex_layer_policy,portal_policy=portal_policy,shared_map_name_symbol=nested_symbol(com_sym,'name'))
    nodes.append(gfx_node);disp.emit(gfx_assets[0].index,gfx_sym,AssetType.GFXWORLD,'exact full GfxWorld graph')
    for i in range(core.gfxworld.material_memory_count):
        raw=int.from_bytes(zone[mm_start+i*8:mm_start+i*8+4],'little');pk=decode_pc_pointer(raw).kind
        child=gfx_inline_matmem_symbols.get(i) if pk in ('following','insert') else mat_raw.get(raw)
        if child is not None:ownership_candidate(gfx_sym,child,f'materialMemory[{i}]',raw,mm_start+i*8,f'GfxWorld materialMemory[{i}]')
    for src_rel,site in ((0x180,'sunSprite'),(0x184,'sunFlare')):
        raw=int.from_bytes(zone[core.gfxworld.root_offset+src_rel:core.gfxworld.root_offset+src_rel+4],'little');pk=decode_pc_pointer(raw).kind
        child=gfx_inline_field_symbols.get(src_rel) if pk in ('following','insert') else mat_raw.get(raw)
        if child is not None:ownership_candidate(gfx_sym,child,site,raw,core.gfxworld.root_offset+src_rel,f'GfxWorld {site}')
    # Retail PS3 gives GfxWorld, ClipMap, MapEnts and GameWorldMp one shared block-4
    # XString, and it is the ComWorld's inline 'maps/mp/<map>.d3dbsp'.  CM_LoadMap resolves
    # the ClipMap by that exact path, so the alias must target the ComWorld name, not
    # GfxWorld.baseName.  GfxWorld keeps its own short baseName inline at +0x04.
    shared_world_name=nested_symbol(com_sym,'name')
    shared_world_planes=nested_symbol(gfx_sym,'planes')
    game_node=GameWorldMpNode(core.gfxworld.base_name,shared_world_name,game_sym,gfx_sym);nodes.append(game_node);disp.emit(game_assets[0].index,game_sym,AssetType.GAMEWORLD_MP,'exact GameWorldMP shared GfxWorld-name relocation')
    clip_node=ClipMapNode(
        core.clipmap,
        clip_sym,
        xmodel_symbols_by_raw=xmodel_raw,
        physpreset_symbols_by_raw=phys_raw,
        xmodel_physpreset_alias_symbols_by_raw=xmodel_phys_alias_raw,
        xmodel_physpreset_owner_symbols_by_raw={raw:alias.rsplit('::',1)[0] for raw,alias in xmodel_phys_alias_raw.items()},
        dynent_physpreset_alias_symbols_by_raw=dynent_phys_alias_raw,
        fx_symbols_by_raw=fx_raw,
        xmodelpieces_symbols_by_raw={},
        shared_name_symbol=shared_world_name,
        shared_planes_symbol=shared_world_planes,
        shared_world_symbol=gfx_sym,
    );nodes.append(clip_node);disp.emit(clip_source_asset.index,clip_sym,AssetType.CLIPMAP_MP,'exact ClipMap/PVS/BSP graph with shared GfxWorld name/planes and closed PhysPreset aliases')

    # MapEnts is physically owned by the regenerated ClipMap graph. If a toolchain declares both
    # ClipMap and ClipMapPVS the exact-one gate above refuses the ambiguous duplicate ownership.
    for a in (x for x in al.assets if x.type_id==PC['map_ents']):disp.merge(a.index,clip_sym,AssetType.CLIPMAP_MP,'MapEnts merged into exact ClipMap nested map-ents payload')

    # 7) StringTables + RawFiles.  Recover the table first because source XAsset order gives a
    # typed physical boundary for the immediately following RawFile run.  This replaces the old
    # whole-zone byte scanner with exact neighbouring-XAsset replay.
    global_xstrings=dict(core.sound_catalog.packed_string_evidence)
    tables=top_level_stringtables(zone,al,global_xstrings,runtime_compatible=runtime_compatible)
    table_xstring_evidence=packed_xstring_evidence_from_tables(tables)
    for raw,value in table_xstring_evidence.items():
        old=global_xstrings.get(raw)
        if old is not None and old!=value:
            raise ValueError(f'packed XString 0x{raw:08X} conflicts between Sound and StringTable evidence')
        global_xstrings[raw]=value
    destination_table_xstring_symbols={}
    for t in tables:
        node=StringTableNode(t.name,t.columns,t.rows,t.values,f'stringtable:{t.source_asset_index:04d}:{_short(t.name)}')
        for cell,(raw,value) in enumerate(zip(t.serialized_pointers,t.values)):
            if value is None or raw not in table_xstring_evidence or decode_pc_pointer(raw).kind!='packed':continue
            old=destination_table_xstring_symbols.get(raw)
            symbol=node.cell_symbol(cell)
            if old is None:destination_table_xstring_symbols[raw]=symbol
        nodes.append(node);disp.emit(t.source_asset_index,node.symbol,AssetType.STRINGTABLE,'exact row-major StringTable including packed-XString evidence')

    raw_assets=sorted((a for a in al.assets if a.type_id==PC['rawfile']),key=lambda a:a.index)
    raw_hints={}
    table_by_index={t.source_asset_index:t for t in tables}
    xmodel_by_index={x.model.source_asset_index:x for x in core.xmodels}
    fx_by_index={x.source_asset_index:x for x in core.fx_references}
    def _material_end(rec):
        end=getattr(rec,'end_offset',None)
        if end is not None:return int(end)
        root=getattr(rec,'root_offset',None)
        if root is None:return None
        try:
            from .pc_material import parse_material_at as _pma
            return int(_pma(zone,int(root))[1])
        except Exception:
            return None
    material_end_by_index={}
    for t in core.materials.top_level:
        end=_material_end(t.material)
        if end is not None:material_end_by_index[t.source_asset_index]=end
    i=0
    while i<len(raw_assets):
        j=i+1
        while j<len(raw_assets) and raw_assets[j].index==raw_assets[j-1].index+1:j+=1
        group=raw_assets[i:j];first=group[0].index;last=group[-1].index;prev=first-1;next_index=last+1
        prev_asset=al.assets[prev] if 0<=prev<len(al.assets) else None
        if prev in table_by_index:
            raw_hints[first]=table_by_index[prev].physical_end
        elif prev in xmodel_by_index and xmodel_by_index[prev].physical_end is not None:
            raw_hints[first]=int(xmodel_by_index[prev].physical_end)
        elif prev in fx_by_index:
            raw_hints[first]=int(fx_by_index[prev].physical_end_offset)
        elif prev in material_end_by_index:
            # mp_shipment's last RawFile sits behind a top-level Material; the Material
            # parser already knows where that record ends.
            raw_hints[first]=int(material_end_by_index[prev])
        elif prev_asset is not None and prev_asset.type_id==PC['clipmap_pvs']:
            # Both stock maps put their first RawFile run directly behind the ClipMap, which
            # Getaway never did (a StringTable sits there instead).  The ClipMap parser
            # already reports the exact physical end of its own stream, so that is the
            # boundary - no byte scanning required.
            raw_hints[first]=int(core.clipmap.known_end)
        else:
            raise ValueError(f'RawFile source-order run #{first}-#{last} has no typed preceding physical boundary')
        if next_index>=len(al.assets):
            raw_hints[next_index]=len(zone)
        else:
            na=al.assets[next_index]
            if na.type_id==PC['sound'] and sound_policy!='omit':
                raw_hints[next_index]=core.sound_catalog.stream_start
            elif na.type_id==PC['xmodel']:
                nx=xmodel_by_index.get(next_index)
                if nx is not None:raw_hints[next_index]=nx.root_offset
        i=j
    raws=top_level_rawfiles(zone,al,root_hints_by_asset_index=raw_hints,packed_name_evidence=global_xstrings,runtime_compatible=runtime_compatible)
    raw_runtime_name_fallbacks=0
    for r in raws:
        shared_name_symbol=destination_table_xstring_symbols.get(r.name_pointer) if decode_pc_pointer(r.name_pointer).kind=='packed' else None
        shared_name_owner=shared_name_symbol.rsplit('::cell[',1)[0] if shared_name_symbol else None
        node=RawFileNode(r.name,r.payload,f'rawfile:{r.source_asset_index:04d}:{_short(r.name)}',shared_name_symbol,shared_name_owner);nodes.append(node)
        runtime_name=bool(getattr(r,'name_runtime_fallback',False));raw_runtime_name_fallbacks+=int(runtime_name)
        shared=' with preserved StringTable B4 XString alias' if shared_name_symbol else ''
        disp.emit(r.source_asset_index,node.symbol,AssetType.RAWFILE,'exact source RawFile payload'+shared+(' with runtime internal packed-name fallback' if runtime_name else ''))

    # 8) Sound. The default generic-map policy intentionally emits no map-specific audio.
    # PC Sound/SndCurve/LoadedSound roots are source-container inputs only and never appear in
    # the fresh PS3 map zone.  The opt-in legacy policy remains for regression work.
    staged={}
    sb=resolve_sound_bindings(core.sound_catalog,staged,ffmpeg=ffmpeg,preserve_all_lists=False)
    if sound_policy=='omit':
        for a in (x for x in al.assets if x.type_id in (PC['sound'],PC['sndcurve'],PC['loaded_sound'])):
            disp.ext(a.index,'sound policy omit: map-specific Sound/SndCurve/LoadedSound is not serialized')
        sound_symbol_by_source={}
    else:
        staged=stage_iwd_sounds(core.iwd_sounds,ffmpeg) if core.iwd_sounds else {}
        sb=resolve_sound_bindings(core.sound_catalog,staged,ffmpeg=ffmpeg,preserve_all_lists=False)
        if not sb.passed:raise ValueError(f'Sound binding incomplete: unresolved={sb.unresolved_owned[:8]} unreferenced={sb.unreferenced_staged[:8]}')
        sound_symbol_by_source={}
        for p in sb.lists:
            symbol=f'sound:{p.source.source_asset_index:04d}:{_safe(p.source.name)}';sound_symbol_by_source[p.source.source_asset_index]=symbol
            if p.owned_by_map:
                node=SoundNode(p,symbol,allow_null_source_soundfiles=False);nodes.append(node);disp.emit(p.source.source_asset_index,symbol,AssetType.SOUND,'exact map-owned SoundAliasList with embedded PS3 LoadedSound payloads')
            else:
                disp.ext(p.source.source_asset_index,'stock SoundAliasList remains PS3 support/common-zone native')

    # SndCurve top-level XAssets are usually semantic children of SoundAliasList nodes. The PC
    # parser maps their packed top-level headers directly to the typed TEMP-XAsset alias cell. An
    # unused inline curve is still an owned XAsset and receives its own standalone PS3 node.
    curve_owner={}
    for p in sb.owned_lists:
        for ap in p.aliases:
            cv=ap.source.volume_curve
            if cv is not None and cv.source_asset_index>=0:
                old=curve_owner.get(cv.source_asset_index)
                if old is not None and old!=sound_symbol_by_source[p.source.source_asset_index]:raise ValueError(f'SndCurve XAsset #{cv.source_asset_index} used by multiple incompatible Sound owners')
                curve_owner[cv.source_asset_index]=sound_symbol_by_source[p.source.source_asset_index]
    curve_by_source={x.source_asset_index:x for x in core.sound_catalog.top_level_curves}
    for a in (() if sound_policy=='omit' else (x for x in al.assets if x.type_id==PC['sndcurve'])):
        owner=curve_owner.get(a.index)
        if owner is not None:
            disp.merge(a.index,owner,AssetType.SOUND,'SndCurve serialized as typed TEMP-XAsset child of SoundAliasList');continue
        cv=curve_by_source.get(a.index)
        if cv is None:raise ValueError(f'SndCurve XAsset #{a.index} has no exact parsed semantic object')
        if decode_pc_pointer(a.serialized_pointer).kind=='packed':
            disp.ext(a.index,'packed SndCurve belongs only to a source-native/shared Sound graph')
        else:
            symbol=f'sndcurve:{a.index:04d}:{_safe(cv.name or "unnamed")}';node=SoundCurveAssetNode(cv,symbol);nodes.append(node);disp.emit(a.index,symbol,AssetType.SNDCURVE,'owned standalone SndCurve')

    # LoadedSound top-level headers follow the same TEMP-XAsset rule. Map every packed header to the
    # exact nested object and merge it with its owned Sound list. Only an actually standalone owned
    # LoadedSound needs a separate destination XAsset.
    loaded_owner={}
    for p in sb.owned_lists:
        for ap in p.aliases:
            sf=ap.source.sound_file
            ls=sf.loaded_sound if sf is not None else None
            if ls is not None and ls.source_asset_index>=0:
                owner=sound_symbol_by_source[p.source.source_asset_index];old=loaded_owner.get(ls.source_asset_index)
                if old is not None and old!=owner:raise ValueError(f'LoadedSound XAsset #{ls.source_asset_index} used by multiple incompatible owned Sound lists')
                loaded_owner[ls.source_asset_index]=owner
    loaded_by_source={x.source_asset_index:x for x in core.sound_catalog.top_level_loaded_sounds}
    for a in (() if sound_policy=='omit' else (x for x in al.assets if x.type_id==PC['loaded_sound'])):
        owner=loaded_owner.get(a.index)
        if owner is not None:
            disp.merge(a.index,owner,AssetType.SOUND,'LoadedSound serialized as typed TEMP-XAsset child of SoundAliasList');continue
        ls=loaded_by_source.get(a.index)
        if ls is None:raise ValueError(f'LoadedSound XAsset #{a.index} has no exact parsed semantic object')
        if decode_pc_pointer(a.serialized_pointer).kind=='packed':
            disp.ext(a.index,'packed LoadedSound belongs only to a source-native/shared Sound graph');continue
        staged_ls=stage_standalone_loaded_sound(ls,ffmpeg)
        lp=StandaloneLoadedPlan(ls,ls.name or staged_ls.asset_name,staged_ls.payload,staged_ls.format,staged_ls.sample_count,staged_ls.channels,staged_ls.sample_rate,staged_ls.duration_ms)
        symbol=f'loadedsound:{a.index:04d}:{_safe(lp.name or "unnamed")}';node=LoadedSoundAssetNode(lp,symbol);nodes.append(node);disp.emit(a.index,symbol,AssetType.LOADED_SOUND,'owned standalone LoadedSound converted to PS3 ID3/MP3')

    # 9) Explicit unsupported/source-native families. Canonical full port refuses to guess custom gameplay/UI assets.
    allowed_external={PC['xanimparts']:'XAnimParts are support/native assets only when explicitly shared',PC['ui_map']:'UI map support asset',PC['font']:'font support asset',PC['menulist']:'menu list support asset',PC['menu']:'menu support asset',PC['localize']:'localize support asset',PC['weapon']:'weapon support asset',PC['snddriverglobals']:'sound driver globals support asset',PC['aitype']:'AI type support asset',PC['mptype']:'MP type support asset',PC['character']:'character support asset',PC['xmodelalias']:'XModelAlias support asset'}
    for typ,reason in allowed_external.items():
        for a in (x for x in al.assets if x.type_id==typ):
            # A source-owned inline object in these unsupported families cannot be called native/shared without evidence.
            if decode_pc_pointer(a.serialized_pointer).kind in ('following','insert'):
                raise ValueError(f'unsupported owned source XAsset #{a.index} type={a.type_name}; Python native writer not implemented')
            disp.ext(a.index,reason+'; source header is packed/shared')
    for a in (x for x in al.assets if x.type_id in (PC['xmodelpieces'],PC['gameworld_sp'])):
        raise ValueError(f'unsupported source family present: XAsset #{a.index} {a.type_name}')

    dispositions=disp.finish()
    top_symbols={d.target_symbol for d in dispositions if d.kind==DispositionKind.EMITTED and d.target_symbol}
    top_symbols.update(supplemental_top_level_symbols)
    # Resolve ownership only inside the dependency closure that will actually be serialized.
    # A deduplicated Material/Image identity can occur first in a dormant source branch; claiming
    # it there would discard the later live owner when ``active_ownership_edges`` filters the
    # dormant edge, leaving a live support node ownerless.  The active closure itself is independent
    # of ownership placement, so compute it once with an empty placement plan.
    provisional=PortPlan(
        tuple(nodes),_script_strings(core),dispositions,core.map_name,findings,(),
        supplemental_top_level_symbols,
    )
    active_symbols={a.symbol for a in provisional.serialized_assets()}
    ownership=[];claimed=set()
    for _priority,_source_order,_seq,edge in sorted(ownership_candidates,key=lambda row:row[:3]):
        if edge.owner_symbol not in active_symbols or edge.child_symbol not in active_symbols:continue
        if edge.child_symbol in top_symbols or edge.child_symbol in claimed:continue
        ownership.append(edge);claimed.add(edge.child_symbol)
    plan=PortPlan(
        tuple(nodes),_script_strings(core),dispositions,core.map_name,findings,tuple(ownership),
        supplemental_top_level_symbols,
    )
    val=plan.validate(al)
    owned_image_nodes=[node for node in nodes if isinstance(node,ImageNode)]
    image_declared_bytes=sum(len(node.image.resource) for node in owned_image_nodes)
    image_embedded_bytes=sum(len(node.image.resource) for node in owned_image_nodes if node.resource_policy is ImageResourcePolicy.SELF_CONTAINED)
    image_deferred_bytes=sum(len(node.image.resource) for node in owned_image_nodes if node.resource_policy is ImageResourcePolicy.FASTFILE_DELAYED)
    image_omitted_bytes=sum(len(node.image.resource) for node in owned_image_nodes if node.resource_policy is ImageResourcePolicy.RETAIL_DELAYED)
    material_image_resource_stats={
        'policy':material_image_resource_policy.value,'owned_images':len(owned_image_nodes),
        'declared_resource_bytes':image_declared_bytes,'embedded_resource_bytes':image_embedded_bytes,
        'deferred_resource_bytes':image_deferred_bytes,'omitted_resource_bytes':image_omitted_bytes,
        'delayed_images':sum(bool(node.image.resource) and node.resource_policy in (ImageResourcePolicy.RETAIL_DELAYED,ImageResourcePolicy.FASTFILE_DELAYED) for node in owned_image_nodes),
        'fastfile_delayed_images':sum(bool(node.image.resource) and node.resource_policy is ImageResourcePolicy.FASTFILE_DELAYED for node in owned_image_nodes),
        'retail_delayed_images':sum(bool(node.image.resource) and node.resource_policy is ImageResourcePolicy.RETAIL_DELAYED for node in owned_image_nodes),
        'self_contained_images':sum(bool(node.image.resource) and node.resource_policy is ImageResourcePolicy.SELF_CONTAINED for node in owned_image_nodes),
    }
    diagnostics={
        'execution_mode':'boot-isolation' if boot_isolation else ('owner-aware-visual-candidate' if runtime_compatible else 'full-port'),
        'boot_isolation':bool(boot_isolation),
        'hardware_tested':False,
        'source_assets':len(al.assets),'destination_assets':len(plan.top_level_assets()),'support_nodes':val['support_nodes'],'dormant_nodes':val['dormant_nodes'],'ownership_edges':val['ownership_edges'],'source_script_strings':len(al.script_strings),'gfxworld_resource_policy':gfxworld_resource_policy.value,'material_image_resource_policy':material_image_resource_policy.value,
        'material_graph':dict(mg.diagnostics),'material_image_resources':material_image_resource_stats,'supplemental_iwd_image_xassets':sum(symbol.startswith('image:') for symbol in supplemental_top_level_symbols),'supplemental_technique_xassets':sum(symbol.startswith('techset:') for symbol in supplemental_top_level_symbols),'sound':{'policy':sound_policy,'source_xassets':sum(a.type_id==PC['sound'] for a in al.assets),'omitted_source_assets':sum(a.type_id in (PC['sound'],PC['sndcurve'],PC['loaded_sound']) for a in al.assets) if sound_policy=='omit' else 0,'source_lists':sb.source_list_count,'owned_lists':sb.owned_list_count,'external_native':sb.external_native_count,'custom_iwd_references':sb.custom_iwd_references,'inline_loaded_references':sb.inline_loaded_references,'runtime_null_soundfile_references':sb.runtime_null_soundfile_references},
        'rawfiles':len(raws),'stringtables':len(tables),'lightdefs':len(lights),'impactfx':len(impacts),'fx_source_packed_visuals':fx_source_packed_visuals,'fx_exact_packed_visuals':fx_exact_packed_visuals,'fx_exact_runner_references':fx_exact_runner_references,'fx_runner_target_count':fx_runner_resolution.target_count,'fx_runner_source_block4_end_anchor':fx_runner_resolution.source_block4_end_anchor,'fx_exact_material_references':fx_exact_material_references,'fx_material_replay_target_count':fx_material_resolution.target_count,'fx_material_replay_unresolved_raws':len(fx_material_resolution.unresolved_raws),'fx_material_replay_unresolved_sample':[f'0x{x:08X}' for x in fx_material_resolution.unresolved_raws[:12]],'fx_material_replay_anchors':len(fx_material_resolution.source_member_starts_by_asset),'fx_material_replay_unavailable':dict(fx_material_resolution.replay_unavailable_by_asset),'fx_runtime_material_visuals':fx_runtime_material_visuals,'fx_runtime_null_visuals':fx_runtime_null_visuals,
        'clipmap_runtime_null_physpreset_targets':0,
        'fx_visual_fallbacks':fx_visual_fallbacks,
        'clipmap_physpreset_bindings':sum(raw != 0 for raw in praw),
        'clipmap_owned_physpresets':len(core.clipmap.dyn_owned_phys_presets),
        'clipmap_xmodel_physpreset_alias_uses':sum(praw.count(raw) for raw in xmodel_phys_alias_raw),
        'clipmap_dynent_physpreset_alias_uses':sum(praw.count(raw) for raw in dynent_phys_alias_raw),
        'validation':val,
    }
    return AssemblyResult(core,plan,mg,sb,diagnostics)


def analyze_and_assemble(
    pc_ff:str|Path,map_name:str,iwd_paths:Sequence[str|Path]=(),*,texture_library_directory:str|Path|None=None,ffmpeg:str='ffmpeg',
    runtime_compatible:bool=False,boot_isolation:bool|None=None,
    gfxworld_resource_policy:GfxWorldResourcePolicy|str=GfxWorldResourcePolicy.FASTFILE_DELAYED,
    material_image_resource_policy:ImageResourcePolicy|str=ImageResourcePolicy.FASTFILE_DELAYED,
    sound_policy:str='omit',
    unresolved_image_policy:str='reference',
    unresolved_material_policy:str='reference',
    primary_light_policy:str='source',
    vertex_layer_policy:str='source',
    portal_policy:str='source',check_output_support:bool=False,
)->AssemblyResult:
    return assemble_core_source(
        analyze_core_source(pc_ff,map_name,iwd_paths,texture_library_directory=texture_library_directory,runtime_compatible=runtime_compatible,sound_policy=sound_policy,
                            check_portal_policy=portal_policy if check_output_support else None),
        ffmpeg=ffmpeg,runtime_compatible=runtime_compatible,boot_isolation=boot_isolation,
        gfxworld_resource_policy=gfxworld_resource_policy,
        material_image_resource_policy=material_image_resource_policy,
        sound_policy=sound_policy,
        unresolved_image_policy=unresolved_image_policy,
        unresolved_material_policy=unresolved_material_policy,
        primary_light_policy=primary_light_policy,
        vertex_layer_policy=vertex_layer_policy,
        portal_policy=portal_policy,
    )
