from pathlib import Path
import sys

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))

from cod4porter.physics import *
from cod4porter.graph import write_graph
from cod4porter.backend.v4_ffio import parse_xasset_list
from cod4porter.zone_writer import RelocatingZoneWriter,TEMP_BLOCK,VIRTUAL_BLOCK

preset=PhysPreset('concrete',1,100.0,0.1,0.5,1.0,1.2,0.2,3.0,False,'concrete')
plane=PhysPlane((0,0,1),32.0,2,0)
side=PhysBrushSide(plane,7,0,1)
brush=PhysBrush((-1,-2,-3),(1,2,3),0x100,[side],[1,2,3,4,5,6],b'\x00',[0,0,0,0,0,0],[1,1,1,1,1,1],1,[plane])
geom=PhysGeom(0,[1,0,0,0,1,0,0,0,1],[0,0,0],[1,1,1],brush)
gl=PhysGeomList([0,0,0],[1,1,1],[0,0,0],[geom])
assert len(build_phys_preset_root(preset))==0x2c
assert len(build_phys_geom_list_root(gl))==0x2c
assert len(build_phys_geom_root(geom))==0x44
assert len(build_brush_root(brush))==0x50
assert len(build_brush_side(side))==0x0c
assert len(build_plane(plane))==0x14
result=write_graph([PhysPresetNode(preset,'phys:concrete')])
a=parse_xasset_list(result.zone.zone_bytes,'ps3')
assert len(a.assets)==1 and a.assets[0].type_id==1
asset=result.context.results['physpreset:phys:concrete']
assert asset['root_location'].block==TEMP_BLOCK and asset['root_location'].offset%4==0
assert asset['name_location'].block==VIRTUAL_BLOCK
for prefix in (None, '', 'metal'):
    from dataclasses import replace
    import struct
    p=replace(preset,sound_alias_prefix=prefix)
    output=write_graph([PhysPresetNode(p,'phys:prefix')])
    root=output.context.results['physpreset:phys:prefix']['root_physical']
    z=output.zone.zone_bytes
    assert struct.unpack_from('>I',z,root+0x1c)[0]==FOLLOWING
    start=root+0x2c+len(p.name.encode('latin1'))+1
    expected=(prefix or '').encode('latin1')+b'\0'
    assert z[start:start+len(expected)]==expected
# inline list standalone writer smoke test
w=RelocatingZoneWriter();w.push_block(VIRTUAL_BLOCK);stats=write_inline_phys_geom_list(w,gl,'test');w.pop_block();z=w.build()
assert stats['geom_count']==1 and stats['brush_count']==1 and stats['side_count']==1
assert stats['root_location'].block==VIRTUAL_BLOCK and stats['root_location'].offset%4==0
print({'passed':True,'physgeom_zone_bytes':len(z.zone_bytes),'asset_zone_bytes':len(result.zone.zone_bytes)})
