from __future__ import annotations
from dataclasses import dataclass
import math, struct
from typing import Iterable, Sequence
from .v4_ffio import parse_zone_header, parse_xasset_list, decode_pc_pointer, FOLLOWING, INSERT, NULL
from .v4_proofs import Allocation, PackedRequest, solve_uniform_basis, TypedAliasLedger

ALIAS_LIST=0x0C
ALIAS=0x5C
SOUNDFILE_LEGACY=0x0C
SOUNDFILE_EXTENDED=0x20
LOADED_SOUND=0x2C
SND_CURVE=0x48
SPEAKER_MAP=0x198


def u32(z,o):return struct.unpack_from('<I',z,o)[0]
def f32(z,o):return struct.unpack_from('<f',z,o)[0]

def _plausible_text(z,offset,max_len=1024):
    if offset<0 or offset>=len(z):return False
    end=z.find(b'\0',offset,min(len(z),offset+max_len+1))
    if end<0 or end==offset:return False
    b=z[offset:end]
    return all(x in (9,10,13) or 32<=x<=126 for x in b)

def _normal_ptr(raw,block4_size,allow_null=True):
    p=decode_pc_pointer(raw)
    if p.kind=='null':return allow_null
    if p.kind=='following':return True
    if p.kind!='packed':return False
    return p.block==4 and p.offset is not None and p.offset<block4_size

def _curve_ptr(raw,block4_size):
    p=decode_pc_pointer(raw)
    if p.kind in ('null','following','insert'):return True
    return p.kind=='packed' and p.block==4 and p.offset<block4_size

def looks_like_sound_list_root(z:bytes,root:int,block4_size:int)->bool:
    if root<0 or root+ALIAS_LIST>len(z):return False
    name=u32(z,root); head=u32(z,root+4); count=u32(z,root+8)
    pn=decode_pc_pointer(name); ph=decode_pc_pointer(head)
    if pn.kind=='null':return False
    if pn.kind=='packed' and (pn.block!=4 or pn.offset>=block4_size):return False
    if pn.kind not in ('following','packed'):return False
    if ph.kind=='packed' and (ph.block!=4 or ph.offset>=block4_size):return False
    if ph.kind not in ('null','following','packed'):return False
    if count>4096:return False
    if count==0 and ph.kind!='null':return False
    if count>0 and ph.kind=='null':return False
    return pn.kind!='following' or _plausible_text(z,root+ALIAS_LIST)

def looks_like_alias_scalar_fields(z:bytes,root:int)->bool:
    # The 0x5C root contains floats and small integral fields. Keep this predicate deliberately
    # semantic but conservative: every float representation in the continuous scalar corridor must
    # be finite. This rejects NaN/Inf noise without inventing game-specific magnitude bounds.
    if root<0 or root+ALIAS>len(z):return False
    for off in (0x18,0x1C,0x20,0x24,0x28,0x2C,0x30,0x34,0x38,0x3C,0x40,0x44,0x4C,0x50,0x54):
        try:v=f32(z,root+off)
        except Exception:return False
        if not math.isfinite(v):return False
    return True

def looks_like_alias_root(z:bytes,root:int,block4_size:int)->bool:
    if root<0 or root+ALIAS>len(z):return False
    if decode_pc_pointer(u32(z,root)).kind=='null':return False
    for off in (0,4,8,12,16,88):
        if not _normal_ptr(u32(z,root+off),block4_size,allow_null=True):return False
    if not _curve_ptr(u32(z,root+72),block4_size):return False
    return looks_like_alias_scalar_fields(z,root)

def scan_sound_shells(zone:bytes)->dict:
    h=parse_zone_header(zone,'pc'); al=parse_xasset_list(zone,'pc'); sound=[a for a in al.assets if a.type_id==0x07]
    if not sound:return {'passed':True,'sound_xassets':0,'candidate_roots':0,'packed_exact_segments':[],'semantic_head_roots':0}
    if any(decode_pc_pointer(a.serialized_pointer).kind not in ('following','insert') for a in sound):
        return {'passed':False,'reason':'top-level Sound XAsset pointer is not inline','sound_xassets':len(sound)}
    block4=h.block_sizes[4]
    candidates=[]
    for root in range(al.asset_data_offset,len(zone)-ALIAS_LIST+1):
        if looks_like_sound_list_root(zone,root,block4):candidates.append(root)
    candset=set(candidates); starts=[]
    need=len(sound)
    # exact maximal +0x0C segments only; sliding windows inside longer numeric tables do not count.
    visited=set()
    for r in candidates:
        if r in visited:continue
        prev=r-ALIAS_LIST
        if prev in candset:continue
        seg=[]; x=r
        while x in candset:seg.append(x);visited.add(x);x+=ALIAS_LIST
        if len(seg)==need:starts.append({'start':r,'end':x,'records':len(seg)})
    # Semantic alias-head index scans byte-by-byte as physical alignment is not guaranteed.
    head_roots=[]
    upper=min(len(zone)-ALIAS+1, max(al.asset_data_offset, len(zone)))
    for root in range(0x2C,upper):
        if looks_like_alias_root(zone,root,block4):head_roots.append(root)
    return {'passed':True,'sound_xassets':len(sound),'block4_size':block4,'candidate_roots':len(candidates),'packed_exact_segments':starts,
            'semantic_head_roots':len(head_roots),'first_semantic_head_roots':head_roots[:64]}

@dataclass(frozen=True)
class SoundAllocationEvent:
    identity:str; kind:str; size:int; alignment:int=4


def build_sound_block4_model(events:Sequence[SoundAllocationEvent],packed_requests:Sequence[PackedRequest],minimum_constraints:int=2,phases:Sequence[int]=(0,1,2,3))->dict:
    """Typed block-4 replay for Sound.

    Kinds are intentionally explicit. Typical kinds:
      head_array, string, sound_file, speaker_map,
      temp_xasset_root_alias:sndcurve, temp_xasset_root_alias:loadedsound,
      temp_reusable_member_alias:loadedsound.data.
    This prevents the FIX107 error (treating normal members as INSERT cells) and the FIX113 error
    (treating packed SndCurve roots as top-level XAsset indices).
    """
    alloc=[Allocation(e.identity,e.kind,e.size,e.alignment,'alias' in e.kind) for e in events]
    return solve_uniform_basis(alloc,packed_requests,phases=phases,minimum_constraints=minimum_constraints)


def build_typed_sound_alias_ledger(rows:Iterable[dict])->TypedAliasLedger:
    ledger=TypedAliasLedger()
    allowed={'sndcurve_root','loadedsound_root','loadedsound_data'}
    for r in rows:
        kind=r['kind']
        if kind not in allowed:raise ValueError(f'unsupported Sound alias kind {kind}')
        ledger.add(int(r.get('block',4)),int(r['offset']),kind,str(r['identity']))
    return ledger
