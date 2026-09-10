from __future__ import annotations

from dataclasses import dataclass, field
import hashlib
import math
import struct
from typing import Mapping, Sequence

from .graph import AssetType, Dependency, GraphContext, nested_symbol
from .zone_writer import RelocatingZoneWriter, PackedOffset, TEMP_BLOCK, VIRTUAL_BLOCK, LARGE_BLOCK, VERTEX_BLOCK, FOLLOWING, INSERT, NULL
from .physics import PhysPreset, PhysGeomList, build_phys_preset_root, write_inline_phys_geom_list
from .vertex_format import pack_rsx_unit_vector, ps3_vertex_color

PC_XMODEL_ROOT_SIZE=0xDC
PC_XSURFACE_ROOT_SIZE=0x38
PC_PACKED_VERTEX_SIZE=0x20
RIGID_VERT_LIST_SIZE=0x0C
COLLISION_TREE_ROOT_SIZE=0x28
COLLISION_NODE_SIZE=0x10
COLLISION_LEAF_SIZE=0x02
PS3_XMODEL_ROOT_SIZE=0xCC
PS3_XSURFACE_ROOT_SIZE=0x4C
PS3_XSURFACE_RUNTIME_WORD_OFFSET=0x38
PS3_XSURFACE_PART_BITS_OFFSET=0x3C
PC_MODEL_COLLISION_SURFACE_SIZE=0x2C
PS3_MODEL_COLLISION_SURFACE_SIZE=0x24
XBONE_INFO_SIZE=0x28

@dataclass(frozen=True)
class XModelLod:
    distance: float
    surface_count: int
    first_surface: int
    part_bits: Sequence[int]

@dataclass
class XSurface:
    tile_mode:int
    deformed:bool
    vertex_count:int
    triangle_count:int
    zone_handle:int
    blend_counts:Sequence[int]
    blend_element_count:int
    rigid_vert_list_count:int
    part_bits:Sequence[int]
    material_name:str
    vertex_source:int|None
    triangle_source:int|None
    blend_source:int|None=None
    rigid_source:int|None=None

@dataclass
class XModel:
    name:str
    num_bones:int
    num_root_bones:int
    lod_ramp_type:int
    num_lods:int
    collision_lod:int
    contents:int
    radius:float
    minimum:Sequence[float]
    maximum:Sequence[float]
    memory_usage:int
    flags:int
    bad:bool
    lods:Sequence[XModelLod]
    surfaces:Sequence[XSurface]
    skeleton_payloads:Mapping[str,tuple[int,int]]
    bone_info_source:int|None
    num_collision_surfaces:int=0
    collision_surface_source:int|None=None
    inline_phys_preset:PhysPreset|None=None
    inline_phys_geoms:PhysGeomList|None=None
    resolved_phys_preset_asset_index:int|None=None
    source_asset_index:int=-1

    @property
    def surface_count(self): return len(self.surfaces)


def secondary_stream_is_physical(s:'XSurface')->bool:
    """Does Load_XSurface put this surface's second vertex stream in the physical block?

    Read out of the PS3 executable: the loader tests the surface, not the model.  At the site
    that loads ``XSurface + 0x24`` it does

        lbz  r0, 1(surface)        ; deformed
        cmpwi r0, 0
        bne  keepCurrentBlock
        lwz  r0, 0x30(surface)     ; rigidVertListCount
        cmpwi r0, 1
        bne  keepCurrentBlock
        li   r3, 6                 ; XFILE_BLOCK_PHYSICAL
        bl   DB_PushStreamPos

    and the identical test guards whether an RSX vertex buffer is created for that stream at
    ``XSurface + 0x28``.  A deformed surface, or one with anything other than a single rigid
    vertex list, keeps the stream in the enclosing virtual block and is skinned on the CPU.
    """
    return not s.deformed and s.rigid_vert_list_count==1

def _finite(v:float,label:str)->float:
    if not math.isfinite(v): raise ValueError(f'{label} must be finite')
    return float(v)

def _norm_material(s:str)->str: return s.strip().lstrip(',')

def _skeleton_len(m:XModel,section:str)->int:
    nonroot=m.num_bones-m.num_root_bones
    return {
        'boneNames':m.num_bones*2,
        'parentList':nonroot,
        'quats':nonroot*8,
        'trans':nonroot*16,
        'partClassification':m.num_bones,
        'baseMat':m.num_bones*0x20,
    }[section]

def _be_u16_words(pc:bytes,offset:int,length:int)->bytes:
    if length%2: raise ValueError('u16 conversion length must be even')
    out=bytearray(length)
    for i in range(0,length,2): struct.pack_into('>H',out,i,struct.unpack_from('<H',pc,offset+i)[0])
    return bytes(out)

def _be_u32_words(pc:bytes,offset:int,length:int)->bytes:
    if length%4: raise ValueError('u32 conversion length must be word aligned')
    out=bytearray(length)
    for i in range(0,length,4): struct.pack_into('>I',out,i,struct.unpack_from('<I',pc,offset+i)[0])
    return bytes(out)

def _read_f32(pc:bytes,off:int)->float: return struct.unpack_from('<f',pc,off)[0]

def ps3_xmodel_memory_usage(pc_memory_usage:int,surface_count:int)->int:
    """Translate the PC XModel memory accounting to the PS3 root/surface ABI.

    Same-revision Retail PS3 models establish that ``memUsage`` changes by the
    serialized-structure size delta: the PS3 XModel root is 0x10 bytes smaller,
    while every PS3 XSurface root is 0x14 bytes larger.  The PC value already
    carries the model's nominal stream allocation, so emitted payload sizes must
    not be added a second time; only the platform ABI root-size delta belongs here.
    """
    if not isinstance(pc_memory_usage,int) or not 0<=pc_memory_usage<=0xffffffff:
        raise ValueError('PC XModel memory usage must be an unsigned 32-bit integer')
    if not isinstance(surface_count,int) or not 0<=surface_count<=255:
        raise ValueError('XModel surface count must be 0..255')
    adjusted=(
        pc_memory_usage
        +(PS3_XMODEL_ROOT_SIZE-PC_XMODEL_ROOT_SIZE)
        +surface_count*(PS3_XSURFACE_ROOT_SIZE-PC_XSURFACE_ROOT_SIZE)
    )
    if not 0<=adjusted<=0xffffffff:
        raise ValueError('translated PS3 XModel memory usage exceeds uint32')
    return adjusted

def build_xmodel_root(m:XModel, inline_phys_preset:bool|None=None, inline_phys_geoms:bool|None=None)->bytes:
    if len(m.lods)!=4: raise ValueError('XModel must carry four LOD descriptors')
    if len(m.minimum)!=3 or len(m.maximum)!=3: raise ValueError('XModel bounds must be vec3')
    if not (0<=m.num_bones<=255 and 0<=m.num_root_bones<=m.num_bones and m.surface_count<=255): raise ValueError('XModel byte counts invalid')
    out=bytearray(PS3_XMODEL_ROOT_SIZE)
    struct.pack_into('>I',out,0x00,FOLLOWING)
    out[0x04]=m.num_bones;out[0x05]=m.num_root_bones;out[0x06]=m.surface_count;out[0x07]=m.lod_ramp_type
    for off,section in ((0x08,'boneNames'),(0x0C,'parentList'),(0x10,'quats'),(0x14,'trans'),(0x18,'partClassification'),(0x1C,'baseMat')):
        struct.pack_into('>I',out,off,FOLLOWING if _skeleton_len(m,section)>0 else NULL)
    struct.pack_into('>I',out,0x20,FOLLOWING if m.surface_count else NULL)
    struct.pack_into('>I',out,0x24,FOLLOWING if m.surface_count else NULL)
    for li,lod in enumerate(m.lods):
        if len(lod.part_bits)!=4: raise ValueError('LOD partBits must have four words')
        dst=0x28+li*0x18
        struct.pack_into('>fHH',out,dst,_finite(lod.distance,'lod distance'),lod.surface_count,lod.first_surface)
        for p,v in enumerate(lod.part_bits): struct.pack_into('>I',out,dst+8+p*4,v & 0xffffffff)
    struct.pack_into('>I',out,0x88,FOLLOWING if m.num_collision_surfaces>0 else NULL)
    struct.pack_into('>i',out,0x8C,m.num_collision_surfaces)
    struct.pack_into('>I',out,0x90,m.contents & 0xffffffff)
    struct.pack_into('>I',out,0x94,FOLLOWING if m.num_bones>0 else NULL)
    struct.pack_into('>f',out,0x98,_finite(m.radius,'radius'))
    for i,v in enumerate(m.minimum): struct.pack_into('>f',out,0x9C+i*4,_finite(v,'min'))
    for i,v in enumerate(m.maximum): struct.pack_into('>f',out,0xA8+i*4,_finite(v,'max'))
    struct.pack_into('>Hh',out,0xB4,m.num_lods,m.collision_lod)
    struct.pack_into('>I',out,0xBC,ps3_xmodel_memory_usage(m.memory_usage,m.surface_count))
    out[0xC0]=m.flags & 0xff; out[0xC1]=1 if m.bad else 0
    has_inline_phys=m.inline_phys_preset is not None if inline_phys_preset is None else inline_phys_preset
    has_inline_geoms=m.inline_phys_geoms is not None if inline_phys_geoms is None else inline_phys_geoms
    struct.pack_into('>I',out,0xC4,FOLLOWING if has_inline_phys else NULL)
    struct.pack_into('>I',out,0xC8,FOLLOWING if has_inline_geoms else NULL)
    return bytes(out)

def build_xsurface_root(s:XSurface)->bytes:
    if len(s.blend_counts)!=4 or len(s.part_bits)!=4: raise ValueError('surface arrays must have four entries')
    out=bytearray(PS3_XSURFACE_ROOT_SIZE);out[0]=s.tile_mode & 0xff;out[1]=1 if s.deformed else 0
    struct.pack_into('>HH',out,2,s.vertex_count,s.triangle_count);out[6]=s.zone_handle & 0xff
    struct.pack_into('>I',out,0x08,FOLLOWING if s.triangle_count else NULL)
    for i,v in enumerate(s.blend_counts): struct.pack_into('>h',out,0x0C+i*2,int(v))
    struct.pack_into('>I',out,0x14,FOLLOWING if s.blend_element_count else NULL)
    struct.pack_into('>I',out,0x18,FOLLOWING if s.vertex_count else NULL)
    struct.pack_into('>I',out,0x24,FOLLOWING if s.vertex_count else NULL)
    struct.pack_into('>I',out,0x30,s.rigid_vert_list_count)
    struct.pack_into('>I',out,0x34,FOLLOWING if s.rigid_vert_list_count else NULL)
    # Retail PS3 keeps a zero-initialized runtime/reserved word at +0x38.  The four
    # partBits words follow it at +0x3C..+0x48; placing partBits[0] at +0x38 shifts
    # every mask and lets the loader consume the final zero as partBits[3].
    struct.pack_into('>I',out,PS3_XSURFACE_RUNTIME_WORD_OFFSET,0)
    for i,v in enumerate(s.part_bits): struct.pack_into('>I',out,PS3_XSURFACE_PART_BITS_OFFSET+i*4,v & 0xffffffff)
    return bytes(out)


def _compact_collision_surface(pc:bytes,off:int)->bytes:
    out=bytearray(PS3_MODEL_COLLISION_SURFACE_SIZE)
    for i in range(3): struct.pack_into('>f',out,i*4,_read_f32(pc,off+0x08+i*4))
    for i in range(3): struct.pack_into('>f',out,0x0C+i*4,_read_f32(pc,off+0x14+i*4))
    struct.pack_into('>i',out,0x18,struct.unpack_from('<i',pc,off+0x20)[0])
    struct.pack_into('>I',out,0x1C,struct.unpack_from('<I',pc,off+0x24)[0])
    struct.pack_into('>I',out,0x20,struct.unpack_from('<I',pc,off+0x28)[0])
    return bytes(out)

def _collision_tree_root(pc:bytes,off:int,node_count:int,leaf_count:int)->bytes:
    out=bytearray(COLLISION_TREE_ROOT_SIZE)
    for i in range(6): struct.pack_into('>f',out,i*4,_read_f32(pc,off+i*4))
    struct.pack_into('>I',out,0x18,node_count)
    struct.pack_into('>I',out,0x1C,FOLLOWING if node_count else NULL)
    struct.pack_into('>I',out,0x20,leaf_count)
    struct.pack_into('>I',out,0x24,FOLLOWING if leaf_count else NULL)
    return bytes(out)

@dataclass
class XModelNode:
    pc_zone:bytes
    model:XModel
    material_symbols:Mapping[str,str]
    symbol:str
    phys_preset_symbols_by_asset_index:Mapping[int,str]=field(default_factory=dict)
    xmodel_symbols_by_asset_index:Mapping[int,str]=field(default_factory=dict)
    suppress_resolved_phys_preset_reference:bool=False
    type:AssetType=field(init=False,default=AssetType.XMODEL)
    root_block:int=field(init=False,default=TEMP_BLOCK)

    def __post_init__(self):
        m=self.model
        if not m.name or len(m.surfaces)>255: raise ValueError('bad XModel')
        for s in m.surfaces:
            if not s.material_name: raise ValueError(f"XModel '{m.name}' unresolved material")
            if s.vertex_count and s.vertex_source is None: raise ValueError(f"XModel '{m.name}' unresolved vertex geometry")
            if s.triangle_count and s.triangle_source is None: raise ValueError(f"XModel '{m.name}' unresolved triangle geometry")
            if s.blend_element_count and s.blend_source is None: raise ValueError('unresolved blend stream')
            if s.rigid_vert_list_count and s.rigid_source is None: raise ValueError('unresolved rigid stream')
        for section in ('boneNames','parentList','quats','trans','partClassification','baseMat'):
            n=_skeleton_len(m,section)
            if n and section not in m.skeleton_payloads: raise ValueError(f"XModel '{m.name}' unresolved skeleton {section}")
        for n in {_norm_material(x.material_name) for x in m.surfaces}:
            if n not in self.material_symbols: raise ValueError(f"XModel '{m.name}' material {n!r} has no symbol")
        if not self.suppress_resolved_phys_preset_reference and m.resolved_phys_preset_asset_index is not None and m.resolved_phys_preset_asset_index not in self.phys_preset_symbols_by_asset_index:
            raise ValueError('resolved PhysPreset has no graph symbol')
        nested_owner=getattr(m,'resolved_nested_phys_preset_owner_asset_index',None)
        if nested_owner is not None and nested_owner not in self.xmodel_symbols_by_asset_index:
            raise ValueError('nested XModel PhysPreset owner has no graph symbol')

    @property
    def dependencies(self):
        deps=[Dependency(self.material_symbols[_norm_material(n)],AssetType.MATERIAL,f"XModel {self.model.name} material {_norm_material(n)}") for n in sorted({s.material_name for s in self.model.surfaces},key=str.lower)]
        if not self.suppress_resolved_phys_preset_reference and self.model.resolved_phys_preset_asset_index is not None:
            deps.append(Dependency(self.phys_preset_symbols_by_asset_index[self.model.resolved_phys_preset_asset_index],AssetType.PHYSPRESET,f"XModel {self.model.name} PhysPreset"))
        nested_owner=getattr(self.model,'resolved_nested_phys_preset_owner_asset_index',None)
        if nested_owner is not None:
            deps.append(Dependency(self.xmodel_symbols_by_asset_index[nested_owner],AssetType.XMODEL,f"XModel {self.model.name} nested PhysPreset alias owner"))
        return tuple(deps)

    def write(self,w:RelocatingZoneWriter,c:GraphContext)->None:
        m=self.model; root_phys=w.physical_position;loc=w.define_symbol(self.symbol);c.register_root(self.symbol,root_phys)
        # Preserve the three PS3 PhysPreset ownership forms measured in retail:
        #   INSERT   = first inline-owned PhysPreset (loader creates a reusable alias cell),
        #   packed   = later XModel reuses that alias cell or a top-level PhysPreset,
        #   NULL     = no preset.
        nested_owner=getattr(m,'resolved_nested_phys_preset_owner_asset_index',None)
        inline_active=m.inline_phys_preset is not None and m.resolved_phys_preset_asset_index is None and nested_owner is None
        root=bytearray(build_xmodel_root(m, inline_phys_preset=inline_active))
        if inline_active:
            kind=getattr(m,'inline_phys_preset_pointer_kind','insert')
            struct.pack_into('>I',root,0xC4,INSERT if kind=='insert' else FOLLOWING)
        elif (not self.suppress_resolved_phys_preset_reference and m.resolved_phys_preset_asset_index is not None) or nested_owner is not None:
            struct.pack_into('>I',root,0xC4,0)
        w.write_in_block(bytes(root),f"XModel '{m.name}' root")
        if not self.suppress_resolved_phys_preset_reference and m.resolved_phys_preset_asset_index is not None:
            ps=self.phys_preset_symbols_by_asset_index[m.resolved_phys_preset_asset_index]
            w.register_pointer_relocation(root_phys+0xC4,c.reference_symbol(ps),description=f"XModel {m.name}.physPreset graph reference")
        elif nested_owner is not None:
            owner_symbol=self.xmodel_symbols_by_asset_index[nested_owner]
            w.register_pointer_relocation(root_phys+0xC4,nested_symbol(owner_symbol,'insert.physPreset'),kind='packed_alias',description=f"XModel {m.name}.physPreset nested INSERT reuse")
        # OAT's IW3 generator loads an asset root in XFILE_BLOCK_TEMP, then pushes
        # the default-normal XFILE_BLOCK_VIRTUAL for *all* of its members.  The
        # individual surface streams below may temporarily select B4/B6 again,
        # but they always return to this enclosing B4 frame.  Keeping only those
        # explicit substreams in B4 leaves the name and the complete post-surface
        # member tail in TEMP and under-declares the loader's B4 allocation.
        w.push_block(VIRTUAL_BLOCK)
        try:
            member_block_start=w.current_location
            w.write_latin1z(m.name,f"XModel '{m.name}' name")
            self._write_skeleton(w)
            surface_root_phys=-1;surface_root_location=None
            if m.surfaces:
                w.align_logical_only(4);surface_root_phys=w.physical_position
                surface_root_location=w.current_location
                for i,s in enumerate(m.surfaces): w.write_in_block(build_xsurface_root(s),f"XModel '{m.name}' XSurface[{i}] root")
            h=hashlib.sha256();stats={'vertices':0,'triangles':0,'deformed':0,'blend':0,'rigid':0,'trees':0,'nodes':0,'leaves':0}
            for i,s in enumerate(m.surfaces):
                rs=self._write_surface(w,s,i,h);stats['vertices']+=s.vertex_count;stats['triangles']+=s.triangle_count;stats['deformed']+=int(s.deformed);stats['blend']+=s.blend_element_count;stats['rigid']+=s.rigid_vert_list_count;stats['trees']+=rs[0];stats['nodes']+=rs[1];stats['leaves']+=rs[2]

            # MaterialHandle is a pointer array in the default-normal block.
            material_handle_phys=-1;material_handle_location=None;owned_material_sites=[]
            if m.surfaces:
                w.align_logical_only(4);material_handle_phys=w.physical_position
                material_handle_location=w.current_location
                for i,surface in enumerate(m.surfaces):
                    material_symbol=self.material_symbols[_norm_material(surface.material_name)];site=f'material[{i}]'
                    marker=c.owned_marker(self.symbol,material_symbol,site)
                    if marker is not None:
                        field_location=w.current_location;c.bind_owned_pointer_field(w,self.symbol,material_symbol,site,field_location)
                        w.write_u32(marker,f"XModel {m.name} {site} owned marker");owned_material_sites.append((material_symbol,site))
                    else:
                        w.write_pointer_to(c.reference_symbol(material_symbol),kind='packed_alias',description=f"XModel {m.name} {site}")
            # Load_XModel consumes the complete Material** array first, then recursively resolves each
            # XAsset pointer in slot order.  Inline support Materials therefore follow the array.
            for material_symbol,site in owned_material_sites:
                c.write_owned_child(w,self.symbol,material_symbol,site)

            collision_phys=-1;collision_location=None
            if m.num_collision_surfaces:
                if m.collision_surface_source is None: raise ValueError('collision surfaces unresolved')
                w.align_logical_only(4);collision_phys=w.physical_position;collision_location=w.current_location
                for i in range(m.num_collision_surfaces): w.write_in_block(_compact_collision_surface(self.pc_zone,m.collision_surface_source+i*PC_MODEL_COLLISION_SURFACE_SIZE),f"XModel {m.name} collision[{i}]")
            bone_info_phys=-1;bone_info_location=None
            if m.num_bones:
                if m.bone_info_source is None: raise ValueError('bone info unresolved')
                w.align_logical_only(4);bone_info_phys=w.physical_position;bone_info_location=w.current_location
                for i in range(m.num_bones): w.write_in_block(_be_u32_words(self.pc_zone,m.bone_info_source+i*XBONE_INFO_SIZE,XBONE_INFO_SIZE),f"XModel {m.name} XBoneInfo[{i}]")

            inline_preset=phys_geom_count=phys_brush=phys_side=0
            inline_preset_root_phys=-1;inline_preset_root_location=None
            if inline_active:
                p=m.inline_phys_preset
                if getattr(m,'inline_phys_preset_pointer_kind','insert')=='insert':
                    # INSERT reserves its persistent alias cell in B4, while the
                    # inline PhysPreset asset root itself is allocated in TEMP.
                    w.define_insert_pointer_alias(nested_symbol(self.symbol,'insert.physPreset'),description=f"XModel {m.name} PhysPreset InsertPointer alias")
                w.push_block(TEMP_BLOCK)
                try:
                    w.align_logical_only(4);inline_preset_root_phys=w.physical_position
                    inline_preset_root_location=w.current_location
                    w.write_in_block(build_phys_preset_root(p),f"XModel {m.name} inline PhysPreset")
                finally:
                    w.pop_block()
                # Load_PhysPreset re-enters default-normal for its XStrings.
                w.write_latin1z(p.name,'inline phys name')
                w.write_latin1z(p.sound_alias_prefix or '', 'inline phys sound')
                inline_preset=1
            phys_geom_location=None
            if m.inline_phys_geoms is not None:
                ps=write_inline_phys_geom_list(w,m.inline_phys_geoms,f"XModel '{m.name}'");phys_geom_count=ps['geom_count'];phys_brush=ps['brush_count'];phys_side=ps['side_count'];phys_geom_location=ps['root_location']
            member_block_end=w.current_location
        finally:
            w.pop_block()
        c.register_result('xmodel:'+self.symbol,{
            'source_asset_index':m.source_asset_index,'name':m.name,'root_physical':root_phys,'root_location':loc,'surface_root_physical':surface_root_phys,
            'member_block_start':member_block_start,'member_block_end':member_block_end,'surface_root_location':surface_root_location,
            'material_handle_physical':material_handle_phys,'material_handle_location':material_handle_location,'bone_info_physical':bone_info_phys,'bone_info_location':bone_info_location,
            'collision_physical':collision_phys,'collision_location':collision_location,'inline_phys_preset_root_physical':inline_preset_root_phys,'inline_phys_preset_root_location':inline_preset_root_location,'phys_geom_location':phys_geom_location,'surface_count':m.surface_count,
            'vertex_count':stats['vertices'],'triangle_count':stats['triangles'],'deformed_surface_count':stats['deformed'],'blend_element_count':stats['blend'],
            'rigid_vert_list_count':stats['rigid'],'collision_tree_count':stats['trees'],'collision_node_count':stats['nodes'],'collision_leaf_count':stats['leaves'],
            'model_collision_surface_count':m.num_collision_surfaces,'material_reference_count':m.surface_count,'owned_material_count':len(owned_material_sites),'owned_material_sites':[site for _,site in owned_material_sites],'inline_phys_preset_count':inline_preset,
            'packed_phys_preset_reference_count':int((not self.suppress_resolved_phys_preset_reference and m.resolved_phys_preset_asset_index is not None) or nested_owner is not None),'nested_phys_preset_reuse_count':int(nested_owner is not None),'phys_geom_list_count':int(m.inline_phys_geoms is not None),
            'phys_geom_count':phys_geom_count,'phys_brush_count':phys_brush,'phys_brush_side_count':phys_side,
            'pc_memory_usage':m.memory_usage,'ps3_memory_usage':ps3_xmodel_memory_usage(m.memory_usage,m.surface_count),
            'geometry_sha256':h.hexdigest().upper()})

    def _write_skeleton(self,w):
        for section,alignment in (('boneNames',2),('parentList',1),('quats',2),('trans',4),('partClassification',1),('baseMat',4)):
            length=_skeleton_len(self.model,section)
            if not length: continue
            off,n=self.model.skeleton_payloads[section]
            if n!=length: raise ValueError('skeleton length mismatch')
            if section in ('boneNames','quats'): payload=_be_u16_words(self.pc_zone,off,n)
            elif section in ('trans','baseMat'): payload=_be_u32_words(self.pc_zone,off,n)
            else: payload=self.pc_zone[off:off+n]
            w.push_block(LARGE_BLOCK);w.align_logical_only(alignment);w.write_in_block(payload,f'XModel {self.model.name} {section}');w.pop_block()

    def _write_surface(self,w,s:XSurface,index:int,h)->tuple[int,int,int]:
        if s.blend_element_count:
            blend=_be_u16_words(self.pc_zone,s.blend_source,s.blend_element_count*2);w.push_block(LARGE_BLOCK);w.align_logical_only(2);w.write_in_block(blend,f'blend {index}');w.pop_block();h.update(blend)
        if s.vertex_count:
            primary=bytearray(s.vertex_count*0x10);secondary=bytearray(s.vertex_count*0x10)
            for v in range(s.vertex_count):
                pc=s.vertex_source+v*PC_PACKED_VERTEX_SIZE;ps=v*0x10
                # Primary stream: position and binormal sign, four plain floats.
                for word in range(4):
                    struct.pack_into('>I',primary,ps+word*4,struct.unpack_from('<I',self.pc_zone,pc+word*4)[0])
                # Secondary stream: colour, texture coordinates, normal, tangent.  Only
                # the texture coordinates are a word - the other three are RSX attribute
                # encodings that differ from PC (see cod4porter.vertex_format).
                secondary[ps:ps+4]=ps3_vertex_color(self.pc_zone[pc+0x10:pc+0x14])
                struct.pack_into('>I',secondary,ps+0x04,struct.unpack_from('<I',self.pc_zone,pc+0x14)[0])
                struct.pack_into('>I',secondary,ps+0x08,pack_rsx_unit_vector(self.pc_zone[pc+0x18:pc+0x1C]))
                struct.pack_into('>I',secondary,ps+0x0C,pack_rsx_unit_vector(self.pc_zone[pc+0x1C:pc+0x20]))
            w.push_block(LARGE_BLOCK);w.align_logical_only(16);w.write_in_block(bytes(primary),f'vertex primary {index}');w.pop_block()
            # Load_XSurface routes the second vertex stream by the surface itself:
            #     if (surf->deformed == 0 && surf->rigidVertListCount == 1)
            #         DB_PushStreamPos(XFILE_BLOCK_PHYSICAL);
            # Any other surface leaves it in the enclosing XFILE_BLOCK_VIRTUAL frame and gets no
            # RSX vertex buffer for it - those surfaces are skinned on the CPU.  Sending every
            # surface to the physical block overruns the virtual block by exactly the bytes of
            # the surfaces that do not qualify and shifts every later virtual-block offset.
            secondary_block=VERTEX_BLOCK if secondary_stream_is_physical(s) else LARGE_BLOCK
            w.push_block(secondary_block);w.align_logical_only(16);w.write_in_block(bytes(secondary),f'vertex secondary {index}');w.pop_block();h.update(primary);h.update(secondary)
        rigid=self._write_rigid(w,s,index,h)
        if s.triangle_count:
            tri=_be_u16_words(self.pc_zone,s.triangle_source,s.triangle_count*6);w.push_block(LARGE_BLOCK);w.align_logical_only(16);w.write_in_block(tri,f'indices {index}');w.pop_block();h.update(tri)
        return rigid

    def _write_rigid(self,w,s:XSurface,index:int,h)->tuple[int,int,int]:
        if not s.rigid_vert_list_count:return (0,0,0)
        count=s.rigid_vert_list_count;src_root=s.rigid_source;nested=src_root+count*RIGID_VERT_LIST_SIZE;roots=bytearray(count*RIGID_VERT_LIST_SIZE);trees=[]
        for i in range(count):
            src=src_root+i*RIGID_VERT_LIST_SIZE;dst=i*RIGID_VERT_LIST_SIZE
            for word in range(4): struct.pack_into('>H',roots,dst+word*2,struct.unpack_from('<H',self.pc_zone,src+word*2)[0])
            tree_raw=struct.unpack_from('<I',self.pc_zone,src+8)[0]
            if tree_raw==0: struct.pack_into('>I',roots,dst+8,NULL);continue
            if tree_raw not in (0xFFFFFFFF,0xFFFFFFFE): raise ValueError('packed rigid collision tree is unresolved')
            tree=nested;node_count=struct.unpack_from('<I',self.pc_zone,tree+0x18)[0];node_ptr=struct.unpack_from('<I',self.pc_zone,tree+0x1C)[0];leaf_count=struct.unpack_from('<I',self.pc_zone,tree+0x20)[0];leaf_ptr=struct.unpack_from('<I',self.pc_zone,tree+0x24)[0];nested+=COLLISION_TREE_ROOT_SIZE
            node_src=-1;leaf_src=-1
            if node_count:
                if node_ptr not in (0xFFFFFFFF,0xFFFFFFFE):raise ValueError('collision nodes unresolved')
                node_src=nested;nested+=node_count*COLLISION_NODE_SIZE
            if leaf_count:
                if leaf_ptr not in (0xFFFFFFFF,0xFFFFFFFE):raise ValueError('collision leaves unresolved')
                leaf_src=nested;nested+=leaf_count*COLLISION_LEAF_SIZE
            struct.pack_into('>I',roots,dst+8,FOLLOWING);trees.append((tree,node_count,node_src,leaf_count,leaf_src))
        w.push_block(LARGE_BLOCK);w.align_logical_only(4);w.write_in_block(bytes(roots),f'rigid roots {index}')
        for tree,nc,ns,lc,ls in trees:
            rt=_collision_tree_root(self.pc_zone,tree,nc,lc);w.align_logical_only(4);w.write_in_block(rt,'collision tree')
            if nc:
                nodes=_be_u16_words(self.pc_zone,ns,nc*COLLISION_NODE_SIZE);w.align_logical_only(16);w.write_in_block(nodes,'collision nodes');h.update(nodes)
            if lc:
                leaves=_be_u16_words(self.pc_zone,ls,lc*COLLISION_LEAF_SIZE);w.align_logical_only(2);w.write_in_block(leaves,'collision leaves');h.update(leaves)
            h.update(rt)
        w.pop_block();h.update(roots);return (len(trees),sum(x[1] for x in trees),sum(x[3] for x in trees))
