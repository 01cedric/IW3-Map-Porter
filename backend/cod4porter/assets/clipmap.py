from __future__ import annotations
from dataclasses import dataclass, field
import struct
from typing import Mapping

from ..graph import AssetType, Dependency, GraphContext, nested_symbol
from ..plane_format import ps3_plane_signbits
from ..zone_writer import (
    RelocatingZoneWriter, TEMP_BLOCK, VIRTUAL_BLOCK, LARGE_BLOCK, INSERT_BLOCK,
    PackedOffset, FOLLOWING, INSERT, NULL,
)
from ..pc_clipmap import ClipMap, SubModel, Brush, DynEnt, DynEntOwnedPhysPreset
from ..physics import build_phys_preset_root

ROOT_SIZE=0x11C; PLANE_SIZE=0x14; STATIC_MODEL_SIZE=0x50; MATERIAL_SIZE=0x48
BRUSH_SIDE_SIZE=0x0C; NODE_SIZE=0x08; LEAF_SIZE=0x2C; LEAF_BRUSH_SIZE=0x02
LEAF_BRUSH_NODE_SIZE=0x14; VERTEX_SIZE=0x0C; BORDER_SIZE=0x1C; PARTITION_SIZE=0x0C
AABB_SIZE=0x20; SUBMODEL_SIZE=0x48; BRUSH_SIZE=0x50; DYNDEF_SIZE=0x60; MAPENTS_ROOT=0x0C
POSE_SIZE=0x20; CLIENT_SIZE=0x0C; COLLISION_SIZE=0x14


def u16be(b,o,v): struct.pack_into('>H',b,o,int(v)&0xffff)
def i16be(b,o,v): struct.pack_into('>h',b,o,int(v))
def u32be(b,o,v): struct.pack_into('>I',b,o,int(v)&0xffffffff)
def i32be(b,o,v): struct.pack_into('>i',b,o,int(v))
def f32be(b,o,v): struct.pack_into('>f',b,o,float(v))
def v3be(b,o,v):
    for i,x in enumerate(v): f32be(b,o+i*4,x)
def v4be(b,o,v):
    for i,x in enumerate(v): f32be(b,o+i*4,x)


def _submodel_bytes(m:SubModel)->bytes:
    d=bytearray(SUBMODEL_SIZE);v3be(d,0,m.minimum);v3be(d,0x0c,m.maximum);f32be(d,0x18,m.radius)
    u16be(d,0x1c,m.first_aabb);u16be(d,0x1e,m.aabb_count);i32be(d,0x20,m.brush_contents);i32be(d,0x24,m.terrain_contents)
    v3be(d,0x28,m.leaf_min);v3be(d,0x34,m.leaf_max);i32be(d,0x40,m.leaf_brush_node);i16be(d,0x44,m.cluster)
    return bytes(d)

def _brush_bytes(b:Brush,box=False)->bytes:
    d=bytearray(BRUSH_SIZE);v3be(d,0,b.minimum);i32be(d,0x0c,b.contents);v3be(d,0x10,b.maximum);u32be(d,0x1c,b.non_axial)
    for i in range(6):
        i16be(d,0x24+i*2,b.axial_materials[i]);i16be(d,0x34+i*2,b.adjacent_offsets[i]);d[0x40+i]=b.axial_edge_counts[i]&0xff
    if box:u32be(d,0x20,NULL);u32be(d,0x30,NULL)
    return bytes(d)

def _count_marker(d,co,po,n):u32be(d,co,n);u32be(d,po,FOLLOWING if n else NULL)

@dataclass
class ClipMapNode:
    clip: ClipMap
    symbol: str
    xmodel_symbols_by_raw: Mapping[int,str]=field(default_factory=dict)
    physpreset_symbols_by_raw: Mapping[int,str]=field(default_factory=dict)
    xmodel_physpreset_alias_symbols_by_raw: Mapping[int,str]=field(default_factory=dict)
    xmodel_physpreset_owner_symbols_by_raw: Mapping[int,str]=field(default_factory=dict)
    dynent_physpreset_alias_symbols_by_raw: Mapping[int,str]=field(default_factory=dict)
    fx_symbols_by_raw: Mapping[int,str]=field(default_factory=dict)
    xmodelpieces_symbols_by_raw: Mapping[int,str]=field(default_factory=dict)
    shared_name_symbol: str|None=None
    shared_planes_symbol: str|None=None
    shared_world_symbol: str|None=None
    type: AssetType=field(init=False,default=AssetType.CLIPMAP_MP)
    root_block: int=field(init=False,default=TEMP_BLOCK)

    @property
    def dependencies(self):
        deps=[];seen=set()
        refs=[]
        refs += [(m.xmodel_raw,AssetType.XMODEL,self.xmodel_symbols_by_raw,'static XModel') for m in self.clip.static_models if m.xmodel_raw]
        for e in self.clip.dyn_models+self.clip.dyn_brushes:
            if e.xmodel_raw: refs.append((e.xmodel_raw,AssetType.XMODEL,self.xmodel_symbols_by_raw,'DynEnt XModel'))
            if e.destroy_fx_raw: refs.append((e.destroy_fx_raw,AssetType.FX,self.fx_symbols_by_raw,'DynEnt FX'))
            if e.destroy_pieces_raw: refs.append((e.destroy_pieces_raw,AssetType.XMODELPIECES,self.xmodelpieces_symbols_by_raw,'DynEnt XModelPieces'))
            if (e.phys_preset_raw and decode_kind(e.phys_preset_raw)=='packed'
                and e.phys_preset_raw not in self.xmodel_physpreset_alias_symbols_by_raw
                and e.phys_preset_raw not in self.dynent_physpreset_alias_symbols_by_raw):
                refs.append((e.phys_preset_raw,AssetType.PHYSPRESET,self.physpreset_symbols_by_raw,'DynEnt PhysPreset'))
        for raw,typ,mapping,label in refs:
            if raw not in mapping: raise ValueError(f'ClipMap {label} 0x{raw:08X} has no exact regenerated binding')
            s=mapping[raw];key=(s,typ)
            if key not in seen:deps.append(Dependency(s,typ,f'ClipMap {label} 0x{raw:08X}'));seen.add(key)
        if self.shared_world_symbol is not None and (self.shared_world_symbol,AssetType.GFXWORLD) not in seen:
            deps.append(Dependency(self.shared_world_symbol,AssetType.GFXWORLD,'ClipMap shared GfxWorld name/planes'));seen.add((self.shared_world_symbol,AssetType.GFXWORLD))
        for owner in sorted(set(self.xmodel_physpreset_owner_symbols_by_raw.values())):
            key=(owner,AssetType.XMODEL)
            if key not in seen:deps.append(Dependency(owner,AssetType.XMODEL,'ClipMap XModel-owned PhysPreset alias'));seen.add(key)
        return tuple(deps)

    def _target(self,ctx:GraphContext,raw:int,typ:AssetType)->str|None:
        if not raw:return None
        if typ==AssetType.PHYSPRESET:
            alias=self.xmodel_physpreset_alias_symbols_by_raw.get(raw) or self.dynent_physpreset_alias_symbols_by_raw.get(raw)
            if alias is not None:return alias
        mp={AssetType.XMODEL:self.xmodel_symbols_by_raw,AssetType.PHYSPRESET:self.physpreset_symbols_by_raw,AssetType.FX:self.fx_symbols_by_raw,AssetType.XMODELPIECES:self.xmodelpieces_symbols_by_raw}[typ]
        if raw not in mp:raise ValueError(f'ClipMap unresolved {typ.name} ref 0x{raw:08X}')
        return ctx.reference_symbol(mp[raw])

    def _owned_phys_symbol(self,owner:DynEntOwnedPhysPreset,suffix:str)->str:
        return nested_symbol(self.symbol,f'dynEnt.{owner.list_kind}.{owner.index}.physPreset.{suffix}')

    def _root(self)->bytes:
        c=self.clip;d=bytearray(ROOT_SIZE);i32be(d,4,c.in_use);i32be(d,8,len(c.planes))
        _count_marker(d,0x10,0x14,len(c.static_models));_count_marker(d,0x18,0x1c,len(c.materials));_count_marker(d,0x20,0x24,len(c.brush_sides));_count_marker(d,0x28,0x2c,len(c.brush_edges));_count_marker(d,0x30,0x34,len(c.nodes));_count_marker(d,0x38,0x3c,len(c.leaves));_count_marker(d,0x40,0x44,len(c.leaf_brush_nodes));_count_marker(d,0x48,0x4c,len(c.root_leaf_brushes));_count_marker(d,0x50,0x54,0);_count_marker(d,0x58,0x5c,len(c.vertices))
        i32be(d,0x60,c.triangle_count);u32be(d,0x64,FOLLOWING if c.triangle_count else NULL);u32be(d,0x68,FOLLOWING)
        _count_marker(d,0x6c,0x70,len(c.borders));_count_marker(d,0x74,0x78,len(c.partitions));_count_marker(d,0x7c,0x80,len(c.aabbs));_count_marker(d,0x84,0x88,len(c.submodels));u16be(d,0x8c,len(c.brushes));u32be(d,0x90,FOLLOWING)
        i32be(d,0x94,c.cluster_count);i32be(d,0x98,c.cluster_bytes);u32be(d,0x9c,FOLLOWING if c.visibility else NULL);i32be(d,0xa0,c.vised);u32be(d,0xa4,INSERT);u32be(d,0xa8,FOLLOWING)
        d[0xac:0xac+SUBMODEL_SIZE]=_submodel_bytes(c.box_model);u16be(d,0xf4,len(c.dyn_models));u16be(d,0xf6,len(c.dyn_brushes))
        for o,n in ((0xf8,len(c.dyn_models)),(0xfc,len(c.dyn_brushes)),(0x100,len(c.dyn_models)),(0x104,len(c.dyn_brushes)),(0x108,len(c.dyn_models)),(0x10c,len(c.dyn_brushes)),(0x110,len(c.dyn_models)),(0x114,len(c.dyn_brushes))):u32be(d,o,FOLLOWING if n else NULL)
        u32be(d,0x118,c.checksum);return bytes(d)

    def write(self,w:RelocatingZoneWriter,ctx:GraphContext):
        c=self.clip
        if w.current_block_index!=TEMP_BLOCK:raise ValueError('ClipMap root must be TEMP block 0')
        local_name=nested_symbol(self.symbol,'name');local_planes=nested_symbol(self.symbol,'planes');name=self.shared_name_symbol or local_name;planes=self.shared_planes_symbol or local_planes;sides=nested_symbol(self.symbol,'brushSides');edges=nested_symbol(self.symbol,'brushEdges');rootbrush=nested_symbol(self.symbol,'rootLeafBrushes');borders=nested_symbol(self.symbol,'borders');mapents=nested_symbol(self.symbol,'mapEnts')
        rp=w.physical_position;rl=w.define_symbol(self.symbol);ctx.register_root(self.symbol,rp);w.write_in_block(self._root(),'ClipMap root');w.register_pointer_relocation(rp,name,description='ClipMap.name shared B4 alias' if self.shared_name_symbol else 'ClipMap.name');w.register_pointer_relocation(rp+0x0c,planes,description='ClipMap.planes shared GfxWorld B4 alias' if self.shared_planes_symbol else 'ClipMap.planes')

        # C09 keeps the complete persistent collision stream in B4. Its name and plane array are
        # exact aliases to the already loaded GfxWorld allocations, so the first physical byte after
        # the TEMP ClipMap root is staticModel[0]. Standalone fixtures may omit those shared symbols
        # and exercise the local fallback path.
        logical={}
        def mark(label):
            loc=w.current_location;logical[label]={'block':loc.block,'offset':loc.offset};return loc
        w.push_block(LARGE_BLOCK)
        np=-1;pp=-1
        if self.shared_name_symbol is None:
            np=w.physical_position;w.define_symbol(local_name);mark('name');w.write_latin1z(c.name,'ClipMap name')
        if self.shared_planes_symbol is None:
            w.align_logical_only(4);pp=w.physical_position;w.define_symbol(local_planes);mark('planes')
            for p in c.planes:
                d=bytearray(PLANE_SIZE);v3be(d,0,p.normal);f32be(d,0x0c,p.distance);d[0x10]=p.type;d[0x11]=ps3_plane_signbits(p.normal,p.type);w.write_in_block(d,'ClipMap plane')

        w.align_logical_only(4);smp=w.physical_position;mark('staticModels')
        for i,m in enumerate(c.static_models):
            ph=w.physical_position;d=bytearray(STATIC_MODEL_SIZE);u16be(d,0,m.next_sector);v3be(d,8,m.origin)
            for j,x in enumerate(m.axis):f32be(d,0x14+j*4,x)
            v3be(d,0x38,m.minimum);v3be(d,0x44,m.maximum);w.write_in_block(d,'ClipMap static model');t=self._target(ctx,m.xmodel_raw,AssetType.XMODEL)
            if t:w.register_pointer_relocation(ph+4,t,description=f'ClipMap.static[{i}].xmodel')
        mp=w.physical_position
        for m in c.materials:
            d=bytearray(MATERIAL_SIZE);b=m.name.encode('latin-1');
            if len(b)>=64:raise ValueError('ClipMap material name >=64 bytes')
            d[:len(b)]=b;i32be(d,0x40,m.surface_flags);i32be(d,0x44,m.content_flags);w.write_in_block(d,'ClipMap material')

        w.align_logical_only(4);sp=w.physical_position;w.define_symbol(sides);mark('brushSides')
        for i,s in enumerate(c.brush_sides):
            ph=w.physical_position;d=bytearray(BRUSH_SIDE_SIZE);u32be(d,4,s.material_index);i16be(d,8,s.adjacent);d[0x0a]=s.edge_count;w.write_in_block(d,'ClipMap brush side');w.register_pointer_relocation(ph,planes,s.plane_index*PLANE_SIZE,description=f'brushSide[{i}].plane')
        ep=w.physical_position;w.define_symbol(edges);w.write_in_block(c.brush_edges,'ClipMap brush edges')

        w.align_logical_only(4);nodep=w.physical_position;mark('nodes')
        for i,n in enumerate(c.nodes):
            ph=w.physical_position;d=bytearray(NODE_SIZE);i16be(d,4,n.front);i16be(d,6,n.back);w.write_in_block(d,'ClipMap node');w.register_pointer_relocation(ph,planes,n.plane_index*PLANE_SIZE,description=f'node[{i}].plane')
        leafp=w.physical_position
        for x in c.leaves:
            d=bytearray(LEAF_SIZE);u16be(d,0,x.first_aabb);u16be(d,2,x.aabb_count);i32be(d,4,x.brush_contents);i32be(d,8,x.terrain_contents);v3be(d,0x0c,x.minimum);v3be(d,0x18,x.maximum);i32be(d,0x24,x.leaf_brush_node);i16be(d,0x28,x.cluster);w.write_in_block(d,'ClipMap leaf')

        w.align_logical_only(2);rlbp=w.physical_position;w.define_symbol(rootbrush);mark('rootLeafBrushes')
        for x in c.root_leaf_brushes:w.write_u16(x,'root leaf brush')
        w.align_logical_only(4);lnp=w.physical_position;mark('leafBrushNodes')
        for i,x in enumerate(c.leaf_brush_nodes):
            ph=w.physical_position;d=bytearray(LEAF_BRUSH_NODE_SIZE);d[0]=x.axis;i16be(d,2,x.count);i32be(d,4,x.contents)
            if x.count<=0:f32be(d,8,x.distance);f32be(d,0x0c,x.range);u16be(d,0x10,x.front);u16be(d,0x12,x.back)
            elif x.storage=='inline':u32be(d,8,FOLLOWING)
            w.write_in_block(d,'leaf brush node')
            if x.count>0 and x.storage=='packed_root':w.register_pointer_relocation(ph+8,rootbrush,x.root_start*2,description=f'leafNode[{i}].brushes')
        ilbp=w.physical_position
        for x in c.leaf_brush_nodes:
            if x.storage=='inline':
                for bi in x.brush_indices:w.write_u16(bi,'inline leaf brush')
        w.align_logical_only(4);vp=w.physical_position;mark('vertices')
        for v in c.vertices:
            d=bytearray(VERTEX_SIZE);v3be(d,0,v);w.write_in_block(d,'ClipMap vertex')
        tip=w.physical_position
        for t in c.triangle_indices:w.write_u16(t,'triangle index')
        walkp=w.physical_position;w.write_in_block(c.walkability,'triangle-edge walkability')

        w.align_logical_only(4);bp=w.physical_position;w.define_symbol(borders);mark('borders')
        for b in c.borders:
            d=bytearray(BORDER_SIZE);v3be(d,0,b.equation);f32be(d,0x0c,b.zbase);f32be(d,0x10,b.zslope);f32be(d,0x14,b.start);f32be(d,0x18,b.length);w.write_in_block(d,'ClipMap border')
        partp=w.physical_position
        for i,p in enumerate(c.partitions):
            ph=w.physical_position;d=bytearray(PARTITION_SIZE);d[0]=p.triangle_count;d[1]=p.border_count;i32be(d,4,p.first_triangle);w.write_in_block(d,'ClipMap partition')
            if p.border_count:w.register_pointer_relocation(ph+8,borders,p.first_border*BORDER_SIZE,description=f'partition[{i}].borders')
        aabbp=w.physical_position
        for a in c.aabbs:
            d=bytearray(AABB_SIZE);v3be(d,0,a.origin);v3be(d,0x0c,a.half);u16be(d,0x18,a.material_index);u16be(d,0x1a,a.child_count);i32be(d,0x1c,a.child_or_partition);w.write_in_block(d,'ClipMap AABB')
        subp=w.physical_position
        for s in c.submodels:w.write_in_block(_submodel_bytes(s),'ClipMap submodel')
        w.align_logical_only(16);brushp=w.physical_position;mark('brushes')
        for i,b in enumerate(c.brushes):
            ph=w.physical_position;w.write_in_block(_brush_bytes(b),'ClipMap brush')
            if b.non_axial:w.register_pointer_relocation(ph+0x20,sides,b.first_side*BRUSH_SIDE_SIZE,description=f'brush[{i}].sides')
            if c.brush_edges:
                edge_idx=0 if b.base_edge<0 else b.base_edge;w.register_pointer_relocation(ph+0x30,edges,edge_idx,description=f'brush[{i}].edges')
        visp=w.physical_position
        if c.visibility:w.write_in_block(c.visibility,'ClipMap visibility')

        # clipMap_t::mapEnts is an inline XAsset.  Its root allocator switches to
        # TEMP, while Load_MapEnts immediately returns to default-normal B4 for
        # name/entityString.  The switch changes logical destinations only.
        mapents_insert=w.define_insert_pointer_alias(nested_symbol(self.symbol,'insert.mapEnts'),description='ClipMap MapEnts InsertPointer alias')
        w.push_block(TEMP_BLOCK);w.align_logical_only(4);mep=w.physical_position;w.define_symbol(mapents);mark('mapEnts');d=bytearray(MAPENTS_ROOT);u32be(d,4,FOLLOWING);u32be(d,8,c.mapents_allocated);w.write_in_block(d,'MapEnts TEMP asset root');w.register_pointer_relocation(mep,name,description='MapEnts.name shared B4 alias' if self.shared_name_symbol else 'MapEnts.name');w.pop_block()
        entp=w.physical_position;mark('entityString');ent=c.mapents_text.encode('latin-1')+b'\0';w.write_in_block(ent,'MapEnts text')
        if c.mapents_allocated<len(ent):raise ValueError('MapEnts allocated smaller than text')
        if c.mapents_allocated>len(ent):w.reserve_in_block(c.mapents_allocated-len(ent),'MapEnts capacity')
        w.align_logical_only(16);boxp=w.physical_position;mark('boxBrush');w.write_in_block(_brush_bytes(c.box_brush,True),'ClipMap box brush')
        if c.dyn_models:w.align_logical_only(4)
        dmp=w.physical_position;mark('dynModels');dyn_stats=self._write_dyn(w,ctx,c.dyn_models,'model')
        owned_rows=self._write_owned_phys(w,'model',mark)
        if c.dyn_brushes:w.align_logical_only(4)
        dbp=w.physical_position;mark('dynBrushes');brush_dyn_stats=self._write_dyn(w,ctx,c.dyn_brushes,'brush')
        owned_rows.extend(self._write_owned_phys(w,'brush',mark))
        w.pop_block()

        # Runtime-only DynEnt state lives in runtime block 1 and emits no source bytes.
        runtime=[];w.push_block(1)
        for label,n,size in (('modelPose',len(c.dyn_models),POSE_SIZE),('brushPose',len(c.dyn_brushes),POSE_SIZE),('modelClient',len(c.dyn_models),CLIENT_SIZE),('brushClient',len(c.dyn_brushes),CLIENT_SIZE),('modelCollision',len(c.dyn_models),COLLISION_SIZE),('brushCollision',len(c.dyn_brushes),COLLISION_SIZE)):
            if n:w.align_logical_only(4);loc=w.current_location;w.reserve_in_block(n*size,'ClipMap runtime '+label);runtime.append((label,loc))
        w.pop_block()
        phys_stats={key:dyn_stats.get(key,0)+brush_dyn_stats.get(key,0) for key in set(dyn_stats)|set(brush_dyn_stats)}
        phys_stats.update({'owned_roots':len(owned_rows),'owned_names':[row['name'] for row in owned_rows],'owned_rows':owned_rows,'source_nonnull':sum(bool(e.phys_preset_raw) for e in c.dyn_models+c.dyn_brushes),'destination_null':0})
        ctx.register_result('clipmap:'+self.symbol,{'root_physical':rp,'root_location':rl,'name_physical':np,'plane_physical':pp,'shared_name_symbol':self.shared_name_symbol,'shared_planes_symbol':self.shared_planes_symbol,'static_model_physical':smp,'material_physical':mp,'brush_side_physical':sp,'brush_edge_physical':ep,'node_physical':nodep,'leaf_physical':leafp,'root_leaf_brush_physical':rlbp,'leaf_node_physical':lnp,'inline_leaf_brush_physical':ilbp,'vertex_physical':vp,'triangle_physical':tip,'walkability_physical':walkp,'border_physical':bp,'partition_physical':partp,'aabb_physical':aabbp,'submodel_physical':subp,'brush_physical':brushp,'visibility_physical':visp,'mapents_physical':mep,'mapents_insert_alias_location':mapents_insert,'entity_physical':entp,'box_brush_physical':boxp,'dyn_model_physical':dmp,'dyn_brush_physical':dbp,'logical_allocations':logical,'physpreset_bindings':phys_stats,'runtime':runtime,'planes':len(c.planes),'static_models':len(c.static_models),'brushes':len(c.brushes),'triangles':c.triangle_count,'clusters':c.cluster_count,'visibility_bytes':len(c.visibility)})

    def _write_owned_phys(self,w:RelocatingZoneWriter,list_kind:str,mark):
        rows=[]
        for owner in (x for x in self.clip.dyn_owned_phys_presets if x.list_kind==list_kind):
            # Recursive DynEntityDef asset-pointer processing occurs immediately
            # after each list's complete root array. INSERT creates its persistent
            # B4 DB alias before the nested TEMP PhysPreset root is loaded.
            if owner.pointer_kind=='insert':
                w.define_insert_pointer_alias(self._owned_phys_symbol(owner,'insert'),description=f'DynEnt {list_kind}[{owner.index}] PhysPreset InsertPointer alias')
            w.push_block(TEMP_BLOCK);w.align_logical_only(4);root_loc=w.current_location;root_phys=w.physical_position
            w.define_symbol(self._owned_phys_symbol(owner,'root'));mark(f'ownedPhys.{owner.list_kind}.{owner.index}')
            p=owner.preset;w.write_in_block(build_phys_preset_root(p),f'DynEnt {owner.list_kind}[{owner.index}] owned PhysPreset')
            w.pop_block()
            w.write_latin1z(p.name,f'DynEnt {owner.list_kind}[{owner.index}] owned PhysPreset name')
            w.write_latin1z(p.sound_alias_prefix or '',f'DynEnt {owner.list_kind}[{owner.index}] owned PhysPreset sound')
            rows.append({'list_kind':owner.list_kind,'index':owner.index,'name':p.name,'pointer_kind':owner.pointer_kind,'root_physical':root_phys,'root_location':{'block':root_loc.block,'offset':root_loc.offset}})
        return rows

    def _write_dyn(self,w,ctx:GraphContext,entities:tuple[DynEnt,...],category:str):
        owners={(x.list_kind,x.index):x for x in self.clip.dyn_owned_phys_presets}
        stats={'bindings':0,'owned_fields':0,'dynent_alias_uses':0,'xmodel_alias_uses':0,'top_level_uses':0}
        for i,e in enumerate(entities):
            ph=w.physical_position;loc=w.current_location;d=bytearray(DYNDEF_SIZE);i32be(d,0,e.type);v4be(d,4,e.quaternion);v3be(d,0x14,e.origin);u16be(d,0x24,e.brush_model);u16be(d,0x26,e.physics_brush_model);i32be(d,0x34,e.health);v3be(d,0x38,e.center_mass);v3be(d,0x44,e.moments);v3be(d,0x50,e.products);i32be(d,0x5c,e.contents)
            owner=owners.get((category,i))
            if owner is not None:
                u32be(d,0x30,INSERT if owner.pointer_kind=='insert' else FOLLOWING)
                w.define_symbol_at(self._owned_phys_symbol(owner,'field'),PackedOffset(loc.block,loc.offset+0x30))
                stats['owned_fields']+=1
            w.write_in_block(d,f'DynEnt {category}[{i}]')
            for off,raw,typ in ((0x20,e.xmodel_raw,AssetType.XMODEL),(0x28,e.destroy_fx_raw,AssetType.FX),(0x2c,e.destroy_pieces_raw,AssetType.XMODELPIECES),(0x30,e.phys_preset_raw,AssetType.PHYSPRESET)):
                if typ==AssetType.PHYSPRESET and owner is not None:stats['bindings']+=1;continue
                t=self._target(ctx,raw,typ)
                if t:
                    w.register_pointer_relocation(ph+off,t,description=f'DynEnt {category}[{i}].{typ.name}')
                    if typ==AssetType.PHYSPRESET:
                        stats['bindings']+=1
                        if raw in self.dynent_physpreset_alias_symbols_by_raw:stats['dynent_alias_uses']+=1
                        elif raw in self.xmodel_physpreset_alias_symbols_by_raw:stats['xmodel_alias_uses']+=1
                        else:stats['top_level_uses']+=1
                elif typ==AssetType.PHYSPRESET and raw:
                    raise ValueError(f'DynEnt {category}[{i}] non-null PhysPreset became NULL')
        return stats


def decode_kind(raw:int)->str:
    if raw==NULL:return 'null'
    if raw==FOLLOWING:return 'following'
    if raw==INSERT:return 'insert'
    return 'packed'
