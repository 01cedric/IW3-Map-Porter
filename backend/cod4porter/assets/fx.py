from __future__ import annotations
from dataclasses import dataclass,field
import struct
from typing import Mapping

from ..graph import AssetType,GraphContext,Dependency
from ..zone_writer import RelocatingZoneWriter,PackedOffset,TEMP_BLOCK,FOLLOWING,NULL,LARGE_BLOCK
from ..backend.v4_ffio import decode_pc_pointer
from ..pc_fx import (
    OwnedFxGraph,FxElement,FxVisual,VisualKind,FxEffectReference,FxTrail,ImpactFx,
    FX_ROOT_SIZE,FX_ELEM_SIZE,VELOCITY_SAMPLE_SIZE,VISUAL_STATE_SAMPLE_SIZE,
    TRAIL_ROOT_SIZE,TRAIL_VERTEX_SIZE,PS3_XMODEL_ALIAS_ROOT_SIZE,IMPACT_REFERENCE_COUNT,
)

def _be32_words(pc:bytes)->bytes:
    if len(pc)%4:raise ValueError('word payload length')
    out=bytearray(len(pc))
    for o in range(0,len(pc),4):struct.pack_into('>I',out,o,struct.unpack_from('<I',pc,o)[0])
    return bytes(out)

@dataclass
class OwnedFxNode:
    source:OwnedFxGraph
    material_symbols_by_root:Mapping[int,str]
    symbol:str
    packed_visual_symbols_by_raw:Mapping[int,tuple[AssetType,str]]=field(default_factory=dict)
    # element type 10 (runner): the visual is an FxEffectDefRef, which the PS3 loader reads as
    # a *name string* (Load_FxEffectDefRef -> Load_XString -> DB_FindXAssetHeader by name),
    # never as an asset handle.  Retail zones alias the target's name string; we write the
    # name inline (FOLLOWING), which the same loader path accepts.
    packed_visual_names_by_raw:Mapping[int,str]=field(default_factory=dict)
    runtime_fallback_material_symbol:str|None=None
    effect_names_by_raw:Mapping[int,str]=field(default_factory=dict)
    allow_runtime_visual_fallback:bool=False
    type:AssetType=field(init=False,default=AssetType.FX);root_block:int=field(init=False,default=TEMP_BLOCK)
    def __post_init__(self):
        for e in self.source.elements:
            for v in e.visuals:
                if v.kind==VisualKind.MATERIAL and (v.material_root is None or v.material_root not in self.material_symbols_by_root):raise ValueError(f'FX {self.source.name} material has no destination symbol')
                if v.kind==VisualKind.PACKED_UNRESOLVED:
                    exact=self.packed_visual_symbols_by_raw.get(v.serialized_pointer)
                    # Type 10 is a typed FxEffectDef runner and is never nullable.
                    # Runtime closure is retained only for untyped material/type-8
                    # visuals whose fallback policy is reported separately.
                    runtime_ok=self.allow_runtime_visual_fallback and (e.element_type<=4 or e.element_type==8)
                    if exact is None and not runtime_ok:raise ValueError(f'FX {self.source.name} unresolved packed visual')
                    if exact is None and e.element_type<=4 and self.runtime_fallback_material_symbol is None:raise ValueError(f'FX {self.source.name} runtime Material visual has no fallback symbol')
            for r in (e.effect_on_impact,e.effect_on_death,e.effect_emitted):
                if r.inline_name is None and r.pointer_kind=='packed' and r.serialized_pointer not in self.effect_names_by_raw:raise ValueError(f'FX {self.source.name} unresolved packed effect ref')
    @property
    def dependencies(self):
        deps=[];seen=set()
        def add(symbol,typ,desc):
            key=(symbol,typ)
            if key not in seen:seen.add(key);deps.append(Dependency(symbol,typ,desc))
        for e in self.source.elements:
            for v in e.visuals:
                if v.kind==VisualKind.MATERIAL:add(self.material_symbols_by_root[v.material_root],AssetType.MATERIAL,f"owned FX '{self.source.name}' Material visual")
                elif v.kind==VisualKind.PACKED_UNRESOLVED:
                    exact=self.packed_visual_symbols_by_raw.get(v.serialized_pointer)
                    if exact is not None:add(exact[1],exact[0],f"owned FX '{self.source.name}' exact packed visual")
                    elif e.element_type<=4 and self.runtime_fallback_material_symbol is not None:add(self.runtime_fallback_material_symbol,AssetType.MATERIAL,f"owned FX '{self.source.name}' runtime Material visual fallback")
        return tuple(deps)
    def _visual_material_symbol(self,v):return self.material_symbols_by_root[v.material_root]
    def _runner_name(self,v):
        name=self.packed_visual_names_by_raw.get(v.serialized_pointer)
        if not name:raise ValueError(f'FX {self.source.name} runner visual has no target effect name')
        return name.lstrip(',')
    @staticmethod
    def _is_runner_ref(v,element_type):return element_type==10 and v.kind==VisualKind.PACKED_UNRESOLVED
    def _root(self):
        pc=self.source.root_bytes
        if len(pc)!=FX_ROOT_SIZE:raise ValueError('FX root size')
        out=bytearray(FX_ROOT_SIZE);struct.pack_into('>I',out,0,FOLLOWING)
        for o in range(4,0x1c,4):struct.pack_into('>I',out,o,struct.unpack_from('<I',pc,o)[0])
        struct.pack_into('>I',out,0x1c,FOLLOWING if self.source.elements else NULL);return bytes(out)
    def _elem_root(self,e:FxElement):
        pc=e.root_bytes
        if len(pc)!=FX_ELEM_SIZE:raise ValueError('FX elem size')
        out=bytearray(FX_ELEM_SIZE)
        for o in range(0,0xa8,4):struct.pack_into('>I',out,o,struct.unpack_from('<I',pc,o)[0])
        out[0xa8:0xae]=pc[0xa8:0xae];struct.pack_into('>H',out,0xae,struct.unpack_from('<H',pc,0xae)[0]);out[0xb0:0xb4]=pc[0xb0:0xb4]
        struct.pack_into('>I',out,0xb4,self._inline_array_pointer(e.velocity_pointer_raw,len(e.velocity_bytes),'velocity'))
        struct.pack_into('>I',out,0xb8,self._inline_array_pointer(e.visual_state_pointer_raw,len(e.visual_state_bytes),'visual state'))
        struct.pack_into('>I',out,0xbc,self._initial_visual_pointer(e))
        for o in range(0xc0,0xd8,4):struct.pack_into('>I',out,o,struct.unpack_from('<I',pc,o)[0])
        for o,r in ((0xd8,e.effect_on_impact),(0xdc,e.effect_on_death),(0xe0,e.effect_emitted)):struct.pack_into('>I',out,o,self._effect_pointer(r))
        for o in range(0xe4,0xf4,4):struct.pack_into('>I',out,o,struct.unpack_from('<I',pc,o)[0])
        struct.pack_into('>I',out,0xf4,FOLLOWING if e.trail else NULL);out[0xf8:0xfc]=pc[0xf8:0xfc];return bytes(out)
    @staticmethod
    def _inline_array_pointer(raw,n,label):
        k=decode_pc_pointer(raw).kind
        if n==0:return NULL
        if k=='packed':raise ValueError(f'FX {label} unresolved packed data')
        if k not in ('following','insert'):raise ValueError(f'FX {label} not inline')
        return FOLLOWING
    def _initial_visual_pointer(self,e):
        if e.visual_count==0:return NULL
        if e.element_type==9 or e.visual_count>1:
            if decode_pc_pointer(e.visual_pointer_raw).kind=='packed':raise ValueError('FX visual table packed unresolved')
            return FOLLOWING
        v=e.visuals[0]
        if v.kind==VisualKind.NULL:return NULL
        if v.kind==VisualKind.MATERIAL:return NULL
        if v.kind in (VisualKind.XMODEL_ALIAS,VisualKind.STRING):return FOLLOWING
        if self._is_runner_ref(v,e.element_type):
            self._runner_name(v);return FOLLOWING
        if v.kind==VisualKind.PACKED_UNRESOLVED:
            if v.serialized_pointer in self.packed_visual_symbols_by_raw:return NULL  # relocation registered after root write
            if self.allow_runtime_visual_fallback and e.element_type<=4:return NULL   # fallback Material relocation
            if self.allow_runtime_visual_fallback and e.element_type==8:return NULL
        raise ValueError('FX visual unresolved')
    def _effect_name(self,r:FxEffectReference):
        return r.inline_name if r.inline_name is not None else self.effect_names_by_raw.get(r.serialized_pointer)
    def _effect_pointer(self,r:FxEffectReference):
        if self._effect_name(r) is not None:return FOLLOWING
        if r.pointer_kind=='null':return NULL
        raise ValueError('FX effect ref unresolved')
    def _visual_graph_symbol(self,v,element_type):
        if v.kind==VisualKind.MATERIAL:return self._visual_material_symbol(v)
        if self._is_runner_ref(v,element_type):return None   # written as a name string, never aliased
        if v.kind==VisualKind.PACKED_UNRESOLVED:
            exact=self.packed_visual_symbols_by_raw.get(v.serialized_pointer)
            if exact is not None:return exact[1]
            if self.allow_runtime_visual_fallback and element_type<=4 and self.runtime_fallback_material_symbol is not None:return self.runtime_fallback_material_symbol
        return None
    def _write_visual_pointer(self,w,c,v,desc,element_type,site):
        target=self._visual_graph_symbol(v,element_type)
        if target is not None:
            marker=c.owned_marker(self.symbol,target,site)
            if marker is not None:
                c.bind_owned_pointer_field(w,self.symbol,target,site,w.current_location);w.write_u32(marker,desc+' owned marker')
            else:w.write_pointer_to(c.reference_symbol(target),kind='packed_alias',description=desc+' graph reference')
        elif v.kind==VisualKind.NULL:w.write_u32(NULL,desc+' NULL')
        elif v.kind in (VisualKind.XMODEL_ALIAS,VisualKind.STRING):w.write_u32(FOLLOWING,desc+' FOLLOWING')
        elif self._is_runner_ref(v,element_type):self._runner_name(v);w.write_u32(FOLLOWING,desc+' runner effect name FOLLOWING')
        elif v.kind==VisualKind.PACKED_UNRESOLVED and self.allow_runtime_visual_fallback and element_type==8:w.write_u32(NULL,desc+' runtime NULL type-8 visual')
        else:raise ValueError('FX visual unresolved')
    def _write_visual_payload(self,w,v,desc,element_type=None):
        if v.kind in (VisualKind.NULL,VisualKind.MATERIAL):return (1 if v.kind==VisualKind.MATERIAL else 0,0,0)
        if self._is_runner_ref(v,element_type):
            w.write_latin1z(self._runner_name(v),desc+' runner effect name');return (0,0,1)
        if v.kind==VisualKind.XMODEL_ALIAS:
            if not v.name:raise ValueError('empty FX xmodel visual')
            # FxElemVisuals.model is an XModel asset pointer.  The inline XModel
            # shell is allocated in TEMP; its member XString is default-normal.
            root=bytearray(PS3_XMODEL_ALIAS_ROOT_SIZE);struct.pack_into('>I',root,0,FOLLOWING)
            w.push_block(TEMP_BLOCK);w.align_logical_only(4);w.write_in_block(root,desc+' XModel TEMP alias root');w.pop_block()
            w.write_latin1z(','+v.name.lstrip(','),desc+' XModel external name');return (0,1,0)
        if v.kind==VisualKind.STRING:
            if not v.name:raise ValueError('empty FX string visual')
            w.write_latin1z(v.name,desc+' string');return (0,0,1)
        if v.kind==VisualKind.PACKED_UNRESOLVED:return (0,0,0)
        raise ValueError('FX visual unresolved')
    @staticmethod
    def _write_velocity(w,src,desc):
        if not src:return
        if len(src)%VELOCITY_SAMPLE_SIZE:raise ValueError('FX velocity alignment')
        w.align_logical_only(4)
        w.write_in_block(_be32_words(src),desc)
    @staticmethod
    def _write_visualstate(w,src,desc):
        if not src:return
        if len(src)%VISUAL_STATE_SAMPLE_SIZE:raise ValueError('FX visualstate alignment')
        w.align_logical_only(4)
        out=bytearray(len(src))
        for s in range(0,len(src),VISUAL_STATE_SAMPLE_SIZE):
            for half in (0,0x18):
                out[s+half:s+half+4]=src[s+half:s+half+4]
                for word in range(4,0x18,4):struct.pack_into('>I',out,s+half+word,struct.unpack_from('<I',src,s+half+word)[0])
        w.write_in_block(out,desc)
    @staticmethod
    def _write_trail(w,t:FxTrail,desc):
        w.align_logical_only(4)
        out=bytearray(TRAIL_ROOT_SIZE)
        for o in (0,4,8,0xc,0x14):struct.pack_into('>I',out,o,struct.unpack_from('<I',t.root_bytes,o)[0])
        struct.pack_into('>I',out,0x10,FOLLOWING if t.vertex_bytes else NULL);struct.pack_into('>I',out,0x18,FOLLOWING if t.index_bytes else NULL);w.write_in_block(out,desc+' root')
        if t.vertex_bytes:
            if len(t.vertex_bytes)%TRAIL_VERTEX_SIZE:raise ValueError('trail vertex alignment')
            w.write_in_block(_be32_words(t.vertex_bytes),desc+' vertices')
        if t.index_bytes:
            if len(t.index_bytes)%2:raise ValueError('trail index alignment')
            outi=bytearray(len(t.index_bytes))
            for o in range(0,len(outi),2):struct.pack_into('>H',outi,o,struct.unpack_from('<H',t.index_bytes,o)[0])
            w.write_in_block(outi,desc+' indices')
    def write(self,w:RelocatingZoneWriter,c:GraphContext):
        if w.current_block_index!=TEMP_BLOCK:raise ValueError('owned FX root must be TEMP')
        rp=w.physical_position;rl=w.define_symbol(self.symbol);c.register_root(self.symbol,rp);w.write_in_block(self._root(),f'owned FX {self.source.name} root')
        # Generated IW3 asset loaders push VIRTUAL around every FxEffectDef member.
        w.push_block(LARGE_BLOCK);member_block_start=w.current_location;w.write_latin1z(self.source.name,'owned FX name')
        w.align_logical_only(4);element_phys=[]
        for e in self.source.elements:
            ep=w.physical_position;el=w.current_location;element_phys.append(ep);eroot=bytearray(self._elem_root(e));single_target=None;single_site=None
            if e.visual_count==1 and len(e.visuals)==1:
                v=e.visuals[0];single_target=self._visual_graph_symbol(v,e.element_type);single_site=f'element[{e.index}].visual[0]'
                if single_target is not None:
                    marker=c.owned_marker(self.symbol,single_target,single_site)
                    if marker is not None:struct.pack_into('>I',eroot,0xbc,marker);c.bind_owned_pointer_field(w,self.symbol,single_target,single_site,PackedOffset(el.block,el.offset+0xbc))
            w.write_in_block(bytes(eroot),f'FX elem {e.index}')
            if single_target is not None and not c.owns_at(self.symbol,single_target,single_site):
                w.register_pointer_relocation(ep+0xbc,c.reference_symbol(single_target),kind='packed_alias',description='FX single graph visual')
        mv=xm=sv=effects=trails=0
        for e in self.source.elements:
            self._write_velocity(w,e.velocity_bytes,f'FX elem {e.index} velocity');self._write_visualstate(w,e.visual_state_bytes,f'FX elem {e.index} visualstate')
            if e.element_type==9 and e.visual_count>0:
                w.align_logical_only(4)
                if len(e.visuals)!=e.visual_count*2:raise ValueError('decal visual cardinality')
                for i,v in enumerate(e.visuals):self._write_visual_pointer(w,c,v,f'FX elem {e.index} visual[{i}]',e.element_type,f'element[{e.index}].visual[{i}]')
                for i,v in enumerate(e.visuals):
                    target=self._visual_graph_symbol(v,e.element_type);site=f'element[{e.index}].visual[{i}]'
                    if target is not None and c.owns_at(self.symbol,target,site):c.write_owned_child(w,self.symbol,target,site);a,b,d=(1 if v.kind==VisualKind.MATERIAL else 0,0,0)
                    else:a,b,d=self._write_visual_payload(w,v,f'FX elem {e.index} visual[{i}]',e.element_type)
                    mv+=a;xm+=b;sv+=d
            elif e.visual_count>1:
                w.align_logical_only(4)
                if len(e.visuals)!=e.visual_count:raise ValueError('visual cardinality')
                for i,v in enumerate(e.visuals):self._write_visual_pointer(w,c,v,f'FX elem {e.index} visual[{i}]',e.element_type,f'element[{e.index}].visual[{i}]')
                for i,v in enumerate(e.visuals):
                    target=self._visual_graph_symbol(v,e.element_type);site=f'element[{e.index}].visual[{i}]'
                    if target is not None and c.owns_at(self.symbol,target,site):c.write_owned_child(w,self.symbol,target,site);a,b,d=(1 if v.kind==VisualKind.MATERIAL else 0,0,0)
                    else:a,b,d=self._write_visual_payload(w,v,f'FX elem {e.index} visual[{i}]',e.element_type)
                    mv+=a;xm+=b;sv+=d
            elif e.visual_count==1:
                if len(e.visuals)!=1:raise ValueError('single visual cardinality')
                v=e.visuals[0];target=self._visual_graph_symbol(v,e.element_type);site=f'element[{e.index}].visual[0]'
                if target is not None and c.owns_at(self.symbol,target,site):c.write_owned_child(w,self.symbol,target,site);a,b,d=(1 if v.kind==VisualKind.MATERIAL else 0,0,0)
                else:a,b,d=self._write_visual_payload(w,v,f'FX elem {e.index} visual[0]',e.element_type)
                mv+=a;xm+=b;sv+=d
            for label,r in (('impact',e.effect_on_impact),('death',e.effect_on_death),('emitted',e.effect_emitted)):
                name=self._effect_name(r)
                if name is not None:w.write_latin1z(name.lstrip(','),f'FX elem {e.index} {label} effect name');effects+=1
            if e.trail:self._write_trail(w,e.trail,f'FX elem {e.index} trail');trails+=1
        member_block_end=w.current_location;w.pop_block()
        c.register_result('fx.owned:'+self.symbol,{'root_physical':rp,'root_location':rl,'member_block_start':member_block_start,'member_block_end':member_block_end,'element_count':len(self.source.elements),'material_visuals':mv,'xmodel_aliases':xm,'string_visuals':sv,'effect_strings':effects,'trails':trails})

@dataclass
class ImpactFxNode:
    source:ImpactFx
    fx_symbols_by_source_asset_index:Mapping[int,str]
    symbol:str
    type:AssetType=field(init=False,default=AssetType.IMPACTFX);root_block:int=field(init=False,default=TEMP_BLOCK)
    def __post_init__(self):
        if len(self.source.fx_source_asset_indices)!=IMPACT_REFERENCE_COUNT:raise ValueError('ImpactFx reference cardinality')
        for t in self.source.fx_source_asset_indices:
            if t is not None and t not in self.fx_symbols_by_source_asset_index:raise ValueError(f'ImpactFx FX #{t} has no PS3 symbol')
    @property
    def dependencies(self):
        seen=[]
        for t in self.source.fx_source_asset_indices:
            if t is not None:
                s=self.fx_symbols_by_source_asset_index[t]
                if s not in seen:seen.append(s)
        return tuple(Dependency(s,AssetType.FX,f"ImpactFx '{self.source.name}' FX target") for s in seen)
    def write(self,w,c):
        rp=w.physical_position;rl=w.define_symbol(self.symbol);c.register_root(self.symbol,rp);root=bytearray(8);struct.pack_into('>II',root,0,FOLLOWING,FOLLOWING);w.write_in_block(root,f'ImpactFx {self.source.name} root')
        w.push_block(LARGE_BLOCK);name_location=w.current_location;w.write_latin1z(self.source.name,'ImpactFx name');w.align_logical_only(4);table=w.physical_position;table_location=w.current_location;nonnull=0
        for i,t in enumerate(self.source.fx_source_asset_indices):
            if t is None:w.write_u32(NULL,f'ImpactFx[{i}] NULL')
            else:w.write_pointer_to(c.reference_symbol(self.fx_symbols_by_source_asset_index[t]),kind='packed_alias',description=f'ImpactFx[{i}]');nonnull+=1
        w.pop_block()
        c.register_result('impactfx:'+self.symbol,{'root_physical':rp,'name_location':name_location,'table_physical':table,'table_location':table_location,'root_location':rl,'nonnull':nonnull})
