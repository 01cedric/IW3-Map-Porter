from __future__ import annotations
from dataclasses import dataclass,field
import struct
from ..graph import AssetType,GraphContext,nested_symbol
from ..zone_writer import RelocatingZoneWriter,TEMP_BLOCK,VIRTUAL_BLOCK,INSERT,NULL,FOLLOWING

@dataclass
class ExternalTechniqueSetNode:
    name:str;symbol:str;prefix_shared:bool=True
    type:AssetType=field(init=False,default=AssetType.TECHSET);root_block:int=field(init=False,default=TEMP_BLOCK);dependencies:tuple=field(init=False,default=())
    @property
    def serialized_name(self):return (',' if self.prefix_shared else '')+self.name.lstrip(',')
    def write(self,w,c):
        rp=w.physical_position;rl=w.define_symbol(self.symbol);c.register_root(self.symbol,rp);root=bytearray(0x70);struct.pack_into('>I',root,0,FOLLOWING);w.write_in_block(bytes(root),f'external TechniqueSet {self.name}')
        # The root is TEMP; Load_MaterialTechniqueSet consumes its XString in
        # IW3's default-normal (VIRTUAL) block.
        w.push_block(VIRTUAL_BLOCK);np=w.physical_position;nl=w.define_symbol(nested_symbol(self.symbol,'name'));w.write_latin1z(self.serialized_name,'external TechniqueSet name');w.pop_block()
        c.register_result('techset.external:'+self.symbol,{'root_physical':rp,'name_physical':np,'root_location':rl,'name_location':nl,'serialized_name':self.serialized_name})

@dataclass
class ExternalXModelNode:
    """Retail name-only XModel shell, also usable for explicit boot isolation.

    Shared source models retain their name and resolve against the target asset
    database. An empty shell does not supply the missing model's geometry.
    """
    name:str;symbol:str;prefix_shared:bool=True
    type:AssetType=field(init=False,default=AssetType.XMODEL);root_block:int=field(init=False,default=TEMP_BLOCK);dependencies:tuple=field(init=False,default=())
    @property
    def serialized_name(self):return (',' if self.prefix_shared else '')+self.name.lstrip(',')
    def write(self,w,c):
        rp=w.physical_position;rl=w.define_symbol(self.symbol);c.register_root(self.symbol,rp)
        root=bytearray(0xCC);struct.pack_into('>I',root,0,FOLLOWING)
        w.write_in_block(bytes(root),f'external XModel {self.name}')
        # Load_XModel takes its XString in the default-normal (VIRTUAL) block, exactly as the
        # owned XModelNode does for the identical field.
        w.push_block(VIRTUAL_BLOCK);np=w.physical_position;nl=w.define_symbol(nested_symbol(self.symbol,'name'))
        w.write_latin1z(self.serialized_name,'external XModel name');w.pop_block()
        c.register_result('xmodel.external:'+self.symbol,{'root_physical':rp,'name_physical':np,'root_location':rl,'name_location':nl,'serialized_name':self.serialized_name})

@dataclass
class ExternalMaterialNode:
    name:str;symbol:str;prefix_shared:bool=True
    type:AssetType=field(init=False,default=AssetType.MATERIAL);root_block:int=field(init=False,default=TEMP_BLOCK);dependencies:tuple=field(init=False,default=())
    @property
    def serialized_name(self):return (',' if self.prefix_shared else '')+self.name.lstrip(',')
    def write(self,w,c):
        rp=w.physical_position;rl=w.define_symbol(self.symbol);c.register_root(self.symbol,rp);root=bytearray(0x80);struct.pack_into('>I',root,0,FOLLOWING);w.write_in_block(bytes(root),f'external Material {self.name}');w.push_block(w.options.insert_block_index);np=w.physical_position;nl=w.define_symbol(nested_symbol(self.symbol,'name'));w.write_latin1z(self.serialized_name,'external Material name');w.pop_block();c.register_result('material.external:'+self.symbol,{'root_physical':rp,'name_physical':np,'root_location':rl,'name_location':nl,'serialized_name':self.serialized_name})
