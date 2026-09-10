from __future__ import annotations
from dataclasses import dataclass,field
import struct
from ..graph import AssetType,GraphContext,Dependency
from ..zone_writer import RelocatingZoneWriter,PackedOffset,TEMP_BLOCK,LARGE_BLOCK,RUNTIME_BLOCK,FOLLOWING,NULL

# Load_Material: 'if (material->0x6C) { material->0x6C = DB_AllocStreamPos(1);
#   Load_uint16Array(true, 26); }'  Retail PS3 writes 0xFFFFFFFF into the field.
PS3_MATERIAL_TECHNIQUE_SLOTS=26
PS3_MATERIAL_RUNTIME_BYTES=PS3_MATERIAL_TECHNIQUE_SLOTS*2
PS3_MATERIAL_RUNTIME_ALIGN=2
PS3_MATERIAL_RUNTIME_MARKER=0xFFFFFFFF

# Retail PS3 stores MaterialInfo.drawSurf as the little-endian image of the packed 64-bit
# GfxDrawSurf, i.e. the PC bytes pass through unswapped.  Byte-reversing the Retail PS3
# mp_shipment values decodes to exactly our field values for all 32 name-matched materials
# (every field but the per-build materialSortedIndex) and yields
# drawSurf.primarySortKey == MaterialInfo.sortKey for all 254 Retail materials; no other
# byte order does.  The GfxDrawSurf bit layout itself is shared with PC and was read out of
# the PS3 draw-surf builder at 0x313EB0 (rldicl 10,58 -> primarySortKey at bits 54..59,
# rldimi 50,10 -> surfType at 50..53, 0,48 -> objectId, 16,40 -> reflectionProbeIndex,
# 24,35 -> customIndex).
PS3_MATERIAL_DRAWSURF_BYTE_ORDER='<'
from ..material import MaterialRecord,MaterialStateConversion

@dataclass
class MaterialNode:
    source:MaterialRecord;state:MaterialStateConversion;platform_state:bytes;technique_symbol:str;image_symbols:tuple[str,...];symbol:str
    type:AssetType=field(init=False,default=AssetType.MATERIAL);root_block:int=field(init=False,default=TEMP_BLOCK)
    def __post_init__(self):
        if len(self.platform_state)!=0x38:raise ValueError('platform_state must be exactly 0x38 bytes')
        if len(self.source.textures)!=len(self.image_symbols):raise ValueError('texture/image cardinality mismatch')
        if self.state.invalid_pc_state_indices or self.state.neutralized_out_of_range:raise ValueError('material has invalid/neutralized state')
    @property
    def dependencies(self):
        d=[Dependency(self.technique_symbol,AssetType.TECHSET,f"Material {self.source.name} TechniqueSet")]
        d.extend(Dependency(s,AssetType.IMAGE,f"Material {self.source.name} image") for s in self.image_symbols);return tuple(d)
    def _root(self):
        s=self.source;st=self.state;r=bytearray(0x80);struct.pack_into('>I',r,0,FOLLOWING);r[4]=s.game_flags;r[5]=s.sort_key;r[6]=s.atlas_rows;r[7]=s.atlas_cols;struct.pack_into('<Q',r,8,s.draw_surf);struct.pack_into('>I',r,0x10,s.surface_type_bits);struct.pack_into('>H',r,0x14,s.hash_index);r[0x18:0x32]=st.state_bits_entry;r[0x32]=len(s.textures);r[0x33]=len(s.constants);r[0x34]=len(st.state_bits);r[0x35]=s.state_flags;r[0x36]=s.camera_region;r[0x38:0x70]=self.platform_state;struct.pack_into('>I',r,0x6C,PS3_MATERIAL_RUNTIME_MARKER);struct.pack_into('>I',r,0x70,NULL);struct.pack_into('>I',r,0x74,FOLLOWING if s.textures else NULL);struct.pack_into('>I',r,0x78,FOLLOWING if s.constants else NULL);struct.pack_into('>I',r,0x7C,FOLLOWING if st.state_bits else NULL);return bytes(r)
    def write(self,w:RelocatingZoneWriter,c:GraphContext):
        s=self.source;rp=w.physical_position;rl=w.define_symbol(self.symbol);c.register_root(self.symbol,rp);w.write_in_block(self._root(),f'Material {s.name} root')
        # Load_Material allocates a 26-entry uint16 runtime table from the RUNTIME block
        # whenever Material+0x6C is non-zero, and Material_Register (reached through
        # DB_LinkXAsset for every Material) stores into *(Material+0x6C) + i*2 for every
        # technique whose +6 field is 2.  A NULL there is a store through a null pointer,
        # so the marker is written unconditionally and the runtime bytes are budgeted.
        w.push_block(RUNTIME_BLOCK);w.align_logical_only(PS3_MATERIAL_RUNTIME_ALIGN)
        w.reserve_in_block(PS3_MATERIAL_RUNTIME_BYTES,f'Material {s.name} technique runtime table');w.pop_block()
        w.register_pointer_relocation(rp+0x70,c.reference_symbol(self.technique_symbol),kind='packed_alias',description='Material.techniqueSet')
        w.push_block(LARGE_BLOCK);name_location=w.current_location;w.write_latin1z(s.name,'Material name');w.pop_block()
        texphys=-1;owned_images=[]
        if s.textures:
            w.push_block(LARGE_BLOCK);w.align_logical_only(4);texphys=w.physical_position
            # The loader first consumes the complete MaterialTextureDef array.  Image/water
            # pointees are then consumed in texture-index order, so ownership sites are recorded
            # now and emitted in the second pass below.
            for i,t in enumerate(s.textures):
                rec=bytearray(0x0C);struct.pack_into('>I',rec,0,t.name_hash);rec[4]=t.name_start;rec[5]=t.name_end;rec[6]=t.sampler;rec[7]=t.semantic;physical=w.physical_position;logical=w.current_location
                image_symbol=self.image_symbols[i];water=i in s.water_by_texture
                if water:
                    struct.pack_into('>I',rec,8,FOLLOWING)
                else:
                    marker=c.owned_marker(self.symbol,image_symbol,f'texture[{i}]')
                    if marker is not None:
                        site=f'texture[{i}]';struct.pack_into('>I',rec,8,marker);owned_images.append((i,image_symbol,site));c.bind_owned_pointer_field(w,self.symbol,image_symbol,site,PackedOffset(logical.block,logical.offset+8))
                    else:
                        struct.pack_into('>I',rec,8,NULL)
                w.write_in_block(bytes(rec),f'Material texture {i}')
                if not water and not c.owns_at(self.symbol,image_symbol,f'texture[{i}]'):
                    w.register_pointer_relocation(physical+8,c.reference_symbol(image_symbol),kind='packed_alias',description='Material texture image')

            owned_by_site={site:(idx,sym) for idx,sym,site in owned_images}
            water_owned=[]
            for i in range(len(s.textures)):
                image_symbol=self.image_symbols[i]
                if i not in s.water_by_texture:
                    site=f'texture[{i}]'
                    if site in owned_by_site:
                        c.write_owned_child(w,self.symbol,image_symbol,site)
                    continue
                water=s.water_by_texture[i]
                if len(water.scalars)!=11:raise ValueError('water scalar count != 11')
                n=water.m*water.n
                if len(water.h0)!=n or len(water.wterm)!=n:raise ValueError('water payload cardinality mismatch')
                w.align_logical_only(4);wp=w.physical_position;wl=w.current_location;root=bytearray(0x48);struct.pack_into('>f',root,0,water.float_time);struct.pack_into('>I',root,4,FOLLOWING if water.h0 else NULL);struct.pack_into('>I',root,8,FOLLOWING if water.h0 else NULL);struct.pack_into('>I',root,0x0C,FOLLOWING if water.wterm else NULL);struct.pack_into('>ii',root,0x10,water.m,water.n)
                for j,v in enumerate(water.scalars):struct.pack_into('>f',root,0x18+j*4,v)
                site=f'water[{i}]';marker=c.owned_marker(self.symbol,image_symbol,site)
                if marker is not None:struct.pack_into('>I',root,0x44,marker);c.bind_owned_pointer_field(w,self.symbol,image_symbol,site,PackedOffset(wl.block,wl.offset+0x44))
                else:struct.pack_into('>I',root,0x44,NULL)
                w.write_in_block(bytes(root),f'water {i} root')
                if marker is None:w.register_pointer_relocation(wp+0x44,c.reference_symbol(image_symbol),kind='packed_alias',description='water image')
                # PS3 water_t splits the PC complex H0 array into two planar float arrays:
                # +0x04 real[M*N], +0x08 imaginary[M*N], +0x0C wTerm[M*N].  Load_water reads
                # three M*N float arrays, and de-interleaving our H0 reproduces the Retail PS3
                # mp_shipment arrays byte for byte (wTerm was already identical).
                for v in water.h0:w.write_f32(v.real,'H0.real')
                for v in water.h0:w.write_f32(v.imaginary,'H0.imag')
                for v in water.wterm:w.write_f32(v,'wTerm')
                if marker is not None:
                    c.write_owned_child(w,self.symbol,image_symbol,site);water_owned.append(site)
            w.pop_block()
        constphys=-1
        if s.constants:
            w.push_block(LARGE_BLOCK);w.align_logical_only(16);constphys=w.physical_position
            for pc in s.constants:
                if len(pc)!=0x20:raise ValueError('constant size')
                ps=bytearray(0x20);struct.pack_into('>I',ps,0,struct.unpack_from('<I',pc,0)[0]);ps[4:16]=pc[4:16]
                for j in range(4):struct.pack_into('>I',ps,16+j*4,struct.unpack_from('<I',pc,16+j*4)[0])
                w.write_in_block(bytes(ps),'material constant')
            w.pop_block()
        statephys=-1;state_object_locations=[]
        if self.state.state_bits:
            w.push_block(LARGE_BLOCK);w.align_logical_only(4);statephys=w.physical_position
            for _ in self.state.state_bits:w.write_u32(FOLLOWING,'state pointer')
            # The pointer array is persistent B4, but each pointed-to 8-byte
            # GfxStateBits value is a nested TEMP allocation. Sibling values
            # reuse the same temporary address while remaining physically inline.
            for a,b in self.state.state_bits:
                w.push_block(TEMP_BLOCK);w.align_logical_only(4);state_object_locations.append(w.current_location)
                w.write_u32(a,'state a');w.write_u32(b,'state b');w.pop_block()
            w.pop_block()
        c.register_result('material:'+self.symbol,{'root_physical':rp,'name_location':name_location,'texture_table_physical':texphys,'constant_table_physical':constphys,'state_pointer_table_physical':statephys,'state_object_locations':tuple(state_object_locations),'root_location':rl,'texture_count':len(s.textures),'state_count':len(self.state.state_bits),'owned_image_sites':[site for _,_,site in owned_images]+(water_owned if s.textures else [])})
