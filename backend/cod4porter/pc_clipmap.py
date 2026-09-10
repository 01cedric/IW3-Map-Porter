from __future__ import annotations
from dataclasses import dataclass,field
import hashlib,math,struct,re
from .backend.v4_ffio import decode_pc_pointer
from .physics import PhysPreset,parse_pc_phys_preset

ROOT=0x11C

def ensure(z,o,n,label='range'):
    if o<0 or n<0 or o+n>len(z):raise ValueError(f'{label} 0x{o:X}+0x{n:X} outside zone 0x{len(z):X}')
def u16(z,o):ensure(z,o,2);return struct.unpack_from('<H',z,o)[0]
def i16(z,o):ensure(z,o,2);return struct.unpack_from('<h',z,o)[0]
def u32(z,o):ensure(z,o,4);return struct.unpack_from('<I',z,o)[0]
def i32(z,o):ensure(z,o,4);return struct.unpack_from('<i',z,o)[0]
def f32(z,o):ensure(z,o,4);return struct.unpack_from('<f',z,o)[0]
def v3(z,o):return (f32(z,o),f32(z,o+4),f32(z,o+8))
def v4(z,o):return (f32(z,o),f32(z,o+4),f32(z,o+8),f32(z,o+12))
def matrix3(z,o):return tuple(f32(z,o+i*4) for i in range(9))
def count(v,lo,hi,label):
    if not lo<=v<=hi:raise ValueError(f'{label}={v} outside {lo}..{hi}')
    return v
def packed(raw,label):
    p=decode_pc_pointer(raw)
    if p.kind!='packed':raise ValueError(f'{label} requires packed pointer, got {p.kind} 0x{raw:08X}')
    return p
def fixed_string(z,o,n):
    ensure(z,o,n,'fixed string');b=z[o:o+n];e=b.find(b'\0');return b[:n if e<0 else e].decode('latin-1')

def _looks_plane(z,o):
    if o<0 or o+0x14>len(z):return False
    x,y,zz,d=v3(z,o)+(f32(z,o+12),)
    return all(math.isfinite(q) for q in (x,y,zz,d)) and abs(x*x+y*y+zz*zz-1)<=0.001 and z[o+0x10]<=3 and z[o+0x11]<=7 and z[o+0x12]==0 and z[o+0x13]==0

def find_root(z):
    marker=b'\xfe\xff\xff\xff\xff\xff\xff\xff';found=[];s=0
    while True:
        pos=z.find(marker,s)
        if pos<0:break
        s=pos+1;r=pos-0xa4
        if r<0 or r+ROOT>len(z):continue
        if i32(z,r+4) not in (0,1):continue
        planes=u32(z,r+8);sm=u32(z,r+0x10);m=u32(z,r+0x18);si=u32(z,r+0x20)
        if not(1<=planes<=500000 and sm<=100000 and 1<=m<=20000 and 1<=si<=1000000):continue
        if u32(z,r+0xa4)!=0xfffffffe or u32(z,r+0xa8)!=0xffffffff:continue
        if u32(z,r+0xc) in (0,0xffffffff,0xfffffffe):continue
        found.append(r)
    found=sorted(set(found))
    if len(found)!=1:raise ValueError(f'expected one ClipMap root, found {len(found)}')
    return found[0]
def locate_planes(z,n):
    # Scan once in C with a regex for the constrained cplane_s trailer
    # [type<=3, signBits<=7, 0, 0], then validate normal/dist fields and
    # distributed array probes.  This preserves the original fail-closed
    # uniqueness check while avoiding a Python byte-by-byte 100+ MiB scan.
    bytes_=n*0x14;limit=len(z)-bytes_;first=[]
    pat=re.compile(rb'[\x00-\x03][\x00-\x07]\x00\x00')
    for m in pat.finditer(z):
        o=m.start()-0x10
        if 0<=o<=limit and _looks_plane(z,o):first.append(o)
    probes=tuple(p for p in (1,7,31,127,511,1023,2047,n-1) if 0<=p<n)
    # A probe hit anywhere inside a long run of valid planes looks exactly like a hit at
    # its start, so a big ClipMap produces dozens of candidates that are all the same
    # array seen from different offsets - stock mp_crash yields 82 for one 11,400-entry
    # array.  Keep only run starts, then validate the array in full rather than by
    # sampling.  Both shipped maps then resolve to exactly one candidate.
    starts=[o for o in first
            if all(_looks_plane(z,o+p*0x14) for p in probes)
            and (o<0x14 or not _looks_plane(z,o-0x14))]
    c=[]
    for o in starts:
        if all(_looks_plane(z,o+i*0x14) for i in range(n)):
            c.append(o)
            if len(c)>1:break
    if len(c)!=1:raise ValueError(f'expected one plane array, found {len(c)}')
    return c[0]


@dataclass(frozen=True)
class Plane: normal:tuple[float,float,float];distance:float;type:int;sign_bits:int
@dataclass(frozen=True)
class StaticModel: next_sector:int;xmodel_raw:int;origin:tuple;axis:tuple;minimum:tuple;maximum:tuple
@dataclass(frozen=True)
class Material: name:str;surface_flags:int;content_flags:int
@dataclass(frozen=True)
class BrushSide: plane_index:int;material_index:int;adjacent:int;edge_count:int
@dataclass(frozen=True)
class Node: plane_index:int;front:int;back:int
@dataclass(frozen=True)
class Leaf: first_aabb:int;aabb_count:int;brush_contents:int;terrain_contents:int;minimum:tuple;maximum:tuple;leaf_brush_node:int;cluster:int
@dataclass(frozen=True)
class LeafBrushNode:
    axis:int;count:int;contents:int;distance:float;range:float;front:int;back:int;storage:str;root_start:int;brush_indices:tuple[int,...]
@dataclass(frozen=True)
class Border: equation:tuple;zbase:float;zslope:float;start:float;length:float
@dataclass(frozen=True)
class Partition:triangle_count:int;border_count:int;first_triangle:int;first_border:int
@dataclass(frozen=True)
class Aabb:origin:tuple;half:tuple;material_index:int;child_count:int;child_or_partition:int
@dataclass(frozen=True)
class SubModel:minimum:tuple;maximum:tuple;radius:float;first_aabb:int;aabb_count:int;brush_contents:int;terrain_contents:int;leaf_min:tuple;leaf_max:tuple;leaf_brush_node:int;cluster:int
@dataclass(frozen=True)
class Brush:
    minimum:tuple;maximum:tuple;contents:int;first_side:int;non_axial:int;axial_materials:tuple[int,...];base_edge:int;adjacent_offsets:tuple[int,...];axial_edge_counts:tuple[int,...]
@dataclass(frozen=True)
class DynEnt:
    list_kind:str;index:int;type:int;quaternion:tuple;origin:tuple;xmodel_raw:int;brush_model:int;physics_brush_model:int;destroy_fx_raw:int;destroy_pieces_raw:int;phys_preset_raw:int;health:int;center_mass:tuple;moments:tuple;products:tuple;contents:int
@dataclass(frozen=True)
class DynEntOwnedPhysPreset:
    list_kind:str;index:int;definition_ordinal:int;pointer_kind:str;preset:PhysPreset;source_root:int;source_end:int;pointer_owner_loadstream_offset:int|None=None;pointer_owner_evidence:tuple[str,...]=()

    @property
    def pointer_field_relative_offset(self)->int:
        return self.definition_ordinal*0x60+0x30
@dataclass(frozen=True)
class ClipMap:
    name:str;root_offset:int;in_use:int;checksum:int;planes:tuple[Plane,...];static_models:tuple[StaticModel,...];materials:tuple[Material,...];brush_sides:tuple[BrushSide,...];brush_edges:bytes;nodes:tuple[Node,...];leaves:tuple[Leaf,...];root_leaf_brushes:tuple[int,...];leaf_brush_nodes:tuple[LeafBrushNode,...];vertices:tuple[tuple,...];triangle_indices:tuple[int,...];walkability:bytes;borders:tuple[Border,...];partitions:tuple[Partition,...];aabbs:tuple[Aabb,...];submodels:tuple[SubModel,...];brushes:tuple[Brush,...];box_model:SubModel;box_brush:Brush;cluster_count:int;cluster_bytes:int;visibility:bytes;vised:int;mapents_text:str;mapents_allocated:int;dyn_models:tuple[DynEnt,...];dyn_brushes:tuple[DynEnt,...];known_end:int;source_cluster_count:int;source_cluster_bytes:int;visibility_expanded:bool;dyn_owned_phys_presets:tuple[DynEntOwnedPhysPreset,...]=()
    @property
    def triangle_count(self):return len(self.triangle_indices)//3


def _leaf(z,r):return Leaf(u16(z,r),u16(z,r+2),i32(z,r+4),i32(z,r+8),v3(z,r+0xc),v3(z,r+0x18),i32(z,r+0x24),i16(z,r+0x28))
def _submodel(z,r):return SubModel(v3(z,r),v3(z,r+0xc),f32(z,r+0x18),u16(z,r+0x1c),u16(z,r+0x1e),i32(z,r+0x20),i32(z,r+0x24),v3(z,r+0x28),v3(z,r+0x34),i32(z,r+0x40),i16(z,r+0x44))
def _infer_root_leaf_base(z,o,n):
    vals=[]
    for j in range(n):
        r=o+j*0x14
        if i16(z,r+2)>0:
            p=decode_pc_pointer(u32(z,r+8))
            if p.kind=='packed':
                if p.block!=4:raise ValueError('leaf brush packed pointer is not block 4')
                vals.append(p.offset)
    return min(vals) if vals else 0
def _infer_border_base(z,o,n):
    vals=[]
    for j in range(n):
        r=o+j*0xc
        if z[r+1]:
            p=decode_pc_pointer(u32(z,r+8))
            if p.kind=='packed':
                if p.block!=4:raise ValueError('partition border pointer is not block 4')
                vals.append(p.offset)
    return min(vals) if vals else 0
def _infer_brush_bases(z,o,n):
    s=[];e=[]
    for j in range(n):
        r=o+j*0x50
        p=decode_pc_pointer(u32(z,r+0x20));q=decode_pc_pointer(u32(z,r+0x30))
        if p.kind=='packed':
            if p.block!=4:raise ValueError('brush side pointer is not block 4')
            s.append(p.offset)
        if q.kind=='packed':
            if q.block!=4:raise ValueError('brush edge pointer is not block 4')
            e.append(q.offset)
    if not s or not e:raise ValueError('cannot infer brush pointer bases')
    return min(s),min(e)
def _brush(z,r,sbase,ebase,side_count,edge_count,box=False):
    non=u32(z,r+0x1c);sp=decode_pc_pointer(u32(z,r+0x20));ep=decode_pc_pointer(u32(z,r+0x30));first=-1
    if non:
        if sp.kind!='packed' or sp.block!=4 or sp.offset<sbase or (sp.offset-sbase)%0xc:raise ValueError('brush side pointer')
        first=(sp.offset-sbase)//0xc
        if first>side_count-non:raise ValueError('brush side range')
    edge=-1
    if ep.kind=='packed':
        if ep.block!=4 or ep.offset<ebase:raise ValueError('brush edge pointer')
        edge=ep.offset-ebase
        if edge>edge_count:raise ValueError('brush edge range')
    elif not box and ep.kind!='null':raise ValueError('brush edge not packed/null')
    mats=tuple(i16(z,r+0x24+j*2) for j in range(6));adj=tuple(i16(z,r+0x34+j*2) for j in range(6));ec=tuple(z[r+0x40+j] for j in range(6))
    return Brush(v3(z,r),v3(z,r+0x10),i32(z,r+0xc),first,non,mats,edge,adj,ec)
def _dyn(z,o,n,kind):
    out=[]
    for j in range(n):
        r=o+j*0x60;t=i32(z,r)
        if t not in (0,1,2):raise ValueError('DynEnt type')
        out.append(DynEnt(kind,j,t,v4(z,r+4),v3(z,r+0x14),u32(z,r+0x20),u16(z,r+0x24),u16(z,r+0x26),u32(z,r+0x28),u32(z,r+0x2c),u32(z,r+0x30),i32(z,r+0x34),v3(z,r+0x38),v3(z,r+0x44),v3(z,r+0x50),i32(z,r+0x5c)))
    return tuple(out)

def _align_up(value:int,alignment:int)->int:return (value+alignment-1)&~(alignment-1)

def _dynent_definition_loadstream_bases(
    *,side_base:int,edge_base:int,side_count:int,edge_count:int,node_count:int,
    leaf_count:int,root_leaf_base:int,root_leaf_count:int,leaf_node_count:int,
    inline_leaf_bytes:int,vertex_count:int,triangle_index_count:int,walk_bytes:int,
    border_base:int,border_count:int,partition_count:int,aabb_count:int,
    submodel_count:int,brush_count:int,visibility_bytes:int,map_name_pointer_kind:str,
    map_name_pointer_block:int|None,
    entity_pointer_kind:str,mapents_allocated:int,dyn_model_count:int,dyn_brush_count:int,
)->tuple[int|None,int|None,int|None,tuple[str,...]]:
    """Replay the owned ClipMap B4 stream from typed internal pointer anchors.

    Physical offsets cannot establish a packed-pointer address because TEMP roots and logical-only
    alignment do not consume the same stream.  BrushSide/BrushEdge, root-leaf-brush and Border
    references do: they are typed pointers back into arrays owned by this exact ClipMap.  Only an
    exact replay supported by at least three such anchors is allowed to certify DynEntDef cells.
    """
    if not dyn_model_count and not dyn_brush_count:return None,None,None,()
    if side_count<=0 or edge_count<=0 or side_base<=0 or edge_base<=0:return None,None,None,()
    if edge_base!=side_base+side_count*0x0c:return None,None,None,()
    evidence=[
        f'BrushSide owner base b4+0x{side_base:X}',
        f'BrushEdge owner base b4+0x{edge_base:X} follows {side_count} BrushSides',
    ]
    cur=side_base+side_count*0x0c+edge_count
    cur=_align_up(cur,4);cur+=node_count*0x08+leaf_count*0x2c
    cur=_align_up(cur,2)
    if root_leaf_base:
        if cur!=root_leaf_base:return None,None,None,()
        evidence.append(f'RootLeafBrush owner base b4+0x{root_leaf_base:X}')
    cur+=root_leaf_count*0x02
    cur=_align_up(cur,4);cur+=leaf_node_count*0x14+inline_leaf_bytes
    cur=_align_up(cur,4);cur+=vertex_count*0x0c+triangle_index_count*0x02+walk_bytes
    cur=_align_up(cur,4)
    if border_base:
        if cur!=border_base:return None,None,None,()
        evidence.append(f'Border owner base b4+0x{border_base:X}')
    if len(evidence)<3:return None,None,None,()
    cur+=border_count*0x1c+partition_count*0x0c+aabb_count*0x20+submodel_count*0x48
    cur=_align_up(cur,16);cur+=brush_count*0x50+visibility_bytes
    # clipMap_t::mapEnts is INSERT-owned.  Its persistent alias cell is B4, while the
    # MapEnts root itself is TEMP.  A packed MapEnts name proves that no local name bytes
    # intervene before the entity string.
    if map_name_pointer_kind!='packed' or map_name_pointer_block!=4 or entity_pointer_kind not in ('following','insert'):
        return None,None,None,()
    cur=_align_up(cur,4);mapents_alias=cur;cur+=4
    evidence.append(f'MapEnts INSERT owner cell b4+0x{mapents_alias:X}')
    if entity_pointer_kind=='insert':cur=_align_up(cur,4)+4
    cur+=mapents_allocated
    cur=_align_up(cur,16);cur+=0x50
    model_base=None;brush_base=None
    if dyn_model_count:
        cur=_align_up(cur,4);model_base=cur;cur+=dyn_model_count*0x60
        evidence.append(f'DynEnt model LoadStream base b4+0x{model_base:X}')
    if dyn_brush_count:
        cur=_align_up(cur,4);brush_base=cur;cur+=dyn_brush_count*0x60
        evidence.append(f'DynEnt brush LoadStream base b4+0x{brush_base:X}')
    return model_base,brush_base,cur,tuple(evidence)

def _advance_owned_xstring_cursor(cursor:int,raw:int,value:str|None)->int|None:
    kind=decode_pc_pointer(raw).kind
    if kind=='packed' or kind=='null':return cursor
    if kind not in ('following','insert') or value is None:return None
    if kind=='insert':cursor=_align_up(cursor,4)+4
    return cursor+len(value.encode('latin-1'))+1

def resolve_dynent_owned_phys_aliases(clip:ClipMap,raw_values)->dict[int,DynEntOwnedPhysPreset]:
    """Bind packed DynEnt pointer-field aliases to their exact inline owners.

    IW3's packed references target the earlier ``DynEntDef.physPreset`` owner cell, not the later
    inline PhysPreset root.  The absolute block-4 address is recovered either from a typed ClipMap
    LoadStream replay or from at least two independent target/owner equations; physical file
    offsets and names are never treated as address evidence.
    """
    owners=tuple(clip.dyn_owned_phys_presets)
    unique=tuple(sorted(set(int(raw)&0xffffffff for raw in raw_values if raw)))
    if not unique:return {}
    if not owners:raise ValueError('packed DynEnt PhysPreset aliases exist without inline owners')
    targets={}
    for raw in unique:
        ptr=decode_pc_pointer(raw)
        if ptr.kind!='packed' or ptr.block!=4:
            raise ValueError(f'DynEnt PhysPreset alias 0x{raw:08X} is not a packed block-4 pointer')
        targets[raw]=ptr.offset
    # A single packed use is resolvable when the parser proved the absolute owner cell by
    # replaying this ClipMap's typed B4 stream.  This is stronger than subtracting an arbitrary
    # owner-relative offset from the target: the latter is merely a one-equation coincidence.
    exact_by_offset={}
    for owner in owners:
        offset=owner.pointer_owner_loadstream_offset
        if offset is not None and owner.pointer_owner_evidence:
            exact_by_offset.setdefault(offset,[]).append(owner)
    exact={}
    for raw,target in targets.items():
        matches=exact_by_offset.get(target,())
        if len(matches)==1:exact[raw]=matches[0]
        elif len(matches)>1:
            raise ValueError(f'DynEnt PhysPreset pointer-owner provenance is ambiguous at b4+0x{target:X}')
    if len(exact)==len(targets):return exact

    valid=[]
    bases={target-owner.pointer_field_relative_offset for target in targets.values() for owner in owners}
    by_relative={owner.pointer_field_relative_offset:owner for owner in owners}
    for base in sorted(bases):
        mapping={}
        for raw,target in targets.items():
            owner=by_relative.get(target-base)
            if owner is None:break
            mapping[raw]=owner
        else:
            if all(mapping[raw] is owner for raw,owner in exact.items()):valid.append((base,mapping))
    # One target can trivially fit every owner and cannot establish a loader basis. Getaway has
    # two independent targets (owners 107 and 150), which uniquely prove the basis and all 36 uses.
    if len(targets)<2 or len(valid)!=1:
        raise ValueError(f'DynEnt PhysPreset alias basis is not unique: targets={len(targets)} candidates={len(valid)}')
    return valid[0][1]

def parse_clipmap(z:bytes,map_name:str)->ClipMap:
    r=find_root(z);pcnt=count(u32(z,r+8),1,500000,'planes');smc=count(u32(z,r+0x10),0,100000,'static models');mc=count(u32(z,r+0x18),1,20000,'materials');sc=count(u32(z,r+0x20),1,1000000,'brush sides');ec=count(u32(z,r+0x28),0,20000000,'brush edges');nc=count(u32(z,r+0x30),1,1000000,'nodes');lc=count(u32(z,r+0x38),1,1000000,'leaves');lnc=count(u32(z,r+0x40),1,1000000,'leaf nodes');rlc=count(u32(z,r+0x48),0,20000000,'root leaf brushes');lsc=count(u32(z,r+0x50),0,20000000,'leaf surfaces')
    if lsc:raise ValueError('ClipMap leaf surfaces not zero')
    vc=count(u32(z,r+0x58),0,20000000,'vertices');tc=count(i32(z,r+0x60),0,20000000,'triangles');bc=count(i32(z,r+0x6c),0,20000000,'borders');partc=count(i32(z,r+0x74),0,20000000,'partitions');ac=count(i32(z,r+0x7c),0,20000000,'aabbs');subc=count(u32(z,r+0x84),1,100000,'submodels');brushc=u16(z,r+0x8c);srcclusters=count(i32(z,r+0x94),1,1000000,'clusters');srcbytes=count(i32(z,r+0x98),0,100000000,'cluster bytes');vised=i32(z,r+0xa0)
    if vised not in (0,1):raise ValueError('vised')
    dmc=u16(z,r+0xf4);dbc=u16(z,r+0xf6);checksum=u32(z,r+0x118);pbase=packed(u32(z,r+0xc),'planes');pphys=locate_planes(z,pcnt);planes=tuple(Plane(v3(z,pphys+j*0x14),f32(z,pphys+j*0x14+0xc),z[pphys+j*0x14+0x10],z[pphys+j*0x14+0x11]) for j in range(pcnt));cur=r+ROOT
    sms=[]
    for j in range(smc):q=cur+j*0x50;sms.append(StaticModel(u16(z,q),u32(z,q+4),v3(z,q+8),matrix3(z,q+0x14),v3(z,q+0x38),v3(z,q+0x44)))
    cur+=smc*0x50;mats=tuple(Material(fixed_string(z,cur+j*0x48,64),i32(z,cur+j*0x48+0x40),i32(z,cur+j*0x48+0x44)) for j in range(mc));cur+=mc*0x48
    sides=[]
    for j in range(sc):
        q=cur+j*0xc;p=packed(u32(z,q),f'side {j} plane')
        if p.block!=pbase.block or p.offset<pbase.offset or (p.offset-pbase.offset)%0x14:raise ValueError('side plane range')
        pi=(p.offset-pbase.offset)//0x14
        if pi>=pcnt:raise ValueError('side plane index')
        sides.append(BrushSide(pi,u32(z,q+4),i16(z,q+8),z[q+0xa]))
    cur+=sc*0xc;edges=z[cur:cur+ec];cur+=ec;nodes=[]
    for j in range(nc):
        q=cur+j*8;p=packed(u32(z,q),f'node {j} plane')
        if p.block!=pbase.block or p.offset<pbase.offset or (p.offset-pbase.offset)%0x14:raise ValueError('node plane')
        nodes.append(Node((p.offset-pbase.offset)//0x14,i16(z,q+4),i16(z,q+6)))
    cur+=nc*8;leaves=tuple(_leaf(z,cur+j*0x2c) for j in range(lc));cur+=lc*0x2c;maxcl=max((x.cluster for x in leaves),default=-1)
    if any(x.cluster<-1 for x in leaves):raise ValueError('leaf cluster below -1')
    clusters=max(srcclusters,maxcl+1);clbytes=max(srcbytes,(clusters+7)//8);rootbrush=tuple(u16(z,cur+j*2) for j in range(rlc));cur+=rlc*2;lnroot=cur;lbbase=_infer_root_leaf_base(z,cur,lnc);leafnodes=[];pending=[]
    for j in range(lnc):
        q=lnroot+j*0x14;axis=z[q];n=i16(z,q+2);contents=i32(z,q+4)
        if n>0:
            p=decode_pc_pointer(u32(z,q+8))
            if p.kind=='packed':
                if not lbbase or p.offset<lbbase or (p.offset-lbbase)%2:raise ValueError('leaf brush packed')
                start=(p.offset-lbbase)//2
                if start>rlc-n:raise ValueError('leaf brush root range')
                leafnodes.append(LeafBrushNode(axis,n,contents,0,0,0,0,'packed_root',start,rootbrush[start:start+n]))
            elif p.kind in ('following','insert'):
                pending.append((j,n));leafnodes.append(LeafBrushNode(axis,n,contents,0,0,0,0,'inline',-1,()))
            else:raise ValueError('leaf brush pointer')
        else:leafnodes.append(LeafBrushNode(axis,n,contents,f32(z,q+8),f32(z,q+0xc),u16(z,q+0x10),u16(z,q+0x12),'none',-1,()))
    cur+=lnc*0x14
    for idx,n in pending:
        vals=tuple(u16(z,cur+j*2) for j in range(n));cur+=n*2;o=leafnodes[idx];leafnodes[idx]=LeafBrushNode(o.axis,o.count,o.contents,o.distance,o.range,o.front,o.back,o.storage,o.root_start,vals)
    verts=tuple(v3(z,cur+j*0xc) for j in range(vc));cur+=vc*0xc;tic=tc*3;tris=tuple(u16(z,cur+j*2) for j in range(tic));cur+=tic*2;walkbytes=((tic+31)//32)*4;walk=z[cur:cur+walkbytes];cur+=walkbytes;borders=tuple(Border(v3(z,cur+j*0x1c),f32(z,cur+j*0x1c+0xc),f32(z,cur+j*0x1c+0x10),f32(z,cur+j*0x1c+0x14),f32(z,cur+j*0x1c+0x18)) for j in range(bc));cur+=bc*0x1c;bbase=_infer_border_base(z,cur,partc);parts=[]
    for j in range(partc):
        q=cur+j*0xc;t=z[q];bn=z[q+1];first=i32(z,q+4);fb=0
        if bn:
            p=packed(u32(z,q+8),'partition borders')
            if p.offset<bbase or (p.offset-bbase)%0x1c:raise ValueError('partition border')
            fb=(p.offset-bbase)//0x1c
        parts.append(Partition(t,bn,first,fb))
    cur+=partc*0xc;aabbs=tuple(Aabb(v3(z,cur+j*0x20),v3(z,cur+j*0x20+0xc),u16(z,cur+j*0x20+0x18),u16(z,cur+j*0x20+0x1a),i32(z,cur+j*0x20+0x1c)) for j in range(ac));cur+=ac*0x20;subs=tuple(_submodel(z,cur+j*0x48) for j in range(subc));cur+=subc*0x48;sbase,ebase=_infer_brush_bases(z,cur,brushc);brushes=tuple(_brush(z,cur+j*0x50,sbase,ebase,sc,ec) for j in range(brushc));cur+=brushc*0x50;srcvis=z[cur:cur+srcclusters*srcbytes];cur+=srcclusters*srcbytes;expanded=clusters!=srcclusters or clbytes!=srcbytes
    if expanded:
        vis=bytearray(b'\xff'*(clusters*clbytes))
        for j in range(srcclusters):vis[j*clbytes:j*clbytes+srcbytes]=srcvis[j*srcbytes:(j+1)*srcbytes]
        visibility=bytes(vis)
    else:visibility=srcvis
    maproot=cur;ensure(z,maproot,0xc,'MapEnts');ep=decode_pc_pointer(u32(z,maproot+4));alloc=count(i32(z,maproot+8),1,100000000,'MapEnts bytes')
    if ep.kind not in ('following','insert'):raise ValueError('MapEnts not inline')
    ent=maproot+0xc;ensure(z,ent,alloc,'MapEnts allocation');zero=z.find(b'\0',ent,ent+alloc)
    if zero<0:raise ValueError('MapEnts no NUL')
    text=z[ent:zero].decode('latin-1');cur=ent+alloc;boxbrush=_brush(z,cur,sbase,ebase,sc,ec,True);cur+=0x50;dynm=_dyn(z,cur,dmc,'model');cur+=dmc*0x60;dynb=_dyn(z,cur,dbc,'brush');cur+=dbc*0x60;boxmodel=_submodel(z,r+0xac)
    model_load_base,brush_load_base,owned_load_cursor,load_evidence=_dynent_definition_loadstream_bases(
        side_base=sbase,edge_base=ebase,side_count=sc,edge_count=ec,node_count=nc,
        leaf_count=lc,root_leaf_base=lbbase,root_leaf_count=rlc,leaf_node_count=lnc,
        inline_leaf_bytes=sum(n*2 for _,n in pending),vertex_count=vc,
        triangle_index_count=tic,walk_bytes=walkbytes,border_base=bbase,border_count=bc,
        partition_count=partc,aabb_count=ac,submodel_count=subc,brush_count=brushc,
        visibility_bytes=srcclusters*srcbytes,
        map_name_pointer_kind=decode_pc_pointer(u32(z,maproot)).kind,
        map_name_pointer_block=decode_pc_pointer(u32(z,maproot)).block,
        entity_pointer_kind=ep.kind,mapents_allocated=alloc,
        dyn_model_count=dmc,dyn_brush_count=dbc,
    )
    owned=[]
    for ordinal,e in enumerate(dynm+dynb):
        kind=decode_pc_pointer(e.phys_preset_raw).kind
        if kind not in ('following','insert'):continue
        root=cur;preset,cur,meta=parse_pc_phys_preset(z,root)
        preset.source_root_offset=root;preset.source_physical_end=cur
        preset.serialized_name_pointer=meta['serialized_name_pointer'];preset.name_identity_proven=meta['name_identity_proven']
        preset.serialized_sound_alias_prefix_pointer=meta['serialized_sound_alias_prefix_pointer'];preset.sound_alias_prefix_identity_proven=meta['sound_alias_prefix_identity_proven']
        definition_base=model_load_base if e.list_kind=='model' else brush_load_base
        pointer_owner=None;owner_evidence=()
        if definition_base is not None and owned_load_cursor is not None:
            field=definition_base+e.index*0x60+0x30
            if kind=='following':
                pointer_owner=field
                owner_evidence=load_evidence+(f'DynEnt {e.list_kind}[{e.index}].physPreset field b4+0x{field:X}',)
            else:
                owned_load_cursor=_align_up(owned_load_cursor,4);pointer_owner=owned_load_cursor;owned_load_cursor+=4
                owner_evidence=load_evidence+(
                    f'DynEnt {e.list_kind}[{e.index}].physPreset field b4+0x{field:X}',
                    f'DynEnt {e.list_kind}[{e.index}].physPreset INSERT owner b4+0x{pointer_owner:X}',
                )
        if owned_load_cursor is not None:
            owned_load_cursor=_advance_owned_xstring_cursor(owned_load_cursor,meta['serialized_name_pointer'],preset.name)
        if owned_load_cursor is not None:
            owned_load_cursor=_advance_owned_xstring_cursor(owned_load_cursor,meta['serialized_sound_alias_prefix_pointer'],preset.sound_alias_prefix)
        owned.append(DynEntOwnedPhysPreset(e.list_kind,e.index,ordinal,kind,preset,root,cur,pointer_owner,owner_evidence))
    return ClipMap(map_name,r,i32(z,r+4),checksum,planes,tuple(sms),mats,tuple(sides),edges,tuple(nodes),leaves,rootbrush,tuple(leafnodes),verts,tris,walk,borders,tuple(parts),aabbs,subs,brushes,boxmodel,boxbrush,clusters,clbytes,visibility,vised,text,alloc,dynm,dynb,cur,srcclusters,srcbytes,expanded,tuple(owned))
