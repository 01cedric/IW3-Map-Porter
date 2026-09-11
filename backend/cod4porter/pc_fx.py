from __future__ import annotations
from dataclasses import dataclass
from enum import Enum
import struct,re
from typing import Sequence

from .backend.v4_ffio import decode_pc_pointer, XAssetList
from .pc_material import parse_material_at

FX_ROOT_SIZE=0x20
FX_ELEM_SIZE=0xFC
VELOCITY_SAMPLE_SIZE=0x60
VISUAL_STATE_SAMPLE_SIZE=0x30
TRAIL_ROOT_SIZE=0x1C
TRAIL_VERTEX_SIZE=0x14
# The PC stream parser consumes the PC XModel ABI. Destination nested visuals
# use the shorter PS3 XModel root and must never reuse this source stride.
EXTERNAL_XMODEL_ALIAS_ROOT_SIZE=0xDC
PS3_XMODEL_ALIAS_ROOT_SIZE=0xCC
IMPACT_ROOT_SIZE=0x08
IMPACT_ENTRY_COUNT=12
IMPACT_NONFLESH=29
IMPACT_FLESH=4
IMPACT_REFS_PER_ENTRY=IMPACT_NONFLESH+IMPACT_FLESH
IMPACT_REFERENCE_COUNT=IMPACT_ENTRY_COUNT*IMPACT_REFS_PER_ENTRY


def u16(z,o):return struct.unpack_from('<H',z,o)[0]
def u32(z,o):return struct.unpack_from('<I',z,o)[0]
def _inline(raw):return decode_pc_pointer(raw).kind in ('following','insert')
def _packed(raw):return decode_pc_pointer(raw).kind=='packed'

def read_z(z:bytes,c:int,max_bytes=4096)->tuple[str,int]:
    if c<0 or c>=len(z):raise ValueError('FX string outside zone')
    e=z.find(b'\0',c,min(len(z),c+max_bytes))
    if e<0 or e==c:raise ValueError('FX string empty/unterminated')
    raw=z[c:e]
    if any(b<0x20 or b>0x7e for b in raw):raise ValueError('FX string non-printable')
    return raw.decode('latin-1'),e+1

class VisualKind(str,Enum):
    NULL='null';MATERIAL='material';XMODEL_ALIAS='xmodel_alias';STRING='string';PACKED_UNRESOLVED='packed_unresolved'

@dataclass(frozen=True)
class FxVisual:
    serialized_pointer:int
    kind:VisualKind
    material_root:int|None=None
    name:str|None=None

@dataclass(frozen=True)
class FxEffectReference:
    serialized_pointer:int
    inline_name:str|None
    @property
    def pointer_kind(self):return decode_pc_pointer(self.serialized_pointer).kind

@dataclass(frozen=True)
class FxTrail:
    root_bytes:bytes
    vertex_count:int
    vertex_pointer_raw:int
    vertex_bytes:bytes
    index_count:int
    index_pointer_raw:int
    index_bytes:bytes

@dataclass(frozen=True)
class FxElement:
    index:int
    root_bytes:bytes
    element_type:int
    visual_count:int
    velocity_interval_count:int
    visual_state_interval_count:int
    velocity_pointer_raw:int
    velocity_bytes:bytes
    visual_state_pointer_raw:int
    visual_state_bytes:bytes
    visual_pointer_raw:int
    visuals:tuple[FxVisual,...]
    effect_on_impact:FxEffectReference
    effect_on_death:FxEffectReference
    effect_emitted:FxEffectReference
    trail_pointer_raw:int
    trail:FxTrail|None

@dataclass(frozen=True)
class OwnedFxGraph:
    root_offset:int
    physical_end_offset:int
    name:str
    root_bytes:bytes
    looping_element_count:int
    one_shot_element_count:int
    emission_element_count:int
    elements:tuple[FxElement,...]
    inline_material_roots:tuple[int,...]

@dataclass(frozen=True)
class ImpactFx:
    source_asset_index:int
    root_offset:int
    physical_end_offset:int
    name:str
    fx_source_asset_indices:tuple[int|None,...]


def _read_inline_bytes(z:bytes,c:int,raw:int,n:int,label:str)->tuple[bytes,int]:
    kind=decode_pc_pointer(raw).kind
    if kind=='null':return b'',c
    if kind=='packed':
        if n==0:return b'',c
        raise ValueError(f'{label} uses unresolved packed/reused payload')
    if kind not in ('following','insert'):raise ValueError(f'{label} unsupported pointer {kind}')
    if c<0 or n<0 or c+n>len(z):raise ValueError(f'{label} outside zone')
    return z[c:c+n],c+n


def _read_visual(z:bytes,c:int,raw:int,etype:int,material_roots:list[int])->tuple[FxVisual,int]:
    kind=decode_pc_pointer(raw).kind
    if kind=='null':return FxVisual(raw,VisualKind.NULL),c
    if kind=='packed':return FxVisual(raw,VisualKind.PACKED_UNRESOLVED),c
    if kind not in ('following','insert'):raise ValueError(f'FX visual pointer {kind}')
    if etype<=4:
        mat,c2=parse_material_at(z,c);material_roots.append(c)
        return FxVisual(raw,VisualKind.MATERIAL,c,mat.name),c2
    if etype==5:
        if c+EXTERNAL_XMODEL_ALIAS_ROOT_SIZE>len(z):raise ValueError('FX XModel alias outside zone')
        if not _inline(u32(z,c)):raise ValueError('FX XModel alias name not inline')
        if any(z[c+i] for i in range(4,EXTERNAL_XMODEL_ALIAS_ROOT_SIZE)):raise ValueError('FX XModel alias body non-zero')
        name,c2=read_z(z,c+EXTERNAL_XMODEL_ALIAS_ROOT_SIZE,512)
        return FxVisual(raw,VisualKind.XMODEL_ALIAS,None,name.lstrip(',')),c2
    if etype in (8,10):
        s,c2=read_z(z,c,4096);return FxVisual(raw,VisualKind.STRING,None,s),c2
    raise ValueError(f'FX inline visual type {etype} unsupported')


def _read_effect(z:bytes,c:int,raw:int)->tuple[FxEffectReference,int]:
    k=decode_pc_pointer(raw).kind
    if k in ('following','insert'):
        s,c2=read_z(z,c,4096);return FxEffectReference(raw,s.lstrip(',')),c2
    if k in ('null','packed'):return FxEffectReference(raw,None),c
    raise ValueError(f'FX effect pointer {k}')


def _read_trail(z:bytes,c:int,raw:int,etype:int,fx_name:str,ei:int)->tuple[FxTrail|None,int]:
    k=decode_pc_pointer(raw).kind
    if k=='null':return None,c
    if k=='packed':raise ValueError(f"FX '{fx_name}' element {ei} packed trail unresolved")
    if k not in ('following','insert') or etype!=3:raise ValueError('invalid FX trail pointer/type')
    if c+TRAIL_ROOT_SIZE>len(z):raise ValueError('trail root outside zone')
    root=z[c:c+TRAIL_ROOT_SIZE];vc=u32(z,c+0x0c);vp=u32(z,c+0x10);ic=u32(z,c+0x14);ip=u32(z,c+0x18);c+=TRAIL_ROOT_SIZE
    vb,c=_read_inline_bytes(z,c,vp,vc*TRAIL_VERTEX_SIZE,'FX trail vertices')
    ib,c=_read_inline_bytes(z,c,ip,ic*2,'FX trail indices')
    return FxTrail(root,vc,vp,vb,ic,ip,ib),c


def parse_owned_fx_at(z:bytes,root:int)->OwnedFxGraph:
    if root<0 or root+FX_ROOT_SIZE>len(z):raise ValueError('FX root outside zone')
    if not _inline(u32(z,root)) or not _inline(u32(z,root+0x1c)):raise ValueError('owned FX must own name+elem array')
    looping=u32(z,root+0x10);oneshot=u32(z,root+0x14);emission=u32(z,root+0x18);total=looping+oneshot+emission
    if not 1<=total<=4096:raise ValueError('invalid FX element count')
    root_bytes=z[root:root+FX_ROOT_SIZE];c=root+FX_ROOT_SIZE;name,c=read_z(z,c,512)
    if c+total*FX_ELEM_SIZE>len(z):raise ValueError('FX elem array outside zone')
    drafts=[]
    for i in range(total):
        e=c+i*FX_ELEM_SIZE;etype=z[e+0xb0]
        if etype>10:raise ValueError(f'FX element type {etype}')
        drafts.append((i,z[e:e+FX_ELEM_SIZE],etype,z[e+0xb1],z[e+0xb2],z[e+0xb3],u32(z,e+0xb4),u32(z,e+0xb8),u32(z,e+0xbc),u32(z,e+0xd8),u32(z,e+0xdc),u32(z,e+0xe0),u32(z,e+0xf4)))
    c+=total*FX_ELEM_SIZE
    mats=[];elements=[]
    for d in drafts:
        i,rb,etype,vcount,velcount,viscount,velptr,visptr,vptr,impact,death,emitted,trailptr=d
        velocity,c=_read_inline_bytes(z,c,velptr,(velcount+1)*VELOCITY_SAMPLE_SIZE,f'FX {name} velocity')
        visualstate,c=_read_inline_bytes(z,c,visptr,(viscount+1)*VISUAL_STATE_SAMPLE_SIZE,f'FX {name} visualstate')
        visuals=[];vk=decode_pc_pointer(vptr).kind
        if vcount==0:
            if vk!='null':raise ValueError('zero visuals with non-null pointer')
        elif etype==9:
            if vk in ('following','insert'):
                n=vcount*8
                if c+n>len(z):raise ValueError('decal visual table outside zone')
                pairs=[(u32(z,c+j*8),u32(z,c+j*8+4)) for j in range(vcount)];c+=n
                for left,right in pairs:
                    v,c=_read_visual(z,c,left,0,mats);visuals.append(v)
                    v,c=_read_visual(z,c,right,0,mats);visuals.append(v)
            elif vk=='packed':visuals.extend(FxVisual(vptr,VisualKind.PACKED_UNRESOLVED) for _ in range(vcount*2))
            elif vk!='null':raise ValueError('invalid decal pointer')
        elif vcount>1:
            if vk in ('following','insert'):
                n=vcount*4
                if c+n>len(z):raise ValueError('visual table outside zone')
                ptrs=[u32(z,c+j*4) for j in range(vcount)];c+=n
                for p in ptrs:
                    v,c=_read_visual(z,c,p,etype,mats);visuals.append(v)
            elif vk=='packed':visuals.extend(FxVisual(vptr,VisualKind.PACKED_UNRESOLVED) for _ in range(vcount))
            elif vk!='null':raise ValueError('invalid visual table pointer')
        else:
            v,c=_read_visual(z,c,vptr,etype,mats);visuals.append(v)
        er1,c=_read_effect(z,c,impact);er2,c=_read_effect(z,c,death);er3,c=_read_effect(z,c,emitted);trail,c=_read_trail(z,c,trailptr,etype,name,i)
        elements.append(FxElement(i,rb,etype,vcount,velcount,viscount,velptr,velocity,visptr,visualstate,vptr,tuple(visuals),er1,er2,er3,trailptr,trail))
    return OwnedFxGraph(root,c,name,root_bytes,looping,oneshot,emission,tuple(elements),tuple(sorted(set(mats))))


def scan_owned_fx(z:bytes,start_offset:int=0)->list[OwnedFxGraph]:
    # Owned FX roots must own both name (+0x00) and element array (+0x1C).
    # Search the FOLLOWING marker in C-speed and semantically replay only roots with
    # a plausible element count.  The old backend scanner iterated every byte in
    # Python and made a real 116 MiB Getaway zone take minutes.
    marker=b'\xff\xff\xff\xff';pos=max(0,start_offset);out={}
    while True:
        root=z.find(marker,pos)
        if root<0 or root+FX_ROOT_SIZE>len(z):break
        pos=root+1
        if u32(z,root+0x1c) not in (0xffffffff,0xfffffffe):continue
        total=u32(z,root+0x10)+u32(z,root+0x14)+u32(z,root+0x18)
        if not 1<=total<=4096:continue
        try:g=parse_owned_fx_at(z,root)
        except (ValueError,IndexError,struct.error):continue
        out[root]=g
    return [out[k] for k in sorted(out)]


def xasset_header_alias_index(asset_list:XAssetList,raw:int,expected_type:int)->int:
    p=decode_pc_pointer(raw)
    if p.kind!='packed' or p.block!=4:raise ValueError('expected packed block4 XAssetHeader alias')
    # Same audited geometry used by pc_xmodel.py; XAssetHeader field is second u32 of each 8-byte record.
    alias_base=(asset_list.asset_pool_offset-(0x2c+0x10)+3)&~3
    delta=p.offset-(alias_base+4)
    if delta<0 or delta%8:raise ValueError('pointer does not address an XAssetHeader slot')
    idx=delta//8
    if idx<0 or idx>=len(asset_list.assets):raise ValueError('XAssetHeader alias outside asset list')
    if int(asset_list.assets[idx].type_id)!=int(expected_type):raise ValueError('XAssetHeader alias type mismatch')
    return idx


def parse_impact_fx(z:bytes,asset_list:XAssetList,impact_type:int=0x1A,fx_type:int=0x19)->list[ImpactFx]:
    if asset_list.structural_index is not None:
        index=asset_list.structural_index;targets={row['root']:row['index'] for row in index.rows(fx_type)};out=[]
        for row in index.rows(impact_type):
            p=index.pointer(row['root']+4);refs=[]
            for i in range(IMPACT_REFERENCE_COUNT):
                target=index.pointer(p+i*4)
                if target is not None and target not in targets:raise ValueError('ImpactFx target is not a top-level FX asset')
                refs.append(targets.get(target))
            out.append(ImpactFx(row['index'],row['root'],row['end'],row['name'].lstrip(','),tuple(refs)))
        return out
    assets=[a for a in asset_list.assets if int(a.type_id)==impact_type]
    if not assets:return []
    if any(decode_pc_pointer(a.serialized_pointer).kind not in ('following','insert') for a in assets):raise ValueError('ImpactFx top-level roots must be inline')
    table_bytes=IMPACT_REFERENCE_COUNT*4;candidates=[]
    start=max(0,asset_list.asset_data_offset)
    # The table pointer at root+4 must be FOLLOWING or INSERT. Find those
    # exact bytes first, including overlapping markers; do not decode two
    # pointers at every byte of a multi-megabyte geometry stream.
    last=len(z)-IMPACT_ROOT_SIZE-table_bytes
    for marker in re.finditer(rb'(?=[\xfe\xff]\xff\xff\xff)',z[start+4:last+8]):
        root=start+marker.start()
        if root>last:break
        nr=u32(z,root);tr=u32(z,root+4);nk=decode_pc_pointer(nr).kind
        if decode_pc_pointer(tr).kind not in ('following','insert') or nk not in ('following','insert','packed'):continue
        c=root+IMPACT_ROOT_SIZE
        try:
            if nk in ('following','insert'):name,c=read_z(z,c,512)
            else:name='impactfx' if len(assets)==1 else f'impactfx_{len(candidates):02d}'
            if c+table_bytes>len(z):continue
            targets=[];nonnull=0
            for i in range(IMPACT_REFERENCE_COUNT):
                raw=u32(z,c+i*4);k=decode_pc_pointer(raw).kind
                if k=='null':targets.append(None);continue
                idx=xasset_header_alias_index(asset_list,raw,fx_type);targets.append(idx);nonnull+=1
            if nonnull:candidates.append((root,c+table_bytes,name.lstrip(','),tuple(targets)))
        except Exception:continue
    if len(candidates)!=len(assets):raise ValueError(f'ImpactFx scan found {len(candidates)} complete tables for {len(assets)} assets')
    return [ImpactFx(a.index,*cand) for a,cand in zip(assets,sorted(candidates))]

# PC IW3 XAssetType values, distinct from the PS3 graph enum.
PC_FX_TYPE=0x19
PC_IMPACTFX_TYPE=0x1A

@dataclass(frozen=True)
class FxReference:
    source_asset_index:int
    root_offset:int
    physical_end_offset:int
    name:str
    source_owned:bool
    graph:OwnedFxGraph|None
    inline_material_roots:tuple[int,...]


@dataclass(frozen=True)
class FxRunnerBinding:
    """One source-proven FxElemDef type-10 runner reference.

    Type-10 visuals are pointers to ``FxEffectDef`` objects, not nullable
    strings.  The PC packed word addresses a block-4 alias cell created by an
    earlier FX owner, so the binding must preserve both the exact occurrence
    and its typed top-level target.
    """

    owner_source_asset_index:int
    owner_name:str
    element_index:int
    visual_index:int
    serialized_pointer:int
    source_block4_alias_offset:int
    target_source_asset_index:int
    target_name:str


@dataclass(frozen=True)
class FxRunnerResolution:
    bindings:tuple[FxRunnerBinding,...]
    source_block4_end_anchor:int|None
    # Owners whose packed references could not be proven, present only when
    # the resolver ran with on_unprovable='collect':
    # ({'owner_source_asset_index','owner_name','root_offset','reason'}, ...)
    failed_owners:tuple=()

    @property
    def target_asset_indices_by_raw(self)->dict[int,int]:
        out={}
        for row in self.bindings:
            old=out.get(row.serialized_pointer)
            if old is not None and old!=row.target_source_asset_index:
                raise ValueError(f'FX runner pointer 0x{row.serialized_pointer:08X} has conflicting targets')
            out[row.serialized_pointer]=row.target_source_asset_index
        return out

    @property
    def reference_count(self)->int:return len(self.bindings)

    @property
    def target_count(self)->int:return len(set(x.target_source_asset_index for x in self.bindings))


@dataclass(frozen=True)
class FxMaterialBinding:
    owner_source_asset_index:int
    owner_name:str
    element_index:int
    visual_index:int
    serialized_pointer:int
    source_block4_pointer_cell_offset:int
    target_owner_source_asset_index:int
    target_owner_name:str
    target_element_index:int
    target_visual_index:int
    target_material_root:int
    target_material_name:str


@dataclass(frozen=True)
class FxMaterialResolution:
    bindings:tuple[FxMaterialBinding,...]
    unresolved_raws:tuple[int,...]
    source_member_starts_by_asset:dict[int,int]
    replay_unavailable_by_asset:dict[int,str]

    @property
    def target_material_roots_by_raw(self)->dict[int,int]:
        out={}
        for row in self.bindings:
            old=out.get(row.serialized_pointer)
            if old is not None and old!=row.target_material_root:
                raise ValueError(f'FX Material pointer 0x{row.serialized_pointer:08X} has conflicting targets')
            out[row.serialized_pointer]=row.target_material_root
        return out

    @property
    def reference_count(self)->int:return len(self.bindings)

    @property
    def target_count(self)->int:return len(set(x.target_material_root for x in self.bindings))


def resolve_fx_runner_bindings(map_name:str,fx_references:Sequence[FxReference],*,zone:bytes|None=None,technique_sets:Sequence[object]=(),on_unprovable:str='raise')->FxRunnerResolution:
    """Resolve packed type-10 visuals from typed LoadStream ordering.

    A distinct packed block-4 word denotes one persistent ``FxEffectDef``
    alias; repeated words denote repeated references to that same target.  PC
    zones serialize the targets of an owner's NEW aliases as the immediately
    preceding, consecutive top-level FX owners; aliases an earlier owner
    already proved are reuses of existing cells (the linker deduplicates
    assets by name) and do not re-serialize their target.  An owner may mix
    both freely.  Physical contiguity of the new-target run and the monotonic
    block-4 cell allocation make the replay unique without consulting a map
    name or an asset-name table.

    The resolver deliberately rejects incomplete, non-monotonic, or
    non-contiguous evidence.  In particular it never guesses a target from a
    nearby address or from a familiar FX name.  ``on_unprovable`` selects what
    an unprovable OWNER does: ``'raise'`` (default) stops exactly as before;
    ``'collect'`` records the owner in ``FxRunnerResolution.failed_owners``
    (no bindings, no proof-state pollution) so the caller can route it into
    the unsupported-FX omission policy instead of aborting the whole port.
    """
    if on_unprovable not in ('raise','collect'):
        raise ValueError("on_unprovable must be 'raise' or 'collect'")
    # Kept in the public signature because callers already pass the map name;
    # source topology, not that label, is the binding evidence.
    _=map_name
    references=tuple(fx_references)
    by_index={x.source_asset_index:x for x in references}
    if len(by_index)!=len(references):
        raise ValueError('FX runner replay has duplicate source asset indices')
    roots=[x.root_offset for x in references]
    if len(set(roots))!=len(roots):
        raise ValueError('FX runner replay has duplicate physical roots')
    for reference in references:
        if reference.physical_end_offset<=reference.root_offset:
            raise ValueError(f"FX runner replay has invalid physical boundary for '{reference.name}'")
        if reference.source_owned:
            graph=reference.graph
            if graph is None:
                raise ValueError(f"FX runner replay lacks owned graph for '{reference.name}'")
            if (graph.root_offset,graph.physical_end_offset,graph.name.lstrip(','))!=(
                reference.root_offset,reference.physical_end_offset,reference.name.lstrip(','),
            ):
                raise ValueError(f"FX runner replay graph/reference boundary differs for '{reference.name}'")

    failed=[]
    def _fail(owner,error):
        if on_unprovable=='raise':raise error
        failed.append({'owner_source_asset_index':int(owner.source_asset_index),
                       'owner_name':owner.name,'root_offset':int(owner.root_offset),
                       'reason':str(error)})

    requests_by_owner={}
    for owner in sorted(references,key=lambda x:x.source_asset_index):
        if not owner.source_owned or owner.graph is None:continue
        rows=[]
        try:
            for elem in owner.graph.elements:
                # Impact/death/emitted references use the same PC FxEffectDef pointer
                # representation as runners. Negative indices identify these three
                # fields without pretending they are visual-array entries.
                for field_index,effect in enumerate((elem.effect_on_impact,elem.effect_on_death,elem.effect_emitted)):
                    if effect.pointer_kind=='packed' and effect.inline_name is None:
                        p=decode_pc_pointer(effect.serialized_pointer)
                        if p.block!=4:raise ValueError(f"FX '{owner.name}' effect reference is not in block 4")
                        rows.append((elem.index,-1-field_index,effect.serialized_pointer,p.offset))
                if elem.element_type!=10:continue
                # When the table pointer itself is packed, parse_owned_fx_at can
                # prove only the table identity, not the individual entries.  It
                # intentionally represents those entries as repeated unresolved
                # visuals, which is insufficient evidence for target binding.
                if elem.visual_count>1 and decode_pc_pointer(elem.visual_pointer_raw).kind=='packed':
                    raise ValueError(
                        f"FX runner '{owner.name}' element {elem.index} has an unresolved packed visual table"
                    )
                for visual_index,visual in enumerate(elem.visuals):
                    if visual.kind is VisualKind.PACKED_UNRESOLVED:
                        p=decode_pc_pointer(visual.serialized_pointer)
                        if p.kind!='packed' or p.block!=4:
                            raise ValueError(f"FX runner '{owner.name}' {elem.index}:{visual_index} is not a packed block-4 reference")
                        rows.append((elem.index,visual_index,visual.serialized_pointer,p.offset))
        except ValueError as error:
            _fail(owner,error);continue
        if rows:requests_by_owner[owner.source_asset_index]=rows
    if not requests_by_owner:return FxRunnerResolution((),None,tuple(failed))

    bindings=[]
    target_by_raw={}
    proven_offset_by_raw={}
    for owner_index,requests in requests_by_owner.items():
        owner=by_index[owner_index]
        try:
            bindings.extend(_resolve_runner_owner(
                owner,owner_index,requests,by_index,zone,technique_sets,
                target_by_raw,proven_offset_by_raw))
        except ValueError as error:
            _fail(owner,error);continue
    return FxRunnerResolution(tuple(bindings),None,tuple(failed))


def _resolve_runner_owner(owner,owner_index,requests,by_index,zone,technique_sets,
                          target_by_raw,proven_offset_by_raw):
    """Resolve one owner's packed FX references; commit proofs only on success."""
    raw_order=[]
    alias_offset_by_raw={}
    for _elem_index,_visual_index,raw,alias_offset in requests:
        old=alias_offset_by_raw.get(raw)
        if old is not None and old!=alias_offset:
            raise ValueError(f"FX runner '{owner.name}' pointer 0x{raw:08X} decodes inconsistently")
        if raw not in alias_offset_by_raw:
            raw_order.append(raw)
            alias_offset_by_raw[raw]=alias_offset

    # Aliases an earlier owner already proved are reuses of existing cells,
    # not new typed targets: the linker deduplicates assets by name and never
    # re-serializes a target for a repeated reference.  The preceding-run
    # model therefore applies to the NEW aliases only; a reused alias must
    # decode to exactly its proven cell and point at an earlier owner.
    new_raws=[raw for raw in raw_order if raw not in target_by_raw]
    for raw in raw_order:
        if raw in target_by_raw:
            target=target_by_raw[raw]
            if target.source_asset_index>=owner_index:
                raise ValueError(f"FX runner '{owner.name}' alias target is not an earlier owner")
            if proven_offset_by_raw.get(raw)!=alias_offset_by_raw[raw]:
                raise ValueError(
                    f"FX runner '{owner.name}' pointer 0x{raw:08X} does not decode to its proven alias cell")

    committed={}
    if new_raws:
        new_offsets=[alias_offset_by_raw[raw] for raw in new_raws]
        if new_offsets!=sorted(new_offsets) or len(set(new_offsets))!=len(new_offsets):
            raise ValueError(f"FX runner '{owner.name}' new block-4 aliases are not in unique LoadStream order")
        # Block-4 cells are allocated monotonically over the LoadStream: a new
        # target's cell must postdate every cell proven so far, or the
        # preceding-run inference is not unique for this owner.
        if proven_offset_by_raw and min(new_offsets)<=max(proven_offset_by_raw.values()):
            raise ValueError(
                f"FX runner '{owner.name}' new alias cell predates an already-proven cell; "
                'the preceding-run inference is not unique')

        # One distinct NEW persistent alias requires one typed target.  The
        # target run must end immediately before the owner in both XAsset
        # order and physical LoadStream order; otherwise more than one
        # assignment is possible and this owner is unprovable.
        first_target_index=owner_index-len(new_raws)
        target_indices=range(first_target_index,owner_index)
        bridged=False
        if any(i not in by_index for i in target_indices) and zone is not None and len(new_raws)>=2:
            # A source-owned shader graph can sit between two target FX assets.
            # Select the preceding FX run only if its full typed member replay
            # reproduces every independent packed target anchor exactly.
            prior=sorted(i for i in by_index if i<owner_index)
            if len(prior)>=len(new_raws):
                target_indices=prior[-len(new_raws):];bridged=True
        targets=[]
        for target_index in target_indices:
            target=by_index.get(target_index)
            if target is None:
                raise ValueError(
                    f"FX runner '{owner.name}' lacks consecutive typed target XAsset #{target_index}"
                )
            if not target.source_owned or target.graph is None:
                raise ValueError(
                    f"FX runner '{owner.name}' target #{target_index} has no owned source graph"
                )
            targets.append(target)
        corridor=targets+[owner]
        techniques={int(t.source_asset_index):t for t in technique_sets}
        for left,right in zip(corridor,corridor[1:]):
            if left.physical_end_offset!=right.root_offset or bridged:
                if bridged:
                    from .pc_technique_replay import replay_technique_members
                    anchor=new_offsets[targets.index(left)]
                    cursor,_cells=_replay_fx_member_b4(zone,left,anchor)
                    physical=left.physical_end_offset
                    for ai in range(left.source_asset_index+1,right.source_asset_index):
                        technique=techniques.get(ai)
                        following=techniques.get(ai+1)
                        end=following.root_offset if ai+1<right.source_asset_index and following else right.root_offset
                        if technique is None or technique.root_offset!=physical:
                            raise ValueError('FX runner corridor contains an unverified intervening asset')
                        cursor=replay_technique_members(zone,technique,cursor,end);physical=end
                    if physical!=right.root_offset:
                        raise ValueError('FX runner corridor physical end differs')
                    if right is not owner and cursor!=new_offsets[targets.index(right)]:
                        raise ValueError(f"FX runner '{owner.name}' typed bridge from '{left.name}' "
                            f"ends at 0x{cursor:X}, conflicts with packed target anchor "
                            f"0x{new_offsets[targets.index(right)]:X}")
                    continue
                raise ValueError(
                    f"FX runner '{owner.name}' target corridor is not physically contiguous "
                    f"at XAsset #{left.source_asset_index}->#{right.source_asset_index}"
                )
        committed=dict(zip(new_raws,targets))

    target_for_raw={raw:target_by_raw[raw] for raw in raw_order if raw in target_by_raw}
    target_for_raw.update(committed)
    rows=[]
    for elem_index,visual_index,raw,alias_offset in requests:
        target=target_for_raw[raw]
        rows.append(FxRunnerBinding(
            owner_index,owner.name,elem_index,visual_index,raw,alias_offset,
            target.source_asset_index,target.name,
        ))
    # Success: commit the new proofs atomically so a later failed owner never
    # inherits partial state from this one.
    for raw,target in committed.items():
        target_by_raw[raw]=target
        proven_offset_by_raw[raw]=alias_offset_by_raw[raw]
    return rows


class _FxMemberReplayUnavailable(ValueError):pass


def _align_up(value:int,alignment:int)->int:return (value+alignment-1)&~(alignment-1)


@dataclass(frozen=True)
class _FxMaterialOwnerCell:
    source_asset_index:int
    owner_name:str
    element_index:int
    visual_index:int
    block4_offset:int
    material_root:int
    material_name:str


def _replay_inline_material_b4(z:bytes,cursor:int,visual:FxVisual,label:str)->int:
    if visual.material_root is None:
        raise _FxMemberReplayUnavailable(f'{label} has no typed Material root')
    if decode_pc_pointer(visual.serialized_pointer).kind!='following':
        raise _FxMemberReplayUnavailable(f'{label} Material root is not FOLLOWING')
    material,_physical_end=parse_material_at(z,visual.material_root)
    if material.name!=visual.name:
        raise ValueError(f"{label} Material identity differs: {visual.name!r}/{material.name!r}")
    if decode_pc_pointer(u32(z,material.root_offset)).kind!='following':
        raise _FxMemberReplayUnavailable(f'{label} Material name is not FOLLOWING')
    cursor+=len(material.name.encode('latin-1'))+1

    texture_pointer=u32(z,material.root_offset+0x44)
    if material.textures:
        if decode_pc_pointer(texture_pointer).kind!='following':
            raise _FxMemberReplayUnavailable(f'{label} Material texture table is not FOLLOWING')
        cursor=_align_up(cursor,4)+len(material.textures)*0x0c
        for texture_index,texture in enumerate(material.textures):
            if texture_index in material.water_by_texture:
                raise _FxMemberReplayUnavailable(f'{label} inline water Material replay is not implemented')
            pointer=decode_pc_pointer(texture.resource_pointer)
            if pointer.kind in ('null','packed'):continue
            if pointer.kind!='following' or texture.inline_image_root is None:
                raise _FxMemberReplayUnavailable(f'{label} inline GfxImage root is not FOLLOWING')
            image_root=texture.inline_image_root
            name_pointer=decode_pc_pointer(u32(z,image_root+0x20))
            if name_pointer.kind=='following':
                if texture.inline_image_name is None:
                    raise _FxMemberReplayUnavailable(f'{label} inline GfxImage name is unavailable')
                cursor+=len(texture.inline_image_name.encode('latin-1'))+1
            elif name_pointer.kind not in ('null','packed'):
                raise _FxMemberReplayUnavailable(f'{label} inline GfxImage name uses {name_pointer.kind}')
            loaddef_pointer=decode_pc_pointer(u32(z,image_root+4))
            if loaddef_pointer.kind=='insert':cursor=_align_up(cursor,4)+4
            elif loaddef_pointer.kind not in ('null','following','packed'):
                raise _FxMemberReplayUnavailable(f'{label} inline GfxImage LoadDef uses {loaddef_pointer.kind}')
    elif decode_pc_pointer(texture_pointer).kind!='null':
        raise _FxMemberReplayUnavailable(f'{label} zero-count Material texture table is non-NULL')

    constant_pointer=u32(z,material.root_offset+0x48)
    if material.constants:
        if decode_pc_pointer(constant_pointer).kind!='following':
            raise _FxMemberReplayUnavailable(f'{label} Material constant table is not FOLLOWING')
        cursor=_align_up(cursor,16)+len(material.constants)*0x20
    elif decode_pc_pointer(constant_pointer).kind!='null':
        raise _FxMemberReplayUnavailable(f'{label} zero-count Material constant table is non-NULL')

    state_pointer=u32(z,material.root_offset+0x4c)
    if material.state_bits:
        if decode_pc_pointer(state_pointer).kind!='following':
            raise _FxMemberReplayUnavailable(f'{label} Material state table is not FOLLOWING')
        cursor=_align_up(cursor,4)+len(material.state_bits)*8
    elif decode_pc_pointer(state_pointer).kind!='null':
        raise _FxMemberReplayUnavailable(f'{label} zero-count Material state table is non-NULL')
    return cursor


def _replay_fx_member_b4(
    z:bytes,reference:FxReference,member_start:int,
)->tuple[int,tuple[_FxMaterialOwnerCell,...]]:
    """Replay one owned PC FxEffectDef's persistent block-4 members.

    The root and nested asset shells are TEMP allocations.  The name,
    FxElemDef array, sample arrays, visual pointer tables, nested asset names,
    Material members and trails advance block 4 in source loader order.
    """
    if not reference.source_owned or reference.graph is None:
        return member_start+len(reference.name.encode('latin-1'))+1,()
    graph=reference.graph
    cursor=member_start+len(graph.name.encode('latin-1'))+1
    cursor=_align_up(cursor,4);element_base=cursor;cursor+=len(graph.elements)*FX_ELEM_SIZE
    cells=[]
    for element in graph.elements:
        label=f"FX '{reference.name}' element {element.index}"
        if element.velocity_bytes:
            if decode_pc_pointer(element.velocity_pointer_raw).kind!='following':
                raise _FxMemberReplayUnavailable(f'{label} velocity is not FOLLOWING')
            cursor=_align_up(cursor,4)+len(element.velocity_bytes)
        if element.visual_state_bytes:
            if decode_pc_pointer(element.visual_state_pointer_raw).kind!='following':
                raise _FxMemberReplayUnavailable(f'{label} visual state is not FOLLOWING')
            cursor=_align_up(cursor,4)+len(element.visual_state_bytes)

        pointer_cells=[]
        if element.element_type==9 and element.visual_count:
            if decode_pc_pointer(element.visual_pointer_raw).kind!='following':
                raise _FxMemberReplayUnavailable(f'{label} decal visual table is not FOLLOWING')
            cursor=_align_up(cursor,4);pointer_cells=[cursor+i*4 for i in range(len(element.visuals))];cursor+=len(element.visuals)*4
        elif element.visual_count>1:
            if decode_pc_pointer(element.visual_pointer_raw).kind!='following':
                raise _FxMemberReplayUnavailable(f'{label} visual table is not FOLLOWING')
            cursor=_align_up(cursor,4);pointer_cells=[cursor+i*4 for i in range(len(element.visuals))];cursor+=len(element.visuals)*4
        elif element.visual_count==1:
            if len(element.visuals)!=1:raise ValueError(f'{label} visual cardinality differs')
            pointer_cells=[element_base+element.index*FX_ELEM_SIZE+0xbc]
        elif element.visuals:
            raise ValueError(f'{label} zero-count visual cardinality differs')

        for visual_index,visual in enumerate(element.visuals):
            if visual.kind is VisualKind.MATERIAL:
                if visual_index>=len(pointer_cells):raise ValueError(f'{label} Material cell is unavailable')
                cells.append(_FxMaterialOwnerCell(
                    reference.source_asset_index,reference.name,element.index,visual_index,
                    pointer_cells[visual_index],int(visual.material_root),str(visual.name),
                ))
                cursor=_replay_inline_material_b4(z,cursor,visual,f'{label} visual {visual_index}')
            elif visual.kind is VisualKind.XMODEL_ALIAS:
                if not visual.name:raise _FxMemberReplayUnavailable(f'{label} XModel alias has no name')
                cursor+=len((','+visual.name.lstrip(',')).encode('latin-1'))+1
            elif visual.kind is VisualKind.STRING:
                if visual.name is None:raise _FxMemberReplayUnavailable(f'{label} string visual has no value')
                cursor+=len(visual.name.encode('latin-1'))+1
        for effect in (element.effect_on_impact,element.effect_on_death,element.effect_emitted):
            if effect.inline_name is not None:
                # _read_effect normalizes a possible leading comma, so its exact
                # source allocation length is no longer available here.
                raise _FxMemberReplayUnavailable(f'{label} owns an inline effect XString')
        if element.trail is not None:
            if decode_pc_pointer(element.trail_pointer_raw).kind!='following':
                raise _FxMemberReplayUnavailable(f'{label} trail is not FOLLOWING')
            cursor=_align_up(cursor,4)+TRAIL_ROOT_SIZE+len(element.trail.vertex_bytes)+len(element.trail.index_bytes)
    return cursor,tuple(cells)


def resolve_fx_material_bindings(
    z:bytes,fx_references:Sequence[FxReference],runner_resolution:FxRunnerResolution,
    technique_sets:Sequence[object]=(),raw_values:Sequence[int]|None=None,
)->FxMaterialResolution:
    """Resolve packed Material visuals through an exact typed B4 replay.

    Runner references independently anchor prior FxEffectDef member/name starts.
    From those anchors the complete typed FX member stream is replayed forward.
    A gap may be crossed only when every intervening XAsset is a physical,
    shell-only TechniqueSet (NULL remap and technique slots), whose sole B4
    allocation is its exact source name.  Packed words bind only when they land
    on one unique, earlier Material pointer field.
    """
    references=tuple(sorted(fx_references,key=lambda x:x.source_asset_index))
    by_index={x.source_asset_index:x for x in references}
    if len(by_index)!=len(references):raise ValueError('FX Material replay has duplicate source asset indices')
    requested=None if raw_values is None else {int(raw)&0xffffffff for raw in raw_values}
    requests=[]
    for owner in references:
        if not owner.source_owned or owner.graph is None:continue
        for element in owner.graph.elements:
            if element.element_type>4:continue
            for visual_index,visual in enumerate(element.visuals):
                if visual.kind is not VisualKind.PACKED_UNRESOLVED:continue
                raw=int(visual.serialized_pointer)&0xffffffff
                if requested is not None and raw not in requested:continue
                pointer=decode_pc_pointer(raw)
                if pointer.kind!='packed' or pointer.block!=4:
                    raise ValueError(f"FX Material '{owner.name}' {element.index}:{visual_index} is not packed block 4")
                requests.append((owner,element.index,visual_index,raw,int(pointer.offset)))
    if not requests:return FxMaterialResolution((),(),{}, {})

    starts={};evidence={}
    for row in runner_resolution.bindings:
        target=by_index.get(row.target_source_asset_index)
        if target is None or not target.source_owned or target.graph is None:
            raise ValueError(f'FX Material replay runner target #{row.target_source_asset_index} is unavailable')
        offset=int(row.source_block4_alias_offset)
        old=starts.get(target.source_asset_index)
        if old is not None and old!=offset:
            raise ValueError(f"FX '{target.name}' has conflicting runner B4 anchors 0x{old:X}/0x{offset:X}")
        starts[target.source_asset_index]=offset
        evidence[target.source_asset_index]=f'runner target b4+0x{offset:X}'

    techniques={int(getattr(t,'source_asset_index')):t for t in technique_sets}
    unavailable={};replayed={}
    for position,reference in enumerate(references):
        start=starts.get(reference.source_asset_index)
        if start is None:continue
        try:end,cells=_replay_fx_member_b4(z,reference,start)
        except _FxMemberReplayUnavailable as exc:
            unavailable[reference.source_asset_index]=str(exc);continue
        replayed[reference.source_asset_index]=(end,cells)
        if position+1>=len(references):continue
        following=references[position+1];cursor=end;physical=reference.physical_end_offset;bridge=[];valid=True
        for asset_index in range(reference.source_asset_index+1,following.source_asset_index):
            technique=techniques.get(asset_index)
            if technique is None or int(getattr(technique,'root_offset'))!=physical:
                valid=False;break
            if decode_pc_pointer(int(getattr(technique,'remapped_pointer'))).kind!='null':
                valid=False;break
            slots=tuple(getattr(technique,'technique_pointers'))
            if not slots or any(decode_pc_pointer(int(raw)).kind!='null' for raw in slots):
                valid=False;break
            name=str(getattr(technique,'name'));size=0x94+len(name.encode('latin-1'))+1
            physical+=size;cursor+=len(name.encode('latin-1'))+1;bridge.append(asset_index)
        if not valid or physical!=following.root_offset:continue
        old=starts.get(following.source_asset_index)
        if old is not None and old!=cursor:
            raise ValueError(
                f"FX '{following.name}' typed B4 replay 0x{cursor:X} conflicts with runner anchor 0x{old:X}"
            )
        if old is None:
            starts[following.source_asset_index]=cursor
            suffix='' if not bridge else ' via shell TechniqueSets '+','.join(map(str,bridge))
            evidence[following.source_asset_index]=f"FX #{reference.source_asset_index} complete replay{suffix}"

    cells_by_offset={}
    for asset_index,(_end,cells) in replayed.items():
        for cell in cells:
            old=cells_by_offset.get(cell.block4_offset)
            if old is not None and old!=cell:
                raise ValueError(f'FX Material owner-cell collision at b4+0x{cell.block4_offset:X}')
            cells_by_offset[cell.block4_offset]=cell

    bindings=[];unresolved=set()
    for owner,element_index,visual_index,raw,target_offset in requests:
        target=cells_by_offset.get(target_offset)
        if target is None:
            unresolved.add(raw);continue
        if (target.source_asset_index,target.element_index,target.visual_index)>=(
            owner.source_asset_index,element_index,visual_index,
        ):
            unresolved.add(raw);continue
        bindings.append(FxMaterialBinding(
            owner.source_asset_index,owner.name,element_index,visual_index,raw,target_offset,
            target.source_asset_index,target.owner_name,target.element_index,target.visual_index,
            target.material_root,target.material_name,
        ))
    bound_raws={row.serialized_pointer for row in bindings}
    unresolved.update({raw for _o,_e,_v,raw,_off in requests if raw not in bound_raws})
    return FxMaterialResolution(tuple(bindings),tuple(sorted(unresolved)),dict(starts),dict(unavailable))



_FX_NAME_CHARS=set('abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-./')


def _looks_fx_name(name:str)->bool:
    """An FX alias name is a path of IW3 asset-name characters, at least three long."""
    if len(name)<3 or len(name)>200:return False
    if not all(c in _FX_NAME_CHARS for c in name):return False
    return any(c.isalpha() for c in name)


def top_level_fx(z:bytes,asset_list:XAssetList,*,excluded_ranges:tuple[tuple[int,int],...]=())->tuple[FxReference,...]:
    if asset_list.structural_index is not None:
        out=[]
        for row in asset_list.structural_index.rows(PC_FX_TYPE):
            root=row['root'];owned=bool(u32(z,root+0x1c))
            graph=parse_owned_fx_at(z,root) if owned else None
            if graph is not None and graph.physical_end_offset!=row['end']:
                raise ValueError(f"FX semantic reader differs from structural end for {row['name']!r}")
            out.append(FxReference(row['index'],root,row['end'],row['name'].lstrip(','),owned,graph,graph.inline_material_roots if graph else ()))
        return tuple(out)
    assets=sorted((a for a in asset_list.assets if int(a.type_id)==PC_FX_TYPE),key=lambda a:a.index)
    if not assets:return ()
    if any(decode_pc_pointer(a.serialized_pointer).kind=='packed' for a in assets):
        raise ValueError('packed/shared top-level FX XAsset has no local physical root/name to pair safely')
    start=max(0,asset_list.asset_data_offset);candidates=[]
    # Name-only support-zone FX shells.  Search candidate FOLLOWING markers in
    # C-speed rather than iterating every byte of the complete zone.
    marker=b'\xff\xff\xff\xff';pos=start
    while True:
        root=z.find(marker,pos)
        if root<0 or root+FX_ROOT_SIZE+2>len(z):break
        pos=root+1
        if any(begin <= root < end for begin,end in excluded_ranges):continue
        if any(u32(z,root+off)!=0 for off in range(4,FX_ROOT_SIZE,4)):continue
        try:name,end=read_z(z,root+FX_ROOT_SIZE,512)
        except ValueError:continue
        name=name.lstrip(',')
        # An FX alias name is an asset path such as 'impacts/small_brick'.  Accepting any
        # string with a letter in it let two byte runs inside stock mp_shipment pass as FX
        # roots ('pp' and 'iD=@eI=@qV>@'), so the scan found 125 roots for 123 XAssets and
        # the porter refused the map.  Require the closed IW3 asset-name character set.
        if not _looks_fx_name(name):continue
        candidates.append((root,end,name,False,None,()))
    owned=[g for g in scan_owned_fx(z,start) if not any(begin <= g.root_offset < end for begin,end in excluded_ranges)]
    candidates.extend((g.root_offset,g.physical_end_offset,g.name.lstrip(','),True,g,g.inline_material_roots) for g in owned)
    # Same physical root can look like a shell before full replay; owned graph wins.
    byroot={}
    for c in candidates:
        old=byroot.get(c[0])
        if old is None or (c[3] and not old[3]):byroot[c[0]]=c
    unique=[byroot[k] for k in sorted(byroot)]
    if len(unique)!=len(assets):
        sample=', '.join(f'0x{x[0]:X}:{x[2]}'+('[owned]' if x[3] else '[alias]') for x in unique[:40])
        raise ValueError(f'PC FX semantic scan found {len(unique)} roots for {len(assets)} declared FX XAssets; {sample}')
    return tuple(FxReference(a.index,c[0],c[1],c[2],c[3],c[4],tuple(c[5])) for a,c in zip(assets,unique))
