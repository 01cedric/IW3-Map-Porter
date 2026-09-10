from __future__ import annotations
import math,struct,re
from dataclasses import replace
from typing import Callable,Sequence
from .backend.v4_ffio import decode_pc_pointer
from .material import MaterialRecord,MaterialTexture,MaterialWater,WaterComplex

# Offset of the semantic byte inside a PC IW3 GfxImage root.
IMAGE_SEMANTIC=0x0B
from .pc_xmodel import PcXModelIntermediate
from .physics import parse_pc_phys_preset,parse_pc_phys_geom_list

ROOT_SIZE=0x50;TEXTURE_SIZE=0x0C;CONSTANT_SIZE=0x20;STATE_SIZE=8;IMAGE_ROOT_SIZE=0x24;IMAGE_LOADDEF_SIZE=0x10;WATER_SEMANTIC=0x0B

def u16(z,o):return struct.unpack_from('<H',z,o)[0]
def u32(z,o):return struct.unpack_from('<I',z,o)[0]
def u64(z,o):return struct.unpack_from('<Q',z,o)[0]
def i32(z,o):return struct.unpack_from('<i',z,o)[0]
def f32(z,o):return struct.unpack_from('<f',z,o)[0]
def inline(raw):return decode_pc_pointer(raw).kind in ('following','insert')
def packed(raw):return decode_pc_pointer(raw).kind=='packed'

def read_latin1z(z,c,max_bytes=512):
    e=z.find(b'\0',c,min(len(z),c+max_bytes))
    if e<0:raise ValueError('unterminated string')
    return z[c:e].decode('latin-1'),e+1

def looks_material_name(s):
    return bool(s) and len(s)<=128 and all(0x20<=ord(c)<=0x7e for c in s)

def parse_material_at(z:bytes,root:int)->tuple[MaterialRecord,int]:
    if root<0 or root+ROOT_SIZE>len(z):raise ValueError('material root outside zone')
    tc=z[root+0x3a];cc=z[root+0x3b];sc=z[root+0x3c];tptr=u32(z,root+0x44);cptr=u32(z,root+0x48);sptr=u32(z,root+0x4c)
    cursor=root+ROOT_SIZE;name,cursor=read_latin1z(z,cursor);textures=[];waters={};pending=[]
    if tc and inline(tptr):
        table=cursor;cursor+=tc*TEXTURE_SIZE
        for idx in range(tc):
            tr=table+idx*TEXTURE_SIZE;semantic=z[tr+7];rp=u32(z,tr+8)
            textures.append(MaterialTexture(u32(z,tr),z[tr+4],z[tr+5],z[tr+6],semantic,rp,None,None));pending.append((idx,semantic,rp))
        for idx,semantic,rp in pending:
            if not inline(rp):continue
            if semantic==WATER_SEMANTIC:
                wr=cursor
                if wr+0x44>len(z):raise ValueError(f'{name} water root overrun')
                ft=f32(z,wr);h0p=u32(z,wr+4);wtp=u32(z,wr+8);m=i32(z,wr+0x0c);n=i32(z,wr+0x10);ip=u32(z,wr+0x40)
                if not 1<=m<=4096 or not 1<=n<=4096:raise ValueError('water dimensions invalid')
                scal=tuple(f32(z,wr+0x14+i*4) for i in range(11));
                if not math.isfinite(ft) or not all(math.isfinite(x) for x in scal):raise ValueError('water nonfinite')
                cursor+=0x44;count=m*n;h0=[];wt=[]
                if count and inline(h0p):
                    for _ in range(count):
                        a=f32(z,cursor);b=f32(z,cursor+4);cursor+=8
                        if not math.isfinite(a) or not math.isfinite(b):raise ValueError('water H0 nonfinite')
                        h0.append(WaterComplex(a,b))
                elif count and decode_pc_pointer(h0p).kind!='null':raise ValueError('water H0 packed reuse unresolved')
                if count and inline(wtp):
                    for _ in range(count):
                        a=f32(z,cursor);cursor+=4
                        if not math.isfinite(a):raise ValueError('water wTerm nonfinite')
                        wt.append(a)
                elif count and decode_pc_pointer(wtp).kind!='null':raise ValueError('water wTerm packed reuse unresolved')
                iname=None;iroot=None
                if inline(ip):
                    iroot=cursor;np=u32(z,iroot+0x20);lp=u32(z,iroot+4);cursor+=IMAGE_ROOT_SIZE
                    if inline(np):iname,cursor=read_latin1z(z,cursor)
                    if inline(lp):rb=u32(z,cursor+0x0c);cursor+=IMAGE_LOADDEF_SIZE+rb
                elif decode_pc_pointer(ip).kind not in ('null','packed'):raise ValueError('water image pointer unsupported')
                waters[idx]=MaterialWater(idx,ft,h0p,wtp,m,n,scal,tuple(h0),tuple(wt),ip,iname,iroot)
                textures[idx]=replace(textures[idx],inline_image_name=iname,inline_image_root=iroot,inline_image_semantic=z[iroot+IMAGE_SEMANTIC] if iroot is not None else None)
            else:
                iroot=cursor;np=u32(z,iroot+0x20);lp=u32(z,iroot+4);cursor+=IMAGE_ROOT_SIZE;iname=None
                if inline(np):iname,cursor=read_latin1z(z,cursor)
                if inline(lp):rb=u32(z,cursor+0x0c);cursor+=IMAGE_LOADDEF_SIZE+rb
                textures[idx]=replace(textures[idx],inline_image_name=iname,inline_image_root=iroot,inline_image_semantic=z[iroot+IMAGE_SEMANTIC] if iroot is not None else None)
    constants=[]
    if cc and inline(cptr):
        constants=[z[cursor+i*CONSTANT_SIZE:cursor+(i+1)*CONSTANT_SIZE] for i in range(cc)];cursor+=cc*CONSTANT_SIZE
    states=[]
    if sc and inline(sptr):
        states=[(u32(z,cursor+i*8),u32(z,cursor+i*8+4)) for i in range(sc)];cursor+=sc*8
    rec=MaterialRecord(root,name,z[root+4],z[root+5],z[root+6],z[root+7],u64(z,root+8),u32(z,root+0x10),u16(z,root+0x14),bytes(z[root+0x18:root+0x18+34]),sc,sptr,'None' if sc==0 else ('Inline' if inline(sptr) else 'PackedUnresolved' if packed(sptr) else 'Unsupported'),tc,cc,z[root+0x3d],z[root+0x3e],u32(z,root+0x40),tuple(textures),tuple(constants),tuple(states),waters)
    object.__setattr__(rec,'end_offset',cursor)
    return rec,cursor

def scan_materials(z:bytes,expected_names:set[str]|None=None)->list[MaterialRecord]:
    # Material roots begin with FOLLOWING/INSERT, but those markers occur millions of
    # times in a real zone.  Apply the complete cheap root-shape checks before calling
    # the recursive Material parser.  This reduces Getaway from multi-minute scanning
    # to ~1-2 seconds without weakening pointer/count validation.
    out=[];pat=re.compile(rb'(?:\xff\xff\xff\xff|\xfe\xff\xff\xff)')
    for mm in pat.finditer(z):
        c=mm.start()
        if c+ROOT_SIZE+2>len(z):continue
        tc=z[c+0x3a];cc=z[c+0x3b];sc=z[c+0x3c]
        if tc>32 or cc>32 or sc>64:continue
        tech=u32(z,c+0x40);tptr=u32(z,c+0x44);cptr=u32(z,c+0x48);sptr=u32(z,c+0x4c)
        if decode_pc_pointer(tech).kind!='packed':continue
        kinds=(decode_pc_pointer(tptr).kind,decode_pc_pointer(cptr).kind,decode_pc_pointer(sptr).kind)
        if any(k not in ('null','following','insert','packed') for k in kinds):continue
        if (tc==0)!=(kinds[0]=='null') or (cc==0)!=(kinds[1]=='null') or (sc==0)!=(kinds[2]=='null'):continue
        try:
            name,_=read_latin1z(z,c+ROOT_SIZE,129)
            if not looks_material_name(name) or expected_names is not None and name not in expected_names:continue
            m,_=parse_material_at(z,c)
            if m.name==name:out.append(m)
        except Exception:pass
    uniq={m.root_offset:m for m in out};return [uniq[k] for k in sorted(uniq)]

def attach_xmodel_inline_materials_and_tails(z:bytes,xs:Sequence[PcXModelIntermediate])->list[MaterialRecord]:
    owned=[]
    for x in xs:
        m=x.model;cursor=x.physical_after_material_handles;x.inline_material_offsets=[None]*len(m.surfaces)
        for i,(s,p) in enumerate(zip(m.surfaces,x.material_handle_pointers)):
            if inline(p):
                x.inline_material_offsets[i]=cursor;mat,cursor=parse_material_at(z,cursor);owned.append(mat);s.material_name=mat.name
            elif p==0:s.material_name='<null>'
            else:s.material_name=f'packed:0x{p:08X}'
        if m.num_collision_surfaces<0 or m.num_collision_surfaces>1_000_000:raise ValueError('XModel collision count implausible')
        if m.num_collision_surfaces and inline(m.collision_surface_pointer):
            m.collision_surface_source=cursor;roots=cursor;cursor+=m.num_collision_surfaces*0x2c
            for ci in range(m.num_collision_surfaces):
                cr=roots+ci*0x2c;tp=u32(z,cr);cnt=i32(z,cr+4)
                if cnt<0 or cnt>10_000_000:raise ValueError('collision tri count implausible')
                if cnt and inline(tp):cursor+=cnt*0x30
        if m.num_bones and inline(m.bone_info_pointer):m.bone_info_source=cursor;cursor+=m.num_bones*0x28
        if inline(m.phys_preset_pointer):
            p,cursor,meta=parse_pc_phys_preset(z,cursor)
            # Keep the same provenance on the semantic preset itself so the global
            # PhysPreset catalog can distinguish proven inline XStrings from packed names.
            for key,value in meta.items():
                object.__setattr__(p,key,value)
            m.inline_phys_preset=p;object.__setattr__(m,'inline_phys_preset_meta',meta)
        if inline(m.phys_geoms_pointer):g,cursor=parse_pc_phys_geom_list(z,cursor);m.inline_phys_geoms=g
        x.physical_end=cursor
    return owned
