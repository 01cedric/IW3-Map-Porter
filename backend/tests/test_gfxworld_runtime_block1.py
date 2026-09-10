import struct
from types import SimpleNamespace

from cod4porter.assets.clipmap import CLIENT_SIZE, COLLISION_SIZE, POSE_SIZE
from cod4porter.assets.gfxworld import (
    GfxWorldNode,
    PS3_DPVS_ALIGNED_RUNTIME,
    PS3_GFX_DRAW_SURF_RUNTIME,
    PS3_GFX_TEXTURE_RUNTIME,
    PS3_SCENE_DYN_MODEL_RUNTIME,
)
from cod4porter.zone_writer import RelocatingZoneWriter, ZONE_HEADER_SIZE


assert PS3_GFX_TEXTURE_RUNTIME==0x18
assert PS3_SCENE_DYN_MODEL_RUNTIME==0x06
assert PS3_GFX_DRAW_SURF_RUNTIME==0x08
assert PS3_DPVS_ALIGNED_RUNTIME==0x04
assert POSE_SIZE+CLIENT_SIZE+COLLISION_SIZE==0x40


def runtime_oracle(counts,smv,sv,dyn_vis0,dyn_vis1):
    # _runtime reads these four values from the source-layout DPVS roots.
    zone=bytearray(0x400)
    struct.pack_into('<ii',zone,0x244+0x24,smv,sv)
    struct.pack_into('<ii',zone,0x2AC,dyn_vis0,dyn_vis1)
    report=SimpleNamespace(root_offset=0,**counts)
    node=object.__new__(GfxWorldNode)
    node.pc_zone=bytes(zone);node.report=report
    writer=RelocatingZoneWriter();runtime_bytes=node._runtime(writer);result=writer.build()
    # Block 1 is purely logical loader memory and must emit no physical payload.
    assert len(result.zone_bytes)==ZONE_HEADER_SIZE
    assert result.block_sizes[1]==runtime_bytes
    return runtime_bytes,node._runtime_contract_allocations


# Exact allocator contracts from CoD4 ELF: BA4C0 texture stride 24,
# BD328 uint32 visibility words, C4208 alignment mask 127, B55B8 draw stride 8.
counts=dict(reflection_probe_count=6,cell_count=1,lightmap_count=1,
    dynamic_model_count=1901,dynamic_brush_count=0,primary_light_count=2,
    sun_primary_light_index=1,static_model_count=1343,static_surface_count=2526,
    static_surface_count_no_decal=0)
size,rows=runtime_oracle(counts,44,80,60,0)
assert size==43440
assert [(r['label'],r['offset'],r['bytes']) for r in rows[:4]]==[
    ('reflection runtime',0,144),('lightmap primary runtime',1168,24),
    ('lightmap secondary runtime',1192,24),('scene dyn model',1220,11406)]
vis=[r for r in rows if r['label'] in ('smodel vis','surface vis','lod','sun shadow')]
assert len(vis)==8 and all(r['offset']%128==0 for r in vis)
assert [r['bytes'] for r in vis]==[176,176,176,320,320,320,352,320]
assert next(r for r in rows if r['label']=='surface materials runtime')['bytes']==20208
assert POSE_SIZE+CLIENT_SIZE+COLLISION_SIZE==64
print('test_gfxworld_runtime_block1 PASS')
