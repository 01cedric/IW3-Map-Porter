import json,struct
from cod4porter.backend.v4_iwd import parse_iwi
from cod4porter.assets.image import from_iwi,ImageNode,ImageResourcePolicy
from cod4porter.graph import write_graph
from cod4porter.zone_writer import FOLLOWING


def make_iwi():
    payload=b'\x11'*8
    header=bytearray(28);header[:3]=b'IWi';header[3]=6;header[4]=0x0B;header[5]=0x02
    struct.pack_into('<HHH',header,6,4,4,1);total=len(header)+len(payload)
    struct.pack_into('<4I',header,12,total,total,total,total)
    return bytes(header)+payload


def build(policy):
    image=from_iwi(parse_iwi('images/test.iwi',make_iwi()),2)
    result=write_graph([ImageNode(image,'image:test',policy)])
    row=result.context.results['image:image:test'];root=row['root_physical'];zone=result.zone.zone_bytes
    return result,row,{
        'declared':struct.unpack_from('>I',zone,root+0x20)[0],
        'delay':zone[root+0x2B],
        'data_marker':struct.unpack_from('>I',zone,root+0x2C)[0],
        'name_marker':struct.unpack_from('>I',zone,root+0x30)[0],
    }


def main():
    contained,crow,cshell=build(ImageResourcePolicy.SELF_CONTAINED)
    delayed,drow,dshell=build(ImageResourcePolicy.RETAIL_DELAYED)
    assert cshell['declared']==dshell['declared']==128
    assert cshell['delay']==0 and dshell['delay']==1
    assert cshell['data_marker']==dshell['data_marker']==FOLLOWING
    assert crow['embedded_resource_bytes']==128 and crow['omitted_resource_bytes']==0
    assert drow['embedded_resource_bytes']==0 and drow['omitted_resource_bytes']==128
    assert crow['resource_physical']>=0 and drow['resource_physical']==-1
    assert crow['loaddef_location'].block==drow['loaddef_location'].block==0
    assert crow['loaddef_location'].offset==crow['root_location'].offset+0x34
    assert drow['loaddef_location'].offset==drow['root_location'].offset+0x34
    assert contained.zone.block_sizes[6]==128 and delayed.zone.block_sizes[6]==0
    assert len(contained.zone.zone_bytes)-len(delayed.zone.zone_bytes)==128
    print(json.dumps({
        'passed':True,'declared_bytes':128,
        'self_contained':{'delay':cshell['delay'],'embedded':crow['embedded_resource_bytes'],'block6':contained.zone.block_sizes[6]},
        'retail_delayed':{'delay':dshell['delay'],'omitted':drow['omitted_resource_bytes'],'block6':delayed.zone.block_sizes[6]},
    },indent=2))


if __name__=='__main__':main()
