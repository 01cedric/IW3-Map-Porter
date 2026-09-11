from __future__ import annotations
from dataclasses import dataclass
import struct
from .backend.v4_ffio import FOLLOWING,decode_pc_pointer

ROOT=0x94;SLOTS=34

def u32(z,o):return struct.unpack_from('<I',z,o)[0]
def _pointer_like(raw:int)->bool:
    p=decode_pc_pointer(raw);return p.kind in ('null','following','insert','packed')
def _read_ascii(z,start,max_bytes=180):
    end=z.find(b'\0',start,min(len(z),start+max_bytes))
    if end<0 or end==start:return None
    b=z[start:end]
    if any(x<0x20 or x>0x7e for x in b):return None
    return b.decode('ascii')
def _name_ok(n:str)->bool:
    if not n or len(n)>160 or n in {'vz','z','d_cinematic','nematic','JJ','0'} or all(c=='p' for c in n):return False
    if any(not(c.isalnum() or c in ',_./~-') for c in n):return False
    return n.startswith(',') or n.lower().startswith('sm2/') or '_' in n or '/' in n

@dataclass(frozen=True)
class TechniqueSet:
    root_offset:int;name:str;world_vertex_format:int;has_been_uploaded:bool;remapped_pointer:int;technique_pointers:tuple[int,...];source_asset_index:int=-1
    @property
    def shared(self):return self.name.startswith(',')
    @property
    def ps3_reference_candidate(self):
        n=self.name.lstrip(',')
        return n[4:] if n.lower().startswith('sm2/') else n

def parse_at(z:bytes,root:int)->TechniqueSet|None:
    if root<0 or root+ROOT+1>len(z) or u32(z,root)!=FOLLOWING:return None
    vf=z[root+4];uploaded=z[root+5]
    if vf>8 or uploaded>1 or z[root+6:root+8]!=b'\0\0':return None
    remap=u32(z,root+8)
    if not _pointer_like(remap):return None
    ptrs=tuple(u32(z,root+12+i*4) for i in range(SLOTS))
    if any(not _pointer_like(x) for x in ptrs):return None
    n=_read_ascii(z,root+ROOT)
    if n is None or not _name_ok(n):return None
    return TechniqueSet(root,n,vf,bool(uploaded),remap,ptrs)

def scan_technique_sets(z:bytes)->list[TechniqueSet]:
    out=[];marker=b'\xff\xff\xff\xff';pos=0;last=len(z)-ROOT-1
    while pos<=last:
        r=z.find(marker,pos,last+4)
        if r<0:break
        rec=parse_at(z,r)
        if rec is not None:out.append(rec)
        pos=r+1
    return out

def top_level_technique_sets(z:bytes,asset_list)->list[TechniqueSet]:
    if asset_list.structural_index is not None:
        out=[]
        for row in asset_list.structural_index.rows(5):
            r=row['root']
            out.append(TechniqueSet(r,row['name'],z[r+4],bool(z[r+5]),u32(z,r+8),
                tuple(u32(z,r+12+i*4) for i in range(SLOTS)),row['index']))
        return out
    assets=[a for a in asset_list.assets if a.type_id==0x05]
    scanned=sorted(scan_technique_sets(z),key=lambda x:x.root_offset)
    if len(scanned)!=len(assets):
        # The byte scanner intentionally starts permissive because TechniqueSet roots have no
        # self-describing length.  On real IW3 zones shader/material byte streams can mimic a
        # root closely enough to pass the structural test.  Keep only candidates whose names
        # belong to the closed IW3/native TechniqueSet grammar used by the PS3 binder.  A shared
        # comma-prefix by itself is not sufficient evidence: arbitrary bytes such as ',AbA' or
        # ',' can occur inside unrelated payloads and previously caused Getaway to overcount
        # 530 candidates for 238 TechniqueSet XAssets.
        from .technique_binding import canonical_stock_name,audited_retail_names
        retail={n.casefold() for n in audited_retail_names()}
        def semantically_closed(t:TechniqueSet)->bool:
            c=t.ps3_reference_candidate
            return bool(c) and (canonical_stock_name(c) or c.casefold() in retail)
        closed=[t for t in scanned if semantically_closed(t)]
        if len(closed)==len(assets):
            scanned=closed
        else:
            raise ValueError(f'TechniqueSet XAsset/scanner cardinality differs ({len(assets)} vs {len(scanned)}; closed={len(closed)})')
    return [TechniqueSet(x.root_offset,x.name,x.world_vertex_format,x.has_been_uploaded,x.remapped_pointer,x.technique_pointers,a.index) for a,x in zip(assets,scanned)]
