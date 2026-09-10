from __future__ import annotations

from pathlib import Path
import struct
import sys

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))

from cod4porter.physics import PhysBrush,PhysBrushSide,PhysGeom,PhysGeomList,PhysPlane,PhysPreset
from cod4porter.xmodel import XModel,XModelLod,XModelNode,XSurface
from cod4porter.zone_writer import FOLLOWING,PackedOffset,RelocatingZoneWriter,TEMP_BLOCK,VIRTUAL_BLOCK,VERTEX_BLOCK


def align(value:int,alignment:int)->int:
    return (value+alignment-1)&~(alignment-1)


class Context:
    def __init__(self):self.results={};self.roots={}
    def register_root(self,symbol,physical):self.roots[symbol]=physical
    def reference_symbol(self,symbol):return symbol
    def owned_marker(self,owner,child,site):return None
    def register_result(self,key,value):self.results[key]=value


# The fixture deliberately crosses every XModel member family and chooses a
# three-byte baseAdjacentSide payload so the deepest PhysGeom planes need a4.
pc=bytearray(0x1000)
payloads={
    'boneNames':(0x000,4),'parentList':(0x010,1),'quats':(0x020,8),
    'trans':(0x030,16),'partClassification':(0x050,2),'baseMat':(0x060,64),
}
for off,length in payloads.values():pc[off:off+length]=bytes(range(length))

blend=0x100
for i in range(3):struct.pack_into('<H',pc,blend+i*2,0x100+i)
vertex0=0x140;vertex1=0x180
tri0=0x200;tri1=0x220
struct.pack_into('<6H',pc,tri0,0,1,0,1,0,1)
struct.pack_into('<3H',pc,tri1,0,0,0)

rigid=0x280
struct.pack_into('<4H',pc,rigid,0,2,0,2)
struct.pack_into('<I',pc,rigid+8,FOLLOWING)
tree=rigid+0x0C
struct.pack_into('<I',pc,tree+0x18,1)
struct.pack_into('<I',pc,tree+0x1C,FOLLOWING)
struct.pack_into('<I',pc,tree+0x20,1)
struct.pack_into('<I',pc,tree+0x24,FOLLOWING)

collision=0x400
bone_info=0x500
for i in range(20):struct.pack_into('<I',pc,bone_info+i*4,i)

surfaces=(
    XSurface(0,False,2,2,0,(3,0,0,0),3,1,(1,2,3,4),'oracle',vertex0,tri0,blend,rigid),
    XSurface(0,False,1,1,0,(0,0,0,0),0,0,(5,6,7,8),'oracle',vertex1,tri1),
)
lods=(XModelLod(0.0,2,0,(1,2,3,4)),)+tuple(XModelLod(1000.0,0,0,(0,0,0,0)) for _ in range(3))
preset=PhysPreset('preset_name',1,1.0,0.1,0.2,1.0,1.0,0.0,0.0,False,'preset_sound')
side_plane=PhysPlane((1.0,0.0,0.0),1.0,0,0)
brush=PhysBrush(
    (-1.0,-1.0,-1.0),(1.0,1.0,1.0),1,
    (PhysBrushSide(side_plane,7,0,1),),(1,2,3,4,5,6),b'abc',
    (0,0,0,0,0,0),(1,1,1,1,1,1),3,(side_plane,),
)
geoms=PhysGeomList((0.0,0.0,0.0),(1.0,1.0,1.0),(0.0,0.0,0.0),(
    PhysGeom(0,(1.0,0.0,0.0,0.0,1.0,0.0,0.0,0.0,1.0),(0.0,0.0,0.0),(1.0,1.0,1.0),brush),
))
model=XModel(
    'block_contract',2,1,0,1,-1,1,2.0,(-1.0,-1.0,-1.0),(1.0,1.0,1.0),1000,0,False,
    lods,surfaces,payloads,bone_info,1,collision,preset,geoms,source_asset_index=7,
)

writer=RelocatingZoneWriter()
writer.push_block(VIRTUAL_BLOCK)
writer.define_symbol_at('material:oracle',PackedOffset(VIRTUAL_BLOCK,0))
writer.write_u32(0,'material oracle cell')
writer.pop_block()
writer.push_block(TEMP_BLOCK)
context=Context()
XModelNode(bytes(pc),model,{'oracle':'material:oracle'},'xmodel:block_contract').write(writer,context)
writer.pop_block()
result=writer.build()
row=context.results['xmodel:xmodel:block_contract']

# Independent destination-cursor replay.  It uses frozen PS3 strides rather
# than writer helpers, so the pre-fix TEMP-tail implementation fails here.
b4=4
b4+=len(b'block_contract\0')
for alignment,size in ((2,4),(1,1),(2,8),(4,16),(1,2),(4,64)):
    b4=align(b4,alignment)+size
b4=align(b4,4)+2*0x4C                         # XSurface roots
b4=align(b4,2)+3*2                            # vertsBlend
b4=align(b4,16)+2*0x10                        # primary vertices
b4=align(b4,4)+0x0C                           # rigid roots
b4=align(b4,4)+0x28                           # collision tree
b4=align(b4,16)+0x10                          # collision node
b4=align(b4,2)+0x02                           # collision leaf
b4=align(b4,16)+2*0x06                        # indices surface 0
b4=align(b4,16)+1*0x10                        # primary vertices surface 1
# Surface 1 has rigidVertListCount 0, so Load_XSurface never pushes the physical block for
# its second vertex stream and it stays here; surface 0 (rigidVertListCount 1, not deformed)
# does qualify and is the only one charged to XFILE_BLOCK_PHYSICAL below.
b4=align(b4,16)+1*0x10                        # secondary vertices surface 1 (stays virtual)
b4=align(b4,16)+1*0x06                        # indices surface 1
b4=align(b4,4)+2*0x04                         # MaterialHandle[]
b4=align(b4,4)+1*0x24                         # compact XModelCollSurf[]
b4=align(b4,4)+2*0x28                         # XBoneInfo[]
b4=align(b4,4)+0x04                           # INSERT alias cell
b4+=len(b'preset_name\0')+len(b'preset_sound\0')
b4=align(b4,4)+0x2C                           # PhysGeomList
b4=align(b4,4)+0x44                           # PhysGeomInfo[]
b4=align(b4,4)+0x50                           # BrushWrapper
b4=align(b4,4)+0x0C                           # cbrushside_t[]
b4=align(b4,4)+0x14                           # following side plane
b4+=3                                         # baseAdjacentSide bytes
b4=align(b4,4)+0x14                           # reusable planes[]

assert result.block_sizes[VIRTUAL_BLOCK]==b4,(result.block_sizes,b4)
assert result.block_sizes[VERTEX_BLOCK]==2*0x10   # surface 0 only: 2 vertices
assert result.block_sizes[TEMP_BLOCK]==0xCC+0x2C
assert row['member_block_start']==PackedOffset(VIRTUAL_BLOCK,4)
assert row['member_block_end']==PackedOffset(VIRTUAL_BLOCK,b4)
for key in ('surface_root_location','material_handle_location','collision_location','bone_info_location','phys_geom_location'):
    location=row[key]
    assert location.block==VIRTUAL_BLOCK,(key,location)
    assert location.offset%4==0,(key,location)
assert row['inline_phys_preset_root_location']==PackedOffset(TEMP_BLOCK,0xCC)

# The generated loader only calls AlignStream for non-NULL member pointers.
# A zero-count model must therefore leave the odd-sized name tail untouched;
# unconditional surface/material/vertex/index alignment would round B4 up.
empty_name='zero_contract'
empty_model=XModel(
    empty_name,0,0,0,0,-1,0,0.0,(0.0,0.0,0.0),(0.0,0.0,0.0),0x10,0,False,
    tuple(XModelLod(1000.0,0,0,(0,0,0,0)) for _ in range(4)),(),{},None,
)
empty_writer=RelocatingZoneWriter()
empty_writer.push_block(TEMP_BLOCK)
empty_context=Context()
XModelNode(b'',empty_model,{},'xmodel:zero_contract').write(empty_writer,empty_context)
empty_writer.pop_block()
empty_result=empty_writer.build()
empty_row=empty_context.results['xmodel:xmodel:zero_contract']
empty_b4=len(empty_name.encode('latin1'))+1
assert empty_result.block_sizes[VIRTUAL_BLOCK]==empty_b4,empty_result.block_sizes
assert empty_result.block_sizes[TEMP_BLOCK]==0xCC
assert empty_result.block_sizes[VERTEX_BLOCK]==0
assert empty_row['member_block_start']==PackedOffset(VIRTUAL_BLOCK,0)
assert empty_row['member_block_end']==PackedOffset(VIRTUAL_BLOCK,empty_b4)
for key in ('surface_root_location','material_handle_location','collision_location','bone_info_location','phys_geom_location'):
    assert empty_row[key] is None,(key,empty_row[key])
assert empty_row['bone_info_physical']==-1

# The same conditional rule applies inside a non-NULL XSurface array: a surface
# may exist while its optional geometry pointers and their source offsets are
# NULL.  Only the surface root and MaterialHandle array then consume B4.
null_surface=XSurface(
    0,False,0,0,0,(0,0,0,0),0,0,(0,0,0,0),'oracle',None,None,
)
null_geometry_name='zero_geometry'
null_geometry_model=XModel(
    null_geometry_name,0,0,0,1,-1,0,0.0,(0.0,0.0,0.0),(0.0,0.0,0.0),0x10,0,False,
    (XModelLod(0.0,1,0,(0,0,0,0)),)+tuple(XModelLod(1000.0,0,0,(0,0,0,0)) for _ in range(3)),
    (null_surface,),{},None,
)
null_writer=RelocatingZoneWriter()
null_writer.push_block(VIRTUAL_BLOCK)
null_writer.define_symbol_at('material:oracle',PackedOffset(VIRTUAL_BLOCK,0))
null_writer.write_u32(0,'material oracle cell')
null_writer.pop_block()
null_writer.push_block(TEMP_BLOCK)
null_context=Context()
XModelNode(b'',null_geometry_model,{'oracle':'material:oracle'},'xmodel:zero_geometry').write(null_writer,null_context)
null_writer.pop_block()
null_result=null_writer.build()
null_row=null_context.results['xmodel:xmodel:zero_geometry']
null_b4=4+len(null_geometry_name.encode('latin1'))+1
null_surface_offset=align(null_b4,4)
null_b4=null_surface_offset+0x4C
null_material_offset=align(null_b4,4)
null_b4=null_material_offset+0x04
assert null_result.block_sizes[VIRTUAL_BLOCK]==null_b4,null_result.block_sizes
assert null_result.block_sizes[VERTEX_BLOCK]==0
assert null_row['surface_root_location']==PackedOffset(VIRTUAL_BLOCK,null_surface_offset)
assert null_row['material_handle_location']==PackedOffset(VIRTUAL_BLOCK,null_material_offset)
assert null_row['bone_info_location'] is None and null_row['bone_info_physical']==-1

print({
    'passed':True,
    'independent_b4_end':b4,
    'temp_high_water':result.block_sizes[TEMP_BLOCK],
    'secondary_vertex_bytes':result.block_sizes[VERTEX_BLOCK],
    'null_pointer_b4_end':empty_b4,
    'null_geometry_b4_end':null_b4,
})
