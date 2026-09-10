import hashlib
from pathlib import Path
import struct
import sys

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))

from cod4porter.xmodel import (
    PC_XMODEL_ROOT_SIZE,
    PC_XSURFACE_ROOT_SIZE,
    PS3_XMODEL_ROOT_SIZE,
    PS3_XSURFACE_PART_BITS_OFFSET,
    PS3_XSURFACE_ROOT_SIZE,
    PS3_XSURFACE_RUNTIME_WORD_OFFSET,
    XModel,
    XModelLod,
    XModelNode,
    XSurface,
    build_xmodel_root,
    secondary_stream_is_physical,
    build_xsurface_root,
    ps3_xmodel_memory_usage,
)
from dataclasses import replace as _dc_replace

from cod4porter.vertex_format import pack_rsx_unit_vector, ps3_vertex_color
from cod4porter.zone_writer import (
    FOLLOWING,
    LARGE_BLOCK,
    VERTEX_BLOCK,
    RelocatingZoneWriter,
)


def be_u32(data:bytes,offset:int)->int:
    return struct.unpack_from('>I',data,offset)[0]


# Retail PS3 XSurface oracle: 0x4C bytes, a zero runtime word at +0x38 and
# partBits[4] at +0x3C..+0x48.  Use distinct masks so a one-word shift cannot
# pass accidentally.
surface=XSurface(
    tile_mode=2,deformed=True,vertex_count=7,triangle_count=5,zone_handle=3,
    blend_counts=(1,2,3,4),blend_element_count=50,rigid_vert_list_count=1,
    part_bits=(0x80000000,0x12345678,0x89ABCDEF,0x00000001),
    material_name='oracle',vertex_source=0,triangle_source=0,
    blend_source=0,rigid_source=0,
)
surface_root=build_xsurface_root(surface)
assert len(surface_root)==PS3_XSURFACE_ROOT_SIZE==0x4C
assert PS3_XSURFACE_RUNTIME_WORD_OFFSET==0x38
assert PS3_XSURFACE_PART_BITS_OFFSET==0x3C
assert be_u32(surface_root,0x34)==FOLLOWING
assert be_u32(surface_root,0x38)==0
assert tuple(be_u32(surface_root,0x3C+i*4) for i in range(4))==tuple(surface.part_bits)
assert be_u32(surface_root,0x48)==surface.part_bits[3]

# Exact +0x30 tail seen on the same-revision Shipment stock surfaces used by
# the oracle below (one rigid list, FOLLOWING root, zero runtime word, bone 0).
shipment_surface=XSurface(
    0,False,1,1,0,(0,0,0,0),0,1,(0x80000000,0,0,0),'oracle',0,0,rigid_source=0,
)
assert build_xsurface_root(shipment_surface)[0x30:0x4C]==bytes.fromhex(
    '00000001 FFFFFFFF 00000000 80000000 00000000 00000000 00000000'
)


# Same-revision PC/Getaway versus Retail PS3/Shipment stock assets.  These 12
# models have matching surface counts and geometry counts on both platforms.
# Every Retail value is exactly PC + 20*numSurfs - 16, which is also the direct
# delta between the 0xDC/0xCC XModel roots and 0x38/0x4C XSurface roots.
retail_memory_oracle=(
    ('body_mp_opforce_assault',398009,4,398073),
    ('body_mp_opforce_cqb',385885,6,385989),
    ('body_mp_opforce_eningeer',403757,9,403921),
    ('body_mp_opforce_support',375785,6,375889),
    ('com_bomb_objective',419511,13,419755),
    ('com_bomb_objective_d',324375,9,324539),
    ('com_cellphone_on',11371,3,11415),
    ('com_plasticcase_beige_big',234323,4,234387),
    ('head_mp_opforce_3hole_mask',149763,7,149887),
    ('head_mp_opforce_gasmask',181775,15,182059),
    ('head_mp_opforce_headwrap',144385,9,144549),
    ('viewhands_op_force',111775,2,111799),
)
assert PS3_XMODEL_ROOT_SIZE-PC_XMODEL_ROOT_SIZE==-16
assert PS3_XSURFACE_ROOT_SIZE-PC_XSURFACE_ROOT_SIZE==20
for name,pc_memory,surface_count,retail_ps3_memory in retail_memory_oracle:
    actual=ps3_xmodel_memory_usage(pc_memory,surface_count)
    assert actual==retail_ps3_memory,(name,actual,retail_ps3_memory)


# Shipment's com_junktire is the one exact-geometry name match that is not an
# inline-ownership oracle: all three Retail surface index/vertex/secondary/rigid
# pointers are packed aliases to previously loaded streams, hence its tiny 244
# is tied to that different ownership graph.  C10 owns/writes the PC source
# streams and must use the structural conversion, never a name special case.
assert ps3_xmodel_memory_usage(36071,3)==36115
assert ps3_xmodel_memory_usage(36071,3)!=244


# The translated value must be serialized at XModel::memUsage +0xBC.
lods=(XModelLod(0.0,3,0,(0x80000000,0,0,0)),)+tuple(
    XModelLod(1000000.0,0,0,(0,0,0,0)) for _ in range(3)
)
model=XModel(
    name='com_junktire',num_bones=0,num_root_bones=0,lod_ramp_type=0,
    num_lods=1,collision_lod=-1,contents=1,radius=1.0,
    minimum=(-1.0,-1.0,-1.0),maximum=(1.0,1.0,1.0),memory_usage=36071,
    flags=0,bad=False,lods=lods,surfaces=(surface,surface,surface),
    skeleton_payloads={},bone_info_source=0,
)
model_root=build_xmodel_root(model)
assert len(model_root)==PS3_XMODEL_ROOT_SIZE
assert be_u32(model_root,0xBC)==36115


# Fail closed if the structural delta cannot be represented as uint32.
for args in ((-1,1),(0x1_0000_0000,1),(0,0),(0xffffffff,255)):
    try:
        ps3_xmodel_memory_usage(*args)
    except ValueError:
        pass
    else:
        raise AssertionError(f'expected invalid PS3 memUsage translation for {args!r}')


# Regression guard for the already-audited vertex split: the primary record remains in
# block 4, the secondary record remains in block 6, both 16-byte aligned and in the
# original field order.  The XSurface/memUsage fixes must not touch this placement.
#
# The two records are NOT the same transform.  The primary record - position and binormal
# sign - is four plain floats and byte-swaps as a word.  In the secondary record only the
# texture coordinate is a word; the colour and the two packed unit vectors are RSX vertex
# attribute encodings measured against Retail PS3 mp_shipment (cod4porter.vertex_format).
pc=bytearray(0x80)
primary_words=(0x01020304,0x11121314,0x21222324,0x31323334)
secondary_words=(0x41424344,0x51525354,0x61626364,0x71727374)
for i,value in enumerate(primary_words+secondary_words):
    struct.pack_into('<I',pc,i*4,value)
struct.pack_into('<HHH',pc,0x40,0x0001,0x0203,0x0405)
split_surface=XSurface(
    0,False,1,1,0,(0,0,0,0),0,0,(0x80000000,0,0,0),'oracle',0,0x40,
)
split_model=XModel(
    'split_oracle',0,0,0,1,-1,0,1.0,(0.0,0.0,0.0),(0.0,0.0,0.0),
    100,0,False,
    (XModelLod(0.0,1,0,(0x80000000,0,0,0)),)+tuple(
        XModelLod(1000000.0,0,0,(0,0,0,0)) for _ in range(3)
    ),
    (split_surface,),{},0,
)
split_node=XModelNode(bytes(pc),split_model,{'oracle':'material:oracle'},'xmodel:split_oracle')
writer=RelocatingZoneWriter()
split_node._write_surface(writer,split_surface,0,hashlib.sha256())
split=writer.build()
expected_primary=b''.join(struct.pack('>I',v) for v in primary_words)
expected_secondary=(
    ps3_vertex_color(struct.pack('<I',secondary_words[0]))
    +struct.pack('>I',secondary_words[1])
    +struct.pack('>I',pack_rsx_unit_vector(struct.pack('<I',secondary_words[2])))
    +struct.pack('>I',pack_rsx_unit_vector(struct.pack('<I',secondary_words[3])))
)
expected_indices=struct.pack('>HHH',0x0001,0x0203,0x0405)
assert LARGE_BLOCK==4 and VERTEX_BLOCK==6
# The fixture surface has rigidVertListCount 0, so Load_XSurface does NOT push the physical
# block for the second stream: everything stays in the virtual block, in the same order.
assert not secondary_stream_is_physical(split_surface)
assert split.block_sizes[LARGE_BLOCK]==len(expected_primary)+len(expected_secondary)+len(expected_indices)
assert split.block_sizes[VERTEX_BLOCK]==0
assert split.zone_bytes[0x24:0x34]==expected_primary
assert split.zone_bytes[0x34:0x44]==expected_secondary
assert split.zone_bytes[0x44:0x4A]==expected_indices

# The same surface with a single rigid vertex list qualifies, and only then does the second
# stream move to the physical block - the routing rule read out of the PS3 executable.
physical_surface=_dc_replace(split_surface,rigid_vert_list_count=1,rigid_source=None)
assert secondary_stream_is_physical(physical_surface)
deformed_surface=_dc_replace(split_surface,deformed=True,rigid_vert_list_count=1)
assert not secondary_stream_is_physical(deformed_surface)
for count in (0,2,3,12,255):
    assert not secondary_stream_is_physical(_dc_replace(split_surface,rigid_vert_list_count=count))
# rigid_source 0x50 points at twelve zero bytes: one rigid vertex list with a NULL tree.
qualifying=_dc_replace(split_surface,rigid_vert_list_count=1,rigid_source=0x50)
assert secondary_stream_is_physical(qualifying)
writer2=RelocatingZoneWriter()
node2=XModelNode(bytes(pc),split_model,{'oracle':'material:oracle'},'xmodel:split_oracle2')
node2._write_surface(writer2,qualifying,0,hashlib.sha256())
split2=writer2.build()
assert split2.block_sizes[VERTEX_BLOCK]==len(expected_secondary),split2.block_sizes
# virtual block: primary(16) + rigid roots(12), then the index array realigned to 16
expected_b4=((len(expected_primary)+0x0C)+15&~15)+len(expected_indices)
assert split2.block_sizes[LARGE_BLOCK]==expected_b4,split2.block_sizes
# and the bytes themselves are unchanged by the routing - only their block moved
assert split2.zone_bytes[0x24:0x34]==expected_primary
assert split2.zone_bytes[0x34:0x44]==expected_secondary
# Guard the specific defect this replaced: a whole-word byte swap of the secondary record,
# which reverses the colour (alpha lands in the first lane) and leaves the two packed unit
# vectors as PC PackedUnitVec bit patterns that RSX reads as X11Y11Z10N.
old_secondary=b''.join(struct.pack('>I',v) for v in secondary_words)
assert split.zone_bytes[0x34:0x44]!=old_secondary
assert split.zone_bytes[0x34:0x38]!=old_secondary[0:4]      # colour
assert split.zone_bytes[0x38:0x3C]==old_secondary[4:8]      # texture coordinate is a word
assert split.zone_bytes[0x3C:0x44]!=old_secondary[8:16]     # normal and tangent


print({
    'passed':True,
    'xsurface_root_bytes':len(surface_root),
    'retail_memusage_oracles':len(retail_memory_oracle),
    'com_junktire_ownership_outlier_handled':True,
    'secondary_vertex_block':VERTEX_BLOCK,
})
