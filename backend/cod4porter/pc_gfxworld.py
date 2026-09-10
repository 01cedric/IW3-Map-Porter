from __future__ import annotations
from dataclasses import dataclass
import hashlib,struct
from typing import Mapping
from .backend.v4_ffio import decode_pc_pointer
from .pc_material import parse_material_at

PC_ROOT=0x2DC;PC_IMAGE_ROOT=0x24;PC_IMAGE_LOADDEF=0x10;PC_CELL=0x38;PC_AABB=0x2C;PC_PORTAL=0x44;PC_LIGHTMAP=8;PC_BRUSH_MODEL=0x38;PC_WORLD_VERTEX=0x2C;PC_SHADOW=0x0C;PC_REGION=8;PC_HULL=0x50;PC_AXIS=0x14;PC_SMODEL_INST=0x1C;PC_SURFACE=0x30;PC_CULL=0x20;PC_DRAW=0x4C

def u16(z,o):return struct.unpack_from('<H',z,o)[0]
def u32(z,o):return struct.unpack_from('<I',z,o)[0]
def i32(z,o):return struct.unpack_from('<i',z,o)[0]
def f32(z,o):return struct.unpack_from('<f',z,o)[0]
def inline(raw):return decode_pc_pointer(raw).kind in ('following','insert')
def packed(raw):return decode_pc_pointer(raw).kind=='packed'
def sha(b):return hashlib.sha256(b).hexdigest().upper()
def ensure(z,o,n,label='range'):
    if o<0 or n<0 or o+n>len(z):raise ValueError(f'{label} 0x{o:X}+0x{n:X} outside zone 0x{len(z):X}')
def read_z(z,c,max_bytes=4096):
    ensure(z,c,1,'string');e=z.find(b'\0',c,min(len(z),c+max_bytes))
    if e<0:raise ValueError('unterminated GfxWorld string')
    return z[c:e].decode('latin-1'),e+1

def require_inline(raw,label):
    if not inline(raw):raise ValueError(f'{label} pointer 0x{raw:08X} not inline')
def count(v,lo,hi,label):
    if not lo<=v<=hi:raise ValueError(f'{label}={v} outside {lo}..{hi}')
    return v

@dataclass(frozen=True)
class GfxImagePayload:
    label:str;root_offset:int;name:str;map_type:int;picmip0:int;picmip1:int;no_picmip:int;semantic:int;track:int;category:int;delay_load_pixels:int;level_count:int;flags:int;width:int;height:int;depth:int;pc_format:int;resource_bytes:int;resource_offset:int;end_offset:int;resource_sha256:str
@dataclass(frozen=True)
class GfxBranch:
    name:str;start:int;end:int;sha256:str
@dataclass(frozen=True)
class GfxFullGraph:
    physical_end:int;aabb_count:int;portal_count:int;portal_vertex_count:int;aabb_inline_values:int;lightgrid_row_count:int;lightgrid_raw_bytes:int;lightgrid_entry_count:int;lightgrid_color_count:int;inline_material_roots:tuple[int,...];inline_water_material_count:int;shadow_surface_values:int;shadow_model_values:int;region_hulls:int;region_axes:int;dpvs_sorted_count:int;surface_material_pointers:tuple[int,...];packed_surface_material_count:int;packed_draw_xmodel_count:int;images:tuple[GfxImagePayload,...];branches:tuple[GfxBranch,...];inline_draw_xmodels:tuple[tuple[int,str,int,int],...]=()
@dataclass(frozen=True)
class GfxWorldReport:
    root_offset:int;base_name:str;plane_count:int;node_count:int;index_count:int;surface_count:int;sky_surface_count:int;vertex_count:int;vertex_layer_data_size:int;sun_primary_light_index:int;primary_light_count:int;cull_group_count:int;reflection_probe_count:int;cell_count:int;cell_bits_count:int;lightmap_count:int;model_count:int;material_memory_count:int;static_model_count:int;static_surface_count:int;static_surface_count_no_decal:int;lit_surfs_begin:int;lit_surfs_end:int;decal_surfs_begin:int;decal_surfs_end:int;emissive_surfs_begin:int;emissive_surfs_end:int;dynamic_model_count:int;dynamic_brush_count:int;checksum:int;full:GfxFullGraph


def _image(z,root,label,images):
    ensure(z,root,PC_IMAGE_ROOT,label+' image');mt=i32(z,root);ld=u32(z,root+4);np=u32(z,root+0x20)
    if not 0<=mt<=5 or z[root+0xa]>1 or z[root+0x1f]>1:raise ValueError('GfxImage metadata')
    c=root+PC_IMAGE_ROOT;name=f'packed:0x{np:08X}'
    if inline(np):name,c=read_z(z,c,512)
    level=flags=width=height=depth=fmt=rb=0;ro=c
    if inline(ld):
        ensure(z,c,PC_IMAGE_LOADDEF,label+' loaddef');level=z[c];flags=z[c+1];width=u16(z,c+2);height=u16(z,c+4);depth=u16(z,c+6);fmt=u32(z,c+8);rb=u32(z,c+0xc);c+=PC_IMAGE_LOADDEF;ro=c;ensure(z,c,rb,label+' resource');c+=rb
    rec=GfxImagePayload(label,root,name,mt,z[root+8],z[root+9],z[root+0xa],z[root+0xb],z[root+0xc],z[root+0x1e],z[root+0x1f],level,flags,width,height,depth,fmt,rb,ro,c,sha(z[ro:ro+rb]))
    images.append(rec);return c

def _skip_lightdef(z,c,images):
    ensure(z,c,0x10,'sun LightDef');r=c;np=u32(z,r);ip=u32(z,r+4);c+=0x10
    if inline(np):_,c=read_z(z,c)
    if inline(ip):c=_image(z,c,'sunLight.def.attenuation',images)
    return c

def _add(branches,z,name,start,end):
    ensure(z,start,end-start,name);branches.append(GfxBranch(name,start,end,sha(z[start:end])))


def parse_gfxworld(z:bytes,map_name:str,clip_counts:Mapping[str,int])->GfxWorldReport:
    needle=map_name.encode('latin-1')+b'\0';roots=[];s=0
    while True:
        at=z.find(needle,s)
        if at<0:break
        s=at+1;r=at-PC_ROOT
        if r>=0 and r+PC_ROOT<=len(z) and u32(z,r+8)==clip_counts['planes'] and u32(z,r+0x150)==clip_counts['submodels'] and u32(z,r+0x244)==clip_counts['static_models']:roots.append(r)
    if len(roots)!=1:raise ValueError(f'GfxWorld root candidates={len(roots)}')
    r=roots[0]
    plane=count(u32(z,r+8),1,1_000_000,'planes');nodes=count(u32(z,r+0xc),1,2_000_000,'nodes');indices=count(u32(z,r+0x10),0,100_000_000,'indices');surfs=count(u32(z,r+0x18),1,5_000_000,'surfaces');sky=count(u32(z,r+0x20),0,surfs,'sky');verts=count(u32(z,r+0x30),1,20_000_000,'verts');vld=count(u32(z,r+0x3c),0,0x7fffffff,'vld');sunidx=count(u32(z,r+0xd8),0,4096,'sunidx');plights=count(u32(z,r+0xdc),0,4096,'plights');culls=count(u32(z,r+0xe0),0,1_000_000,'culls');probes=z[r+0xe4];cells=count(u32(z,r+0xf0),1,1_000_000,'cells');cellbits=count(u32(z,r+0x100),0,100_000_000,'cellbits');lmaps=count(u32(z,r+0x108),0,100_000,'lightmaps');models=count(u32(z,r+0x150),1,100_000,'models');checksum=u32(z,r+0x170);matmem=count(u32(z,r+0x174),0,5_000_000,'matmem')
    dpvs=r+0x244;smodels=count(u32(z,dpvs),0,1_000_000,'smodels');ss=count(u32(z,dpvs+4),0,surfs,'static surfaces');ssnd=count(u32(z,dpvs+8),0,ss,'static no decal');lb=count(u32(z,dpvs+0xc),0,ss,'lit begin');le=count(u32(z,dpvs+0x10),lb,ss,'lit end');db=count(u32(z,dpvs+0x14),0,ss,'decal begin');de=count(u32(z,dpvs+0x18),db,ss,'decal end');eb=count(u32(z,dpvs+0x1c),0,ss,'emissive begin');ee=count(u32(z,dpvs+0x20),eb,ss,'emissive end')
    dyn=r+0x2ac;dm=count(u32(z,dyn+8),0,1_000_000,'dyn model');dbr=count(u32(z,dyn+0xc),0,1_000_000,'dyn brush')
    if sunidx>=plights and plights:raise ValueError('sun light index')
    if smodels!=clip_counts['static_models'] or models!=clip_counts['submodels'] or dm!=clip_counts['dynamic_models'] or dbr!=clip_counts['dynamic_brushes']:raise ValueError('GfxWorld/ClipMap root count mismatch')
    c=r+PC_ROOT;base,c=read_z(z,c,512)
    if base.lower()!=map_name.lower():raise ValueError('GfxWorld basename mismatch')
    images=[];branches=[]
    st=c;c+=indices*2;_add(branches,z,'indices',st,c)
    st=c;c+=sky*4;_add(branches,z,'skyStartSurfs',st,c)
    if inline(u32(z,r+0x28)):c=_image(z,c,'skyImage',images)
    if inline(u32(z,r+0xc8)):
        st=c;ensure(z,c,0x40,'sunLight');ld=u32(z,c+0x3c);c+=0x40
        if inline(ld):c=_skip_lightdef(z,c,images)
        _add(branches,z,'sunLight',st,c)
    st=c
    if probes and not inline(u32(z,r+0xe8)):raise ValueError('reflection probe pointer')
    roots_probe=c;c+=probes*0x10;ensure(z,roots_probe,probes*0x10,'reflection roots')
    for i in range(probes):
        if inline(u32(z,roots_probe+i*0x10+0xc)):c=_image(z,c,f'reflectionProbe[{i}]',images)
    _add(branches,z,'reflectionProbes',st,c)
    st=c
    if plane and not inline(u32(z,r+0xf4)):raise ValueError('plane ptr')
    c+=plane*0x14;ensure(z,st,c-st,'planes');_add(branches,z,'planes',st,c)
    st=c
    if nodes and not inline(u32(z,r+0xf8)):raise ValueError('nodes ptr')
    c+=nodes*2;ensure(z,st,c-st,'nodes');_add(branches,z,'nodes',st,c)
    st=c
    if cells and not inline(u32(z,r+0x104)):raise ValueError('cells ptr')
    cellroots=c;c+=cells*PC_CELL;ensure(z,cellroots,cells*PC_CELL,'cells');cellstates=[]
    for i in range(cells):
        q=cellroots+i*PC_CELL;a=i32(z,q+0x18);po=i32(z,q+0x20);cu=i32(z,q+0x28);pr=z[q+0x30]
        count(a,0,10_000_000,'cell aabb');count(po,0,10_000_000,'cell portals');count(cu,0,10_000_000,'cell cull');cellstates.append((a,u32(z,q+0x1c),po,u32(z,q+0x24),cu,u32(z,q+0x2c),pr,u32(z,q+0x34)))
    aabbtotal=portaltotal=portalverts=inlinevals=0
    for a,ap,po,pp,cu,cup,pr,prp in cellstates:
        if a:
            require_inline(ap,'cell aabb');aroots=c;c+=a*PC_AABB;ensure(z,aroots,a*PC_AABB,'aabbs');aabbtotal+=a
            for j in range(a):
                q=aroots+j*PC_AABB;n=u16(z,q+0x22);p=u32(z,q+0x24)
                if n==0 and p!=0:raise ValueError('zero AABB with pointer')
                if n and inline(p):ensure(z,c,n*2,'aabb values');c+=n*2;inlinevals+=n
                elif n and not packed(p):raise ValueError('aabb packed kind')
        if po:
            require_inline(pp,'cell portals');proots=c;c+=po*PC_PORTAL;ensure(z,proots,po*PC_PORTAL,'portals');portaltotal+=po
            for j in range(po):
                q=proots+j*PC_PORTAL;vc=z[q+0x28];vp=u32(z,q+0x24)
                if vc:require_inline(vp,'portal verts');ensure(z,c,vc*0xc,'portal verts');c+=vc*0xc;portalverts+=vc
        if cu:require_inline(cup,'cell culls');ensure(z,c,cu*4,'cell culls');c+=cu*4
        if pr:require_inline(prp,'cell refl');ensure(z,c,pr,'cell refl');c+=pr
    _add(branches,z,'cells/AABB/portals',st,c)
    st=c
    if lmaps:require_inline(u32(z,r+0x10c),'lightmaps')
    lmroots=c;c+=lmaps*8;ensure(z,lmroots,lmaps*8,'lightmap roots')
    for i in range(lmaps):
        for j,label in ((0,'primary'),(4,'secondary')):
            if inline(u32(z,lmroots+i*8+j)):c=_image(z,c,f'lightmap[{i}].{label}',images)
    grid=r+0x110;ra=i32(z,grid+0x14);ca=i32(z,grid+0x18)
    if ra not in (0,1,2) or ca not in (0,1,2):raise ValueError('lightgrid axes')
    rmin=u16(z,grid+8+ra*2);rmax=u16(z,grid+0xe+ra*2);rows=rmax-rmin+1
    if rows:require_inline(u32(z,grid+0x1c),'rowdata')
    ensure(z,c,rows*2,'rows');c+=rows*2
    rawbytes=u32(z,grid+0x20)
    if rawbytes:require_inline(u32(z,grid+0x24),'raw rows')
    ensure(z,c,rawbytes,'raw rows');c+=rawbytes
    entries=u32(z,grid+0x28)
    if entries:require_inline(u32(z,grid+0x2c),'entries')
    ensure(z,c,entries*4,'entries');c+=entries*4
    colors=u32(z,grid+0x30)
    if colors:require_inline(u32(z,grid+0x34),'colors')
    ensure(z,c,colors*0xa8,'colors');c+=colors*0xa8;_add(branches,z,'lightmaps/LightGrid',st,c)
    st=c
    if models:require_inline(u32(z,r+0x154),'models')
    c+=models*PC_BRUSH_MODEL;ensure(z,st,models*PC_BRUSH_MODEL,'brush models')
    if matmem:require_inline(u32(z,r+0x178),'matmem')
    mmroots=c;c+=matmem*8;ensure(z,mmroots,matmem*8,'mat memory');_add(branches,z,'brush-model/material-memory roots',st,c)
    st=c;imats=[];water=0
    for i in range(matmem):
        if inline(u32(z,mmroots+i*8)):
            imats.append(c);m,c=parse_material_at(z,c);water+=bool(m.water_by_texture)
    _add(branches,z,'recursive Material graph',st,c)
    st=c
    if verts:require_inline(u32(z,r+0x34),'world verts')
    ensure(z,c,verts*PC_WORLD_VERTEX,'world verts');c+=verts*PC_WORLD_VERTEX;_add(branches,z,'vd.vertices',st,c)
    st=c
    if vld:require_inline(u32(z,r+0x40),'vld')
    ensure(z,c,vld,'vld');c+=vld;_add(branches,z,'vld.data',st,c)
    st=c
    for ptr_off in (0x180,0x184):
        if inline(u32(z,r+ptr_off)):
            imats.append(c);m,c=parse_material_at(z,c);water+=bool(m.water_by_texture)
    if inline(u32(z,r+0x21c)):c=_image(z,c,'outdoorImage',images)
    _add(branches,z,'sun/outdoor aliases',st,c)
    st=c
    if plights:require_inline(u32(z,r+0x23c),'shadow geom')
    shroots=c;c+=plights*PC_SHADOW;ensure(z,shroots,plights*PC_SHADOW,'shadow roots');shs=shm=0
    shadows=[]
    for i in range(plights):
        q=shroots+i*PC_SHADOW;sv=u16(z,q);mv=u16(z,q+2);sp=u32(z,q+4);mp=u32(z,q+8);shadows.append((sv,mv))
        if sv:require_inline(sp,'shadow surf')
        if mv:require_inline(mp,'shadow model')
    for sv,mv in shadows:ensure(z,c,sv*2,'shadow surf');c+=sv*2;shs+=sv;ensure(z,c,mv*2,'shadow model');c+=mv*2;shm+=mv
    if plights:require_inline(u32(z,r+0x240),'light regions')
    regroots=c;c+=plights*PC_REGION;ensure(z,regroots,plights*PC_REGION,'regions');regions=[]
    for i in range(plights):regions.append((u32(z,regroots+i*8),u32(z,regroots+i*8+4)))
    hulltotal=axistotal=0
    for hc,hp in regions:
        count(hc,0,1_000_000,'hulls')
        if not hc:continue
        require_inline(hp,'hulls ptr');hroots=c;c+=hc*PC_HULL;ensure(z,hroots,hc*PC_HULL,'hulls');hulltotal+=hc;hs=[]
        for i in range(hc):hs.append((u32(z,hroots+i*PC_HULL+0x48),u32(z,hroots+i*PC_HULL+0x4c)))
        for ac,ap in hs:
            if ac:require_inline(ap,'axes');ensure(z,c,ac*PC_AXIS,'axes');c+=ac*PC_AXIS;axistotal+=ac
    _add(branches,z,'shadow/light-region graph',st,c)
    st=c;sortcnt=ss+ssnd
    if sortcnt:require_inline(u32(z,r+0x28c),'dpvs sorted')
    ensure(z,c,sortcnt*2,'sorted');c+=sortcnt*2
    if smodels:require_inline(u32(z,r+0x290),'smodel inst')
    ensure(z,c,smodels*PC_SMODEL_INST,'smodel inst');c+=smodels*PC_SMODEL_INST
    if surfs:require_inline(u32(z,r+0x294),'surfaces')
    surfroots=c;c+=surfs*PC_SURFACE;ensure(z,surfroots,surfs*PC_SURFACE,'surfaces');mps=[];packedm=0
    for i in range(surfs):
        raw=u32(z,surfroots+i*PC_SURFACE+0x10);mps.append(raw);k=decode_pc_pointer(raw).kind
        if k=='packed':packedm+=1
        elif k in ('following','insert'):raise ValueError('inline surface material after reorder')
    if culls:require_inline(u32(z,r+0x298),'cull groups')
    ensure(z,c,culls*PC_CULL,'cull groups');c+=culls*PC_CULL
    if smodels:require_inline(u32(z,r+0x29c),'draw inst')
    drawroots=c;c+=smodels*PC_DRAW;ensure(z,drawroots,smodels*PC_DRAW,'draw inst');packedx=0;inline_models=[]
    for i in range(smodels):
        k=decode_pc_pointer(u32(z,drawroots+i*PC_DRAW+0x38)).kind
        if k=='packed':packedx+=1
        elif k in ('following','insert'):
            from .gfxworld_xmodels import read_shared_shell
            owner=c;name,c=read_shared_shell(z,c)
            inline_models.append((i,name,owner,c))
    _add(branches,z,'DPVS serialized graph',st,c)
    full=GfxFullGraph(c,aabbtotal,portaltotal,portalverts,inlinevals,rows,rawbytes,entries,colors,tuple(sorted(set(imats))),water,shs,shm,hulltotal,axistotal,sortcnt,tuple(mps),packedm,packedx,tuple(images),tuple(branches),tuple(inline_models))
    return GfxWorldReport(r,base,plane,nodes,indices,surfs,sky,verts,vld,sunidx,plights,culls,probes,cells,cellbits,lmaps,models,matmem,smodels,ss,ssnd,lb,le,db,de,eb,ee,dm,dbr,checksum,full)
