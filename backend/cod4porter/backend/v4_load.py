from __future__ import annotations
from dataclasses import dataclass, asdict
import hashlib, struct
from .v4_ffio import FOLLOWING, NULL, parse_zone_header, parse_xasset_list, decode_ps3_pointer, sha256

TECH_ROOT=0x70
TECH_SLOTS=26
MAT_ROOT=0x80
STATE_ENTRY=0x1A
PLATFORM_STATE=0x38
TEXDEF=0x0C
CONSTDEF=0x20
STATEBITS=0x0C
IMAGE_ROOT=0x34
IMAGE_DESC=0x28
RAW_ROOT=0x0C
RESOURCE_BLOCK=6
ASSET_DATA_BLOCK=4
EXPECTED_TYPES=[0x07,0x04,0x04,0x04,0x21]

def u16(b,o): return struct.unpack_from('>H',b,o)[0]
def u32(b,o): return struct.unpack_from('>I',b,o)[0]
def u64(b,o): return struct.unpack_from('>Q',b,o)[0]
def f32(b,o): return struct.unpack_from('>f',b,o)[0]
def cstr(b,c):
    e=b.find(b'\0',c)
    if e<0: raise ValueError(f'unterminated string @0x{c:X}')
    return b[c:e].decode('latin-1'),e+1

def alias_pointer(asset_index:int, block_sizes:tuple[int,...]) -> int:
    off=asset_index*8+4
    if off>=block_sizes[ASSET_DATA_BLOCK]: raise ValueError('asset alias outside block4')
    return ((ASSET_DATA_BLOCK<<29)|off)+1

@dataclass
class Image:
    root:int; map_type:int; name:str; format:int; mip_count:int; dimension:int; remap:int
    width:int; height:int; depth:int; pitch:int; resource_size:int; category:int; delay:int
    resource_sha256:str; resource_pointer:int; name_pointer:int

@dataclass
class Material:
    root:int; name:str; game_flags:int; sort_key:int; rows:int; cols:int; draw_surf:int
    surface_type_bits:int; hash_index:int; state_bits_entry_hex:str; texture_count:int
    constant_count:int; state_bits_count:int; state_flags:int; camera_region:int
    platform_state_hex:str; technique_set_pointer:int; texture_table_pointer:int
    constant_table_pointer:int; state_bits_table_pointer:int; textures:list[dict]; state_bits:list[list[int]]

@dataclass
class LoadProfile:
    script_strings:int; xassets:int; asset_type_order:list[str]; technique_name:str
    technique_state_hex:str; materials:list[Material]; rawfile_name:str; rawfile_bytes:int
    resource_bytes:int; serialized_content_end:int; trailing_capacity:int; trailing_nonzero:int
    zone_memory_size:int; declared_resource_bytes:int


def parse_image(zone:bytes,cursor:int,prefix:str)->tuple[Image,int]:
    if cursor+IMAGE_ROOT>len(zone): raise ValueError(prefix+' image root truncated')
    root=cursor; map_type=u32(zone,root); desc=zone[root+4:root+4+IMAGE_DESC]
    fmt=desc[0]; mip=desc[1]; dim=desc[2]; remap=u32(desc,4); w=u16(desc,8); h=u16(desc,0x0A); d=u16(desc,0x0C)
    pitch=u32(desc,0x10); resource_size=u32(desc,0x1C); category=desc[0x26]; delay=desc[0x27]
    resource_ptr=u32(zone,root+0x2C); name_ptr=u32(zone,root+0x30)
    expected=NULL if resource_size==0 else FOLLOWING
    if resource_ptr!=expected: raise ValueError(f'{prefix} resource pointer 0x{resource_ptr:08X} != 0x{expected:08X}')
    if name_ptr!=FOLLOWING: raise ValueError(prefix+' image name pointer not FOLLOWING')
    cursor=root+IMAGE_ROOT; name,cursor=cstr(zone,cursor)
    if cursor+resource_size>len(zone): raise ValueError(prefix+' resource truncated')
    data=zone[cursor:cursor+resource_size]; cursor+=resource_size
    return Image(root,map_type,name,fmt,mip,dim,remap,w,h,d,pitch,resource_size,category,delay,sha256(data),resource_ptr,name_ptr),cursor


def parse_material(zone:bytes,cursor:int,tech_alias:int,index:int)->tuple[Material,int]:
    p=f'Material[{index}]'; root=cursor
    if root+MAT_ROOT>len(zone): raise ValueError(p+' root truncated')
    if u32(zone,root)!=FOLLOWING: raise ValueError(p+' name pointer not FOLLOWING')
    game=zone[root+4]; sort=zone[root+5]; rows=zone[root+6]; cols=zone[root+7]
    draw=u64(zone,root+8); surface=u32(zone,root+0x10); hidx=u16(zone,root+0x14)
    if u16(zone,root+0x16)!=0: raise ValueError(p+' align word nonzero')
    entry=zone[root+0x18:root+0x18+STATE_ENTRY]
    texc=zone[root+0x32]; constc=zone[root+0x33]; statec=zone[root+0x34]; stateflags=zone[root+0x35]; camera=zone[root+0x36]
    if zone[root+0x37]!=0: raise ValueError(p+' count align byte nonzero')
    platform=zone[root+0x38:root+0x38+PLATFORM_STATE]
    tech=u32(zone,root+0x70); tptr=u32(zone,root+0x74); cptr=u32(zone,root+0x78); sptr=u32(zone,root+0x7C)
    if tech not in (NULL,tech_alias): raise ValueError(f'{p} technique alias 0x{tech:08X} invalid')
    for name,ptr,count in [('texture',tptr,texc),('constant',cptr,constc),('stateBits',sptr,statec)]:
        exp=NULL if count==0 else FOLLOWING
        if ptr!=exp: raise ValueError(f'{p} {name} pointer 0x{ptr:08X} != 0x{exp:08X}')
    cursor=root+MAT_ROOT; name,cursor=cstr(zone,cursor)
    headers=[]
    if cursor+texc*TEXDEF>len(zone): raise ValueError(p+' texture table truncated')
    for j in range(texc):
        r=cursor+j*TEXDEF
        headers.append((r,u32(zone,r),zone[r+4],zone[r+5],zone[r+6],zone[r+7],u32(zone,r+8)))
    cursor+=texc*TEXDEF
    textures=[]
    for j,hdr in enumerate(headers):
        r,nh,ns,ne,samp,sem,imgptr=hdr
        if imgptr!=FOLLOWING: raise ValueError(f'{p} texture[{j}] image pointer not FOLLOWING')
        img,cursor=parse_image(zone,cursor,f'{p}.texture[{j}]')
        textures.append({'record_offset':r,'name_hash':f'{nh:08X}','name_start':ns,'name_end':ne,'sampler':samp,'semantic':sem,'image_pointer':imgptr,'image':asdict(img)})
    # constants are retained only as raw hashes and floats; load baseline normally has none.
    if cursor+constc*CONSTDEF>len(zone): raise ValueError(p+' constants truncated')
    cursor+=constc*CONSTDEF
    states=[]
    for j in range(statec):
        if cursor+STATEBITS>len(zone): raise ValueError(p+' statebits truncated')
        states.append([u32(zone,cursor),u32(zone,cursor+4),u32(zone,cursor+8)]); cursor+=STATEBITS
    return Material(root,name,game,sort,rows,cols,draw,surface,hidx,entry.hex().upper(),texc,constc,statec,stateflags,camera,platform.hex().upper(),tech,tptr,cptr,sptr,textures,states),cursor


def parse_load_zone(zone:bytes)->LoadProfile:
    header=parse_zone_header(zone,'ps3'); assets=parse_xasset_list(zone,'ps3')
    if len(assets.script_strings)!=0: raise ValueError('load profile requires zero script strings')
    if len(assets.assets)!=5: raise ValueError('load profile requires exactly five assets')
    got=[a.type_id for a in assets.assets]
    if got!=EXPECTED_TYPES: raise ValueError(f'load asset type order mismatch {got}')
    if any(a.serialized_pointer!=FOLLOWING for a in assets.assets): raise ValueError('load top-level roots must all be FOLLOWING')
    cursor=assets.asset_data_offset
    if cursor+TECH_ROOT>len(zone): raise ValueError('TechniqueSet root truncated')
    if u32(zone,cursor)!=FOLLOWING: raise ValueError('TechniqueSet.name not FOLLOWING')
    state=zone[cursor+4:cursor+8]
    slots=[u32(zone,cursor+8+i*4) for i in range(TECH_SLOTS)]
    if any(slots): raise ValueError('load TechniqueSet slots must all be null')
    cursor+=TECH_ROOT; techname,cursor=cstr(zone,cursor)
    talias=alias_pointer(0,header.block_sizes)
    mats=[]
    for i in range(3):
        m,cursor=parse_material(zone,cursor,talias,i); mats.append(m)
    root=cursor
    if root+RAW_ROOT>len(zone): raise ValueError('RawFile root truncated')
    nameptr,length,bufptr=struct.unpack_from('>III',zone,root)
    if nameptr!=FOLLOWING or bufptr!=FOLLOWING: raise ValueError('RawFile name/buffer must be FOLLOWING')
    cursor=root+RAW_ROOT; rawname,cursor=cstr(zone,cursor)
    if cursor+length+1>len(zone): raise ValueError('RawFile data truncated')
    raw=zone[cursor:cursor+length]
    if zone[cursor+length]!=0: raise ValueError('RawFile missing NUL')
    cursor+=length+1
    resource=sum(t['image']['resource_size'] for m in mats for t in m.textures)
    declared=header.block_sizes[RESOURCE_BLOCK]
    if resource!=declared: raise ValueError(f'resource block {declared} != inline images {resource}')
    parsed_payload=cursor-0x24
    if header.zone_memory_size!=parsed_payload: raise ValueError(f'zone memory size {header.zone_memory_size} != parsed payload {parsed_payload}')
    trailing=zone[cursor:]
    return LoadProfile(0,5,[a.type_name for a in assets.assets],techname,state.hex().upper(),mats,rawname,len(raw),resource,cursor,len(trailing),sum(1 for x in trailing if x),header.zone_memory_size,declared)
