from __future__ import annotations
from dataclasses import dataclass, field
from typing import Sequence
import hashlib, struct

from ..graph import AssetType, GraphContext, Dependency, nested_symbol
from ..zone_writer import RelocatingZoneWriter, PackedOffset, TEMP_BLOCK, VIRTUAL_BLOCK, LARGE_BLOCK, FOLLOWING, INSERT, NULL

@dataclass
class RawFileNode:
    name:str; data:bytes; symbol:str
    name_symbol:str|None=None
    name_owner_symbol:str|None=None
    type:AssetType=field(init=False,default=AssetType.RAWFILE); root_block:int=field(init=False,default=TEMP_BLOCK)
    @property
    def dependencies(self):
        return () if self.name_owner_symbol is None else (Dependency(self.name_owner_symbol,AssetType.STRINGTABLE,f"RawFile '{self.name}' shared StringTable XString"),)
    def write(self,w:RelocatingZoneWriter,c:GraphContext):
        root_phys=w.physical_position; root=w.define_symbol(self.symbol); c.register_root(self.symbol,root_phys)
        w.write_u32(NULL if self.name_symbol else FOLLOWING,'RawFile.name')
        w.write_u32(len(self.data),'RawFile.len');w.write_u32(FOLLOWING,'RawFile.buffer')
        if self.name_symbol:
            w.register_pointer_relocation(root_phys,self.name_symbol,description=f"RawFile '{self.name}' shared B4 XString")
            name_phys=-1;name_loc=None
        else:
            w.push_block(VIRTUAL_BLOCK);name_phys=w.physical_position;name_loc=w.current_location;w.write_latin1z(self.name,'RawFile name');w.pop_block()
        w.push_block(VIRTUAL_BLOCK);buf_phys=w.physical_position;buf_loc=w.current_location;w.write_in_block(self.data,'RawFile buffer');w.write_in_block(b'\0','RawFile terminator');w.pop_block()
        c.register_result('rawfile:'+self.symbol,{'root_physical':root_phys,'name_physical':name_phys,'name_location':name_loc,'name_symbol':self.name_symbol,'buffer_physical':buf_phys,'buffer_location':buf_loc,'length':len(self.data),'sha256':hashlib.sha256(self.data).hexdigest().upper(),'root_location':root})

@dataclass
class StringTableNode:
    name:str; columns:int; rows:int; values:Sequence[str|None]; symbol:str
    type:AssetType=field(init=False,default=AssetType.STRINGTABLE); root_block:int=field(init=False,default=LARGE_BLOCK); dependencies:tuple=field(init=False,default=())
    def __post_init__(self):
        if self.columns*self.rows != len(self.values): raise ValueError('StringTable shape mismatch')
    def cell_symbol(self,index:int)->str:
        if not 0<=index<len(self.values):raise IndexError('StringTable cell index outside table')
        return nested_symbol(self.symbol,f'cell[{index}]')
    def write(self,w,c):
        w.align_logical_only(4)
        root_phys=w.physical_position;root=w.define_symbol(self.symbol);c.register_root(self.symbol,root_phys)
        w.write_following_pointer('StringTable.name');w.write_u32(self.columns,'columns');w.write_u32(self.rows,'rows')
        w.write_null_pointer('values') if not self.values else w.write_following_pointer('values')
        name_phys=w.physical_position;w.write_latin1z(self.name,'StringTable name');w.align_logical_only(4);ptr_phys=w.physical_position
        for v in self.values: w.write_null_pointer('null cell') if v is None else w.write_following_pointer('cell')
        payload=w.physical_position
        for i,v in enumerate(self.values):
            if v is not None:
                w.define_symbol(self.cell_symbol(i));w.write_latin1z(v,f'cell {i}')
        c.register_result('stringtable:'+self.symbol,{'root_physical':root_phys,'name_physical':name_phys,'pointer_array_physical':ptr_phys,'payload_physical':payload,'root_location':root,'value_count':len(self.values)})

@dataclass
class ExternalFxNode:
    name:str; symbol:str
    type:AssetType=field(init=False,default=AssetType.FX);root_block:int=field(init=False,default=TEMP_BLOCK);dependencies:tuple=field(init=False,default=())
    def write(self,w,c):
        root_phys=w.physical_position;root=w.define_symbol(self.symbol);c.register_root(self.symbol,root_phys)
        payload=bytearray(0x20);struct.pack_into('>I',payload,0,FOLLOWING);w.write_in_block(bytes(payload),f'external FX {self.name}')
        w.push_block(VIRTUAL_BLOCK);name_phys=w.physical_position;name_loc=w.current_location;w.write_latin1z(','+self.name.lstrip(','),'external FX name');w.pop_block()
        c.register_result('fx.external:'+self.symbol,{'root_physical':root_phys,'name_physical':name_phys,'name_location':name_loc,'root_location':root})

@dataclass
class LightDefNode:
    name:str; sampler:int; lmap_lookup_start:int; attenuation_image_symbol:str|None; symbol:str
    attenuation_external_image_name:str|None=None
    attenuation_external_pointer_kind:str='insert'
    type:AssetType=field(init=False,default=AssetType.LIGHTDEF);root_block:int=field(init=False,default=TEMP_BLOCK)
    def __post_init__(self):
        if self.attenuation_image_symbol and self.attenuation_external_image_name:
            raise ValueError('LightDef attenuation cannot be both graph-owned and inline external')
        if self.attenuation_external_pointer_kind not in ('following','insert'):
            raise ValueError('LightDef inline external attenuation pointer must be FOLLOWING or INSERT')
    @property
    def dependencies(self):
        return () if not self.attenuation_image_symbol else (Dependency(self.attenuation_image_symbol,AssetType.IMAGE,f'LightDef {self.name} attenuation'),)
    def write(self,w,c):
        root_phys=w.physical_position;root=w.define_symbol(self.symbol);c.register_root(self.symbol,root_phys)
        rec=bytearray(0x10);struct.pack_into('>I',rec,0,FOLLOWING)
        owned=False
        if self.attenuation_external_image_name:
            struct.pack_into('>I',rec,4,INSERT if self.attenuation_external_pointer_kind=='insert' else FOLLOWING)
            owned=True
        elif self.attenuation_image_symbol:
            marker=c.owned_marker(self.symbol,self.attenuation_image_symbol,'attenuation')
            if marker is not None:
                struct.pack_into('>I',rec,4,marker);owned=True
            else:
                struct.pack_into('>I',rec,4,NULL)
        else:
            struct.pack_into('>I',rec,4,NULL)
        rec[8]=self.sampler;struct.pack_into('>i',rec,0x0C,self.lmap_lookup_start)
        if self.attenuation_image_symbol and owned:
            c.bind_owned_pointer_field(w,self.symbol,self.attenuation_image_symbol,'attenuation',PackedOffset(root.block,root.offset+4))
        w.write_in_block(bytes(rec),f'LightDef {self.name} root')
        w.push_block(VIRTUAL_BLOCK);name_location=w.current_location;w.write_latin1z(self.name,f'LightDef {self.name} name');w.pop_block()
        external_root_phys=-1;external_name_phys=-1;external_root_location=None
        if self.attenuation_external_image_name:
            # PS3 Shipment proves that the loader logically aligns this nested GfxImage root to
            # four bytes while the FastFile bytes stay physically adjacent to the LightDef name.
            w.align_logical_only(4)
            if self.attenuation_external_pointer_kind=='insert':
                w.define_insert_pointer_alias(
                    nested_symbol(self.symbol,'attenuation.insertAlias'),
                    canonical_identity='lightdef-attenuation:'+self.symbol,
                    description=f'LightDef {self.name} attenuation INSERT alias',
                )
            external_root_phys=w.physical_position
            external_root_location=w.define_symbol(nested_symbol(self.symbol,'attenuation.image'))
            shell=bytearray(0x34)
            struct.pack_into('>I',shell,0x30,FOLLOWING)
            w.write_in_block(bytes(shell),f'LightDef {self.name} external attenuation GfxImage shell')
            w.push_block(VIRTUAL_BLOCK);external_name_phys=w.physical_position;external_name_location=w.current_location
            w.write_latin1z(','+self.attenuation_external_image_name.lstrip(','),f'LightDef {self.name} attenuation image name');w.pop_block()
        elif self.attenuation_image_symbol:
            if owned:
                w.align_logical_only(4)
                c.write_owned_child(w,self.symbol,self.attenuation_image_symbol,'attenuation')
            else:
                w.register_pointer_relocation(root_phys+4,c.reference_symbol(self.attenuation_image_symbol),description='LightDef attenuation image')
        c.register_result('lightdef:'+self.symbol,{
            'root_physical':root_phys,'root_location':root,'attenuation_owned':owned,
            'name_location':name_location,
            'attenuation_external':self.attenuation_external_image_name is not None,
            'attenuation_external_root_physical':external_root_phys,
            'attenuation_external_name_physical':external_name_phys,
            'attenuation_external_name_location':external_name_location if self.attenuation_external_image_name else None,
            'attenuation_external_root_location':external_root_location,
        })
