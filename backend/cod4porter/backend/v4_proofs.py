from __future__ import annotations
from dataclasses import dataclass, asdict
from collections import defaultdict
import hashlib, struct
from typing import Any, Iterable, Sequence

FOLLOWING=0xFFFFFFFF
INSERT=0xFFFFFFFE
NULL=0

@dataclass(frozen=True)
class Allocation:
    identity:str
    kind:str
    size:int
    alignment:int=1
    alias_cell:bool=False

@dataclass(frozen=True)
class ReplayAllocation:
    identity:str
    kind:str
    relative_offset:int
    size:int
    alignment:int
    alias_cell:bool

@dataclass(frozen=True)
class PackedRequest:
    description:str
    logical_offset:int
    expected_kind:str


def align(value:int,boundary:int)->int:
    if boundary<=1:return value
    return (value+(boundary-1))&~(boundary-1)


def replay_allocations(allocations:Sequence[Allocation],phase:int=0)->tuple[ReplayAllocation,...]:
    if phase<0: raise ValueError('phase must be nonnegative')
    cursor=phase
    out=[]
    for a in allocations:
        if a.size<0: raise ValueError('allocation size cannot be negative')
        if a.alignment<=0 or a.alignment&(a.alignment-1): raise ValueError('alignment must be a power of two')
        cursor=align(cursor,a.alignment)
        out.append(ReplayAllocation(a.identity,a.kind,cursor,a.size,a.alignment,a.alias_cell))
        cursor+=a.size
    return tuple(out)


def solve_uniform_basis(allocations:Sequence[Allocation],requests:Sequence[PackedRequest],phases:Iterable[int]=(0,1,2,3),minimum_constraints:int=2)->dict:
    """Find a single absolute block base/phase that maps every packed request to one typed allocation.

    This intentionally cannot turn one unconstrained packed pointer into a proof.  By default at least two
    independent packed requests are required.  Callers may lower minimum_constraints only when an external
    exact anchor independently proves the base.
    """
    if len(requests)<minimum_constraints:
        return {'passed':False,'reason':f'need at least {minimum_constraints} independent packed constraints','request_count':len(requests)}
    solutions=[]
    for phase in phases:
        replay=replay_allocations(allocations,phase)
        by_kind=defaultdict(list)
        for a in replay: by_kind[a.kind].append(a)
        bases=None
        candidates_per_request=[]
        for req in requests:
            cand=[]
            for a in by_kind.get(req.expected_kind,[]):
                base=req.logical_offset-a.relative_offset
                if base>=0: cand.append((base,a))
            if not cand:
                bases=set(); break
            candidates_per_request.append((req,cand))
            rb={b for b,_ in cand}
            bases=rb if bases is None else bases&rb
            if not bases: break
        for base in sorted(bases or []):
            bindings=[]; ok=True
            for req,cand in candidates_per_request:
                matches=[a for b,a in cand if b==base]
                if len(matches)!=1: ok=False; break
                a=matches[0]
                bindings.append({'description':req.description,'logical_offset':req.logical_offset,'identity':a.identity,'kind':a.kind,'relative_offset':a.relative_offset})
            if ok: solutions.append({'phase':phase,'base':base,'bindings':bindings})
    semantic={(s['base'],tuple((b['description'],b['identity']) for b in s['bindings'])) for s in solutions}
    if not solutions:
        return {'passed':False,'reason':'no block base/phase satisfies all typed packed constraints','solutions':[]}
    # Multiple phases are acceptable only if they collapse to the same absolute base and semantic bindings.
    bases={s['base'] for s in solutions}; binds={tuple((b['description'],b['identity']) for b in s['bindings']) for s in solutions}
    if len(bases)!=1 or len(binds)!=1:
        return {'passed':False,'reason':'ambiguous block base/semantic binding across surviving phases','solutions':solutions}
    return {'passed':True,'base':next(iter(bases)),'phases':[s['phase'] for s in solutions],'bindings':solutions[0]['bindings'],'solution_count':len(solutions)}


class TypedAliasLedger:
    """Typed logical alias cells. TEMP XAsset-root aliases and TEMP reusable member aliases are distinct kinds."""
    def __init__(self): self._cells:dict[tuple[int,int,str],str]={}
    def add(self,block:int,offset:int,kind:str,identity:str):
        key=(block,offset,kind); old=self._cells.get(key)
        if old is not None and old!=identity: raise ValueError(f'alias conflict {key}: {old!r} vs {identity!r}')
        self._cells[key]=identity
    def resolve(self,block:int,offset:int,kind:str)->str|None: return self._cells.get((block,offset,kind))
    def to_rows(self): return [{'block':b,'offset':o,'kind':k,'identity':v} for (b,o,k),v in sorted(self._cells.items())]


def consensus_alias(phases:Sequence[TypedAliasLedger],block:int,offset:int,kind:str,minimum_votes:int=1)->dict:
    vals=[]
    for i,p in enumerate(phases):
        v=p.resolve(block,offset,kind)
        if v is not None: vals.append((i,v))
    if len(vals)<minimum_votes: return {'passed':False,'reason':'insufficient alias evidence','votes':vals}
    identities={v for _,v in vals}
    if len(identities)!=1: return {'passed':False,'reason':'phase alias disagreement','votes':vals}
    return {'passed':True,'identity':next(iter(identities)),'votes':vals}


def texturedef_signature(record:dict)->tuple:
    required=('name_hash','name_start','name_end','sampler','semantic')
    missing=[k for k in required if k not in record]
    if missing: raise ValueError('texturedef missing '+','.join(missing))
    return tuple(record[k] for k in required)


def bind_texture_signatures(inline_records:Sequence[dict],packed_records:Sequence[dict])->dict:
    """Exact complete TextureDef signature -> unique previously observed GfxImage identity."""
    sigmap=defaultdict(set)
    for r in inline_records:
        sigmap[texturedef_signature(r)].add(r['image_identity'])
    bindings=[]; blockers=[]
    for r in packed_records:
        sig=texturedef_signature(r); ids=sigmap.get(sig,set())
        if len(ids)==1: bindings.append({'request':r.get('description'),'identity':next(iter(ids)),'signature':sig})
        else: blockers.append({'request':r.get('description'),'signature':sig,'candidate_identities':sorted(ids)})
    return {'passed':not blockers,'bindings':bindings,'blockers':blockers}


def solve_material_state_slots(state_bits_entry:Sequence[int],state_table_count:int,technique_slots:Sequence[int|None])->dict:
    """Resolve stale Material stateBitsEntry indices without fabricating state records.

    An out-of-range non-FF index may be replaced by FF only when the corresponding TechniqueSet slot is
    *proven inactive* (pointer == 0).  Active (nonzero) or unknown (None/missing) slots remain blockers.
    """
    if state_table_count<0: raise ValueError('negative state table count')
    out=list(state_bits_entry); changes=[]; blockers=[]
    for slot,idx in enumerate(out):
        if not 0<=idx<=255: blockers.append({'slot':slot,'index':idx,'reason':'index outside byte'}); continue
        if idx==0xFF or idx<state_table_count: continue
        active=technique_slots[slot] if slot<len(technique_slots) else None
        if active==0:
            changes.append({'slot':slot,'old_index':idx,'new_index':0xFF,'proof':'TechniqueSet slot is null/inactive'})
            out[slot]=0xFF
        elif active is None:
            blockers.append({'slot':slot,'index':idx,'reason':'TechniqueSet slot activity unknown'})
        else:
            blockers.append({'slot':slot,'index':idx,'reason':f'TechniqueSet slot active pointer=0x{active:08X}'})
    return {'passed':not blockers,'resolved_state_bits_entry':out,'changes':changes,'blockers':blockers}


def platform_signature(material:dict)->str:
    keys=('sort_key','camera_region','technique_set','state_bits_entry','state_bits','texture_signatures')
    parts=[]
    for k in keys:
        if k in material: parts.append(f'{k}={material[k]!r}')
    if not parts: raise ValueError('material has no platform signature fields')
    return '|'.join(parts)


class PlatformStateCatalog:
    def __init__(self): self._obs=defaultdict(set); self._sources=defaultdict(list)
    def add(self,signature:str,payload:bytes,source:str):
        if len(payload)!=0x38: raise ValueError('PS3 Material PlatformState must be exactly 0x38 bytes')
        hx=payload.hex().upper(); self._obs[signature].add(hx); self._sources[(signature,hx)].append(source)
    def resolve(self,signature:str)->dict:
        values=self._obs.get(signature,set())
        if len(values)!=1: return {'passed':False,'signature':signature,'candidate_count':len(values),'candidates':sorted(values)}
        hx=next(iter(values)); return {'passed':True,'signature':signature,'platform_state_hex':hx,'sources':self._sources[(signature,hx)]}
    def coverage(self,signatures:Iterable[str])->dict:
        rows=[self.resolve(s) for s in signatures]; return {'passed':all(r['passed'] for r in rows),'rows':rows}


@dataclass(frozen=True)
class PcWater:
    float_time_bits:int; h0_pointer:int; wterm_pointer:int; m:int; n:int
    scalar_bits:tuple[int,...]  # Lx,Lz,gravity,windvel,winddir x/y,amplitude,codeConstant[4] = 11 u32
    image_pointer:int
    h0_payload:bytes=b''
    wterm_payload:bytes=b''


def parse_pc_water(root:bytes,h0_payload:bytes=b'',wterm_payload:bytes=b'')->PcWater:
    if len(root)!=0x44: raise ValueError('PC water_t root must be 0x44 bytes')
    words=struct.unpack('<17I',root)
    # word0 floatTime, word1/2 H0/wTerm, word3/4 M/N, word5..15 eleven scalar bit patterns, word16 image
    return PcWater(words[0],words[1],words[2],struct.unpack('<i',root[0x0C:0x10])[0],struct.unpack('<i',root[0x10:0x14])[0],tuple(words[5:16]),words[16],h0_payload,wterm_payload)


def normalized_pc_float_payload_to_ps3_sha256(payload:bytes)->str:
    if len(payload)%4: raise ValueError('float payload length must be multiple of four')
    out=bytearray()
    for i in range(0,len(payload),4):
        bits=struct.unpack_from('<I',payload,i)[0]; out+=struct.pack('>I',bits)
    return hashlib.sha256(out).hexdigest().upper()


def ps3_float_payload_sha256(payload:bytes)->str:
    if len(payload)%4: raise ValueError('float payload length must be multiple of four')
    return hashlib.sha256(payload).hexdigest().upper()


def build_ps3_water_root(pc:PcWater,ps3_extra_pointer:int,image_pointer:int|None=None)->bytes:
    """Transform measured PC 0x44 water_t to PS3 0x48 layout.

    The extra +0x0C PS3 pointer is evidence-driven: callers must provide the observed/required pointer marker.
    No default value is invented here.
    """
    if ps3_extra_pointer not in (NULL,FOLLOWING,INSERT) and ps3_extra_pointer<1: raise ValueError('invalid PS3 extra pointer')
    image=pc.image_pointer if image_pointer is None else image_pointer
    out=bytearray(0x48)
    struct.pack_into('>I',out,0x00,pc.float_time_bits)
    struct.pack_into('>I',out,0x04,pc.h0_pointer)
    struct.pack_into('>I',out,0x08,pc.wterm_pointer)
    struct.pack_into('>I',out,0x0C,ps3_extra_pointer)
    struct.pack_into('>i',out,0x10,pc.m); struct.pack_into('>i',out,0x14,pc.n)
    for i,bits in enumerate(pc.scalar_bits): struct.pack_into('>I',out,0x18+i*4,bits)
    struct.pack_into('>I',out,0x44,image)
    return bytes(out)


def water_observation(pc:PcWater,ps3_root:bytes,ps3_h0:bytes=b'',ps3_wterm:bytes=b'')->dict:
    if len(ps3_root)!=0x48: return {'passed':False,'reason':'PS3 root is not 0x48 bytes'}
    extra=struct.unpack_from('>I',ps3_root,0x0C)[0]; m=struct.unpack_from('>i',ps3_root,0x10)[0]; n=struct.unpack_from('>i',ps3_root,0x14)[0]
    scalars=tuple(struct.unpack_from('>11I',ps3_root,0x18)); image=struct.unpack_from('>I',ps3_root,0x44)[0]
    root_match=(m==pc.m and n==pc.n and scalars==pc.scalar_bits and struct.unpack_from('>I',ps3_root,0)[0]==pc.float_time_bits)
    h0_match=(not pc.h0_payload and not ps3_h0) or normalized_pc_float_payload_to_ps3_sha256(pc.h0_payload)==ps3_float_payload_sha256(ps3_h0)
    wt_match=(not pc.wterm_payload and not ps3_wterm) or normalized_pc_float_payload_to_ps3_sha256(pc.wterm_payload)==ps3_float_payload_sha256(ps3_wterm)
    return {'passed':root_match and h0_match and wt_match,'extra_pointer':extra,'image_pointer':image,'root_fields_match':root_match,'h0_match':h0_match,'wterm_match':wt_match,
            'pc_h0_normalized_sha256':normalized_pc_float_payload_to_ps3_sha256(pc.h0_payload) if pc.h0_payload else None,
            'ps3_h0_sha256':ps3_float_payload_sha256(ps3_h0) if ps3_h0 else None,
            'pc_wterm_normalized_sha256':normalized_pc_float_payload_to_ps3_sha256(pc.wterm_payload) if pc.wterm_payload else None,
            'ps3_wterm_sha256':ps3_float_payload_sha256(ps3_wterm) if ps3_wterm else None}


def exact_identity_bind(requests:Sequence[dict],observations:Sequence[dict],key_fields:Sequence[str],identity_field:str='identity')->dict:
    """Generic fail-closed identity resolver for packed Image/FX/LightDef references."""
    idx=defaultdict(set)
    for o in observations:
        key=tuple(o.get(k) for k in key_fields); idx[key].add(o[identity_field])
    bindings=[]; blockers=[]
    for r in requests:
        key=tuple(r.get(k) for k in key_fields); ids=idx.get(key,set())
        if len(ids)==1: bindings.append({'request':r.get('description'),'identity':next(iter(ids)),'key':key})
        else: blockers.append({'request':r.get('description'),'key':key,'candidate_identities':sorted(ids)})
    return {'passed':not blockers,'bindings':bindings,'blockers':blockers}
