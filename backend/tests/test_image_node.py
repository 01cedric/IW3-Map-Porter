from pathlib import Path
import sys,json,tempfile,zipfile
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
from cod4porter.backend.v4_iwd import parse_iwi
from cod4porter.assets.image import from_iwi,ImageNode
from cod4porter.graph import write_graph

def make_iwi():
 import struct
 payload=b'\x11'*8;hdr=bytearray(28);hdr[:3]=b'IWi';hdr[3]=6;hdr[4]=0x0B;hdr[5]=0x02;struct.pack_into('<HHH',hdr,6,4,4,1);total=36;struct.pack_into('<4I',hdr,12,total,total,total,total);return bytes(hdr)+payload

def main():
 iwi=parse_iwi('images/test.iwi',make_iwi());a=from_iwi(iwi,2);r=write_graph([ImageNode(a,'image:test')]);row=r.context.results['image:image:test'];assert row['resource_bytes']==128 # 8 padded to 0x80
 assert row['loaddef_location'].block==0 and row['loaddef_location'].offset==row['root_location'].offset+0x34
 assert (row['width'],row['height'],row['depth'],row['level_count'],row['face_count'])==(4,4,1,1,1)
 assert row['mips'][0]['width']==4 and row['mips'][0]['height']==4 and row['mips'][0]['bytes']==8
 assert r.zone.block_sizes[0]>=0x44
 print(json.dumps({'passed':True,'resource_bytes':len(a.resource),'unpadded':a.unpadded,'zone_bytes':len(r.zone.zone_bytes)},indent=2))
if __name__=='__main__':main()
