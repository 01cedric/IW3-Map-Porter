from __future__ import annotations
from dataclasses import dataclass
import struct, math
from .backend.v4_ffio import decode_pc_pointer
from .pc_gfxworld import GfxWorldReport, PC_CELL, PC_AABB, PC_PORTAL, PC_SMODEL_INST


def u16(z,o): return struct.unpack_from('<H',z,o)[0]
def u32(z,o): return struct.unpack_from('<I',z,o)[0]
def i32(z,o): return struct.unpack_from('<i',z,o)[0]
def f32(z,o): return struct.unpack_from('<f',z,o)[0]
def align(v,a): return (v+a-1)&~(a-1)
def ensure(z,o,n,label):
    if o<0 or n<0 or o+n>len(z): raise ValueError(f'{label} range 0x{o:X}+0x{n:X} outside zone')
def require_inline(raw,label):
    if decode_pc_pointer(raw).kind not in ('following','insert'): raise ValueError(f'{label} is not inline')


def ps3_children_offset(source_offset:int, child_count:int, node_index:int, node_count:int)->int:
    """Rebase an AABB's relative child address from 44-byte to 40-byte records.

    childrenOffset is a signed byte displacement from this node, not an index
    or a packed zone pointer. The SPU adds it directly to the node address.
    """
    if not child_count:
        return 0  # Unused on a leaf.
    label=f'GfxWorld AABB node {node_index}'
    if source_offset <= 0 or source_offset % PC_AABB:
        raise ValueError(f'{label}: child offset {source_offset} is not a forward PC record boundary')
    delta=source_offset//PC_AABB
    if node_index+delta+child_count > node_count:
        raise ValueError(f'{label}: child range exceeds its cell AABB array')
    return delta*0x28

@dataclass(frozen=True)
class AabbBinding:
    cell_index:int; aabb_index:int; record_offset:int; smodel_count:int; pointer_kind:str; pointer_raw:int
    inline_data_offset:int|None; canonical_inline_data_offset:int|None; target_byte_offset:int
@dataclass(frozen=True)
class AabbReusePlan:
    block4_cell_logical_base:int; pointer_consistent_candidates:int; bounds_perfect_candidates:int
    inline_pointer_count:int; packed_pointer_count:int; null_pointer_count:int; reused_smodel_values:int; inline_smodel_values:int
    bindings_by_record:dict[int,AabbBinding]
@dataclass(frozen=True)
class _Range:
    order:int;cell:int;aabb:int;record:int;relative:int;physical:int;count:int;bytes:int
@dataclass(frozen=True)
class _Ref:
    order:int;cell:int;aabb:int;record:int;count:int;bytes:int;target:int;raw:int


def _branch(report,name):
    for b in report.full.branches:
        if b.name==name:return b
    raise ValueError(f'missing branch {name}')

def _resolve_range(ranges,ref,base):
    match=None
    for r in ranges:
        if r.order>=ref.order: continue
        start=base+r.relative; end=start+r.bytes; tend=ref.target+ref.bytes
        if ref.target<start or tend>end or ((ref.target-start)&1):continue
        m=(r,ref.target-start)
        if match is not None: raise ValueError('packed AABB target resolves to multiple inline ranges')
        match=m
    return match

def resolve_aabb_reuse(z:bytes,report:GfxWorldReport)->AabbReusePlan:
    cb=_branch(report,'cells/AABB/portals'); db=_branch(report,'DPVS serialized graph')
    roots=cb.start;ensure(z,roots,report.cell_count*PC_CELL,'cells')
    cells=[]
    for ci in range(report.cell_count):
        q=roots+ci*PC_CELL;cells.append((i32(z,q+0x18),u32(z,q+0x1c),i32(z,q+0x20),u32(z,q+0x24),i32(z,q+0x28),u32(z,q+0x2c),z[q+0x30],u32(z,q+0x34)))
    physical=roots+report.cell_count*PC_CELL; logical=report.cell_count*PC_CELL; order=0
    ranges=[];refs=[];bindings={};meta={};inp=packp=nullp=inv=reused=0
    for ci,(ac,ap,pc,pp,cc,cp,pr,prp) in enumerate(cells):
        if ac:
            require_inline(ap,f'cell[{ci}].aabb');logical=align(logical,4);aroots=physical;size=ac*PC_AABB;ensure(z,aroots,size,'aabb roots');physical+=size;logical+=size
            for ai in range(ac):
                q=aroots+ai*PC_AABB;n=u16(z,q+0x22);raw=u32(z,q+0x24);ptr=decode_pc_pointer(raw);order+=1;meta[q]=((f32(z,q),f32(z,q+4),f32(z,q+8)),(f32(z,q+0xc),f32(z,q+0x10),f32(z,q+0x14)))
                if not n:
                    if ptr.kind!='null':raise ValueError('zero AABB count with non-null pointer')
                    nullp+=1;bindings[q]=AabbBinding(ci,ai,q,0,ptr.kind,raw,None,None,0);continue
                if ptr.kind in ('following','insert'):
                    logical=align(logical,2);bytes_=n*2;ensure(z,physical,bytes_,'AABB smodel values');ranges.append(_Range(order,ci,ai,q,logical,physical,n,bytes_));bindings[q]=AabbBinding(ci,ai,q,n,ptr.kind,raw,physical,physical,0);physical+=bytes_;logical+=bytes_;inp+=1;inv+=n
                elif ptr.kind=='packed':
                    dec=ptr
                    if dec.block != 4:raise ValueError('packed AABB not block4')
                    refs.append(_Ref(order,ci,ai,q,n,n*2,dec.offset,raw));packp+=1;reused+=n
                else:raise ValueError(f'unsupported AABB pointer {ptr.kind}')
        if pc:
            require_inline(pp,f'cell[{ci}].portals');logical=align(logical,4);proots=physical;bytes_=pc*PC_PORTAL;ensure(z,proots,bytes_,'portal roots');physical+=bytes_;logical+=bytes_
            for pi in range(pc):
                q=proots+pi*PC_PORTAL;vc=z[q+0x28];vp=u32(z,q+0x24)
                if vc:require_inline(vp,'portal vertices');logical=align(logical,4);ensure(z,physical,vc*12,'portal vertices');physical+=vc*12;logical+=vc*12
        if cc:require_inline(cp,'cell culls');logical=align(logical,4);ensure(z,physical,cc*4,'culls');physical+=cc*4;logical+=cc*4
        if pr:require_inline(prp,'cell probes');ensure(z,physical,pr,'probes');physical+=pr;logical+=pr
    if physical!=cb.end:raise ValueError(f'AABB resolver end 0x{physical:X} != branch 0x{cb.end:X}')
    if not refs:return AabbReusePlan(0,0,0,inp,0,nullp,0,inv,bindings)
    if not ranges:raise ValueError('packed AABB refs but no inline owner')
    # Every packed reference constrains the unknown block-4 base.  The old implementation
    # formed the UNION of all per-reference candidate bases (millions of 2-byte deltas for
    # mp_getaway) and only afterwards tested every candidate against every reference.  The
    # exact solution is the INTERSECTION of those per-reference constraints, so seed from
    # the most restrictive reference and filter that small set.  This preserves the same
    # pointer proof while avoiding the combinatorial candidate explosion.
    eligible_by_ref=[]
    for ref in refs:
        eligible=[r for r in ranges if r.order<ref.order and r.bytes>=ref.bytes]
        if not eligible:
            raise ValueError('packed AABB ref has no earlier inline owner range')
        estimate=0
        for r in eligible:
            hi=ref.target-r.relative
            if hi<0:continue
            lo=max(0,hi-(r.bytes-ref.bytes))
            first=(lo+3)&~3
            last=hi&~3
            if first<=last:estimate+=(last-first)//4+1
        eligible_by_ref.append((estimate,ref,eligible))
    eligible_by_ref.sort(key=lambda item:item[0])

    _,seed,seed_ranges=eligible_by_ref[0]
    candidates=set()
    for r in seed_ranges:
        hi=seed.target-r.relative
        if hi<0:continue
        lo=max(0,hi-(r.bytes-seed.bytes))
        first=(lo+3)&~3
        last=hi&~3
        if first<=last:candidates.update(range(first,last+1,4))

    def valid_for_ref(base,ref,eligible):
        # Equivalent to `_resolve_range(...) is not None` for candidate filtering, but it
        # avoids constructing temporary tuples and only considers ranges that can contain
        # this reference.  Ambiguity is checked later by the canonical resolver.
        for r in eligible:
            start=base+r.relative
            if ref.target<start:continue
            if ref.target+ref.bytes>start+r.bytes:continue
            if (ref.target-start)&1:continue
            return True
        return False

    for _,ref,eligible in eligible_by_ref[1:]:
        if not candidates:break
        candidates={base for base in candidates if valid_for_ref(base,ref,eligible)}
    pointer_consistent=sorted(candidates)
    if not pointer_consistent:raise ValueError('no pointer-consistent AABB block4 base')
    # Preserve the old resolver's uniqueness/ambiguity semantics exactly.
    for base in pointer_consistent:
        for ref in refs:
            _resolve_range(ranges,ref,base)
    sorted_count=report.static_surface_count+report.static_surface_count_no_decal; smodels=db.start+sorted_count*2;ensure(z,smodels,report.static_model_count*PC_SMODEL_INST,'smodel inst')
    perfect=[]
    for base in pointer_consistent:
        valid=True
        for ref in refs:
            resolved=_resolve_range(ranges,ref,base)
            if resolved is None:valid=False;break
            rng,delta=resolved;src=rng.physical+delta;mn,mx=meta[ref.record]
            for vi in range(ref.count):
                sm=u16(z,src+vi*2)
                if sm>=report.static_model_count:valid=False;break
                q=smodels+sm*PC_SMODEL_INST
                for axis in range(3):
                    vmin=f32(z,q+axis*4);vmax=f32(z,q+0xc+axis*4)
                    if not (math.isfinite(vmin) and math.isfinite(vmax)) or vmin<mn[axis]-0.01 or vmax>mx[axis]+0.01:valid=False;break
                if not valid:break
            if not valid:break
        if valid:perfect.append(base)
    if len(perfect)!=1:raise ValueError(f'AABB base candidates pointer={len(pointer_consistent)} bounds-perfect={len(perfect)}')
    base=perfect[0]
    for ref in refs:
        rng,delta=_resolve_range(ranges,ref,base)
        bindings[ref.record]=AabbBinding(ref.cell,ref.aabb,ref.record,ref.count,'packed',ref.raw,None,rng.physical,delta)
    return AabbReusePlan(base,len(pointer_consistent),1,inp,packp,nullp,reused,inv,bindings)
