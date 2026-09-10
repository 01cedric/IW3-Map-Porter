import struct,sys
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from cod4porter.pc_fx import parse_owned_fx_at,VisualKind,ImpactFx,IMPACT_REFERENCE_COUNT,PS3_XMODEL_ALIAS_ROOT_SIZE
from cod4porter.assets.fx import OwnedFxNode,ImpactFxNode
from cod4porter.assets.basic import ExternalFxNode
from cod4porter.graph import write_graph
from cod4porter.backend.v4_ffio import parse_xasset_list


def main():
    z=bytearray(0x20+len(b'fx/test\0')+0xfc+len(b'texture/test\0'))
    struct.pack_into('<I',z,0,0xffffffff)
    struct.pack_into('<I',z,0x10,1)
    struct.pack_into('<I',z,0x1c,0xffffffff)
    c=0x20;z[c:c+8]=b'fx/test\0';c+=8
    e=c;c+=0xfc
    z[e+0xb0]=8;z[e+0xb1]=1
    struct.pack_into('<I',z,e+0xbc,0xffffffff)
    z[c:c+13]=b'texture/test\0';c+=13
    g=parse_owned_fx_at(bytes(z),0)
    assert g.name=='fx/test' and g.physical_end_offset==len(z)
    assert len(g.elements)==1 and g.elements[0].visuals[0].kind==VisualKind.STRING
    r=write_graph([OwnedFxNode(g,{},'fx:test')])
    al=parse_xasset_list(r.zone.zone_bytes,'ps3')
    assert len(al.assets)==1 and al.assets[0].type_id==0x1b
    rr=r.context.results['fx.owned:fx:test'];assert rr['element_count']==1 and rr['string_visuals']==1

    # PC parses a 0xDC XModel shell, but PS3 must emit the 0xCC ABI and restore
    # the comma that marks this zero-body shell as a native/shared XModel alias.
    fxname=b'fx/model_alias\0';modelname=b',model/test\0'
    zmodel=bytearray(0x20+len(fxname)+0xfc+0xdc+len(modelname))
    struct.pack_into('<I',zmodel,0,0xffffffff);struct.pack_into('<I',zmodel,0x10,1);struct.pack_into('<I',zmodel,0x1c,0xffffffff)
    cursor=0x20;zmodel[cursor:cursor+len(fxname)]=fxname;cursor+=len(fxname);elem=cursor;cursor+=0xfc
    zmodel[elem+0xb0]=5;zmodel[elem+0xb1]=1;struct.pack_into('<I',zmodel,elem+0xbc,0xffffffff)
    struct.pack_into('<I',zmodel,cursor,0xffffffff);cursor+=0xdc;zmodel[cursor:cursor+len(modelname)]=modelname
    model_graph=parse_owned_fx_at(bytes(zmodel),0);assert model_graph.elements[0].visuals[0].name=='model/test'
    model_out=write_graph([OwnedFxNode(model_graph,{},'fx:model_alias')]);model_zone=model_out.zone.zone_bytes
    name_at=model_zone.find(modelname);assert name_at>0
    alias_root=name_at-PS3_XMODEL_ALIAS_ROOT_SIZE
    assert struct.unpack_from('>I',model_zone,alias_root)[0]==0xffffffff
    assert not any(model_zone[alias_root+4:name_at])
    assert model_out.context.results['fx.owned:fx:model_alias']['xmodel_aliases']==1

    refs=(0,)+(None,)*(IMPACT_REFERENCE_COUNT-1)
    impact=ImpactFx(99,0,0,'impactfx',refs)
    assets=[ExternalFxNode('fx/base','fx:base'),ImpactFxNode(impact,{0:'fx:base'},'impact:main')]
    r2=write_graph(assets);al2=parse_xasset_list(r2.zone.zone_bytes,'ps3')
    assert [a.type_id for a in al2.assets]==[0x1b,0x1c]
    assert r2.context.results['impactfx:impact:main']['nonnull']==1
    print('PASS test_fx')

if __name__=='__main__':main()
