from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
import math
import struct
from typing import Callable, Iterable, Iterator, Sequence

try:
    import numpy as np
except Exception:  # optional acceleration; scalar fallback remains correct
    np = None

from .sound import (
    LoadedSound, SoundAlias, SoundAliasList, SoundCurve, SoundFile, SoundFileType, SpeakerMap,
)
from .backend.v4_ffio import (
    FOLLOWING, INSERT, NULL, decode_pc_pointer, parse_zone_header, parse_xasset_list,
)

ALIAS_LIST = 0x0C
ALIAS = 0x5C
SOUNDFILE_LEGACY = 0x0C
SOUNDFILE_EXTENDED = 0x20
LOADED_SOUND = 0x2C
SND_CURVE = 0x48
SPEAKER_MAP = 0x198

class SoundFileLayout(str, Enum):
    LEGACY_0C = 'iw3-0x0c'
    EXTENDED_20 = 'extended-0x20'

class LargeKind(str, Enum):
    HEAD = 'head_array'
    STRING = 'string'
    SOUNDFILE = 'sound_file'
    SPEAKER = 'speaker_map'
    CURVE_ALIAS = 'temp_xasset_root_alias:sndcurve'
    LOADED_ALIAS = 'temp_xasset_root_alias:loadedsound'
    DATA_ALIAS = 'temp_reusable_member_alias:loadedsound.data'

# Only MssSound::data is TEMP + reusable and therefore owns a logical-only block-4 alias cell.
# SndCurve/LoadedSound roots are TEMP XAssets, but are not themselves reusable normal-block allocations.
_ALIAS_KINDS = {LargeKind.DATA_ALIAS}

@dataclass
class Allocation:
    local_offset: int
    physical: int
    physical_end: int
    kind: LargeKind
    value: object
    @property
    def physical_bytes(self) -> int:
        return self.physical_end - self.physical
    @property
    def logical_bytes(self) -> int:
        return 4 if self.kind in _ALIAS_KINDS else self.physical_bytes

@dataclass
class PackedRef:
    raw: int
    source_physical: int
    kind: LargeKind
    assign: Callable[[object], None]
    description: str
    expected_count: int | None = None
    resolved: bool = False
    resolved_value: object | None = None

@dataclass
class ParseState:
    zone: bytes
    cursor: int
    layout: SoundFileLayout
    large_cursor: int = 0
    allocations: list[Allocation] = field(default_factory=list)
    refs: list[PackedRef] = field(default_factory=list)
    inline_curve_roots: set[int] = field(default_factory=set)
    inline_soundfiles: int = 0
    large_offsets_absolute: bool = False
    resolved_basis: int | None = None
    resolved_phase: int = -1
    equivalent_structural_phases: int = 0
    resolved_internal_strings: int = 0
    external_strings: int = 0

    def alloc(self, physical: int, physical_end: int, alignment: int, kind: LargeKind, value: object) -> int:
        self.large_cursor = _align(self.large_cursor, alignment)
        start = self.large_cursor
        self.large_cursor += physical_end - physical
        self.allocations.append(Allocation(start, physical, physical_end, kind, value))
        return start

    def alloc_alias(self, physical_anchor: int, kind: LargeKind, value: object) -> int:
        if kind not in _ALIAS_KINDS:
            raise ValueError(f'{kind} is not a typed TEMP alias')
        self.large_cursor = _align(self.large_cursor, 4)
        start = self.large_cursor
        self.large_cursor += 4
        self.allocations.append(Allocation(start, physical_anchor, physical_anchor, kind, value))
        return start

@dataclass(frozen=True)
class PhaseAllocation:
    local_offset: int
    allocation: Allocation

@dataclass(frozen=True)
class SoundCatalog:
    lists: tuple[SoundAliasList, ...]
    source_layout: SoundFileLayout
    stream_start: int
    stream_end: int
    block4_basis: int | None
    packed_reference_count: int
    resolved_packed_reference_count: int
    equivalent_structural_phases: int
    resolved_internal_packed_strings: int
    external_or_phase_dependent_strings: int
    packed_string_evidence: tuple[tuple[int,str], ...] = ()
    curve_alias_evidence: tuple[tuple[int,SoundCurve], ...] = ()
    loaded_alias_evidence: tuple[tuple[int,LoadedSound], ...] = ()
    loaded_data_alias_evidence: tuple[tuple[int,bytes], ...] = ()
    top_level_curves: tuple[SoundCurve, ...] = ()
    top_level_loaded_sounds: tuple[LoadedSound, ...] = ()


def _u32(z: bytes, o: int) -> int:
    return struct.unpack_from('<I', z, o)[0]
def _i32(z: bytes, o: int) -> int:
    return struct.unpack_from('<i', z, o)[0]
def _f32(z: bytes, o: int) -> float:
    return struct.unpack_from('<f', z, o)[0]
def _align(v: int, a: int) -> int:
    return (v + a - 1) & ~(a - 1)
def _ensure(z: bytes, o: int, n: int, what: str) -> None:
    if o < 0 or n < 0 or o + n > len(z):
        raise ValueError(f'{what} at 0x{o:X} exceeds source zone')

def _read_text(z: bytes, cursor: int, max_bytes: int = 1025) -> tuple[str, int]:
    _ensure(z, cursor, 1, 'string')
    end = z.find(b'\0', cursor, min(len(z), cursor + max_bytes))
    if end < 0:
        raise ValueError(f'unterminated string at 0x{cursor:X}')
    raw = z[cursor:end]
    if not raw:
        raise ValueError(f'empty required string at 0x{cursor:X}')
    if any(b < 0x20 and b not in (9, 10, 13) for b in raw):
        raise ValueError(f'implausible control byte in string at 0x{cursor:X}')
    value = raw.decode('latin-1')
    if len(value) > 1024:
        raise ValueError('sound string exceeds 1024 characters')
    return value, end + 1

def _ptr_ok_normal(raw: int, block4: int, allow_null: bool = True) -> bool:
    p = decode_pc_pointer(raw)
    if p.kind == 'null': return allow_null
    if p.kind == 'following': return True
    return p.kind == 'packed' and p.block == 4 and p.offset is not None and p.offset < block4

def _ptr_ok_curve(raw: int, block4: int) -> bool:
    p = decode_pc_pointer(raw)
    if p.kind in ('null', 'following', 'insert'): return True
    return p.kind == 'packed' and p.block == 4 and p.offset is not None and p.offset < block4

def _looks_alias_scalar(z: bytes, root: int) -> bool:
    if root < 0 or root + ALIAS > len(z): return False
    # actual snd_alias_t float fields; integer fields are intentionally not range-guessed.
    for off in (24, 28, 32, 36, 40, 44, 52, 56, 60, 64, 76, 80, 84):
        if not math.isfinite(_f32(z, root + off)):
            return False
    return True

def looks_alias(z: bytes, root: int, block4: int) -> bool:
    if root < 0 or root + ALIAS > len(z): return False
    if decode_pc_pointer(_u32(z, root)).kind == 'null': return False
    for off in (0, 4, 8, 12, 16, 88):
        if not _ptr_ok_normal(_u32(z, root + off), block4): return False
    if not _ptr_ok_curve(_u32(z, root + 72), block4): return False
    return _looks_alias_scalar(z, root)

def looks_list_root(z: bytes, root: int, block4: int) -> bool:
    if root < 0 or root + ALIAS_LIST > len(z): return False
    name = decode_pc_pointer(_u32(z, root)); head = decode_pc_pointer(_u32(z, root + 4)); count = _u32(z, root + 8)
    if count > 100_000: return False
    if name.kind not in ('following', 'packed'): return False
    if name.kind == 'packed' and (name.block != 4 or name.offset is None or name.offset >= block4): return False
    if count == 0: return head.kind == 'null'
    if head.kind not in ('following', 'packed'): return False
    if head.kind == 'packed' and (head.block != 4 or head.offset is None or head.offset >= block4): return False
    if name.kind == 'following':
        try: _read_text(z, root + ALIAS_LIST)
        except Exception: return False
    return True


def _vector_list_candidates(zone: bytes, start: int, end: int, block4: int) -> list[int]:
    """Four phase vector scan: structures are 4-byte aligned internally but physical traversal can have any byte phase."""
    if np is None:
        return [o for o in range(start, max(start, end - ALIAS_LIST + 1)) if looks_list_root(zone, o, block4)]
    roots: list[int] = []
    last = min(end, len(zone) - ALIAS_LIST + 1)
    for phase in range(4):
        first = start + ((phase - start) & 3)
        if first >= last: continue
        nwords = (last - first + 3) // 4
        a = np.frombuffer(zone, dtype='<u4', count=nwords, offset=first)
        if len(a) < 3: continue
        name = a[:-2]; head = a[1:-1]; count = a[2:]
        # packed decode, vectorized. FOLLOWING and packed block4 are legal.
        name_follow = name == FOLLOWING
        name_enc = (name.astype(np.uint64) - 1) & 0xFFFFFFFF
        name_packed = (~np.isin(name, [NULL, FOLLOWING, INSERT])) & ((name_enc >> 28) == 4) & ((name_enc & 0x0FFFFFFF) < block4)
        head_null = head == NULL; head_follow = head == FOLLOWING
        head_enc = (head.astype(np.uint64) - 1) & 0xFFFFFFFF
        head_packed = (~np.isin(head, [NULL, FOLLOWING, INSERT])) & ((head_enc >> 28) == 4) & ((head_enc & 0x0FFFFFFF) < block4)
        valid_count = count <= 100_000
        valid_head = ((count == 0) & head_null) | ((count > 0) & (head_follow | head_packed))
        mask = valid_count & valid_head & (name_follow | name_packed)
        idxs = np.nonzero(mask)[0]
        for idx in idxs.tolist():
            o = first + idx * 4
            # only FOLLOWING name needs text check; scalar vector filter leaves false positives otherwise.
            if int(name[idx]) == FOLLOWING:
                try: _read_text(zone, o + ALIAS_LIST)
                except Exception: continue
            roots.append(o)
    roots.sort()
    return roots


def _iter_alias_candidates(zone: bytes, start: int, end: int, block4: int, chunk_bytes: int = 8 << 20) -> Iterator[int]:
    """Memory-bounded vector alias scan, sorted by physical offset."""
    start = max(0, start); end = min(end, len(zone) - ALIAS + 1)
    if start >= end: return
    if np is None:
        for o in range(start, end):
            if looks_alias(zone, o, block4): yield o
        return
    for chunk_start in range(start, end, chunk_bytes):
        chunk_end = min(end, chunk_start + chunk_bytes)
        found: list[int] = []
        for phase in range(4):
            first = chunk_start + ((phase - chunk_start) & 3)
            last = min(chunk_end, len(zone) - ALIAS + 1)
            if first >= last: continue
            # Need 23 words from each candidate; create one 4-byte-phase array with enough tail.
            max_root = last - 1
            n_roots = ((max_root - first) // 4) + 1
            total_words = n_roots + 22
            if first + total_words * 4 > len(zone):
                total_words = (len(zone) - first) // 4
                n_roots = max(0, total_words - 22)
            if n_roots <= 0: continue
            a = np.frombuffer(zone, dtype='<u4', count=total_words, offset=first)
            def ptr_normal(x):
                enc = (x.astype(np.uint64) - 1) & 0xFFFFFFFF
                return (x == NULL) | (x == FOLLOWING) | ((~np.isin(x,[NULL,FOLLOWING,INSERT])) & ((enc>>28)==4) & ((enc & 0x0FFFFFFF)<block4))
            def ptr_curve(x):
                enc = (x.astype(np.uint64) - 1) & 0xFFFFFFFF
                return np.isin(x,[NULL,FOLLOWING,INSERT]) | ((~np.isin(x,[NULL,FOLLOWING,INSERT])) & ((enc>>28)==4) & ((enc & 0x0FFFFFFF)<block4))
            alias_name = a[0:n_roots]
            mask = alias_name != NULL
            for word_off in (0,1,2,3,4,22):
                mask &= ptr_normal(a[word_off:word_off+n_roots])
            mask &= ptr_curve(a[18:18+n_roots])
            # reinterpret each float field's raw u32 bits.
            for word_off in (6,7,8,9,10,11,13,14,15,16,19,20,21):
                fv = a[word_off:word_off+n_roots].view('<f4')
                mask &= np.isfinite(fv)
            idxs = np.nonzero(mask)[0]
            found.extend(first + int(i)*4 for i in idxs.tolist())
        for o in sorted(found):
            yield o


def _read_large_string(st: ParseState, raw: int, source: int, assign: Callable[[str], None], desc: str) -> str | None:
    p = decode_pc_pointer(raw)
    if p.kind == 'null': return None
    if p.kind == 'following':
        start = st.cursor; value, st.cursor = _read_text(st.zone, st.cursor)
        st.alloc(start, st.cursor, 1, LargeKind.STRING, value)
        return value
    if p.kind == 'packed':
        if p.block != 4: raise ValueError(f'{desc} points to PC block {p.block}, expected block 4')
        st.refs.append(PackedRef(raw, source, LargeKind.STRING, lambda v: assign(str(v)), desc))
        return None
    raise ValueError(f'{desc} unsupported pointer kind {p.kind}')


def _validate_speaker(z: bytes, root: int) -> None:
    for m in range(4):
        rec = root + 8 + m*0x64
        if _u32(z, rec) > 6: raise ValueError('SpeakerMap speakerCount exceeds six')
        for speaker in range(6):
            level = rec + 4 + speaker*0x10
            n = _i32(z, level+4)
            if not 0 <= n <= 2: raise ValueError('SpeakerMap numLevels exceeds two')
            if not math.isfinite(_f32(z,level+8)) or not math.isfinite(_f32(z,level+12)):
                raise ValueError('SpeakerMap contains non-finite levels')


def _parse_loaded(st: ParseState) -> LoadedSound:
    z=st.zone; root=st.cursor; _ensure(z,root,LOADED_SOUND,'LoadedSound')
    namep=_u32(z,root); fmt=_u32(z,root+4); nbytes=_u32(z,root+0x0C); rate=_u32(z,root+0x10)
    bits=_u32(z,root+0x14); channels=_u32(z,root+0x18); samples=_u32(z,root+0x1C); block=_u32(z,root+0x20); datap=_u32(z,root+0x28)
    if nbytes > 0x7FFFFFFF: raise ValueError('LoadedSound payload too large')
    st.cursor=root+LOADED_SOUND
    loaded=LoadedSound(root,None,fmt,nbytes,rate,bits,channels,samples,block,b'')
    # LoadedSound root lives in TEMP; it does not allocate a normal/block-4 reusable alias cell.
    name_box={'v':None}
    val=_read_large_string(st,namep,root,lambda v:name_box.__setitem__('v',v),'LoadedSound name')
    loaded.name=val if val is not None else name_box['v']
    p=decode_pc_pointer(datap)
    if nbytes:
        if p.kind in ('following','insert'):
            _ensure(z,st.cursor,nbytes,'LoadedSound payload'); payload=z[st.cursor:st.cursor+nbytes]
            st.alloc_alias(st.cursor,LargeKind.DATA_ALIAS,payload); st.cursor += nbytes; loaded.payload=payload
        elif p.kind=='packed':
            if p.block!=4: raise ValueError('LoadedSound data packed outside block4')
            expected=int(nbytes)
            def assign(v):
                b=bytes(v)
                if len(b)!=expected: raise ValueError('LoadedSound packed payload size mismatch')
                loaded.payload=b
            st.refs.append(PackedRef(datap,root+0x28,LargeKind.DATA_ALIAS,assign,'LoadedSound data',expected))
        else: raise ValueError('LoadedSound has bytes but no data pointer')
    return loaded


def _parse_curve(st: ParseState) -> SoundCurve:
    z=st.zone; root=st.cursor; _ensure(z,root,SND_CURVE,'SndCurve')
    namep=_u32(z,root); count=_u32(z,root+4)
    if count>8: raise ValueError('SndCurve knot count >8')
    knots=tuple(_f32(z,root+8+i*4) for i in range(16))
    if any(not math.isfinite(x) for x in knots):raise ValueError('SndCurve non-finite knot')
    st.cursor=root+SND_CURVE; curve=SoundCurve(root,None,count,knots)
    # SndCurve root lives in TEMP; only its filename participates in the normal/block-4 stream.
    val=_read_large_string(st,namep,root,lambda v:setattr(curve,'name',v),'SndCurve name')
    if val is not None: curve.name=val
    st.inline_curve_roots.add(root); return curve


def _parse_speaker(st: ParseState) -> SpeakerMap:
    z=st.zone; root=st.cursor; _ensure(z,root,SPEAKER_MAP,'SpeakerMap')
    if z[root]>1 or z[root+1:root+4]!=b'\0\0\0': raise ValueError('SpeakerMap header invalid')
    _validate_speaker(z,root); namep=_u32(z,root+4); words=tuple(_u32(z,root+8+i*4) for i in range((SPEAKER_MAP-8)//4))
    st.cursor=root+SPEAKER_MAP; sm=SpeakerMap(root,bool(z[root]),None,words); st.alloc(root,st.cursor,4,LargeKind.SPEAKER,sm)
    val=_read_large_string(st,namep,root+4,lambda v:setattr(sm,'name',v),'SpeakerMap name')
    if val is not None: sm.name=val
    return sm


def _parse_soundfile(st: ParseState) -> SoundFile:
    z=st.zone;root=st.cursor
    if st.layout==SoundFileLayout.LEGACY_0C:
        size=SOUNDFILE_LEGACY;_ensure(z,root,size,'SoundFile legacy');typ=z[root];exists=z[root+1]
        if typ>SoundFileType.STREAMED or exists>1 or z[root+2:root+4]!=b'\0\0':raise ValueError('legacy SoundFile header invalid')
    else:
        size=SOUNDFILE_EXTENDED;_ensure(z,root,size,'SoundFile extended');typ=z[root];exists=z[root+0x1C]
        if typ>SoundFileType.PRIMED or exists>1:raise ValueError('extended SoundFile header invalid')
    a=_u32(z,root+4);b=_u32(z,root+8);st.cursor=root+size;st.inline_soundfiles+=1
    f=SoundFile(root,SoundFileType(typ),bool(exists),a,b)
    setattr(f,'source_layout',st.layout.value); st.alloc(root,st.cursor,4,LargeKind.SOUNDFILE,f)
    if f.type in (SoundFileType.STREAMED,SoundFileType.PRIMED):
        if f.type==SoundFileType.PRIMED and st.layout!=SoundFileLayout.EXTENDED_20:raise ValueError('Primed only valid in extended layout')
        v=_read_large_string(st,a,root+4,lambda x:setattr(f,'stream_directory',x),'StreamedSound.dir');
        if v is not None:f.stream_directory=v
        v=_read_large_string(st,b,root+8,lambda x:setattr(f,'stream_name',x),'StreamedSound.name');
        if v is not None:f.stream_name=v
    elif f.type==SoundFileType.LOADED:
        p=decode_pc_pointer(a)
        if p.kind in ('following','insert'):f.loaded_sound=_parse_loaded(st)
        elif p.kind=='packed':
            if p.block!=4:raise ValueError('LoadedSound root packed outside block4')
            # Packed LoadedSound is a TEMP-XAsset/shared reference, not a Sound-local block-4 member.
            # Preserve the raw identity for the global/shared binding stage; do not contaminate the local basis proof.
            setattr(f,'packed_loaded_pointer',a)
        elif p.kind!='null':raise ValueError('Loaded SoundFile unsupported pointer')
    return f


def _populate_alias_children(st: ParseState, aliases: Sequence[SoundAlias]) -> None:
    for a in aliases:
        raw=a.raw_words
        for idx,attr,desc in ((0,'alias_name','aliasName'),(1,'subtitle','subtitle'),(2,'secondary_alias_name','secondaryAliasName'),(3,'chain_alias_name','chainAliasName')):
            v=_read_large_string(st,raw[idx],a.head_offset+idx*4,lambda x,aa=a,at=attr:setattr(aa,at,x),'SoundAlias.'+desc)
            if v is not None:setattr(a,attr,v)
        p=decode_pc_pointer(raw[4])
        if p.kind=='following':a.sound_file=_parse_soundfile(st)
        elif p.kind=='packed':
            if p.block!=4:raise ValueError('SoundFile packed outside block4')
            st.refs.append(PackedRef(raw[4],a.head_offset+16,LargeKind.SOUNDFILE,lambda v,aa=a:setattr(aa,'sound_file',v),'SoundAlias.soundFile'))
        elif p.kind!='null':raise ValueError('unsupported SoundFile pointer')
        p=decode_pc_pointer(raw[18])
        if p.kind in ('following','insert'):a.volume_curve=_parse_curve(st)
        elif p.kind=='packed':
            if p.block!=4:raise ValueError('SndCurve packed outside block4')
            # Packed SndCurve is a TEMP-XAsset/shared reference. It is resolved separately from the
            # normal-block Head/SoundFile/SpeakerMap ledger (FIX111/FIX113 semantics).
            setattr(a,'packed_curve_pointer',raw[18])
        elif p.kind!='null':raise ValueError('unsupported SndCurve pointer')
        p=decode_pc_pointer(raw[22])
        if p.kind=='following':a.speaker_map=_parse_speaker(st)
        elif p.kind=='packed':
            if p.block!=4:raise ValueError('SpeakerMap packed outside block4')
            st.refs.append(PackedRef(raw[22],a.head_offset+88,LargeKind.SPEAKER,lambda v,aa=a:setattr(aa,'speaker_map',v),'SoundAlias.speakerMap'))
        elif p.kind!='null':raise ValueError('unsupported SpeakerMap pointer')


def _read_alias_array(z: bytes, start: int, count: int) -> list[SoundAlias]:
    _ensure(z,start,count*ALIAS,'alias heads');out=[]
    for i in range(count):
        root=start+i*ALIAS;words=tuple(_u32(z,root+j*4) for j in range(23));out.append(SoundAlias(root,words))
    return out


def _parse_list(st: ParseState, asset_index: int, ordinal: int) -> SoundAliasList:
    z=st.zone;root=st.cursor;_ensure(z,root,ALIAS_LIST,'snd_alias_list_t')
    namep=_u32(z,root);headp=_u32(z,root+4);count=_u32(z,root+8)
    if count>100_000:raise ValueError('implausible SoundAlias count')
    st.cursor=root+ALIAS_LIST;name=''; aliases:list[SoundAlias]=[]
    p=decode_pc_pointer(namep)
    if p.kind=='following':name,_= _read_text(z,st.cursor); start=st.cursor; name,st.cursor=_read_text(z,start);st.alloc(start,st.cursor,1,LargeKind.STRING,name)
    elif p.kind=='packed':
        if p.block!=4:raise ValueError('list name packed outside block4')
        holder={'v':''};st.refs.append(PackedRef(namep,root,LargeKind.STRING,lambda v:holder.__setitem__('v',str(v)),'sound list name'))
        setattr_holder=holder
    else:raise ValueError('top-level Sound list name unsupported')
    hp=decode_pc_pointer(headp)
    if count==0 and hp.kind!='null':raise ValueError('empty list non-null head')
    if count and hp.kind not in ('following','packed'):raise ValueError('non-empty list unsupported head')
    if count and hp.kind=='following':
        hs=st.cursor;aliases=_read_alias_array(z,hs,count);st.cursor += count*ALIAS;st.alloc(hs,st.cursor,4,LargeKind.HEAD,aliases);_populate_alias_children(st,aliases)
    elif count:
        if hp.block!=4:raise ValueError('packed head outside block4')
        def assign(v):
            src=list(v)
            if len(src)<count:raise ValueError('packed head shorter than list count')
            aliases.extend(src[:count])
        st.refs.append(PackedRef(headp,root+4,LargeKind.HEAD,assign,f'Sound XAsset #{asset_index} reusable head',int(count)))
    lst=SoundAliasList(ordinal,asset_index,root,name,tuple(aliases))
    setattr(lst,'_alias_buffer',aliases)
    setattr(lst,'serialized_name_pointer',namep);setattr(lst,'physical_end_offset',st.cursor)
    if p.kind=='packed':
        # replace holder callback now that list exists
        ref=st.refs[-1] if st.refs and st.refs[-1].description=='sound list name' else None
        # Search backwards robustly because head ref may have been appended after name ref.
        for rr in reversed(st.refs):
            if rr.source_physical==root and rr.description=='sound list name':
                rr.assign=lambda v,ll=lst:setattr(ll,'name',str(v));break
    return lst


def _parse_canonical_head(st: ParseState, count: int) -> list[SoundAlias]:
    if not 0<count<=100_000:raise ValueError('canonical head count invalid')
    hs=st.cursor;aliases=_read_alias_array(st.zone,hs,count);st.cursor += count*ALIAS
    st.alloc(hs,st.cursor,4,LargeKind.HEAD,aliases);_populate_alias_children(st,aliases);return aliases


def _phase_allocs(st: ParseState, phase: int) -> tuple[list[PhaseAllocation],int]:
    cur=0;out=[]
    for a in st.allocations:
        alignment=1 if a.kind==LargeKind.STRING else 4
        if alignment>1:
            low=(phase+cur)&(alignment-1)
            if low:cur += alignment-low
        out.append(PhaseAllocation(cur,a));cur += a.logical_bytes
    return out,cur


def _resolve_refs(st: ParseState) -> int | None:
    if not st.refs:return None
    if st.large_offsets_absolute:
        unresolved=[]
        for r in st.refs:
            p=decode_pc_pointer(r.raw)
            if p.kind!='packed' or p.block!=4:raise ValueError(f'{r.description} not a block4 packed pointer')
            matches=[a for a in st.allocations if a.local_offset==p.offset and a.kind==r.kind]
            if len(matches)==1:r.assign(matches[0].value);r.resolved=True;r.resolved_value=matches[0].value
            else:unresolved.append(r)
        if unresolved:raise ValueError(f'{len(unresolved)}/{len(st.refs)} absolute Sound refs unresolved; first={unresolved[0].description}')
        return 0
    for r in st.refs:
        p=decode_pc_pointer(r.raw)
        if p.kind!='packed' or p.block!=4:raise ValueError(f'{r.description} packed outside block4')
    structural=[r for r in st.refs if r.kind!=LargeKind.STRING]
    if not structural:
        # no structural basis; strings remain external/shared by construction
        st.external_strings=sum(1 for r in st.refs if r.kind==LargeKind.STRING);return None
    passing=[]
    for phase in range(4):
        pa,end=_phase_allocs(st,phase);bases=set()
        for r in structural:
            off=decode_pc_pointer(r.raw).offset
            for x in pa:
                if x.allocation.kind==r.kind and off is not None and off>=x.local_offset:
                    b=off-x.local_offset
                    if (b&3)==phase:bases.add(b)
        for basis in bases:
            ok=True
            for r in structural:
                off=decode_pc_pointer(r.raw).offset
                if off is None or off<basis:ok=False;break
                local=off-basis
                if sum(1 for x in pa if x.local_offset==local and x.allocation.kind==r.kind)!=1:ok=False;break
            if ok:passing.append((basis,phase,pa,end))
    if not passing:raise ValueError(f'no structurally exact Sound block4 phase/basis for {len(structural)} refs')
    for r in structural:
        target=None
        for basis,phase,pa,end in passing:
            off=decode_pc_pointer(r.raw).offset;local=off-basis
            ms=[x.allocation for x in pa if x.local_offset==local and x.allocation.kind==r.kind]
            if len(ms)!=1:raise ValueError('structural ref lost uniqueness')
            if target is None:target=ms[0]
            elif target is not ms[0]:raise ValueError('structurally exact phases are not semantically equivalent')
        r.assign(target.value);r.resolved=True;r.resolved_value=target.value
    consensus=external=0
    for r in [x for x in st.refs if x.kind==LargeKind.STRING]:
        target=None;unanimous=True
        for basis,phase,pa,end in passing:
            off=decode_pc_pointer(r.raw).offset
            if off is None or off<basis:unanimous=False;break
            local=off-basis;ms=[x.allocation for x in pa if x.local_offset==local and x.allocation.kind==LargeKind.STRING]
            if len(ms)!=1:unanimous=False;break
            if target is None:target=ms[0]
            elif target is not ms[0]:unanimous=False;break
        if unanimous and target is not None:r.assign(target.value);r.resolved=True;r.resolved_value=target.value;consensus+=1
        else:external+=1
    rep=sorted(passing,key=lambda x:(x[1],x[0]))[0];st.resolved_basis=rep[0];st.resolved_phase=rep[1]
    st.equivalent_structural_phases=len(passing);st.resolved_internal_strings=consensus;st.external_strings=external
    return rep[0]


def _measure_string(z:bytes,raw:int,pc:int,lc:int,block4:int)->tuple[bool,int,int]:
    p=decode_pc_pointer(raw)
    if p.kind=='null':return True,pc,lc
    if p.kind=='packed':return (p.block==4 and p.offset is not None and p.offset<block4),pc,lc
    if p.kind!='following':return False,pc,lc
    try:v,end=_read_text(z,pc)
    except Exception:return False,pc,lc
    n=end-pc;return (lc+n<=block4),end,lc+n

def _measure_loaded(z:bytes,pc:int,lc:int,block4:int)->tuple[bool,int,int]:
    if pc<0 or pc+LOADED_SOUND>len(z):return False,pc,lc
    namep=_u32(z,pc);n=_u32(z,pc+0x0C);datap=_u32(z,pc+0x28);pc+=LOADED_SOUND;lc=_align(lc,4)+4
    if lc>block4:return False,pc,lc
    ok,pc,lc=_measure_string(z,namep,pc,lc,block4)
    if not ok:return False,pc,lc
    if not n:return True,pc,lc
    p=decode_pc_pointer(datap)
    if p.kind in ('following','insert'):
        lc=_align(lc,4)+4
        if lc>block4 or pc+n>len(z):return False,pc,lc
        return True,pc+n,lc
    return (p.kind=='packed' and p.block==4 and p.offset is not None and p.offset<block4),pc,lc

def _measure_curve(z:bytes,pc:int,lc:int,block4:int)->tuple[bool,int,int]:
    if pc<0 or pc+SND_CURVE>len(z) or _u32(z,pc+4)>8:return False,pc,lc
    if any(not math.isfinite(_f32(z,pc+8+i*4)) for i in range(16)):return False,pc,lc
    namep=_u32(z,pc);pc+=SND_CURVE;lc=_align(lc,4)+4
    if lc>block4:return False,pc,lc
    return _measure_string(z,namep,pc,lc,block4)

def _measure_speaker(z:bytes,pc:int,lc:int,block4:int)->tuple[bool,int,int]:
    if pc<0 or pc+SPEAKER_MAP>len(z) or z[pc]>1 or z[pc+1:pc+4]!=b'\0\0\0':return False,pc,lc
    try:_validate_speaker(z,pc)
    except Exception:return False,pc,lc
    namep=_u32(z,pc+4);pc+=SPEAKER_MAP;lc=_align(lc,4)+SPEAKER_MAP
    if lc>block4:return False,pc,lc
    return _measure_string(z,namep,pc,lc,block4)

def _measure_soundfile(z:bytes,layout:SoundFileLayout,pc:int,lc:int,block4:int)->tuple[bool,int,int]:
    root=pc
    if layout==SoundFileLayout.LEGACY_0C:
        size=0x0C
        if root+size>len(z):return False,pc,lc
        typ=z[root];exists=z[root+1]
        if typ>2 or exists>1 or z[root+2:root+4]!=b'\0\0':return False,pc,lc
    else:
        size=0x20
        if root+size>len(z):return False,pc,lc
        typ=z[root];exists=z[root+0x1C]
        if typ>3 or exists>1:return False,pc,lc
    a=_u32(z,root+4);b=_u32(z,root+8);pc=root+size;lc=_align(lc,4)+size
    if lc>block4:return False,pc,lc
    if typ in (2,3):
        ok,pc,lc=_measure_string(z,a,pc,lc,block4)
        if not ok:return False,pc,lc
        return _measure_string(z,b,pc,lc,block4)
    if typ==1:
        p=decode_pc_pointer(a)
        if p.kind in ('following','insert'):return _measure_loaded(z,pc,lc,block4)
        return (p.kind in ('null','packed') and (p.kind!='packed' or (p.block==4 and p.offset<block4))),pc,lc
    return True,pc,lc

def _measure_head(z:bytes,physical:int,count:int,logical:int,layout:SoundFileLayout,block4:int)->tuple[bool,int,int]:
    if count<=0 or count>4096 or physical<0 or physical+count*ALIAS>len(z):return False,physical,logical
    for i in range(count):
        if not looks_alias(z,physical+i*ALIAS,block4):return False,physical,logical
    pc=physical+count*ALIAS;lc=_align(logical,4)+count*ALIAS
    if lc>block4:return False,pc,lc
    for i in range(count):
        root=physical+i*ALIAS
        for off in (0,4,8,12):
            ok,pc,lc=_measure_string(z,_u32(z,root+off),pc,lc,block4)
            if not ok:return False,pc,lc
        p=decode_pc_pointer(_u32(z,root+16))
        if p.kind=='following':
            ok,pc,lc=_measure_soundfile(z,layout,pc,lc,block4)
            if not ok:return False,pc,lc
        elif p.kind=='packed':
            if p.block!=4 or p.offset>=block4:return False,pc,lc
        elif p.kind!='null':return False,pc,lc
        p=decode_pc_pointer(_u32(z,root+72))
        if p.kind in ('following','insert'):
            ok,pc,lc=_measure_curve(z,pc,lc,block4)
            if not ok:return False,pc,lc
        elif p.kind=='packed':
            if p.block!=4 or p.offset>=block4:return False,pc,lc
        elif p.kind!='null':return False,pc,lc
        p=decode_pc_pointer(_u32(z,root+88))
        if p.kind=='following':
            ok,pc,lc=_measure_speaker(z,pc,lc,block4)
            if not ok:return False,pc,lc
        elif p.kind=='packed':
            if p.block!=4 or p.offset>=block4:return False,pc,lc
        elif p.kind!='null':return False,pc,lc
    return True,pc,lc


def _head_descriptors(st:ParseState)->list[tuple[int,int]]:
    grouped={}
    for r in st.refs:
        if r.kind==LargeKind.HEAD and r.expected_count:
            p=decode_pc_pointer(r.raw);grouped[p.offset]=max(grouped.get(p.offset,0),r.expected_count)
    return sorted((int(k),v) for k,v in grouped.items())


def _try_replay_heads(shell:ParseState,heads:list[tuple[int,int]],first_physical:int,shell_start:int,block4:int)->ParseState|None:
    aggregate=ParseState(shell.zone,first_physical,shell.layout,heads[0][0],large_offsets_absolute=True)
    search_from=first_physical
    for i,(logical,count) in enumerate(heads):
        next_log=heads[i+1][0] if i+1<len(heads) else None
        if i==0:candidate=first_physical
        else:
            delta=max(0,(next_log-logical) if next_log is not None else 0);window=min(0x200000,max(0x10000,delta*8+0x10000));last=min(shell_start,search_from+window)
            candidate=-1
            for p in _iter_alias_candidates(shell.zone,search_from,last,block4):
                ok,pe,le=_measure_head(shell.zone,p,count,logical,shell.layout,block4)
                if not ok:continue
                if next_log is not None and _align(le,4)!=next_log:continue
                if candidate>=0 and next_log is None:return None
                candidate=p
                if next_log is not None:break
            if candidate<0 and last<min(shell_start,search_from+0x200000):
                for p in _iter_alias_candidates(shell.zone,last,min(shell_start,search_from+0x200000),block4):
                    ok,pe,le=_measure_head(shell.zone,p,count,logical,shell.layout,block4)
                    if ok and (next_log is None or _align(le,4)==next_log):candidate=p;break
            if candidate<0:return None
        ok,pe,le=_measure_head(shell.zone,candidate,count,logical,shell.layout,block4)
        if not ok or (next_log is not None and _align(le,4)!=next_log):return None
        trial=ParseState(shell.zone,candidate,shell.layout,logical,large_offsets_absolute=True)
        try:_parse_canonical_head(trial,count)
        except Exception:return None
        if next_log is not None and _align(trial.large_cursor,4)>next_log:return None
        aggregate.cursor=trial.cursor;aggregate.large_cursor=trial.large_cursor;aggregate.inline_soundfiles+=trial.inline_soundfiles
        aggregate.allocations.extend(trial.allocations);aggregate.refs.extend(trial.refs);aggregate.inline_curve_roots.update(trial.inline_curve_roots);search_from=trial.cursor
    return aggregate


def _discover_canonical(shell:ParseState,shell_start:int,block4:int)->None:
    heads=_head_descriptors(shell)
    if not heads:raise ValueError('packed-only Sound run has no count-bearing head refs')
    first_log,first_count=heads[0];next_log=heads[1][0] if len(heads)>1 else None
    first_bytes=first_count*ALIAS;end=shell_start-first_bytes
    if end<=0x2C:raise ValueError('no pre-shell corridor for canonical heads')
    successes=[]
    # Progressive bounded search first; expand only if exact proof has not been found.
    windows=(2<<20,8<<20,32<<20,None)
    seen=set()
    for win in windows:
        start=max(0x2C,0 if win is None else end-win)
        for p in _iter_alias_candidates(shell.zone,start,end+1,block4):
            if p in seen:continue
            seen.add(p)
            # require whole first array semantic shape
            if any(not looks_alias(shell.zone,p+i*ALIAS,block4) for i in range(first_count)):continue
            ok,pe,le=_measure_head(shell.zone,p,first_count,first_log,shell.layout,block4)
            if not ok or (next_log is not None and _align(le,4)!=next_log):continue
            replay=_try_replay_heads(shell,heads,p,shell_start,block4)
            if replay is not None:
                successes.append(replay)
                if len(successes)>1:break
        if len(successes)==1:break
        if len(successes)>1:break
    if len(successes)!=1:raise ValueError(f'canonical packed Sound replay found {len(successes)} complete corridors')
    can=successes[0]
    if shell.allocations and any(a.kind!=LargeKind.STRING for a in shell.allocations):raise ValueError('canonical recovery mixed with non-string relative allocations')
    shell.allocations.clear();shell.allocations.extend(can.allocations);shell.refs.extend(can.refs);shell.inline_curve_roots.update(can.inline_curve_roots)
    shell.inline_soundfiles += can.inline_soundfiles;shell.large_cursor=can.large_cursor;shell.large_offsets_absolute=True


def _recover_names(lists:Sequence[SoundAliasList])->None:
    for lst in lists:
        if lst.name:
            for a in lst.aliases:
                if a.alias_name is None and decode_pc_pointer(a.raw_words[0]).kind=='packed':a.alias_name=lst.name
    for lst in lists:
        if lst.name:continue
        names={a.alias_name for a in lst.aliases if a.alias_name}
        if len(names)!=1:raise ValueError(f'Sound XAsset #{lst.source_asset_index} packed name has {len(names)} alias candidates')
        lst.name=next(iter(names))



def _absolute_alias_evidence(st:ParseState):
    if st.large_offsets_absolute:
        rows=[PhaseAllocation(a.local_offset,a) for a in st.allocations]
        basis=0
    elif st.resolved_basis is not None and st.resolved_phase>=0:
        rows,_=_phase_allocs(st,st.resolved_phase);basis=st.resolved_basis
    else:
        return (),(),()
    curves={};loaded={};data={}
    for x in rows:
        a=x.allocation;off=basis+x.local_offset
        target=None
        if a.kind==LargeKind.CURVE_ALIAS: target=curves
        elif a.kind==LargeKind.LOADED_ALIAS: target=loaded
        elif a.kind==LargeKind.DATA_ALIAS: target=data
        if target is None:continue
        old=target.get(off)
        if old is not None and old is not a.value and old!=a.value:
            raise ValueError(f'Sound alias block4+0x{off:X} has conflicting typed values')
        target[off]=a.value
    return tuple(sorted(curves.items())),tuple(sorted(loaded.items())),tuple(sorted(data.items()))


def _standalone_curve(zone:bytes,cursor:int,asset_index:int,packed_strings:dict[int,str])->tuple[SoundCurve,int]:
    _ensure(zone,cursor,SND_CURVE,'top-level SndCurve')
    root=cursor;namep=_u32(zone,root);count=_u32(zone,root+4)
    if count>8:raise ValueError(f'SndCurve XAsset #{asset_index} knot count >8')
    knots=tuple(_f32(zone,root+8+i*4) for i in range(16))
    if any(not math.isfinite(x) for x in knots):raise ValueError(f'SndCurve XAsset #{asset_index} non-finite knot')
    cursor=root+SND_CURVE;p=decode_pc_pointer(namep);name=None
    if p.kind=='following':name,cursor=_read_text(zone,cursor)
    elif p.kind=='packed':name=packed_strings.get(namep)
    elif p.kind!='null':raise ValueError(f'SndCurve XAsset #{asset_index} unsupported name pointer {p.kind}')
    return SoundCurve(root,name,count,knots,asset_index),cursor


def _standalone_loaded(zone:bytes,cursor:int,asset_index:int,packed_strings:dict[int,str],packed_data:dict[int,bytes])->tuple[LoadedSound,int]:
    _ensure(zone,cursor,LOADED_SOUND,'top-level LoadedSound')
    root=cursor;namep=_u32(zone,root);fmt=_u32(zone,root+4);nbytes=_u32(zone,root+0x0C);rate=_u32(zone,root+0x10)
    bits=_u32(zone,root+0x14);channels=_u32(zone,root+0x18);samples=_u32(zone,root+0x1C);block=_u32(zone,root+0x20);datap=_u32(zone,root+0x28)
    if nbytes>0x7fffffff:raise ValueError('top-level LoadedSound payload too large')
    cursor=root+LOADED_SOUND;p=decode_pc_pointer(namep);name=None
    if p.kind=='following':name,cursor=_read_text(zone,cursor)
    elif p.kind=='packed':name=packed_strings.get(namep)
    elif p.kind!='null':raise ValueError(f'LoadedSound XAsset #{asset_index} unsupported name pointer {p.kind}')
    payload=b'';dp=decode_pc_pointer(datap)
    if nbytes:
        if dp.kind in ('following','insert'):
            _ensure(zone,cursor,nbytes,'top-level LoadedSound payload');payload=zone[cursor:cursor+nbytes];cursor+=nbytes
        elif dp.kind=='packed':
            payload=packed_data.get(datap,b'')
            if len(payload)!=nbytes:raise ValueError(f'LoadedSound XAsset #{asset_index} packed data has no exact {nbytes}-byte alias evidence')
        else:raise ValueError(f'LoadedSound XAsset #{asset_index} has bytes but {dp.kind} data pointer')
    return LoadedSound(root,name,fmt,nbytes,rate,bits,channels,samples,block,payload,asset_index),cursor


def associate_top_level_sound_children(zone:bytes,asset_list,catalog:SoundCatalog)->SoundCatalog:
    """Attach top-level SndCurve/LoadedSound XAsset indices to the exact objects parsed from Sound.

    IW3's asset writer writes a nested TEMP XAsset the first time it is seen and records a logical
    InsertPointer cell. Its later top-level XAssetHeader is a packed pointer to that cell. That direct
    relation is used here; no XAsset-pool ordinal arithmetic is performed.
    """
    from dataclasses import replace
    curves=[a for a in asset_list.assets if a.type_id==0x08]
    loaded_assets=[a for a in asset_list.assets if a.type_id==0x09]
    if not curves and not loaded_assets:return catalog
    curve_alias=dict(catalog.curve_alias_evidence);loaded_alias=dict(catalog.loaded_alias_evidence);data_alias=dict(catalog.loaded_data_alias_evidence)
    packed_strings=dict(catalog.packed_string_evidence)
    cursor=catalog.stream_end;top_curves=[];top_loaded=[]
    seen_curve_obj={};seen_loaded_obj={}
    for a in curves:
        p=decode_pc_pointer(a.serialized_pointer)
        if p.kind=='packed':
            if p.block!=4 or p.offset not in curve_alias:raise ValueError(f'SndCurve XAsset #{a.index} packed root has no exact TEMP-XAsset alias evidence')
            cv=curve_alias[p.offset]
            prior=seen_curve_obj.get(id(cv))
            if prior is not None and prior!=a.index:raise ValueError(f'SndCurve root is claimed by source XAssets #{prior} and #{a.index}')
            seen_curve_obj[id(cv)]=a.index;cv.source_asset_index=a.index;top_curves.append(cv)
        elif p.kind in ('following','insert'):
            cv,cursor=_standalone_curve(zone,cursor,a.index,packed_strings);top_curves.append(cv)
        else:raise ValueError(f'SndCurve XAsset #{a.index} unsupported top-level pointer {p.kind}')
    packed_data_by_raw={}
    # Alias evidence is keyed by decoded block offset; convert it to the serialized raw words used by roots.
    for off,payload in data_alias.items():
        packed_data_by_raw[((4<<28)|off)+1]=payload
    for a in loaded_assets:
        p=decode_pc_pointer(a.serialized_pointer)
        if p.kind=='packed':
            if p.block!=4 or p.offset not in loaded_alias:raise ValueError(f'LoadedSound XAsset #{a.index} packed root has no exact TEMP-XAsset alias evidence')
            ls=loaded_alias[p.offset]
            prior=seen_loaded_obj.get(id(ls))
            if prior is not None and prior!=a.index:raise ValueError(f'LoadedSound root is claimed by source XAssets #{prior} and #{a.index}')
            seen_loaded_obj[id(ls)]=a.index;ls.source_asset_index=a.index;top_loaded.append(ls)
        elif p.kind in ('following','insert'):
            ls,cursor=_standalone_loaded(zone,cursor,a.index,packed_strings,packed_data_by_raw);top_loaded.append(ls)
        else:raise ValueError(f'LoadedSound XAsset #{a.index} unsupported top-level pointer {p.kind}')
    return replace(catalog,top_level_curves=tuple(top_curves),top_level_loaded_sounds=tuple(top_loaded))

def parse_sound_run_at(zone:bytes,asset_list,start:int,layout:SoundFileLayout)->SoundCatalog:
    sounds=[a for a in asset_list.assets if a.type_id==0x07]
    if not sounds:return SoundCatalog((),layout,start,start,None,0,0,0,0,0)
    block4=parse_zone_header(zone,'pc').block_sizes[4]
    st=ParseState(zone,start,layout);lists=[]
    for ordinal,a in enumerate(sounds):
        root=st.cursor;lst=_parse_list(st,a.index,ordinal);setattr(lst,'physical_end_offset',st.cursor);lists.append(lst)
        if st.cursor<=root:raise ValueError('Sound parser made no physical progress')
    has_packed_heads=any(r.kind==LargeKind.HEAD for r in st.refs);has_owned_head=any(a.kind==LargeKind.HEAD for a in st.allocations)
    if has_packed_heads and not has_owned_head:_discover_canonical(st,start,block4)
    basis=_resolve_refs(st);_recover_names(lists)
    # packed head callbacks mutate the temporary alias list captured by SoundAliasList only if tuple wasn't frozen.
    # Rebuild list tuples after all callbacks have completed.
    for l in lists:
        buffer=getattr(l,'_alias_buffer',None)
        if buffer is not None:
            l.aliases=tuple(buffer)
    resolved=sum(1 for r in st.refs if r.resolved)
    string_evidence={}
    for r in st.refs:
        if r.kind!=LargeKind.STRING or not r.resolved or not isinstance(r.resolved_value,str):
            continue
        previous=string_evidence.get(r.raw)
        if previous is not None and previous!=r.resolved_value:
            raise ValueError(f'packed Sound XString 0x{r.raw:08X} resolved inconsistently: {previous!r} vs {r.resolved_value!r}')
        string_evidence[r.raw]=r.resolved_value
    curves,loaded,data=_absolute_alias_evidence(st)
    return SoundCatalog(tuple(lists),layout,start,st.cursor,basis,len(st.refs),resolved,st.equivalent_structural_phases,st.resolved_internal_strings,st.external_strings,tuple(sorted(string_evidence.items())),curves,loaded,data)


def _trial_parse_prefix(zone:bytes,asset_list,start:int,layout:SoundFileLayout,count:int=4)->bool:
    sounds=[a for a in asset_list.assets if a.type_id==0x07]
    st=ParseState(zone,start,layout)
    try:
        for ordinal,a in enumerate(sounds[:count]):_parse_list(st,a.index,ordinal)
        return True
    except Exception:return False


def _sound_anchor_from_preceding_map_rawfile(zone:bytes,asset_list,map_name:str|None,block4:int)->tuple[int,...]:
    """Return strongly evidenced Sound starts immediately after the map's final pre-Sound RawFile.

    mp_getaway's source XAsset order has RawFile #511 immediately before Sound #512.  Cross-type
    *physical* adjacency is not assumed in general; this fast path activates only when the preceding
    source asset is RawFile and the map-specific ``vision/<map>.vision`` RawFile root can be proved
    byte-for-byte.  Its declared payload end (optionally followed by one C-string terminator byte)
    is then tested as an actual snd_alias_list_t root.  If any part of that proof fails the caller
    falls back to the broad structural scanner.
    """
    if not map_name:return ()
    sounds=[a for a in asset_list.assets if a.type_id==0x07]
    if not sounds:return ()
    first=sounds[0]
    if first.index<=0:return ()
    prior=asset_list.assets[first.index-1]
    if prior.index!=first.index-1 or prior.type_id!=0x1F:return ()
    wanted=f'vision/{map_name}.vision'.encode('latin-1')+b'\0'
    hits=[];pos=zone.find(wanted)
    while pos>=0:
        root=pos-ALIAS_LIST
        if root>=0:
            try:
                if decode_pc_pointer(_u32(zone,root)).kind in ('following','insert') and decode_pc_pointer(_u32(zone,root+8)).kind in ('following','insert'):
                    length=_u32(zone,root+4)
                    payload_start=pos+len(wanted)
                    end=payload_start+length
                    if 0<=length<=16*1024*1024 and end<=len(zone):
                        for candidate in (end,end+1):
                            if candidate<len(zone) and looks_list_root(zone,candidate,block4):hits.append(candidate)
            except (ValueError,IndexError,struct.error):
                pass
        pos=zone.find(wanted,pos+1)
    return tuple(sorted(set(hits)))

def locate_and_parse_sound(zone:bytes,asset_list=None,map_name:str|None=None,progress:Callable[[str],None]|None=None)->SoundCatalog:
    if asset_list is None:asset_list=parse_xasset_list(zone,'pc')
    sounds=[a for a in asset_list.assets if a.type_id==0x07]
    if not sounds:return SoundCatalog((),SoundFileLayout.LEGACY_0C,asset_list.asset_data_offset,asset_list.asset_data_offset,None,0,0,0,0,0)
    if any(decode_pc_pointer(a.serialized_pointer).kind not in ('following','insert') for a in sounds):raise ValueError('top-level Sound XAsset is not inline')
    if asset_list.structural_index is not None:
        rows=asset_list.structural_index.rows(7)
        result=parse_sound_run_at(zone,asset_list,rows[0]['root'],SoundFileLayout.LEGACY_0C)
        actual=[item.root_offset for item in result.lists]
        if actual!=[row['root'] for row in rows]:
            raise ValueError('Sound semantic reader differs from structural asset order')
        return result
    block4=parse_zone_header(zone,'pc').block_sizes[4]

    # FIX105/FIX113-derived fast path: prove the map-specific final RawFile boundary first.
    anchors=_sound_anchor_from_preceding_map_rawfile(zone,asset_list,map_name,block4)
    if anchors:
        if progress:progress(f'Sound scan: preceding RawFile anchor produced {len(anchors)} candidate start(s): '+', '.join(f'0x{x:X}' for x in anchors))
        matches=[];errors=[]
        for r in anchors:
            for layout in (SoundFileLayout.LEGACY_0C,SoundFileLayout.EXTENDED_20):
                try:matches.append(parse_sound_run_at(zone,asset_list,r,layout))
                except Exception as e:errors.append(f'0x{r:X}/{layout.value}: {e}')
        # Same root under two layouts is not two identities if only one can complete the exact graph.
        if len(matches)==1:
            if progress:progress(f'Sound scan PASS (RawFile anchor): start=0x{matches[0].stream_start:X}, layout={matches[0].source_layout.value}, lists={len(matches[0].lists)}')
            return matches[0]
        if len(matches)>1:
            raise ValueError(f'preceding RawFile Sound anchor is layout-ambiguous: {[(hex(x.stream_start),x.source_layout.value) for x in matches]}')
        if progress:progress('Sound RawFile anchor did not complete; falling back to broad structural scan: '+('; '.join(errors[:2])))

    if progress:progress('Sound scan: vector-indexing top-level list roots')
    roots=_vector_list_candidates(zone,asset_list.asset_data_offset,len(zone),block4)
    # Strong ranking: exact packed-only N-root segments first. Prefix trials are deliberately restricted
    # to a small ranked frontier; trial-parsing every coarse root was the old O(zone*candidates) hotspot.
    rootset=set(roots);n=len(sounds);rank=[];seen=set()
    for r in roots:
        if r-ALIAS_LIST in rootset:continue
        x=r;cnt=0
        while x in rootset and cnt<=n:x+=ALIAS_LIST;cnt+=1
        if cnt==n and r not in seen:rank.append(r);seen.add(r)
    # Preserve broad correctness without spending minutes parsing four nested lists at every false root.
    frontier=[r for r in roots if r not in seen][:4096]
    for layout in (SoundFileLayout.LEGACY_0C,SoundFileLayout.EXTENDED_20):
        for r in frontier:
            if r not in seen and _trial_parse_prefix(zone,asset_list,r,layout,4):rank.append(r);seen.add(r)
    if progress:progress(f'Sound scan: {len(roots):,} coarse list roots, {len(rank):,} ranked starts')
    matches=[];errors=[]
    for r in rank:
        for layout in (SoundFileLayout.LEGACY_0C,SoundFileLayout.EXTENDED_20):
            try:
                doc=parse_sound_run_at(zone,asset_list,r,layout);matches.append(doc)
                if len(matches)>1:break
            except Exception as e:
                if len(errors)<12:errors.append(f'0x{r:X}/{layout.value}: {e}')
        if len(matches)>1:break
    if len(matches)!=1:
        raise ValueError(f'PC Sound run resolved {len(matches)} unique traversal(s) for {len(sounds)} XAssets; samples={errors[:4]}')
    if progress:progress(f'Sound scan PASS: start=0x{matches[0].stream_start:X}, layout={matches[0].source_layout.value}, lists={len(matches[0].lists)}')
    return matches[0]
