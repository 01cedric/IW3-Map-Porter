from __future__ import annotations

from dataclasses import dataclass, asdict
import hashlib
import struct
from pathlib import Path
from typing import Iterable

from .v4_ffio import (
    FOLLOWING, INSERT, NULL,
    decode_pc_pointer, parse_zone_header, parse_xasset_list,
)

RAW_ROOT=0x0C
STRINGTABLE_ROOT=0x10
FX_ROOT=0x20
IMPACT_ROOT=0x08
IMPACT_ROWS=12
IMPACT_NONFLESH=29
IMPACT_FLESH=4
IMPACT_REFS=IMPACT_ROWS*(IMPACT_NONFLESH+IMPACT_FLESH)  # 396
LIGHTDEF_ROOT=0x10
PC_IMAGE_ROOT=0x24
PC_IMAGE_LOADDEF=0x10


def u16(z,o): return struct.unpack_from('<H',z,o)[0]
def u32(z,o): return struct.unpack_from('<I',z,o)[0]
def i32(z,o): return struct.unpack_from('<i',z,o)[0]

def _inline(raw:int)->bool: return decode_pc_pointer(raw).kind in ('following','insert')

def _printable_z(zone:bytes,cursor:int,max_bytes:int,latin1:bool=False,allow_empty:bool=False):
    if cursor<0 or cursor>=len(zone): return None
    end=min(len(zone),cursor+max_bytes)
    p=cursor
    while p<end and zone[p]!=0:
        b=zone[p]
        if latin1:
            if b<0x09 or b>0xFE or b in (0x0B,0x0C): return None
        elif b<0x20 or b>0x7E: return None
        p+=1
    if p>=end or zone[p]!=0 or (p==cursor and not allow_empty): return None
    enc='latin-1' if latin1 else 'ascii'
    return zone[cursor:p].decode(enc),p+1


def _pathish(name:str)->bool:
    if not name or len(name)>260:return False
    allowed=set('_-./\\@')
    return all(c.isalnum() or c in allowed for c in name)

@dataclass(frozen=True)
class RawFile:
    root:int; end:int; name:str; length:int; sha256:str; payload:bytes


def scan_rawfiles(zone:bytes)->list[RawFile]:
    out=[]; search=0
    while search<=len(zone)-RAW_ROOT-2:
        root=search;search+=1
        if zone[root] not in (0xFE,0xFF):continue
        if not _inline(u32(zone,root)) or not _inline(u32(zone,root+8)):continue
        length=u32(zone,root+4)
        if length>16*1024*1024:continue
        r=_printable_z(zone,root+RAW_ROOT,260)
        if not r:continue
        name,cursor=r;name=name.replace('\\','/')
        if not _pathish(name):continue
        if cursor+length>len(zone):continue
        payload=zone[cursor:cursor+length]
        # Conservative text/blob sanity: zero-length is legal; source bytes are never modified.
        out.append(RawFile(root,cursor+length,name,length,hashlib.sha256(payload).hexdigest().upper(),payload))
        search=max(search,cursor+length)
    # same physical/name record only once
    unique={}
    for x in out:unique.setdefault((x.root,x.name.casefold()),x)
    return sorted(unique.values(),key=lambda x:x.root)

@dataclass(frozen=True)
class StringTable:
    root:int; end:int; name:str; columns:int; rows:int; values:tuple[str|None,...]


def scan_stringtables(zone:bytes)->list[StringTable]:
    out=[];root=0x2C
    while root<=len(zone)-STRINGTABLE_ROOT-1:
        if zone[root] not in (0xFE,0xFF):root+=1;continue
        if not _inline(u32(zone,root)):root+=1;continue
        columns=u32(zone,root+4);rows=u32(zone,root+8)
        if not (1<=columns<=1024) or rows>1_000_000:root+=1;continue
        cells=columns*rows
        if cells>1_000_000:root+=1;continue
        vp=decode_pc_pointer(u32(zone,root+0x0C))
        if cells==0:
            if vp.kind not in ('null','following','insert'):root+=1;continue
        elif vp.kind not in ('following','insert'):root+=1;continue
        nr=_printable_z(zone,root+STRINGTABLE_ROOT,512,latin1=True)
        if not nr:root+=1;continue
        name,cursor=nr;name=name.replace('\\','/')
        low=name.lower()
        if not _pathish(name) or name[:1] in ('$' , ',') or not (low.endswith('.csv') or low.endswith('.str') or '/' in low):root+=1;continue
        if cursor+cells*4>len(zone):root+=1;continue
        ptrs=[u32(zone,cursor+i*4) for i in range(cells)]
        if any(decode_pc_pointer(x).kind not in ('null','following','insert') for x in ptrs):root+=1;continue
        cursor+=cells*4;vals=[];ok=True
        for raw in ptrs:
            if raw==NULL:vals.append(None);continue
            rr=_printable_z(zone,cursor,16*1024,latin1=True,allow_empty=True)
            if rr is None:ok=False;break
            v,cursor=rr;vals.append(v)
        if not ok:root+=1;continue
        out.append(StringTable(root,cursor,name,int(columns),int(rows),tuple(vals)))
        root=max(root+1,cursor)
    return out


def _asset_alias_base(asset_list)->int:
    logical_prefix=0x2C+0x10
    if asset_list.asset_pool_offset<logical_prefix:raise ValueError('XAsset pool before list prefix')
    return (asset_list.asset_pool_offset-logical_prefix+3)&~3


def resolve_xasset_header_alias(asset_list,raw:int,expected_type:int)->int|None:
    p=decode_pc_pointer(raw)
    if p.kind!='packed' or p.block!=4:return None
    first=_asset_alias_base(asset_list)+4
    if p.offset is None or p.offset<first:return None
    delta=p.offset-first
    if delta&7:return None
    idx=delta//8
    if idx>=len(asset_list.assets) or asset_list.assets[idx].type_id!=expected_type:return None
    return int(idx)

@dataclass(frozen=True)
class FxAlias:
    root:int; end:int; name:str


def scan_fx_alias_shells(zone:bytes,asset_data_offset:int=0)->list[FxAlias]:
    out=[];start=max(0x2C,asset_data_offset)
    for root in range(start,max(start,len(zone)-FX_ROOT-1)):
        if u32(zone,root)!=FOLLOWING:continue
        if any(u32(zone,root+off)!=0 for off in range(4,FX_ROOT,4)):continue
        rr=_printable_z(zone,root+FX_ROOT,512)
        if not rr:continue
        name,end=rr;name=name.lstrip(',')
        if not name or len(name)>256 or not any(c.isalpha() for c in name):continue
        out.append(FxAlias(root,end,name))
    return out

@dataclass(frozen=True)
class ImpactFx:
    root:int; end:int; name:str; targets:tuple[int|None,...]


def scan_impactfx(zone:bytes)->list[ImpactFx]:
    al=parse_xasset_list(zone,'pc')
    declared=[a for a in al.assets if a.type_id==0x1A]
    if not declared:return []
    out=[];table_bytes=IMPACT_REFS*4
    for root in range(al.asset_data_offset,len(zone)-IMPACT_ROOT-table_bytes+1):
        namep=decode_pc_pointer(u32(zone,root));tablep=decode_pc_pointer(u32(zone,root+4))
        if tablep.kind!='following' or namep.kind not in ('following','packed'):continue
        cursor=root+IMPACT_ROOT
        if namep.kind=='following':
            rr=_printable_z(zone,cursor,512)
            if not rr:continue
            name,cursor=rr
        else:name='impactfx' if len(declared)==1 else f'impactfx_{len(out):02d}'
        if cursor+table_bytes>len(zone):continue
        targets=[];non=0;valid=True
        for i in range(IMPACT_REFS):
            raw=u32(zone,cursor+i*4)
            if raw==0:targets.append(None);continue
            idx=resolve_xasset_header_alias(al,raw,0x19)
            if idx is None:valid=False;break
            targets.append(idx);non+=1
        if valid and non:out.append(ImpactFx(root,cursor+table_bytes,name.lstrip(','),tuple(targets)))
    return out

@dataclass(frozen=True)
class LightDef:
    root:int; end:int; name:str; image_pointer:int; inline_image_name:str|None
    inline_image_semantic:int|None; sampler:int; lmap_lookup_start:int


def _skip_inline_pc_image(zone:bytes,root:int):
    if root<0 or root>len(zone)-PC_IMAGE_ROOT:return None
    map_type=i32(zone,root);loadraw=u32(zone,root+4);semantic=zone[root+0x0B];name_raw=u32(zone,root+0x20)
    if map_type<0 or map_type>5:return None
    loadp=decode_pc_pointer(loadraw);namep=decode_pc_pointer(name_raw)
    if loadp.kind not in ('null','packed','following','insert') or namep.kind not in ('null','packed','following','insert'):return None
    cursor=root+PC_IMAGE_ROOT;name=None
    if namep.kind in ('following','insert'):
        rr=_printable_z(zone,cursor,512)
        if not rr:return None
        name,cursor=rr
    if loadp.kind in ('following','insert'):
        if cursor+PC_IMAGE_LOADDEF>len(zone):return None
        w=u16(zone,cursor+2);h=u16(zone,cursor+4);d=u16(zone,cursor+6);n=u32(zone,cursor+0x0C)
        if not w or not h or not d or cursor+PC_IMAGE_LOADDEF+n>len(zone):return None
        cursor+=PC_IMAGE_LOADDEF+n
    return name,semantic,cursor


def scan_lightdefs(zone:bytes)->list[LightDef]:
    al=parse_xasset_list(zone,'pc');declared=[a for a in al.assets if a.type_id==0x11]
    if not declared:return []
    # A packed top-level LightDef has no owned local root; this scanner deliberately refuses to guess it.
    if any(decode_pc_pointer(a.serialized_pointer).kind=='packed' for a in declared):return []
    # The first word must be FOLLOWING or INSERT. Search those four-byte markers in C-speed instead
    # of executing a Python loop for every byte of a 100+ MiB custom-map zone. The semantic replay
    # below is unchanged, so this is a performance correction rather than a relaxed predicate.
    roots=set();start=max(0,al.asset_data_offset)
    for marker in (b'\xff\xff\xff\xff',b'\xfe\xff\xff\xff'):
        pos=zone.find(marker,start)
        while pos>=0:
            if pos<=len(zone)-LIGHTDEF_ROOT-1:roots.add(pos)
            pos=zone.find(marker,pos+1)
    out=[]
    for root in sorted(roots):
        np=decode_pc_pointer(u32(zone,root));ip=decode_pc_pointer(u32(zone,root+4))
        if np.kind not in ('following','insert'):continue
        if ip.kind not in ('null','packed','following','insert'):continue
        lmap=i32(zone,root+0x0C)
        if lmap < -1 or lmap>1_000_000:continue
        rr=_printable_z(zone,root+LIGHTDEF_ROOT,512)
        if not rr:continue
        name,cursor=rr
        if not name or len(name)>256 or not any(c.isalpha() for c in name):continue
        image_name=None;semantic=None
        if ip.kind in ('following','insert'):
            ii=_skip_inline_pc_image(zone,cursor)
            if not ii:continue
            image_name,semantic,cursor=ii
        out.append(LightDef(root,cursor,name,u32(zone,root+4),image_name,semantic,zone[root+8],lmap))
    # physical duplicate roots impossible; name duplication allowed in theory but root unique.
    return sorted({x.root:x for x in out}.values(),key=lambda x:x.root)


def scan_asset_families(zone:bytes)->dict:
    al=parse_xasset_list(zone,'pc')
    declared={}
    for a in al.assets:declared[a.type_name]=declared.get(a.type_name,0)+1
    raw=scan_rawfiles(zone);st=scan_stringtables(zone);fx=scan_fx_alias_shells(zone,al.asset_data_offset);owned_fx=scan_owned_fx_graphs(zone,al.asset_data_offset);impact=scan_impactfx(zone);light=scan_lightdefs(zone);phys=scan_physpresets(zone)
    fx_by_root={x.root:{'root':x.root,'end':x.end,'name':x.name,'owned':False,'material_roots':[]} for x in fx}
    for x in owned_fx:fx_by_root[x.root]={'root':x.root,'end':x.end,'name':x.name,'owned':True,'material_roots':list(x.material_roots)}
    fx_all=[fx_by_root[k] for k in sorted(fx_by_root)]
    return {
      'declared':declared,
      'rawfiles':{'declared':declared.get('rawfile',0),'candidates':len(raw),'records':[{'root':x.root,'end':x.end,'name':x.name,'length':x.length,'sha256':x.sha256} for x in raw]},
      'stringtables':{'declared':declared.get('stringtable',0),'candidates':len(st),'records':[asdict(x) for x in st]},
      'fx':{'declared':declared.get('fx',0),'alias_shell_candidates':len(fx),'owned_graph_candidates':len(owned_fx),'candidates':len(fx_all),'records':fx_all,
            'exact':len(fx_all)==declared.get('fx',0)},
      'impactfx':{'declared':declared.get('impactfx',0),'candidates':len(impact),'records':[{'root':x.root,'end':x.end,'name':x.name,'resolved_references':sum(v is not None for v in x.targets)} for x in impact],
                  'exact':len(impact)==declared.get('impactfx',0)},
      'lightdef':{'declared':declared.get('lightdef',0),'candidates':len(light),'records':[asdict(x) for x in light],
                  'exact':len(light)==declared.get('lightdef',0)},
      'physpreset':{'declared':declared.get('physpreset',0),'candidates':len(phys),'records':[asdict(x) for x in phys],
                    'exact':len(phys)==declared.get('physpreset',0),'packed_sound_requests':physpreset_sound_requests(phys)},
    }

# ---------------- Complete owned FX graph walk ----------------
FX_ELEM=0xFC
VELOCITY_SAMPLE=0x60
VISUAL_STATE_SAMPLE=0x30
TRAIL_DEF=0x1C
TRAIL_VERTEX=0x14
DECAL_PAIR=0x08
EXTERNAL_XMODEL_ALIAS=0xDC
MATERIAL_ROOT=0x50
MATERIAL_TEXTURE=0x0C
MATERIAL_CONSTANT=0x20
MATERIAL_STATEBITS=0x08
WATER_SEMANTIC=0x0B
MAX_FX_ELEMS=4096

@dataclass(frozen=True)
class OwnedFx:
    root:int; end:int; name:str; material_roots:tuple[int,...]


def _has(zone:bytes,off:int,n:int)->bool:return off>=0 and n>=0 and off<=len(zone)-n

def _skip_pc_image(zone:bytes,cursor:int)->int|None:
    if not _has(zone,cursor,PC_IMAGE_ROOT):return None
    load=u32(zone,cursor+4);name=u32(zone,cursor+0x20);p=cursor+PC_IMAGE_ROOT
    if _inline(name):
        rr=_printable_z(zone,p,512,latin1=True)
        if not rr:return None
        _,p=rr
    if _inline(load):
        if not _has(zone,p,PC_IMAGE_LOADDEF):return None
        n=u32(zone,p+0x0C)
        if not _has(zone,p+PC_IMAGE_LOADDEF,n):return None
        p+=PC_IMAGE_LOADDEF+n
    return p


def _skip_pc_material(zone:bytes,root:int)->tuple[str,int]|None:
    if not _has(zone,root,MATERIAL_ROOT) or not _inline(u32(zone,root)):return None
    texc=zone[root+0x3A];constc=zone[root+0x3B];statec=zone[root+0x3C]
    texptr=u32(zone,root+0x44);constptr=u32(zone,root+0x48);stateptr=u32(zone,root+0x4C)
    rr=_printable_z(zone,root+MATERIAL_ROOT,512,latin1=True)
    if not rr:return None
    name,cursor=rr
    if texc and _inline(texptr):
        if not _has(zone,cursor,texc*MATERIAL_TEXTURE):return None
        table=cursor;cursor+=texc*MATERIAL_TEXTURE
        pending=[]
        for i in range(texc):
            tr=table+i*MATERIAL_TEXTURE;pending.append((zone[tr+7],u32(zone,tr+8)))
        for semantic,raw in pending:
            if not _inline(raw):continue
            if semantic==WATER_SEMANTIC:
                if not _has(zone,cursor,0x44):return None
                h0=u32(zone,cursor+4);wt=u32(zone,cursor+8);m=i32(zone,cursor+0x0C);n=i32(zone,cursor+0x10);image=u32(zone,cursor+0x40)
                if m<0 or n<0 or m>16384 or n>16384:return None
                samples=m*n;cursor+=0x44
                if samples and _inline(h0):
                    if not _has(zone,cursor,samples*8):return None
                    cursor+=samples*8
                if samples and _inline(wt):
                    if not _has(zone,cursor,samples*4):return None
                    cursor+=samples*4
                if _inline(image):
                    cursor2=_skip_pc_image(zone,cursor)
                    if cursor2 is None:return None
                    cursor=cursor2
            else:
                cursor2=_skip_pc_image(zone,cursor)
                if cursor2 is None:return None
                cursor=cursor2
    if constc and _inline(constptr):
        if not _has(zone,cursor,constc*MATERIAL_CONSTANT):return None
        cursor+=constc*MATERIAL_CONSTANT
    if statec and _inline(stateptr):
        if not _has(zone,cursor,statec*MATERIAL_STATEBITS):return None
        cursor+=statec*MATERIAL_STATEBITS
    return name,cursor


def _fx_name_ok(name:str)->bool:
    return bool(name) and len(name)<=256 and any(c.isalpha() for c in name) and ('/' in name or '\\' in name or '_' in name) and all(0x20<=ord(c)<=0x7E for c in name)


def _consume_sample(zone:bytes,cursor:int,raw:int,count:int,stride:int)->int|None:
    p=decode_pc_pointer(raw)
    if p.kind in ('following','insert'):
        n=count*stride
        return cursor+n if _has(zone,cursor,n) else None
    return cursor if p.kind in ('null','packed') else None


def _consume_external_xmodel_alias(zone:bytes,cursor:int)->int|None:
    if not _has(zone,cursor,EXTERNAL_XMODEL_ALIAS) or not _inline(u32(zone,cursor)):return None
    if any(zone[cursor+i]!=0 for i in range(4,EXTERNAL_XMODEL_ALIAS)):return None
    rr=_printable_z(zone,cursor+EXTERNAL_XMODEL_ALIAS,512)
    return rr[1] if rr else None


def _consume_visual(zone:bytes,cursor:int,raw:int,etype:int,material_roots:list[int])->int|None:
    p=decode_pc_pointer(raw)
    if p.kind not in ('following','insert'):
        return cursor if p.kind in ('null','packed') else None
    if etype<=4:
        m=_skip_pc_material(zone,cursor)
        if not m:return None
        material_roots.append(cursor);return m[1]
    if etype==5:return _consume_external_xmodel_alias(zone,cursor)
    if etype in (8,10):
        rr=_printable_z(zone,cursor,4096)
        return rr[1] if rr else None
    return None


def _consume_effect_ref(zone:bytes,cursor:int,raw:int)->int|None:
    p=decode_pc_pointer(raw)
    if p.kind in ('following','insert'):
        rr=_printable_z(zone,cursor,4096)
        return rr[1] if rr else None
    return cursor if p.kind in ('null','packed') else None


def _consume_trail(zone:bytes,cursor:int,raw:int,etype:int)->int|None:
    p=decode_pc_pointer(raw)
    if p.kind not in ('following','insert'):
        return cursor if p.kind in ('null','packed') else None
    if etype!=3 or not _has(zone,cursor,TRAIL_DEF):return None
    vc=u32(zone,cursor+0x0C);vp=u32(zone,cursor+0x10);ic=u32(zone,cursor+0x14);ip=u32(zone,cursor+0x18)
    if vc>1_000_000 or ic>4_000_000:return None
    cursor+=TRAIL_DEF
    if _inline(vp):
        n=vc*TRAIL_VERTEX
        if not _has(zone,cursor,n):return None
        cursor+=n
    elif decode_pc_pointer(vp).kind not in ('null','packed'):return None
    if _inline(ip):
        n=ic*2
        if not _has(zone,cursor,n):return None
        cursor+=n
    elif decode_pc_pointer(ip).kind not in ('null','packed'):return None
    return cursor


def _walk_owned_fx(zone:bytes,root:int)->OwnedFx|None:
    if not _has(zone,root,FX_ROOT) or not _inline(u32(zone,root)):return None
    counts=(u32(zone,root+0x10),u32(zone,root+0x14),u32(zone,root+0x18));total=sum(counts)
    if total<1 or total>MAX_FX_ELEMS or not _inline(u32(zone,root+0x1C)):return None
    cursor=root+FX_ROOT;rr=_printable_z(zone,cursor,512)
    if not rr:return None
    name,cursor=rr
    if not _fx_name_ok(name):return None
    nbytes=total*FX_ELEM
    if not _has(zone,cursor,nbytes):return None
    drafts=[]
    for idx in range(total):
        e=cursor+idx*FX_ELEM;etype=zone[e+0xB0]
        if etype>10:return None
        drafts.append((etype,zone[e+0xB1],zone[e+0xB2],zone[e+0xB3],u32(zone,e+0xB4),u32(zone,e+0xB8),u32(zone,e+0xBC),u32(zone,e+0xD8),u32(zone,e+0xDC),u32(zone,e+0xE0),u32(zone,e+0xF4)))
    cursor+=nbytes;materials=[]
    for etype,vcount,vint,sint,vptr,sptr,visual,eimpact,edeath,eemit,trail in drafts:
        cursor2=_consume_sample(zone,cursor,vptr,vint+1,VELOCITY_SAMPLE)
        if cursor2 is None:return None
        cursor=cursor2;cursor2=_consume_sample(zone,cursor,sptr,sint+1,VISUAL_STATE_SAMPLE)
        if cursor2 is None:return None
        cursor=cursor2;vk=decode_pc_pointer(visual).kind
        if vcount==0:
            if vk!='null':return None
        elif etype==9:
            if _inline(visual):
                n=vcount*DECAL_PAIR
                if not _has(zone,cursor,n):return None
                pairs=[(u32(zone,cursor+i*8),u32(zone,cursor+i*8+4)) for i in range(vcount)];cursor+=n
                for left,right in pairs:
                    for raw in (left,right):
                        cursor2=_consume_visual(zone,cursor,raw,0,materials)
                        if cursor2 is None:return None
                        cursor=cursor2
            elif vk not in ('null','packed'):return None
        elif vcount>1:
            if _inline(visual):
                n=vcount*4
                if not _has(zone,cursor,n):return None
                ptrs=[u32(zone,cursor+i*4) for i in range(vcount)];cursor+=n
                for raw in ptrs:
                    cursor2=_consume_visual(zone,cursor,raw,etype,materials)
                    if cursor2 is None:return None
                    cursor=cursor2
            elif vk not in ('null','packed'):return None
        else:
            cursor2=_consume_visual(zone,cursor,visual,etype,materials)
            if cursor2 is None:return None
            cursor=cursor2
        for raw in (eimpact,edeath,eemit):
            cursor2=_consume_effect_ref(zone,cursor,raw)
            if cursor2 is None:return None
            cursor=cursor2
        cursor2=_consume_trail(zone,cursor,trail,etype)
        if cursor2 is None:return None
        cursor=cursor2
        if cursor-root>32*1024*1024:return None
    return OwnedFx(root,cursor,name,tuple(sorted(set(materials))))


def scan_owned_fx_graphs(zone:bytes,start_offset:int=0)->list[OwnedFx]:
    out=[];start=max(0,start_offset);end=len(zone)-FX_ROOT-2
    for root in range(start,end+1):
        # very cheap prefilter before the complete walk
        if zone[root] not in (0xFE,0xFF):continue
        fx=_walk_owned_fx(zone,root)
        if fx:out.append(fx)
    return sorted({x.root:x for x in out}.values(),key=lambda x:x.root)

# ---------------- PhysPreset source inventory / SoundAliasPrefix proof requests ----------------
PHYSPRESET_ROOT=0x2C

@dataclass(frozen=True)
class PhysPreset:
    root:int; end:int; name:str|None; name_pointer:int; sound_alias_prefix:str|None
    sound_pointer:int; type:int; scalar_bits:tuple[int,...]; temp_default_to_cylinder:bool


def scan_physpresets(zone:bytes)->list[PhysPreset]:
    al=parse_xasset_list(zone,'pc');declared=[a for a in al.assets if a.type_id==0x01]
    if not declared:return []
    if any(decode_pc_pointer(a.serialized_pointer).kind not in ('following','insert') for a in declared):return []
    out=[]
    for root in range(al.asset_data_offset,len(zone)-PHYSPRESET_ROOT+1):
        name_raw=u32(zone,root);np=decode_pc_pointer(name_raw)
        if np.kind not in ('following','insert','packed'):continue
        typ=i32(zone,root+4)
        if typ < -1 or typ>64:continue
        scalar_offsets=(0x08,0x0C,0x10,0x14,0x18,0x20,0x24);bits=[];valid=True
        for off in scalar_offsets:
            b=u32(zone,root+off);v=struct.unpack('<f',struct.pack('<I',b))[0]
            if not (-100_000_000.0<=v<=100_000_000.0) or v!=v or v in (float('inf'),float('-inf')):valid=False;break
            bits.append(b)
        if not valid:continue
        sound_raw=u32(zone,root+0x1C);sp=decode_pc_pointer(sound_raw)
        if sp.kind not in ('null','following','insert','packed'):continue
        cursor=root+PHYSPRESET_ROOT;name=None;sound=None
        if np.kind in ('following','insert'):
            rr=_printable_z(zone,cursor,512)
            if not rr:continue
            name,cursor=rr
        if sp.kind in ('following','insert'):
            rr=_printable_z(zone,cursor,512,allow_empty=True)
            if rr is None:continue
            sound,cursor=rr
            if sound=='':sound=None
        out.append(PhysPreset(root,cursor,name,name_raw,sound,sound_raw,typ,tuple(bits),zone[root+0x28]!=0))
    # Only expose exact-cardinality candidate set as top-level semantic inventory.
    return sorted({x.root:x for x in out}.values(),key=lambda x:x.root)


def physpreset_sound_requests(records:Iterable[PhysPreset])->list[dict]:
    req=[]
    for ordinal,r in enumerate(records):
        p=decode_pc_pointer(r.sound_pointer)
        if p.kind=='packed':
            req.append({'description':f'PhysPreset[{ordinal}].sndAliasPrefix','block':p.block,'offset':p.offset,'kind':'xstring','root':r.root})
    return req
