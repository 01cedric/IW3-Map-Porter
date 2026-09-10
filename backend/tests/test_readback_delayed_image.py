import json,struct
from cod4porter.backend.v4_iwd import parse_iwi
from cod4porter.assets.image import from_iwi,ImageNode,ImageResourcePolicy
from cod4porter.graph import write_graph
from cod4porter.readback import _family_check


def image_asset():
    payload=b'\xA5'*8;header=bytearray(28);header[:3]=b'IWi';header[3]=6;header[4]=0x0B;header[5]=0x02
    struct.pack_into('<HHH',header,6,4,4,1);total=len(header)+len(payload);struct.pack_into('<4I',header,12,total,total,total,total)
    return from_iwi(parse_iwi('images/readback.iwi',bytes(header)+payload),2)


def check(policy):
    node=ImageNode(image_asset(),'image:readback',policy);result=write_graph([node])
    row=result.context.results['image:image:readback']
    return _family_check(result.zone.zone_bytes,result.zone.block_sizes,node,row)


def main():
    contained=check(ImageResourcePolicy.SELF_CONTAINED);delayed=check(ImageResourcePolicy.RETAIL_DELAYED)
    assert contained['passed'],contained
    assert delayed['passed'],delayed
    assert contained['proof']['delay_load_pixels']==0
    assert delayed['proof']['delay_load_pixels']==1
    assert delayed['proof']['resource_physical']==-1
    print(json.dumps({'passed':True,'self_contained':contained['proof'],'retail_delayed':delayed['proof']},indent=2))


if __name__=='__main__':main()
