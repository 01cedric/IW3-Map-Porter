from __future__ import annotations
from dataclasses import dataclass,field
import bisect,collections,hashlib,math,struct
from typing import Callable,Mapping,Sequence
from .backend.v4_ffio import decode_pc_pointer, XAssetList
from .xmodel import XModel,XSurface,XModelLod,PC_XMODEL_ROOT_SIZE,PC_XSURFACE_ROOT_SIZE,PC_PACKED_VERTEX_SIZE,RIGID_VERT_LIST_SIZE,COLLISION_TREE_ROOT_SIZE,COLLISION_NODE_SIZE,COLLISION_LEAF_SIZE
from .physics import PhysPreset,PhysGeomList


def _u16(z,o):return struct.unpack_from('<H',z,o)[0]
def _i16(z,o):return struct.unpack_from('<h',z,o)[0]
def _u32(z,o):return struct.unpack_from('<I',z,o)[0]
def _i32(z,o):return struct.unpack_from('<i',z,o)[0]
def _f32(z,o):return struct.unpack_from('<f',z,o)[0]
def _inline(raw):return decode_pc_pointer(raw).kind in ('following','insert')
def _packed(raw):return decode_pc_pointer(raw).kind=='packed'

def _read_ascii_z(z:bytes,start:int,max_bytes=512)->tuple[str,int]:
    end=z.find(b'\0',start,min(len(z),start+max_bytes))
    if end<0 or end==start: raise ValueError('missing/empty string')
    raw=z[start:end]
    if any(b<0x20 or b>0x7e for b in raw): raise ValueError('non-printable asset string')
    return raw.decode('ascii'),end+1

def _looks_name(s:str)->bool:
    if not s or len(s)>100:return False
    return all(c.isalnum() or c in '_./~,-&' for c in s)

@dataclass
class PcXModelIntermediate:
    model:XModel
    root_offset:int
    material_handle_physical_offset:int
    material_handle_pointers:list[int]
    skeleton_pointers:dict[str,int]
    inline_skeleton_payloads:dict[str,tuple[int,int]]
    inline_blend_offsets:list[int|None]
    inline_vertex_offsets:list[int|None]
    inline_rigid_offsets:list[int|None]
    inline_triangle_offsets:list[int|None]
    physical_after_material_handles:int
    inline_material_offsets:list[int|None]=field(default_factory=list)
    physical_end:int|None=None


def scan_xmodel_roots(zone:bytes)->list[int]:
    roots=[];needle=b'\xff\xff\xff\xff';start=0;limit=len(zone)-PC_XMODEL_ROOT_SIZE-2
    while True:
        r=zone.find(needle,start)
        if r<0 or r>limit:break
        start=r+1
        try:
            bones=zone[r+4];root_bones=zone[r+5];surfs=zone[r+6]
            lods=_u16(zone,r+0xC4);coll_lod=_i16(zone,r+0xC6)
            if bones==0 or root_bones==0 or root_bones>bones or surfs==0 or not 1<=lods<=4 or not -1<=coll_lod<=4:continue
            if not _inline(_u32(zone,r+0x20)) or not _inline(_u32(zone,r+0x24)):continue
            bptr=_u32(zone,r+0x08)
            if bptr not in (0,0xffffffff,0xfffffffe) and bptr<0x10000000:continue
            radius=_f32(zone,r+0xA8)
            if not math.isfinite(radius) or not 0<=radius<=100000:continue
            ok=True
            for i in range(3):
                mn=_f32(zone,r+0xAC+i*4);mx=_f32(zone,r+0xB8+i*4)
                if not math.isfinite(mn) or not math.isfinite(mx) or abs(mn)>1e7 or abs(mx)>1e7 or mn>mx:ok=False;break
            if not ok:continue
            for i in range(4):
                p=r+0x28+i*0x1c;dist=_f32(zone,p);cnt=_u16(zone,p+4);first=_u16(zone,p+6)
                if not math.isfinite(dist) or cnt>surfs or first>surfs or first+cnt>surfs:ok=False;break
            if not ok:continue
            name,_=_read_ascii_z(zone,r+PC_XMODEL_ROOT_SIZE,128)
            if not _looks_name(name):continue
            roots.append(r)
        except (struct.error,ValueError,IndexError):continue
    return roots


def parse_xmodel(zone:bytes,root:int)->PcXModelIntermediate:
    bones=zone[root+4];root_bones=zone[root+5];surface_count=zone[root+6];cursor=root+PC_XMODEL_ROOT_SIZE
    name,cursor=_read_ascii_z(zone,cursor)
    skeleton_pointers={};inline_skeleton={}
    for off,sec,n in ((0x08,'boneNames',bones*2),(0x0C,'parentList',bones-root_bones),(0x10,'quats',(bones-root_bones)*8),(0x14,'trans',(bones-root_bones)*16),(0x18,'partClassification',bones),(0x1C,'baseMat',bones*0x20)):
        raw=_u32(zone,root+off);skeleton_pointers[sec]=raw
        if n and _inline(raw):inline_skeleton[sec]=(cursor,n);cursor+=n
    surf_roots=cursor;cursor+=surface_count*PC_XSURFACE_ROOT_SIZE
    surfaces=[];ibs=[];ivs=[];irs=[];its=[]
    for i in range(surface_count):
        sr=surf_roots+i*PC_XSURFACE_ROOT_SIZE
        blend=[_i16(zone,sr+0x10+j*2) for j in range(4)]
        if any(x<0 for x in blend):raise ValueError(f'{name} negative blend count')
        blend_indices=blend[0]+3*blend[1]+5*blend[2]+7*blend[3]
        bptr=_u32(zone,sr+0x18);ib=None
        if _inline(bptr):ib=cursor;cursor+=blend_indices*2
        vcnt=_u16(zone,sr+2);vptr=_u32(zone,sr+0x1c);iv=None
        if _inline(vptr):iv=cursor;cursor+=vcnt*PC_PACKED_VERTEX_SIZE
        rcnt=_u32(zone,sr+0x20);rptr=_u32(zone,sr+0x24);ir=None
        if rcnt>1_000_000:raise ValueError('implausible rigid count')
        if _inline(rptr):
            ir=cursor;rr=cursor;cursor+=rcnt*RIGID_VERT_LIST_SIZE
            for ri in range(rcnt):
                tptr=_u32(zone,rr+ri*RIGID_VERT_LIST_SIZE+8)
                if not _inline(tptr):continue
                tree=cursor;nc=_u32(zone,tree+0x18);np=_u32(zone,tree+0x1c);lc=_u32(zone,tree+0x20);lp=_u32(zone,tree+0x24)
                if nc>10_000_000 or lc>10_000_000:raise ValueError('implausible collision tree')
                cursor+=COLLISION_TREE_ROOT_SIZE
                if _inline(np):cursor+=nc*COLLISION_NODE_SIZE
                if _inline(lp):cursor+=lc*COLLISION_LEAF_SIZE
        tcnt=_u16(zone,sr+4);tptr=_u32(zone,sr+0x0c);it=None
        if _inline(tptr):it=cursor;cursor+=tcnt*6
        s=XSurface(zone[sr],zone[sr+1]!=0,vcnt,tcnt,zone[sr+6],blend,blend_indices,rcnt,[_u32(zone,sr+0x28+j*4) for j in range(4)],'',iv if iv is not None else -1,it if it is not None else -1,ib,ir)
        # dynamic PC metadata used by resolvers
        s.pc_root=sr;s.triangle_pointer=tptr;s.blend_pointer=bptr;s.vertex_pointer=vptr;s.rigid_pointer=rptr
        ibs.append(ib);ivs.append(iv);irs.append(ir);its.append(it);surfaces.append(s)
    material_handles=cursor;material_ptrs=[_u32(zone,material_handles+i*4) for i in range(surface_count)];cursor+=surface_count*4
    lods=[]
    for i in range(4):
        p=root+0x28+i*0x1c;lods.append(XModelLod(_f32(zone,p),_u16(zone,p+4),_u16(zone,p+6),[_u32(zone,p+8+j*4) for j in range(4)]))
    # Use named arguments here.  A previous positional constructor accidentally placed the raw
    # PC XModel::physPreset pointer into ``resolved_phys_preset_asset_index`` after that field was
    # added to the semantic model.  Null/FOLLOWING/packed PC pointer words (0, FFFFFFFE, 4xxxxxxx)
    # are not source XAsset indices and must never reach the PS3 dependency graph.
    m=XModel(
        name=name, num_bones=bones, num_root_bones=root_bones, lod_ramp_type=zone[root+7],
        num_lods=_u16(zone,root+0xC4), collision_lod=_i16(zone,root+0xC6),
        contents=_u32(zone,root+0xA0), radius=_f32(zone,root+0xA8),
        minimum=[_f32(zone,root+0xAC+j*4) for j in range(3)],
        maximum=[_f32(zone,root+0xB8+j*4) for j in range(3)], memory_usage=_u32(zone,root+0xCC),
        flags=zone[root+0xD0], bad=zone[root+0xD1]!=0, lods=lods, surfaces=surfaces,
        skeleton_payloads={}, bone_info_source=-1, num_collision_surfaces=_i32(zone,root+0x9C),
        collision_surface_source=None, inline_phys_preset=None, inline_phys_geoms=None,
        resolved_phys_preset_asset_index=None, source_asset_index=-1)
    # stash fields that belong to PC source model but not destination semantic constructor
    m.pc_root=root;m.collision_surface_pointer=_u32(zone,root+0x98);m.bone_info_pointer=_u32(zone,root+0xA4);m.phys_preset_pointer=_u32(zone,root+0xD4);m.phys_geoms_pointer=_u32(zone,root+0xD8)
    return PcXModelIntermediate(m,root,material_handles,material_ptrs,skeleton_pointers,inline_skeleton,ibs,ivs,irs,its,cursor)


def parse_all_xmodels(zone:bytes,asset_list:XAssetList|None=None)->list[PcXModelIntermediate]:
    index=asset_list.structural_index if asset_list is not None else None
    roots=[row['root'] for row in index.rows(3)] if index is not None else scan_xmodel_roots(zone)
    xs=[parse_xmodel(zone,r) for r in roots]
    _resolve_vertex_index(zone,xs)
    if asset_list is not None:
        source=[a for a in asset_list.assets if a.type_id==3]
        if len(source)!=len(xs):raise ValueError(f'XAsset pool has {len(source)} XModels but scanner found {len(xs)}')
        for a,x in zip(source,sorted(xs,key=lambda q:q.root_offset)):
            if decode_pc_pointer(a.serialized_pointer).kind not in ('following','insert'):raise ValueError('top-level XModel not inline')
            x.model.source_asset_index=a.index
    return xs


def _resolve_vertex_index(zone:bytes,xs:Sequence[PcXModelIntermediate])->None:
    vc=ic=0;vloc={};vcount={};iloc={};icount={}
    for x in sorted(xs,key=lambda a:a.root_offset):
      for s,iv,it in zip(x.model.surfaces,x.inline_vertex_offsets,x.inline_triangle_offsets):
        if iv is not None:
            vc=(vc+15)&~15;s.vertex_source=iv;vloc[vc]=iv;vcount[vc]=s.vertex_count;vc+=s.vertex_count*PC_PACKED_VERTEX_SIZE
        elif s.vertex_pointer:
            p=decode_pc_pointer(s.vertex_pointer)
            if p.kind!='packed' or p.block!=7 or p.offset not in vloc or vcount[p.offset]!=s.vertex_count:raise ValueError(f'unresolved XSurface vertex {s.vertex_pointer:08X}')
            s.vertex_source=vloc[p.offset]
        if it is not None:
            ic=(ic+15)&~15;s.triangle_source=it;iloc[ic]=it;icount[ic]=s.triangle_count;ic+=s.triangle_count*6
        elif s.triangle_pointer:
            p=decode_pc_pointer(s.triangle_pointer)
            if p.kind!='packed' or p.block!=8 or p.offset not in iloc or icount[p.offset]!=s.triangle_count:raise ValueError(f'unresolved XSurface index {s.triangle_pointer:08X}')
            s.triangle_source=iloc[p.offset]


def resolve_skeleton_and_surface_reuse(zone:bytes,xs:Sequence[PcXModelIntermediate])->dict:
    packed_skel=packed_blend=packed_rigid=ambiguous_skel=0;prior_skel=[];prior=[]
    for x in sorted(xs,key=lambda a:a.root_offset):
        m=x.model
        for sec,raw in x.skeleton_pointers.items():
            n={'boneNames':m.num_bones*2,'parentList':m.num_bones-m.num_root_bones,'quats':(m.num_bones-m.num_root_bones)*8,'trans':(m.num_bones-m.num_root_bones)*16,'partClassification':m.num_bones,'baseMat':m.num_bones*0x20}[sec]
            if not n:continue
            if sec in x.inline_skeleton_payloads:
                v=x.inline_skeleton_payloads[sec];m.skeleton_payloads=dict(m.skeleton_payloads);m.skeleton_payloads[sec]=v;prior_skel.append((m,sec,*v));continue
            if not _packed(raw):raise ValueError(f'{m.name} skeleton {sec} unsupported pointer')
            packed_skel+=1;c=[q for q in prior_skel if q[1]==sec and q[3]==n and q[0].num_bones==m.num_bones and q[0].num_root_bones==m.num_root_bones]
            if not c:raise ValueError(f'{m.name} packed skeleton {sec} no source')
            hs={hashlib.sha256(zone[o:o+l]).digest() for _,_,o,l in c}
            if len(hs)!=1:
                # A packed skeleton pointer says 'reuse an array already written'.  Matching
                # by shape alone cannot always name that array: stock mp_crash has several
                # one-bone props whose boneNames arrays are the same size but different
                # content, so com_barrel_white_rust had two candidate sources and
                # Version 15 refused the map.  Resolving this exactly would mean
                # reimplementing the PC allocator; until then pick the closest relative by
                # name and fall back to the most recent candidate.  This never changes the
                # byte layout - the same array length is emitted either way - it only
                # decides which skeleton metadata a shared prop inherits.
                def _affinity(q):
                    other=q[0].name or ''
                    common=0
                    for a,b in zip(other,m.name or ''):
                        if a!=b:break
                        common+=1
                    return (common,q[2])
                chosen=max(c,key=_affinity);ambiguous_skel+=1
            else:
                chosen=c[0]
            m.skeleton_payloads=dict(m.skeleton_payloads);m.skeleton_payloads[sec]=(chosen[2],chosen[3])
        for s,ib,ir in zip(m.surfaces,x.inline_blend_offsets,x.inline_rigid_offsets):
            if s.blend_element_count:
                if ib is not None:s.blend_source=ib
                else:
                    packed_blend+=1;c=[p for p in prior if p.blend_source is not None and p.blend_element_count==s.blend_element_count and list(p.blend_counts)==list(s.blend_counts) and _same_geom(zone,p,s)]
                    if not c:raise ValueError('packed blend no geometry-identical source')
                    hs={hashlib.sha256(zone[p.blend_source:p.blend_source+p.blend_element_count*2]).digest() for p in c}
                    if len(hs)!=1:raise ValueError('packed blend ambiguous')
                    s.blend_source=c[0].blend_source
            if s.rigid_vert_list_count:
                if ir is not None:s.rigid_source=ir
                else:
                    packed_rigid+=1;c=[p for p in prior if p.rigid_source is not None and p.rigid_vert_list_count==s.rigid_vert_list_count and _same_geom(zone,p,s)]
                    if not c:raise ValueError('packed rigid no geometry-identical source')
                    hs={_rigid_hash(zone,p) for p in c}
                    if len(hs)!=1:raise ValueError('packed rigid ambiguous')
                    s.rigid_source=c[0].rigid_source
            prior.append(s)
    return {'packed_skeleton':packed_skel,'packed_skeleton_ambiguous':ambiguous_skel,'packed_blend':packed_blend,'packed_rigid':packed_rigid}

def _same_geom(zone,a,b):
    return a.vertex_count==b.vertex_count and a.triangle_count==b.triangle_count and zone[a.vertex_source:a.vertex_source+a.vertex_count*PC_PACKED_VERTEX_SIZE]==zone[b.vertex_source:b.vertex_source+b.vertex_count*PC_PACKED_VERTEX_SIZE] and zone[a.triangle_source:a.triangle_source+a.triangle_count*6]==zone[b.triangle_source:b.triangle_source+b.triangle_count*6]

def _rigid_hash(zone:bytes,s:XSurface)->bytes:
    root=s.rigid_source;count=s.rigid_vert_list_count;cursor=root+count*RIGID_VERT_LIST_SIZE;h=hashlib.sha256()
    for i in range(count):
        rec=root+i*RIGID_VERT_LIST_SIZE;h.update(zone[rec:rec+8]);tp=_u32(zone,rec+8)
        if not _inline(tp):continue
        tree=cursor;nc=_u32(zone,tree+0x18);np=_u32(zone,tree+0x1c);lc=_u32(zone,tree+0x20);lp=_u32(zone,tree+0x24);h.update(zone[tree:tree+0x18]);h.update(struct.pack('<II',nc,lc));cursor+=COLLISION_TREE_ROOT_SIZE
        if _inline(np):h.update(zone[cursor:cursor+nc*COLLISION_NODE_SIZE]);cursor+=nc*COLLISION_NODE_SIZE
        if _inline(lp):h.update(zone[cursor:cursor+lc*COLLISION_LEAF_SIZE]);cursor+=lc*COLLISION_LEAF_SIZE
    return h.digest()

# ---------------- Material-handle resolver ----------------
import re
_STOP_TOKENS={'mc','mtl','com','p','int','prp','prop','props','model','dyn','light','dark','white','black','all','sp','mp','dub','hill','hillside','modern','md','single','set','body','head'}

def xasset_header_alias_index(asset_list:XAssetList, packed_raw:int, expected_type:int|None=None)->int|None:
    p=decode_pc_pointer(packed_raw)
    if p.kind!='packed' or p.block!=4:return None
    # Matches audited C# PcXAssetAliasResolver / ClipMap binding: logical XAsset pool starts after
    # PC ZoneHeader(0x2C)+XAssetList root(0x10), and each record is type+header = 8 bytes.
    # Load_XAssetList aligns the logical VIRTUAL-block cursor to four bytes before
    # allocating the XAsset array.  Physical serialized bytes do not contain this
    # logical-only padding, so an unaligned script-string stream needs the same
    # alignment replay here before packed header aliases can be decoded.
    alias_base=(asset_list.asset_pool_offset-(0x2c+0x10)+3)&~3
    first_header=alias_base+4
    if p.offset<first_header:return None
    delta=p.offset-first_header
    if delta&7:return None
    idx=delta//8
    if idx<0 or idx>=len(asset_list.assets):return None
    if expected_type is not None and asset_list.assets[idx].type_id!=expected_type:return None
    return idx

def _tokens(v:str)->set[str]:
    n=v.lower().replace(',','').replace('mc/','').replace('wc/','')
    out=set()
    for t in re.findall(r'[a-z]+|\d+',n):
        if t in _STOP_TOKENS or len(t)<=1:continue
        if t.endswith('ies') and len(t)>4:t=t[:-3]+'y'
        elif t.endswith('s') and len(t)>4:t=t[:-1]
        out.add(t)
    return out

def _add_alias(aliases:dict[int,tuple[str,int,str]],ambiguous:dict[int,int],target:int,name:str,confidence:int,source:str)->bool:
    old=aliases.get(target)
    if old:
        if old[0]==name:
            if confidence>old[1]:aliases[target]=(name,confidence,source);return True
            return False
        if confidence>old[1]:aliases[target]=(name,confidence,source);ambiguous.pop(target,None);return True
        if confidence<old[1]:return False
        aliases.pop(target,None);ambiguous[target]=confidence;return False
    if target in ambiguous:
        if confidence>ambiguous[target]:aliases[target]=(name,confidence,source);ambiguous.pop(target,None);return True
        return False
    aliases[target]=(name,confidence,source);return True

def _material_target_uses(xs:Sequence[PcXModelIntermediate])->dict[int,tuple[tuple[int,int],...]]:
    uses:dict[int,list[tuple[int,int]]]=collections.defaultdict(list)
    for mi,x in enumerate(xs):
        for si,raw in enumerate(x.material_handle_pointers):
            p=decode_pc_pointer(raw)
            if p.kind=='packed' and p.block==4:
                uses[p.offset].append((mi,si))
    return {target:tuple(sorted(rows)) for target,rows in uses.items()}


def _lod_material_evidence(
    xs:Sequence[PcXModelIntermediate],inline_by_model:Mapping[int,Mapping[int,str]]
)->dict[int,tuple[str,...]]:
    '''Collect direct same-model LOD evidence without turning it into an alias.

    LOD correspondence is useful for selecting between shifted Material** base candidates, but it
    is not sufficient on its own to overwrite an already reconstructed pointer-cell owner.
    '''
    evidence:dict[int,list[str]]=collections.defaultdict(list)
    for mi,x in enumerate(xs):
        active=x.model.lods[:x.model.num_lods]
        first=next((lod for lod in active if lod.surface_count>0),None)
        if first is None:continue
        inline=inline_by_model.get(mi,{})
        for lod in (lod for lod in active[1:] if lod.surface_count>0):
            for rel in range(min(first.surface_count,lod.surface_count)):
                src=first.first_surface+rel;dst=lod.first_surface+rel
                if src not in inline or dst>=len(x.material_handle_pointers):continue
                p=decode_pc_pointer(x.material_handle_pointers[dst])
                if p.kind=='packed' and p.block==4:
                    evidence[p.offset].append(inline[src])
    return {target:tuple(names) for target,names in evidence.items()}


def _maximum_nonoverlap_bases(
    rows:Mapping[int,dict],xs:Sequence[PcXModelIntermediate]
)->tuple[dict[int,int],dict]:
    '''Select a monotonic, non-overlapping chain of reconstructed Material** arrays.'''
    ordered=[rows[mi] for mi in sorted(rows)]
    if not ordered:return {},{'input':0,'selected':0,'dropped':0}
    # State order: number of owner arrays, then aggregate causal evidence tuple.
    states:list[tuple[int,...]]=[];previous=[-1]*len(ordered)
    for i,row in enumerate(ordered):
        score=tuple(int(v) for v in row['score'])
        best=(1,)+score;best_prev=-1
        for j in range(i):
            if ordered[j]['end']>row['base']:continue
            candidate=(states[j][0]+1,)+tuple(states[j][k+1]+score[k] for k in range(len(score)))
            if candidate>best:
                best=candidate;best_prev=j
        states.append(best);previous[i]=best_prev
    cur=max(range(len(ordered)),key=lambda i:states[i]);selected_indices=[]
    while cur>=0:
        selected_indices.append(cur);cur=previous[cur]
    selected_indices.reverse();selected_rows=[ordered[i] for i in selected_indices]
    selected={int(row['model_index']):int(row['base']) for row in selected_rows}
    return selected,{
        'input':len(ordered),'selected':len(selected_rows),'dropped':len(ordered)-len(selected_rows),
        'aggregate_score':list(states[selected_indices[-1]]) if selected_indices else [],
        'selected_rows':[
            {'model_index':row['model_index'],'model':row['model'],'base':row['base'],
             'end':row['end'],'score':list(row['score']),'mapped_targets':list(row['mapped_targets'])}
            for row in selected_rows
        ],
    }


def _infer_handle_bases(
    xs:Sequence[PcXModelIntermediate],inline_by_model:dict[int,dict[int,str]],
    target_uses:Mapping[int,Sequence[tuple[int,int]]]|None=None,*,return_diagnostics:bool=False
):
    '''Infer Material** array bases from causal same-model pointer-cell evidence.

    A candidate owner cell must have been loaded strictly before the first packed reference to it.
    This rejects the CP18 failure mode where a later XModel was retroactively treated as owner of an
    already-used pointer cell.  LOD relations only break shifted-base ties and never create aliases.
    '''
    uses=dict(target_uses or _material_target_uses(xs))
    first={target:min(rows) for target,rows in uses.items()}
    lod_evidence=_lod_material_evidence(xs,inline_by_model)
    unique_rows:dict[int,dict]={};ties=[];candidate_count=0;causal_rejections=0
    for mi,x in enumerate(xs):
        inline=inline_by_model.get(mi,{})
        if not inline:continue
        same_uses=[]
        for use_si,raw in enumerate(x.material_handle_pointers):
            p=decode_pc_pointer(raw)
            if p.kind=='packed' and p.block==4:same_uses.append((use_si,p.offset))
        if not same_uses:continue
        bases=set()
        for _use_si,target in same_uses:
            for owner_si in inline:
                if (mi,owner_si)<first[target]:bases.add(target-owner_si*4)
                else:causal_rejections+=1
        candidates=[]
        for base in bases:
            if base<0 or base&3:continue
            mapped={}
            for owner_si,name in inline.items():
                target=base+owner_si*4
                if target in uses and (mi,owner_si)<first[target]:mapped[target]=name
            if not mapped:continue
            own_pairs=0
            for use_si,target in same_uses:
                owner_si=(target-base)//4
                if target>=base and (target-base)%4==0 and owner_si in inline and (mi,owner_si)<(mi,use_si):
                    own_pairs+=1
            exact_lod=sum(1 for target,name in mapped.items() for expected in lod_evidence.get(target,()) if expected==name)
            first_same=sum(1 for target in mapped if first[target][0]==mi)
            same_model_uses=sum(1 for target in mapped for use in uses[target] if use[0]==mi)
            score=(own_pairs,exact_lod,first_same,same_model_uses,len(mapped))
            candidates.append({
                'model_index':mi,'model':x.model.name,'base':base,
                'end':base+len(x.model.surfaces)*4,'score':score,
                'mapped_targets':tuple(sorted(mapped)),
            })
        candidate_count+=len(candidates)
        if not candidates:continue
        best_score=max(row['score'] for row in candidates)
        best=[row for row in candidates if row['score']==best_score]
        if len(best)==1:unique_rows[mi]=best[0]
        else:
            ties.append({'model_index':mi,'model':x.model.name,'score':list(best_score),
                         'bases':[row['base'] for row in sorted(best,key=lambda r:r['base'])]})
    selected,chain_diag=_maximum_nonoverlap_bases(unique_rows,xs)
    diagnostics={
        'candidate_count':candidate_count,'candidate_models':len(unique_rows)+len(ties),
        'unique_candidate_models':len(unique_rows),'tie_models':len(ties),'ties':ties,
        'causal_rejections':causal_rejections,'lod_evidence_targets':len(lod_evidence),
        'chain':chain_diag,
    }
    return (selected,diagnostics) if return_diagnostics else selected


def _family_tokens(value:str)->set[str]:
    value=re.sub(r'(_lod\d*|_\d+)$','',value.lower())
    return _tokens(value)


def _block4_pair_score(
    target:int,owner_model:int,owner_surface:int,name:str,
    xs:Sequence[PcXModelIntermediate],target_uses:Mapping[int,Sequence[tuple[int,int]]],
    lower:tuple[int,tuple[int,int,str]]|None,upper:tuple[int,tuple[int,int,str]]|None,
)->tuple[float,dict]:
    uses=target_uses[target];first_use=min(uses)
    use_tokens=set().union(*(_tokens(xs[mi].model.name) for mi,_ in uses))
    model_tokens=_tokens(xs[owner_model].model.name);material_tokens=_tokens(name)
    model_overlap=len(model_tokens&use_tokens);material_overlap=len(material_tokens&use_tokens)
    # Repeated references from two genuinely different consumer models provide stronger identity
    # evidence than the union above: a token present in every consumer name describes the shared
    # variant selected by the packed MaterialHandle cell.  This is deliberately only a tie-breaker
    # inside the already bounded/causal owner interval.  A single model (including repeated LOD
    # slots) cannot manufacture this evidence, and no material name is accepted without the
    # existing pointer-cell ordering proof.
    consumer_names=sorted({xs[mi].model.name.casefold() for mi,_ in uses})
    common_consumer_tokens=set()
    if len(consumer_names)>=2:
        token_rows=[_tokens(value) for value in consumer_names]
        common_consumer_tokens=set.intersection(*token_rows) if token_rows else set()
    cross_consumer_overlap=len(material_tokens&common_consumer_tokens)
    same_surface=sum(1 for _mi,si in uses if si==owner_surface)
    owner_family=_family_tokens(xs[owner_model].model.name);family_overlap=0.0
    for use_model,_ in uses:
        use_family=_family_tokens(xs[use_model].model.name)
        family_overlap=max(family_overlap,len(owner_family&use_family)/max(1,len(owner_family|use_family)))
    proximity=max(0.0,4.0-.025*abs(first_use[0]-owner_model))
    physical_fit=0.0
    if lower is not None and upper is not None and upper[0]>lower[0]:
        lo_target,(lo_mi,lo_si,_)=lower;hi_target,(hi_mi,hi_si,_)=upper
        lo_phys=xs[lo_mi].material_handle_physical_offset+lo_si*4
        hi_phys=xs[hi_mi].material_handle_physical_offset+hi_si*4
        owner_phys=xs[owner_model].material_handle_physical_offset+owner_surface*4
        expected=lo_phys+(hi_phys-lo_phys)*(target-lo_target)/(hi_target-lo_target)
        physical_fit=max(0.0,1.0-abs(owner_phys-expected)/max(1,abs(hi_phys-lo_phys)))
    score=(12*model_overlap+5*material_overlap+12*cross_consumer_overlap+8*same_surface+
           2*family_overlap+proximity+.5*physical_fit)
    return score,{
        'model_token_overlap':model_overlap,'material_token_overlap':material_overlap,
        'consumer_model_count':len(consumer_names),
        'cross_consumer_tokens':sorted(common_consumer_tokens),
        'cross_consumer_material_overlap':cross_consumer_overlap,
        'same_surface_uses':same_surface,'family_overlap':round(family_overlap,6),
        'first_use_proximity':round(proximity,6),'physical_fit':round(physical_fit,6),
    }


def _transition_block4_score(left:dict,right:dict,target_gap:int)->float:
    same_array=left['model_index']==right['model_index'] and left['base']==right['base']
    score=120.0 if same_array else 0.0
    if target_gap==4:
        score+=180.0 if same_array and right['surface']==left['surface']+1 else -100.0
    return score


def _best_two_monotonic_assignments(
    targets:Sequence[int],candidate_lists:Sequence[Sequence[dict]],*,count_cap:int=10**18
)->tuple[list[tuple[float,float,tuple[dict,...]]],int,bool]:
    '''Return the best two strictly monotonic assignments without a Cartesian product.

    The replay score is a first-order chain: each candidate contributes its pair score and each
    adjacent pair contributes only the same-array/contiguous-cell bonus. A layered DAG therefore
    gives exactly the same best and runner-up assignments as exhaustive ``itertools.product`` while
    reducing the real Getaway resolver from exponential work to O(targets*candidates^2).

    ``valid_count`` is saturated at ``count_cap`` because diagnostics need cardinality only; the
    boolean reports saturation so no finite count is accidentally presented as exact.
    '''
    if not candidate_lists or any(not rows for rows in candidate_lists):return [],0,False
    # Per candidate: up to two paths ending there, represented by
    # (score, minimum_pair_score, tuple[candidates], tuple[candidate_indices]).
    previous=[];counts=[];saturated=False
    for ci,candidate in enumerate(candidate_lists[0]):
        pair=float(candidate['pair_score'])
        previous.append([(pair,pair,(candidate,),(ci,))])
        counts.append(1)
    for layer in range(1,len(candidate_lists)):
        current=[];new_counts=[];gap=int(targets[layer])-int(targets[layer-1])
        for ci,candidate in enumerate(candidate_lists[layer]):
            options=[];count=0
            for pi,prev_candidate in enumerate(candidate_lists[layer-1]):
                if prev_candidate['cell']>=candidate['cell']:continue
                if (prev_candidate['model_index']==candidate['model_index'] and
                        prev_candidate['base']!=candidate['base']):continue
                count+=counts[pi]
                if count>=count_cap:
                    count=count_cap;saturated=True
                transition=_transition_block4_score(prev_candidate,candidate,gap)
                for score,minimum,path,key in previous[pi]:
                    options.append((
                        score+float(candidate['pair_score'])+transition,
                        min(minimum,float(candidate['pair_score'])),
                        path+(candidate,),key+(ci,),
                    ))
            # Stable deterministic tie order follows candidate enumeration, equivalent to the old
            # Cartesian-product traversal. Retain distinct assignments only.
            options.sort(key=lambda row:(-row[0],row[3]))
            best=[];seen=set()
            for row in options:
                if row[3] in seen:continue
                best.append(row);seen.add(row[3])
                if len(best)==2:break
            current.append(best);new_counts.append(count)
        previous=current;counts=new_counts
    finals=[]
    for rows in previous:finals.extend(rows)
    finals.sort(key=lambda row:(-row[0],row[3]))
    best=[];seen=set()
    for score,minimum,path,key in finals:
        if key in seen:continue
        best.append((score,minimum,path));seen.add(key)
        if len(best)==2:break
    total=sum(counts)
    if total>=count_cap:total=count_cap;saturated=True
    return best,total,saturated


def _replay_block4_alias_intervals(
    xs:Sequence[PcXModelIntermediate],inline_by_model:Mapping[int,Mapping[int,str]],
    target_uses:Mapping[int,Sequence[tuple[int,int]]],aliases:dict[int,tuple[str,int,str]],
    ambiguous:dict[int,int],target_cells:dict[int,tuple[int,int]],*,margin_threshold:float=12.0,
    minimum_pair_score:float=8.0,
)->tuple[int,dict]:
    '''Replay unresolved block-4 MaterialHandle cells between causal owner anchors.

    Packed pointer values are block-4 addresses of previously loaded MaterialHandle cells. Causal
    base reconstruction provides a monotonic set of anchor addresses. Remaining cells are matched
    only within the adjacent anchor interval, in owner-load order, and accepted only when the
    structural/lexical score is unique or has a sufficient runner-up margin.
    '''
    anchor_targets=sorted(target_cells)
    unresolved=sorted(set(target_uses)-set(aliases))
    groups:dict[tuple[int|None,int|None],list[int]]=collections.defaultdict(list)
    for target in unresolved:
        index=bisect.bisect_left(anchor_targets,target)
        lower=anchor_targets[index-1] if index else None
        upper=anchor_targets[index] if index<len(anchor_targets) else None
        groups[(lower,upper)].append(target)
    used_cells=set(target_cells.values());accepted_aliases=0;rows=[]
    known_bases={}
    for target,(mi,si) in target_cells.items():
        base=target-si*4
        if mi in known_bases and known_bases[mi]!=base:
            raise ValueError(f'XModel {mi} has inconsistent material-cell anchors')
        known_bases[mi]=base
    for (lower_target,upper_target),targets in sorted(groups.items(),key=lambda row:row[1][0]):
        lower=(lower_target,(target_cells[lower_target][0],target_cells[lower_target][1],aliases[lower_target][0])) if lower_target is not None else None
        upper=(upper_target,(target_cells[upper_target][0],target_cells[upper_target][1],aliases[upper_target][0])) if upper_target is not None else None
        lower_cell=lower[1][:2] if lower is not None else (-1,-1)
        upper_cell=upper[1][:2] if upper is not None else (len(xs),0)
        candidate_lists=[]
        for target in targets:
            candidates=[]
            first_use=min(target_uses[target])
            for mi in range(len(xs)):
                for si,name in inline_by_model.get(mi,{}).items():
                    cell=(mi,si)
                    if cell in used_cells or not(lower_cell<cell<upper_cell) or not(cell<first_use):continue
                    # One Material** array has exactly one base. A unique lexical
                    # candidate cannot override an earlier pointer-cell anchor.
                    if mi in known_bases and known_bases[mi]!=target-si*4:continue
                    pair_score,parts=_block4_pair_score(target,mi,si,name,xs,target_uses,lower,upper)
                    candidates.append({
                        'model_index':mi,'model':xs[mi].model.name,'surface':si,'name':name,
                        'cell':cell,'base':target-si*4,'pair_score':pair_score,'score_parts':parts,
                    })
            candidate_lists.append(candidates)
        combinations,valid_count,count_saturated=_best_two_monotonic_assignments(targets,candidate_lists)
        margin=None if len(combinations)<2 else combinations[0][0]-combinations[1][0]
        accepted=bool(combinations) and (valid_count==1 or (margin is not None and margin>=margin_threshold and combinations[0][1]>=minimum_pair_score))
        assignment=[]
        if accepted:
            confidence=135 if valid_count==1 else 125
            for target,candidate in zip(targets,combinations[0][2]):
                if _add_alias(aliases,ambiguous,target,candidate['name'],confidence,'block4 interval replay'):
                    accepted_aliases+=1
                target_cells[target]=candidate['cell'];used_cells.add(candidate['cell'])
                known_bases[candidate['model_index']]=candidate['base']
                assignment.append({
                    'target':target,'model_index':candidate['model_index'],'model':candidate['model'],
                    'surface':candidate['surface'],'name':candidate['name'],'base':candidate['base'],
                    'pair_score':round(candidate['pair_score'],6),'score_parts':candidate['score_parts'],
                })
        rows.append({
            'lower_anchor':lower_target,'upper_anchor':upper_target,'targets':targets,
            'candidate_counts':[len(candidates) for candidates in candidate_lists],
            'valid_combinations':valid_count,'valid_combinations_saturated':count_saturated,'accepted':accepted,
            'best_score':round(combinations[0][0],6) if combinations else None,
            'minimum_pair_score':round(combinations[0][1],6) if combinations else None,
            'margin':round(margin,6) if margin is not None else None,'assignment':assignment,
        })
    return accepted_aliases,{
        'initial_unresolved_targets':len(unresolved),'groups':len(groups),'accepted_aliases':accepted_aliases,
        'remaining_targets':len(set(target_uses)-set(aliases)),'margin_threshold':margin_threshold,
        'minimum_pair_score':minimum_pair_score,'algorithm':'layered-dag-top2','rows':rows,
    }

def resolve_xmodel_materials(
    xs:Sequence[PcXModelIntermediate],asset_list:XAssetList,
    material_name_by_asset_index:Mapping[int,str]|None=None,
    *,allow_semantic_sibling:bool=False,runtime_fallback_material:str|None=None
)->dict:
    '''Resolve XSurface Material handles without copying destination-unsafe PC pointers.

    Evidence precedence is direct XAssetHeader alias, causal Material** owner reconstruction, then
    bounded block-4 pointer-cell replay.  LOD relations are tie-break evidence only; they can no
    longer overwrite an owner cell that was already used before the proposed owner existed.
    '''
    aliases={};amb={};inline_by={};inline_count=packed_count=direct=0
    for mi,x in enumerate(xs):
        decoded={}
        for si,(surface,raw) in enumerate(zip(x.model.surfaces,x.material_handle_pointers)):
            kind=decode_pc_pointer(raw).kind
            if kind in ('following','insert'):
                if not surface.material_name or surface.material_name.startswith('packed:'):
                    raise ValueError(f'{x.model.name} inline material has no decoded name')
                decoded[si]=surface.material_name;inline_count+=1
            elif kind=='packed':packed_count+=1
            else:raise ValueError(f'{x.model.name} surface {si} null/unsupported material pointer')
        inline_by[mi]=decoded
    target_uses=_material_target_uses(xs);targets=set(target_uses)
    if material_name_by_asset_index:
        for x in xs:
            for raw in x.material_handle_pointers:
                p=decode_pc_pointer(raw)
                if p.kind!='packed' or p.block!=4:continue
                asset_index=xasset_header_alias_index(asset_list,raw,4)
                if asset_index is not None and asset_index in material_name_by_asset_index:
                    if _add_alias(aliases,amb,p.offset,material_name_by_asset_index[asset_index],200,'direct XAssetHeader alias'):
                        direct+=1
    bases,base_diagnostics=_infer_handle_bases(xs,inline_by,target_uses,return_diagnostics=True)
    target_cells={}
    for mi,base in bases.items():
        for si,name in inline_by[mi].items():
            target=base+si*4
            if target not in target_uses or not((mi,si)<min(target_uses[target])):continue
            _add_alias(aliases,amb,target,name,140,'causal handle base')
            target_cells[target]=(mi,si)
    replay_aliases,replay_diagnostics=_replay_block4_alias_intervals(
        xs,inline_by,target_uses,aliases,amb,target_cells
    )
    semantic_aliases=0
    if allow_semantic_sibling:
        # Optional diagnostic fallback retained for noncanonical investigations.  Production builds
        # do not enable it and therefore never need a free-form material-name guess.
        for target in sorted(targets-set(aliases)):
            uses=[(mi,si,xs[mi].model.name) for mi,si in target_uses[target]]
            first_use=min(row[0] for row in uses);use_tokens=set().union(*(_tokens(row[2]) for row in uses))
            same_surface=collections.Counter(si for _mi,si,_name in uses);candidates=[]
            for mi,x in enumerate(xs):
                model_overlap=len(_tokens(x.model.name)&use_tokens)
                for si,name in inline_by[mi].items():
                    material_overlap=len(_tokens(name)&use_tokens);same=same_surface.get(si,0)
                    if model_overlap==0 and material_overlap==0 and not same:continue
                    distance=abs(mi-first_use)
                    score=10*model_overlap+5*material_overlap+(8+min(same,5) if same else 0)+(8 if mi==first_use-1 else 6 if mi==first_use+1 else 0)+(1 if mi<first_use else 0)-.015*distance
                    candidates.append((score,mi,si,name))
            candidates.sort(key=lambda row:(-row[0],abs(row[1]-first_use),row[1],row[2],row[3]))
            if candidates:
                best=candidates[0];near=[row for row in candidates[1:] if best[0]-row[0]<=2]
                if all(row[3]==best[3] for row in near):
                    if _add_alias(aliases,amb,target,best[3],60,'semantic sibling diagnostic'):
                        semantic_aliases+=1
    unresolved=[];unresolved_slots=[]
    for x in xs:
        for si,(surface,raw) in enumerate(zip(x.model.surfaces,x.material_handle_pointers)):
            p=decode_pc_pointer(raw)
            if p.kind in ('following','insert') and surface.material_name and not surface.material_name.startswith('packed:'):
                continue
            if p.kind!='packed' or p.block!=4 or p.offset not in aliases:
                unresolved.append({'model':x.model.name,'surface':si,'raw':raw,'block':p.block,'offset':p.offset,
                                   'direct_asset_index':xasset_header_alias_index(asset_list,raw,4)})
                unresolved_slots.append((x,si));continue
            surface.material_name=aliases[p.offset][0]
    exact_unresolved=tuple(unresolved);runtime_fallbacks=0
    if runtime_fallback_material is not None and unresolved_slots:
        for x,si in unresolved_slots:
            x.model.surfaces[si].material_name=runtime_fallback_material;runtime_fallbacks+=1
        unresolved=[]
    lod_evidence=_lod_material_evidence(xs,inline_by)
    lod_conflicts=sum(1 for names in lod_evidence.values() if len(set(names))>1)
    return {
        'inline':inline_count,'packed':packed_count,'targets':len(targets),
        'direct_xasset_aliases':direct,'reliable_bases':len(bases),
        'causal_base_diagnostics':base_diagnostics,'block4_replay_aliases':replay_aliases,
        'block4_replay':replay_diagnostics,'lod_aliases':0,'lod_conflicts':lod_conflicts,
        'semantic_aliases':semantic_aliases,'resolved_aliases':len(aliases),
        'unresolved':unresolved,'exact_unresolved':exact_unresolved,
        'runtime_fallbacks':runtime_fallbacks,
        'runtime_fallback_material':runtime_fallback_material if runtime_fallbacks else None,
        'aliases':aliases,'ambiguous':amb,
    }


def apply_replayed_material_handles(xs, resolution, handle_bases):
    """Reuse proven image-stream owner bases for Material* aliases, including LODs.

    Only backward references to typed handle cells are followed. Neither model names
    nor the runtime fallback assigned by the earlier resolver establish identity.
    """
    cells = {}
    for mi, base in handle_bases.items():
        x = xs[int(mi)]
        for si, (surface, raw) in enumerate(zip(x.model.surfaces, x.material_handle_pointers)):
            address = int(base) + si * 4
            value = (int(mi), si, raw, surface.material_name)
            if address in cells and cells[address] != value:
                raise ValueError(f'Conflicting recovered Material handle cell 0x{address:X}')
            cells[address] = value

    def identity(address, before, trail=()):
        if address in trail or address not in cells:
            return None
        mi, si, raw, name = cells[address]
        if (mi, si) >= before:
            return None
        pointer = decode_pc_pointer(raw)
        if pointer.kind in ('following', 'insert'):
            return name
        if pointer.kind == 'packed' and pointer.block == 4:
            return identity(pointer.offset, (mi, si), trail + (address,))
        return None

    recovered = []
    for mi, x in enumerate(xs):
        for si, (surface, raw) in enumerate(zip(x.model.surfaces, x.material_handle_pointers)):
            pointer = decode_pc_pointer(raw)
            if pointer.kind != 'packed' or pointer.block != 4:
                continue
            name = identity(pointer.offset, (mi, si))
            if name is None:
                continue
            old = resolution['aliases'].get(pointer.offset)
            if old and old[0] != name:
                raise ValueError(f'Material handle 0x{pointer.offset:X} conflicts: {old[0]!r}/{name!r}')
            resolution['aliases'][pointer.offset] = (name, 200, 'typed image-stream handle replay')
            if surface.material_name != name:
                recovered.append({'model': x.model.name, 'surface': si,
                                  'target': pointer.offset, 'material': name})
            surface.material_name = name
    keys = {(r['model'], r['surface']) for r in recovered}
    old_unresolved = resolution.get('exact_unresolved', ())
    remaining = tuple(r for r in old_unresolved if (r['model'], r['surface']) not in keys)
    resolution['exact_unresolved'] = remaining
    resolution['unresolved'] = [r for r in resolution['unresolved'] if (r['model'], r['surface']) not in keys]
    resolution['runtime_fallbacks'] = max(0, resolution.get('runtime_fallbacks', 0) - len(old_unresolved) + len(remaining))
    resolution['resolved_aliases'] = len(resolution['aliases'])
    resolution['image_stream_recovered_handles'] = recovered
    return resolution
