import struct,sys
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
from cod4porter.pc_gfxworld import parse_gfxworld,PC_ROOT,PC_WORLD_VERTEX,PC_CELL,PC_BRUSH_MODEL,PC_SURFACE
from cod4porter.assets.gfxworld import GfxWorldNode,PS3_ROOT,ps3_vertex_layer_offset
from cod4porter.graph import write_graph
from cod4porter.backend.v4_ffio import parse_xasset_list
F=0xffffffff

def fixture():
    name='maps/mp/test.d3dbsp';phys=PC_ROOT+len(name)+1+0x14+2+PC_CELL+2+PC_BRUSH_MODEL+PC_WORLD_VERTEX+PC_SURFACE
    z=bytearray(phys);r=0
    def w(o,v):struct.pack_into('<I',z,r+o,v)
    w(0x08,1);w(0x0c,1);w(0x10,0);w(0x18,1);w(0x20,0);w(0x30,1);w(0x3c,0)
    w(0xd8,0);w(0xdc,0);w(0xe0,0);z[0xe4]=0;w(0xf0,1);w(0x100,0);w(0x108,0);w(0x150,1);w(0x154,F);w(0x170,0x12345678);w(0x174,0)
    w(0xf4,F);w(0xf8,F);w(0x104,F);w(0x34,F);w(0x110+0x1c,F)
    dp=0x244
    for o in range(0,0x2c,4):w(dp+o,0)
    w(0x294,F);w(0x2ac+8,0);w(0x2ac+0xc,0)
    c=PC_ROOT;z[c:c+len(name)+1]=name.encode()+b'\0';c+=len(name)+1
    c+=0x14;c+=2;c+=PC_CELL;c+=2;c+=PC_BRUSH_MODEL
    # vertex: finite positions/coords
    struct.pack_into('<4f',z,c,1.0,2.0,3.0,1.0);struct.pack_into('<I',z,c+0x10,0xffffffff);struct.pack_into('<4f',z,c+0x14,0.25,0.5,0.1,0.2);c+=PC_WORLD_VERTEX
    # surface: no triangles, one vertex, null material
    struct.pack_into('<iH',z,c+4,0,1);struct.pack_into('<H',z,c+0xa,0);struct.pack_into('<i',z,c+0xc,0);c+=PC_SURFACE
    return bytes(z),name

def main():
    assert ps3_vertex_layer_offset(0,4,4096)==4*0x1c
    try:ps3_vertex_layer_offset(8,4,4096)
    except ValueError:pass
    else:raise AssertionError('extra PC vertex-layer streams must fail closed')
    z,name=fixture();rep=parse_gfxworld(z,name,{'planes':1,'submodels':1,'static_models':0,'dynamic_models':0,'dynamic_brushes':0})
    node=GfxWorldNode(z,rep,'gfx:test');res=write_graph([node]);al=parse_xasset_list(res.zone.zone_bytes,'ps3')
    assert len(al.assets)==1 and al.assets[0].type_id==0x12
    rr=res.context.results['gfxworld:gfx:test'];root=rr['root_physical'];zone=res.zone.zone_bytes
    assert struct.unpack_from('>I',zone,root+8)[0]==1 and struct.unpack_from('>I',zone,root+0x1c)[0]==1
    assert rr['index_count']==0 and rr['vertex_count']==1 and rr['surface_count']==1
    assert len(zone)>=root+PS3_ROOT
    print({'passed':True,'zone_bytes':len(zone),'block_sizes':res.zone.block_sizes,'root':hex(root)})
if __name__=='__main__':main()
