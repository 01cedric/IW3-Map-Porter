import struct,sys
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
from cod4porter.pc_gfxworld import parse_gfxworld,PC_ROOT,PC_WORLD_VERTEX,PC_CELL,PC_BRUSH_MODEL,PC_SURFACE
F=0xffffffff

def main():
    name='maps/mp/test.d3dbsp';phys=PC_ROOT+len(name)+1+0x14+2+PC_CELL+2+PC_BRUSH_MODEL+PC_WORLD_VERTEX+PC_SURFACE
    z=bytearray(phys);r=0
    def w(o,v):struct.pack_into('<I',z,r+o,v)
    w(0x08,1);w(0x0c,1);w(0x10,0);w(0x18,1);w(0x20,0);w(0x30,1);w(0x3c,0)
    w(0xd8,0);w(0xdc,0);w(0xe0,0);z[0xe4]=0;w(0xf0,1);w(0x100,0);w(0x108,0);w(0x150,1);w(0x154,F);w(0x170,0x12345678);w(0x174,0)
    w(0xf4,F);w(0xf8,F);w(0x104,F);w(0x34,F)
    # lightgrid embedded root at 0x110: row axis/col axis 0, mins/maxs all zero => 1 row
    w(0x110+0x1c,F)
    # dpvs
    dp=0x244;w(dp+0,0);w(dp+4,0);w(dp+8,0);w(dp+0x0c,0);w(dp+0x10,0);w(dp+0x14,0);w(dp+0x18,0);w(dp+0x1c,0);w(dp+0x20,0)
    w(0x294,F)
    # dyn counts zero
    w(0x2ac+8,0);w(0x2ac+0xc,0)
    c=PC_ROOT;z[c:c+len(name)+1]=name.encode()+b'\0';c+=len(name)+1
    # plane
    c+=0x14
    # node
    c+=2
    # cell zero nested counts
    c+=PC_CELL
    # lightgrid row data one ushort
    c+=2
    # model
    c+=PC_BRUSH_MODEL
    # vertex
    c+=PC_WORLD_VERTEX
    # surface; material null
    c+=PC_SURFACE
    assert c==len(z)
    rep=parse_gfxworld(bytes(z),name,{'planes':1,'submodels':1,'static_models':0,'dynamic_models':0,'dynamic_brushes':0})
    assert rep.full.physical_end==len(z) and rep.surface_count==1 and rep.vertex_count==1
    assert len(rep.full.branches)==14
    print({'passed':True,'end':hex(rep.full.physical_end),'branches':len(rep.full.branches)})
if __name__=='__main__':main()
