from __future__ import annotations
from dataclasses import dataclass
import hashlib,json
from pathlib import Path
from typing import Mapping,Sequence

from .assets.external import ExternalTechniqueSetNode,ExternalMaterialNode
from .assets.image import ImageNode,ExternalImageNode,ImageResourcePolicy,from_iwi,neutral_bc1_image
from .assets.material import MaterialNode
from .assets.techset import OwnedTechniqueSetNode,TechsetEmissionRegistry
from .backend.v4_iwd import IwdImageEntry
from .material_planning import MaterialUniverse,PlannedMaterial
from .technique_binding import TechniqueBinding


def make_technique_node(binding,registry):
    """The zone node serving one technique binding: owned when the binding
    carries a compiled/donor PS3 TechniqueSet, a ,name reference otherwise."""
    name=binding.candidate_name
    if getattr(binding,'owns_ps3_techset',False):
        return OwnedTechniqueSetNode(binding.compiled,sym('techset',name),registry)
    return ExternalTechniqueSetNode(name,sym('techset',name),True)


# Shared Material names owned by the zones that stay loaded while a PS3 map runs.
# Measured with tools/ps3_material_catalog.py; a ',name' outside this list is a
# reference the console cannot resolve, exactly like a map-owned TechniqueSet.
_SHARED_MATERIAL_REF=Path(__file__).resolve().parent/'reference'/'ps3_shared_material_availability.json'
def _load_shared_materials()->frozenset[str]:
    if not _SHARED_MATERIAL_REF.is_file():return frozenset()
    return frozenset(str(x).casefold() for x in json.loads(_SHARED_MATERIAL_REF.read_text(encoding='utf-8')).get('names',()))
PS3_SHARED_MATERIALS=_load_shared_materials()
# The engine's default Material asset (EBOOT default-name table 0x68094C, type 4).
PS3_DEFAULT_MATERIAL='$default'

def norm(name:str)->str:return name.replace('\\','/').lstrip('/').lstrip(',')
def sym(prefix:str,name:str)->str:return f'{prefix}:{hashlib.sha1(name.casefold().encode()).hexdigest()[:16]}:{name}'

@dataclass(frozen=True)
class MaterialGraphPlan:
    technique_nodes:tuple[object,...]
    image_nodes:tuple[object,...]
    material_nodes:tuple[object,...]
    technique_symbol_by_candidate:Mapping[str,str]
    image_symbol_by_name:Mapping[str,str]
    material_symbol_by_name:Mapping[str,str]
    material_symbol_by_source_asset_index:Mapping[int,str]
    diagnostics:Mapping[str,object]
    techset_registry:object=None


def _planned_signature(p:PlannedMaterial)->tuple:
    s=p.source
    return (
        s.game_flags,s.sort_key,s.atlas_rows,s.atlas_cols,s.draw_surf,s.surface_type_bits,s.hash_index,
        p.state.state_bits_entry,p.state.state_bits,s.state_flags,s.camera_region,p.platform_state,
        p.technique.candidate_name.casefold(),tuple(x.casefold() for x in p.image_names),
        tuple((t.name_hash,t.name_start,t.name_end,t.sampler,t.semantic) for t in s.textures),
        tuple(s.constants),
    )


def balanced_resource_policies(
    resource_bytes:Mapping[str,int],*,base_bytes:tuple[int,int]=(0,0),
)->tuple[dict[str,ImageResourcePolicy],tuple[int,int]]:
    """Split complete image payloads across PS3 blocks 3 and 6 deterministically.

    Images remain indivisible: dimensions, faces and complete mip chains are not
    changed.  Largest-first placement minimizes the larger of the two material
    resource bins while the case-folded name makes ties reproducible.
    """
    if len(base_bytes)!=2 or any(int(value)<0 for value in base_bytes):
        raise ValueError('balanced image resource base must contain two non-negative block sizes')
    bins=[int(base_bytes[0]),int(base_bytes[1])];out={}
    for name,size in sorted(resource_bytes.items(),key=lambda row:(-int(row[1]),row[0].casefold())):
        if int(size)<0:raise ValueError(f"negative image resource size for '{name}'")
        target=0 if bins[0]<=bins[1] else 1
        out[name]=ImageResourcePolicy.FASTFILE_DELAYED if target==0 else ImageResourcePolicy.SELF_CONTAINED
        bins[target]+=int(size)
    return out,(bins[0],bins[1])


def build_material_graph(universe:MaterialUniverse,iwd_images:Mapping[str,IwdImageEntry],*,include_orphan_iwds:bool=False,extra_image_semantics:Mapping[str,int]|None=None,image_resource_policy:ImageResourcePolicy|str=ImageResourcePolicy.SELF_CONTAINED,include_runtime_default_material:bool=False,unresolved_image_policy:str='reference',unresolved_material_policy:str='reference')->MaterialGraphPlan:
    if isinstance(image_resource_policy,str):image_resource_policy=ImageResourcePolicy(image_resource_policy)
    if unresolved_image_policy not in ('reference','neutral'):
        raise ValueError("unresolved_image_policy must be 'reference' or 'neutral'")
    if unresolved_material_policy not in ('reference','default'):
        raise ValueError("unresolved_material_policy must be 'reference' or 'default'")
    # TechniqueSet nodes: native/shared references stay ,name shells; compiled or
    # donor bindings own their full PS3 TechniqueSet in this zone.
    techset_registry=TechsetEmissionRegistry()
    tech_by={}
    for p in universe.planned_owned:
        c=p.technique.candidate_name
        if c.casefold() not in tech_by:
            tech_by[c.casefold()]=make_technique_node(p.technique,techset_registry)
    technique_symbols={node.name.casefold():node.symbol for node in tech_by.values()}

    # Enforce one material semantic identity per normalized name.
    by_name={}
    for p in universe.planned_owned:
        n=norm(p.source.name).casefold();sig=_planned_signature(p)
        if n in by_name and by_name[n][1]!=sig:
            raise ValueError(f"Material semantic name '{norm(p.source.name)}' has conflicting exact PS3 plans")
        by_name.setdefault(n,(p,sig))

    # Shared comma materials are external references. They cannot collide with an owned material.
    external_names={norm(m.name).casefold():m for _,m in universe.external_shared}
    overlap=set(by_name)&set(external_names)
    if overlap:raise ValueError('owned/shared Material identity collision: '+', '.join(sorted(overlap)))

    # A GfxImage carries exactly one semantic, and the PC zone states it at +0x0B of the
    # image root that OWNS the pixels.  A MaterialTextureDef slot only says how that
    # material samples the image, so a texture shared by two slots with different roles
    # (mp_getaway binds rock_wall_01_nml as both a normal map and a colour map) is not a
    # conflict - the owning root decides.  Slot semantics stay the fallback for images
    # that are only ever reached through a packed reuse pointer.
    owner_semantic={};slot_semantic={};required_names={};owner_names={}
    for p in universe.planned_owned:
        for t,name in zip(p.source.textures,p.image_names):
            k=norm(name).casefold();required_names.setdefault(k,norm(name))
            if t.inline_image_semantic is None:
                slot_semantic.setdefault(k,t.semantic);continue
            prior=owner_semantic.get(k)
            if prior is not None and prior!=t.inline_image_semantic:
                raise ValueError(
                    f"Image '{norm(name)}' has two owning PC GfxImage roots declaring "
                    f'different semantics {prior} and {t.inline_image_semantic}'
                )
            owner_semantic[k]=t.inline_image_semantic;owner_names[k]=norm(name)
    semantic={k:owner_semantic.get(k,slot_semantic.get(k,2)) for k in required_names}
    overrides={norm(k).casefold():int(v) for k,v in (extra_image_semantics or {}).items()}
    if include_orphan_iwds:
        for k,e in iwd_images.items():
            kk=k.casefold();required_names.setdefault(kk,e.image.name);semantic.setdefault(kk,overrides.get(kk,2))
    for kk,value in overrides.items():
        if kk in semantic and semantic[kk]!=value and kk in required_names:
            raise ValueError(f"Image '{required_names[kk]}' source semantic {value} conflicts with Material semantic {semantic[kk]}")

    balanced_by_name={};balanced_bins=(0,0)
    if image_resource_policy is ImageResourcePolicy.BALANCED_SPLIT:
        balanced_by_name,balanced_bins=balanced_resource_policies({
            k:len(from_iwi(entry.image,semantic[k]).resource)
            for k,name in required_names.items()
            if (entry:=iwd_images.get(k)) is not None
        })

    image_nodes=[];image_symbols={};owned_images=external_images=0;unresolved_neutral=0;unresolved_names=[]
    for k,name in sorted(required_names.items()):
        symbol=sym('image',name);entry=iwd_images.get(k)
        if entry is not None:
            effective_policy=balanced_by_name.get(k,image_resource_policy)
            node=ImageNode(from_iwi(entry.image,semantic[k]),symbol,effective_policy);owned_images+=1
        elif k.startswith('__cp11_neutral_semantic_'):
            # Neutral proof images are generated inside the FastFile.  Retail is unanimous
            # on this field - every GfxImage in both measured Retail PS3 map zones has
            # delayLoadPixels=1 - so these tiny 0x80-byte resources go through the same
            # block-3 FIFO instead of being the only self-contained images in the zone.
            node=ImageNode(neutral_bc1_image(name,semantic[k]),symbol,ImageResourcePolicy.FASTFILE_DELAYED);owned_images+=1
        elif unresolved_image_policy=='neutral':
            # No PC source supplied the pixels for this name.  Retail PS3 map zones own
            # every image they use - there is no IWD on the console and no cross-map
            # asset sharing - so a ',name' reference here asks DB_FindXAssetHeader for
            # something no loaded zone provides.  Measured 2026-09-04: of 288 such names
            # in mp_getaway, 285 occur in NO always-loaded zone (common_mp, ui_mp,
            # code_post_gfx_mp).  Writing a tiny owned placeholder keeps the zone
            # self-contained: the surface renders flat instead of textured, but nothing
            # is left dangling.  Supply the missing IWDs and this branch disappears.
            node=ImageNode(neutral_bc1_image(name,semantic[k]),symbol,ImageResourcePolicy.FASTFILE_DELAYED)
            owned_images+=1;unresolved_neutral+=1;unresolved_names.append(name)
        else:
            node=ExternalImageNode(name,symbol);external_images+=1
        image_nodes.append(node);image_symbols[k]=symbol

    material_nodes=[];material_symbols={};source_symbols={};owned=external=0
    for k,(p,_) in sorted(by_name.items()):
        name=norm(p.source.name);symbol=sym('material',name);tech_symbol=technique_symbols[p.technique.candidate_name.casefold()]
        imgs=tuple(image_symbols[norm(x).casefold()] for x in p.image_names)
        material_nodes.append(MaterialNode(p.source,p.state,p.platform_state,tech_symbol,imgs,symbol));material_symbols[k]=symbol;owned+=1
    rebound_materials=[]
    for k,m in sorted(external_names.items()):
        name=norm(m.name)
        if (unresolved_material_policy=='default' and PS3_SHARED_MATERIALS
                and k not in PS3_SHARED_MATERIALS and k!='default'):
            # No loaded PS3 zone owns this Material.  A dangling ,name is worse than an
            # obviously wrong surface: the loader has nothing to bind.  ,$default always
            # resolves - it is the engine's own default Material (DB default-asset name
            # table 0x68094C) and code_post_gfx_mp carries it.  (A Material called plain
            # 'default' does not exist on PS3: the shared catalog entry of that name is a
            # techset/physpreset/impactfx; the emulated database turned ',default' into a
            # clone of $default on every map.)
            rebound_materials.append(name);continue
        symbol=sym('material.ext',name);material_nodes.append(ExternalMaterialNode(name,symbol,True));material_symbols[k]=symbol;external+=1
    # Quarantined materials: planning failed per material (any error class) and
    # the universe collected them instead of aborting.  They take the same
    # hardware-proven ,$default closure as CP12-A boot isolation, one surface
    # at a time.  A same-named healthy identity, if one exists, keeps priority.
    quarantined_rows=tuple(getattr(universe,'quarantined_materials',()) or ())
    quarantined_names=[norm(str(row['name'])) for row in quarantined_rows]
    # Runtime-compatible mode also uses ,default for unresolved FX/other asset visuals.
    # Do not make its existence depend on XModel fallback debt: once exact XModel closure
    # reached zero, that accidental dependency made the owner-aware graph fail before write.
    if include_runtime_default_material or rebound_materials or quarantined_names or universe.xmodel_material_resolution.get('runtime_fallbacks',0):
        k='default'
        if k not in material_symbols:
            name=PS3_DEFAULT_MATERIAL;symbol=sym('material.ext',name);material_nodes.append(ExternalMaterialNode(name,symbol,True));material_symbols[k]=symbol;external+=1
    for name in rebound_materials:
        material_symbols[norm(name).casefold()]=material_symbols['default']
    for name in quarantined_names:
        material_symbols.setdefault(norm(name).casefold(),material_symbols['default'])
    for x in universe.top_level:
        k=norm(x.material.name).casefold()
        if k not in material_symbols:raise ValueError(f"Top-level Material '{x.material.name}' has no destination symbol")
        source_symbols[x.source_asset_index]=material_symbols[k]

    owned_image_nodes=[node for node in image_nodes if isinstance(node,ImageNode)]
    declared_bytes=sum(len(node.image.resource) for node in owned_image_nodes)
    embedded_bytes=sum(len(node.image.resource) for node in owned_image_nodes if node.resource_policy is ImageResourcePolicy.SELF_CONTAINED)
    delayed_images=sum(bool(node.image.resource) and node.resource_policy is ImageResourcePolicy.RETAIL_DELAYED for node in owned_image_nodes)
    return MaterialGraphPlan(tuple(tech_by.values()),tuple(image_nodes),tuple(material_nodes),
        {node.name.casefold():node.symbol for node in tech_by.values()},image_symbols,material_symbols,source_symbols,
        {'owned_materials':owned,'external_materials':external,'owned_images':owned_images,'external_images':external_images,'technique_nodes':len(tech_by),'xmodel_material_runtime_fallbacks':universe.xmodel_material_resolution.get('runtime_fallbacks',0),'neutral_image_fallbacks':universe.image_proof.get('neutral_fallback_count',0),'image_resource_policy':image_resource_policy.value,'unresolved_image_policy':unresolved_image_policy,'unresolved_material_policy':unresolved_material_policy,'materials_rebound_to_default':len(rebound_materials),'materials_rebound_to_default_names':tuple(sorted(rebound_materials)),'materials_quarantined':len(quarantined_rows),'materials_quarantined_rows':tuple({'name':str(r['name']),'reason':str(r['reason'])} for r in quarantined_rows),'unresolved_neutral_images':unresolved_neutral,'unresolved_neutral_image_names':tuple(sorted(unresolved_names)),'declared_image_resource_bytes':declared_bytes,'planned_embedded_image_resource_bytes':embedded_bytes,'planned_omitted_image_resource_bytes':declared_bytes-embedded_bytes,'planned_delayed_images':delayed_images,'balanced_split_block3_bytes':balanced_bins[0],'balanced_split_block6_bytes':balanced_bins[1]},techset_registry)
