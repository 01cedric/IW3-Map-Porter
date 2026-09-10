from __future__ import annotations
from dataclasses import dataclass,field
import math,struct

from .backend.v4_ffio import decode_pc_pointer
from .graph import AssetType,Dependency,GraphContext,nested_symbol
from .zone_writer import RelocatingZoneWriter,TEMP_BLOCK,LARGE_BLOCK,FOLLOWING,NULL

COMWORLD_ROOT_SIZE=0x10
PRIMARY_LIGHT_SIZE=0x44
GAMEWORLD_MP_ROOT_SIZE=0x04

def u32(z,o):return struct.unpack_from('<I',z,o)[0]
def i32(z,o):return struct.unpack_from('<i',z,o)[0]
def f32(z,o):return struct.unpack_from('<f',z,o)[0]
def _inline(raw):return decode_pc_pointer(raw).kind in ('following','insert')

def read_z(z,c,max_bytes=4096):
    e=z.find(b'\0',c,min(len(z),c+max_bytes))
    if e<0:raise ValueError('unterminated world string')
    return z[c:e].decode('latin-1'),e+1

@dataclass(frozen=True)
class ComPrimaryLight:
    index:int;type:int;can_use_shadow_map:int;exponent:int;unused:int;scalars:tuple[float,...];def_name_pointer:int;def_name:str|None

@dataclass(frozen=True)
class ComWorld:
    root_offset:int;physical_end_offset:int;name:str;is_in_use:int;primary_lights:tuple[ComPrimaryLight,...]
    @property
    def primary_light_count(self):return len(self.primary_lights)
    @property
    def packed_def_name_count(self):return sum(decode_pc_pointer(x.def_name_pointer).kind=='packed' for x in self.primary_lights)
    @property
    def resolved_packed_def_name_count(self):
        return sum(decode_pc_pointer(x.def_name_pointer).kind=='packed' and x.def_name is not None for x in self.primary_lights)


def _resolve_repeated_primary_light_def_names(lights:tuple[ComPrimaryLight,...])->tuple[ComPrimaryLight,...]:
    """Bind the common IW3 ``one inline XString, then packed reuses`` pattern.

    This is intentionally narrower than a generic packed-pointer guess.  We accept exactly one
    inline owner followed only by repetitions of exactly one packed raw word.  Getaway uses this
    for lights 2..101: light 2 owns ``light_point_linear`` and lights 3..101 point back to it.
    Retaining the resolved value lets the PS3 writer recreate real block-4 aliases instead of
    silently turning source-nonnull fields into NULL.
    """
    inline=[x for x in lights if _inline(x.def_name_pointer) and x.def_name is not None]
    packed=[x for x in lights if decode_pc_pointer(x.def_name_pointer).kind=='packed']
    if not packed:
        return lights
    if len(inline)!=1 or len({x.def_name_pointer for x in packed})!=1:
        return lights
    anchor=inline[0]
    if any(x.index<=anchor.index for x in packed):
        return lights
    nonnull_after=[x for x in lights if x.index>anchor.index and decode_pc_pointer(x.def_name_pointer).kind!='null']
    if not nonnull_after or any(decode_pc_pointer(x.def_name_pointer).kind!='packed' for x in nonnull_after):
        return lights
    return tuple(
        ComPrimaryLight(x.index,x.type,x.can_use_shadow_map,x.exponent,x.unused,x.scalars,x.def_name_pointer,anchor.def_name)
        if decode_pc_pointer(x.def_name_pointer).kind=='packed' else x
        for x in lights
    )


def parse_comworld(z:bytes,expected_name:str,expected_primary_light_count:int)->ComWorld:
    needle=expected_name.encode('latin-1')+b'\0';candidates=[];search=0
    while True:
        no=z.find(needle,search)
        if no<0:break
        search=no+1;root=no-COMWORLD_ROOT_SIZE
        try:
            if root<0 or root+COMWORLD_ROOT_SIZE>len(z):continue
            if not _inline(u32(z,root)):continue
            inuse=i32(z,root+4);count=i32(z,root+8);lp=u32(z,root+0xc)
            if inuse not in (0,1) or count!=expected_primary_light_count:continue
            if count==0 and decode_pc_pointer(lp).kind!='null':continue
            if count and not _inline(lp):continue
            cur=root+COMWORLD_ROOT_SIZE
            if cur!=no:continue
            name,cur=read_z(z,cur)
            if name.lower()!=expected_name.lower():continue
            arr=cur;n=count*PRIMARY_LIGHT_SIZE
            if arr+n>len(z):continue
            lights=[];ok=True
            for idx in range(count):
                lr=arr+idx*PRIMARY_LIGHT_SIZE;shadow=z[lr+1]
                if shadow not in (0,1):ok=False;break
                scal=tuple(f32(z,lr+4+j*4) for j in range(15))
                if not all(math.isfinite(v) for v in scal):ok=False;break
                dp=u32(z,lr+0x40);kind=decode_pc_pointer(dp).kind
                if kind not in ('null','following','insert','packed'):ok=False;break
                lights.append(ComPrimaryLight(idx,z[lr],shadow,z[lr+2],z[lr+3],scal,dp,None))
            if not ok:continue
            cur=arr+n;updated=[]
            for light in lights:
                if _inline(light.def_name_pointer):
                    dn,cur=read_z(z,cur);updated.append(ComPrimaryLight(light.index,light.type,light.can_use_shadow_map,light.exponent,light.unused,light.scalars,light.def_name_pointer,dn))
                else:updated.append(light)
            candidates.append(ComWorld(root,cur,name,inuse,_resolve_repeated_primary_light_def_names(tuple(updated))))
        except Exception:continue
    if len(candidates)!=1:raise ValueError(f'ComWorld scan expected 1 candidate, got {len(candidates)}')
    return candidates[0]

@dataclass
class ComWorldNode:
    source:ComWorld;symbol:str
    type:AssetType=field(init=False,default=AssetType.COMWORLD);root_block:int=field(init=False,default=TEMP_BLOCK);dependencies:tuple=field(init=False,default=())
    def write(self,w:RelocatingZoneWriter,c:GraphContext):
        rp=w.physical_position;rl=w.define_symbol(self.symbol);c.register_root(self.symbol,rp);root=bytearray(COMWORLD_ROOT_SIZE);struct.pack_into('>IiiI',root,0,FOLLOWING,self.source.is_in_use,self.source.primary_light_count,FOLLOWING if self.source.primary_lights else NULL);w.write_in_block(root,'ComWorld root')
        w.push_block(LARGE_BLOCK);name_phys=w.physical_position;name_loc=w.define_symbol(self.symbol+'::name');w.write_latin1z(self.source.name,'ComWorld name')
        if self.source.primary_lights:w.align_logical_only(4)
        packed_fields=[]
        for light in self.source.primary_lights:
            b=bytearray(PRIMARY_LIGHT_SIZE);b[0:4]=bytes((light.type,light.can_use_shadow_map,light.exponent,light.unused))
            for j,v in enumerate(light.scalars):struct.pack_into('>f',b,4+j*4,v)
            kind=decode_pc_pointer(light.def_name_pointer).kind
            if kind=='null':marker=NULL
            elif kind in ('following','insert') and light.def_name is not None:marker=FOLLOWING
            elif kind=='packed' and light.def_name is not None:marker=NULL
            else:raise ValueError(f'ComWorld light {light.index} source-nonnull defName has no exact XString identity')
            field=w.physical_position+0x40
            struct.pack_into('>I',b,0x40,marker);w.write_in_block(b,f'ComWorld light {light.index}')
            if kind=='packed':packed_fields.append((field,light))
        inline_symbols={}
        inline_names=[]
        for light in self.source.primary_lights:
            kind=decode_pc_pointer(light.def_name_pointer).kind
            if kind not in ('following','insert') or light.def_name is None:continue
            string_symbol=nested_symbol(self.symbol,f'primaryLight[{light.index}].defName')
            w.define_symbol(string_symbol);w.write_latin1z(light.def_name,f'ComWorld light {light.index} defName')
            inline_symbols.setdefault(light.def_name,string_symbol);inline_names.append(light.def_name)
        w.pop_block()
        for field,light in packed_fields:
            target=inline_symbols.get(light.def_name)
            if target is None:raise ValueError(f'ComWorld light {light.index} packed defName has no inline destination owner')
            w.register_pointer_relocation(field,target,description=f'ComWorld light {light.index}.defName B4 alias')
        c.register_result('comworld:'+self.symbol,{'root_physical':rp,'name_physical':name_phys,'root_location':rl,'name_location':name_loc,'primary_light_count':self.source.primary_light_count,'packed_def_names_source':self.source.packed_def_name_count,'packed_def_names_resolved':self.source.resolved_packed_def_name_count,'packed_def_name_relocations':len(packed_fields),'inline_def_names':tuple(inline_names)})

@dataclass
class GameWorldMpNode:
    asset_name:str;shared_world_name_symbol:str;symbol:str;shared_world_owner_symbol:str|None=None
    type:AssetType=field(init=False,default=AssetType.GAMEWORLD_MP);root_block:int=field(init=False,default=TEMP_BLOCK)
    @property
    def dependencies(self):
        return () if self.shared_world_owner_symbol is None else (Dependency(self.shared_world_owner_symbol,AssetType.GFXWORLD,'GameWorldMP shared GfxWorld name'),)
    def write(self,w:RelocatingZoneWriter,c:GraphContext):
        rp=w.physical_position;rl=w.define_symbol(self.symbol);c.register_root(self.symbol,rp);w.write_u32(0,'GameWorldMP name relocation slot');w.register_pointer_relocation(rp,self.shared_world_name_symbol,description='GameWorldMP shared map name');c.register_result('gameworldmp:'+self.symbol,{'root_physical':rp,'root_location':rl})
