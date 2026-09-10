from __future__ import annotations
from dataclasses import dataclass
import struct
from pathlib import Path

from .backend.v4_ffio import read_ps3_fastfile, build_ps3_fastfile, parse_zone_header, parse_xasset_list, require_retail_ps3_adler32, FOLLOWING, NULL
from .zone_writer import declared_block_size

TECH_ROOT=0x70; TECH_SLOTS=26; MAT_ROOT=0x80; TEXDEF=0x0C; CONSTDEF=0x20; STATEBITS=0x0C; IMAGE_ROOT=0x34; IMAGE_DESC=0x28; RAW_ROOT=0x0C
EXPECTED_TYPES=(0x07,0x04,0x04,0x04,0x21); TEMP_BLOCK=0; VIRTUAL_BLOCK=4; RESOURCE_BLOCK=6

def u16(b,o):return struct.unpack_from('>H',b,o)[0]
def u32(b,o):return struct.unpack_from('>I',b,o)[0]
def cstr(b,c):
    e=b.find(b'\0',c)
    if e<0:raise ValueError('unterminated string')
    return b[c:e].decode('latin-1'),e+1

def z(s:str)->bytes:return s.encode('latin-1')+b'\0'
def align(value:int,boundary:int)->int:return (value+boundary-1)&~(boundary-1)

@dataclass(frozen=True)
class LoadImage:
    map_type:int; descriptor:bytes; name:str; resource:bytes
@dataclass(frozen=True)
class LoadTexture:
    header8:bytes; image:LoadImage
@dataclass(frozen=True)
class LoadMaterial:
    root_prefix:bytes; name:str; textures:tuple[LoadTexture,...]; constants:tuple[bytes,...]; states:tuple[bytes,...]
@dataclass(frozen=True)
class LoadRawFile:
    name:str; data:bytes
@dataclass(frozen=True)
class LoadDocument:
    external_memory_size:int; block_sizes:tuple[int,...]; preferred_capacity:int; trailer:bytes
    technique_state:bytes; technique_slots:tuple[int,...]; technique_name:str
    materials:tuple[LoadMaterial,...]; rawfile:LoadRawFile


def compute_load_block_sizes(doc:LoadDocument,levelbriefing_resource:bytes|None=None)->tuple[int,...]:
    """Replay the PS3 load-zone allocator, independently of physical stream size.

    The five XAsset records and all normal members live in block 4.  Asset roots
    are temporary block-0 allocations.  A serialized 0x0c state record is split
    logically into a 4-byte reusable pointer cell in block 4 plus an 8-byte
    GfxStateBits object in TEMP.  Inline image pixels are 0x80-aligned in block 6.
    """
    if len(doc.materials)!=3:raise ValueError(f'expected three load materials, got {len(doc.materials)}')
    b4=len(EXPECTED_TYPES)*8
    b4+=len(z(doc.technique_name))
    b0=TECH_ROOT
    b6=0
    replaced=0
    for m in doc.materials:
        b0=max(b0,MAT_ROOT)
        b4+=len(z(m.name))
        if m.textures:
            b4=align(b4,4)
            b4+=len(m.textures)*TEXDEF
        for t in m.textures:
            resource=t.image.resource
            if levelbriefing_resource is not None and m.name=='$levelbriefing':
                resource=levelbriefing_resource;replaced+=1
            b0=max(b0,MAT_ROOT+IMAGE_ROOT+(0x10 if resource else 0))
            b4+=len(z(t.image.name))
            if resource:
                b6=align(b6,0x80)+len(resource)
        if m.constants:
            b4=align(b4,0x10)
            b4+=len(m.constants)*CONSTDEF
        if m.states:
            if any(len(state)!=STATEBITS or u32(state,0)!=FOLLOWING for state in m.states):
                raise ValueError(f"material '{m.name}' has a non-retail GfxStateBits record")
            b4=align(b4,4)
            b4+=len(m.states)*4
            b0=max(b0,MAT_ROOT+8)
    if levelbriefing_resource is not None and replaced!=1:raise ValueError(f'expected one levelbriefing replacement, got {replaced}')
    b0=max(b0,RAW_ROOT)
    b4+=len(z(doc.rawfile.name))+len(doc.rawfile.data)+1
    # Verified against the real Retail PS3 mp_shipment_load.ff: this replay lands on
    # its declared Block 4 = 198 exactly.  The Fix89B mp_getaway_load baseline also
    # declares 198 while its own content ends at 197 - that value was inherited from
    # the Shipment header and is not a rounding rule.
    return tuple(declared_block_size(x) for x in (b0,0,0,0,b4,0,b6))


def read_load_document(path:str|Path)->LoadDocument:
    ff=read_ps3_fastfile(path);zbytes=ff.zone;h=parse_zone_header(zbytes,'ps3');al=parse_xasset_list(zbytes,'ps3')
    if tuple(a.type_id for a in al.assets)!=EXPECTED_TYPES:raise ValueError('not expected COD4 MP load profile')
    if any(a.serialized_pointer!=FOLLOWING for a in al.assets):raise ValueError('load roots not following')
    c=al.asset_data_offset
    if u32(zbytes,c)!=FOLLOWING:raise ValueError('tech name not following')
    state=zbytes[c+4:c+8];slots=tuple(u32(zbytes,c+8+i*4) for i in range(TECH_SLOTS));c+=TECH_ROOT;tech,c=cstr(zbytes,c)
    mats=[]
    for _ in range(3):
        root=c
        if u32(zbytes,root)!=FOLLOWING:raise ValueError('material name not following')
        prefix=bytes(zbytes[root:root+MAT_ROOT]);texc=prefix[0x32];constc=prefix[0x33];statec=prefix[0x34]
        c=root+MAT_ROOT;name,c=cstr(zbytes,c)
        tex_headers=[]
        for i in range(texc):
            rec=bytes(zbytes[c+i*TEXDEF:c+(i+1)*TEXDEF]);tex_headers.append(rec)
        c+=texc*TEXDEF;textures=[]
        for rec in tex_headers:
            if struct.unpack_from('>I',rec,8)[0]!=FOLLOWING:raise ValueError('image not following')
            ir=c;map_type=u32(zbytes,ir);desc=bytes(zbytes[ir+4:ir+4+IMAGE_DESC]);rsize=u32(desc,0x1C)
            rp=u32(zbytes,ir+0x2C);np=u32(zbytes,ir+0x30)
            if np!=FOLLOWING or rp!=(FOLLOWING if rsize else NULL):raise ValueError('bad image pointers')
            c=ir+IMAGE_ROOT;iname,c=cstr(zbytes,c);resource=bytes(zbytes[c:c+rsize]);c+=rsize
            textures.append(LoadTexture(rec[:8],LoadImage(map_type,desc,iname,resource)))
        constants=tuple(bytes(zbytes[c+i*CONSTDEF:c+(i+1)*CONSTDEF]) for i in range(constc));c+=constc*CONSTDEF
        states=tuple(bytes(zbytes[c+i*STATEBITS:c+(i+1)*STATEBITS]) for i in range(statec));c+=statec*STATEBITS
        mats.append(LoadMaterial(prefix,name,tuple(textures),constants,states))
    rr=c;namep,length,bufp=struct.unpack_from('>III',zbytes,rr)
    if namep!=FOLLOWING or bufp!=FOLLOWING:raise ValueError('rawfile pointers bad')
    c=rr+RAW_ROOT;rname,c=cstr(zbytes,c);data=bytes(zbytes[c:c+length]);c+=length
    if zbytes[c]!=0:raise ValueError('rawfile terminator missing')
    c+=1
    return LoadDocument(h.external_memory_size,h.block_sizes,len(zbytes),ff.trailer,state,slots,tech,tuple(mats),LoadRawFile(rname,data))


def build_load_zone(doc:LoadDocument, levelbriefing_resource:bytes|None=None, capacity:int|None=None)->bytes:
    out=bytearray(b'\0'*36)
    # XAssetList and five source-order records.
    out += struct.pack('>4I',0,NULL,5,FOLLOWING)
    for t in EXPECTED_TYPES:out += struct.pack('>2I',t,FOLLOWING)
    # TechniqueSet: preserve measured four state bytes and slots.
    out += struct.pack('>I',FOLLOWING)+doc.technique_state+b''.join(struct.pack('>I',x) for x in doc.technique_slots)
    if len(out)-36-16-40 != TECH_ROOT: raise AssertionError('tech root size')
    out += z(doc.technique_name)
    resource_total=0;replaced=0
    # XAssetHeader alias #0 = block4+4. Retail load materials point to this exact header.
    tech_alias=((4<<29)|4)+1
    for m in doc.materials:
        root=bytearray(m.root_prefix)
        # Reassert deterministic local pointers/tech alias while preserving all scalar/platform bytes.
        root[0:4]=struct.pack('>I',FOLLOWING)
        texc=len(m.textures);constc=len(m.constants);statec=len(m.states)
        root[0x32]=texc;root[0x33]=constc;root[0x34]=statec
        # Preserve whether this measured material used a TechniqueSet; only normalize valid alias.
        oldtech=u32(m.root_prefix,0x70);root[0x70:0x74]=struct.pack('>I',tech_alias if oldtech!=NULL else NULL)
        root[0x74:0x78]=struct.pack('>I',FOLLOWING if texc else NULL)
        root[0x78:0x7C]=struct.pack('>I',FOLLOWING if constc else NULL)
        root[0x7C:0x80]=struct.pack('>I',FOLLOWING if statec else NULL)
        out += root+z(m.name)
        for t in m.textures:
            out += t.header8+struct.pack('>I',FOLLOWING)
        for t in m.textures:
            resource=t.image.resource
            if levelbriefing_resource is not None and m.name=='$levelbriefing':resource=levelbriefing_resource;replaced+=1
            desc=bytearray(t.image.descriptor);struct.pack_into('>I',desc,0x1C,len(resource))
            out += struct.pack('>I',t.image.map_type)+bytes(desc)+struct.pack('>II',FOLLOWING if resource else NULL,FOLLOWING)+z(t.image.name)+resource
            resource_total += len(resource)
        for x in m.constants:out += x
        for x in m.states:out += x
    if levelbriefing_resource is not None and replaced!=1:raise ValueError(f'expected one levelbriefing replacement, got {replaced}')
    out += struct.pack('>III',FOLLOWING,len(doc.rawfile.data),FOLLOWING)+z(doc.rawfile.name)+doc.rawfile.data+b'\0'
    content_end=len(out)
    blocks=compute_load_block_sizes(doc,levelbriefing_resource)
    if blocks[RESOURCE_BLOCK]!=resource_total:raise AssertionError(f'logical resource block {blocks[RESOURCE_BLOCK]} != physical resources {resource_total}')
    struct.pack_into('>I',out,0,content_end-36);struct.pack_into('>I',out,4,doc.external_memory_size)
    for i,x in enumerate(blocks):struct.pack_into('>I',out,8+i*4,x)
    target=doc.preferred_capacity if capacity is None else capacity
    # Every Retail PS3 zone is padded to a whole 64-KiB decompression block; the Retail
    # mp_shipment_load.ff is 19 x 65536.  A 4-KiB fallback produced a zone whose tail
    # block was short of the container's own frame size.
    if target<content_end or target%0x10000:target=(max(target,content_end)+0xFFFF)&~0xFFFF
    out.extend(b'\0'*(target-len(out)))
    return bytes(out)


def write_load_fastfile(source_baseline:str|Path, output:str|Path, levelbriefing_resource:bytes|None=None)->dict:
    src=read_load_document(source_baseline);source_zone=read_ps3_fastfile(source_baseline).zone;zone=build_load_zone(src,levelbriefing_resource)
    ff=build_ps3_fastfile(zone,src.trailer);Path(output).write_bytes(ff)
    # strict independent readback
    output_ff=read_ps3_fastfile(output)
    retail_adler32_frames=require_retail_ps3_adler32(output_ff)
    check=read_load_document(output)
    return {
        'output':str(Path(output).resolve()),
        'zone_bytes':len(zone),
        'file_bytes':len(ff),
        'material_count':len(check.materials),
        'resource_bytes':sum(len(t.image.resource) for m in check.materials for t in m.textures),
        'block_sizes':check.block_sizes,
        'source_block_sizes':src.block_sizes,
        'logical_header_recomputed':check.block_sizes==compute_load_block_sizes(check),
        'physical_payload_exact_vs_source':source_zone[36:]==zone[36:],
        'retail_adler32_frames':retail_adler32_frames,
        'exact_zone_vs_source':source_zone==zone,
    }
