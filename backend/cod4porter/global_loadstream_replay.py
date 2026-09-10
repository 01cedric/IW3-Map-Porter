"""Deterministic replay of PC zone load-stream allocation and pointer aliases.

The replay is deliberately evidence-driven. It does not resolve an asset by
name or by nearest address. FOLLOWING pointers allocate the pointee only;
INSERT pointers additionally reserve one persistent pointer-sized alias cell
in the configured alias block. Packed references resolve only through an
already registered object or alias cell.
"""
from __future__ import annotations
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Iterable, Mapping, MutableMapping, Sequence

class ReplayError(ValueError): pass
class PointerKind(str,Enum):
    NULL='null'; FOLLOWING='following'; INSERT='insert'; PACKED='packed'; DIRECT='direct'

def align_up(value:int,alignment:int)->int:
    if value<0: raise ReplayError('negative cursor')
    if alignment<=0 or alignment & (alignment-1): raise ReplayError(f'alignment must be power of two: {alignment}')
    return (value+alignment-1)&~(alignment-1)

@dataclass(frozen=True)
class VirtualAddress:
    block:int; offset:int
    def __post_init__(self):
        if self.block<0 or self.offset<0: raise ReplayError(f'invalid address {self}')

@dataclass(frozen=True)
class Allocation:
    label:str; owner:str; address:VirtualAddress; size:int; alignment:int; kind:str

@dataclass(frozen=True)
class AliasCell:
    label:str; owner:str; address:VirtualAddress; target:VirtualAddress; target_key:str

@dataclass
class Cursor:
    offset:int=0
    limit:int|None=None
    def allocate(self,size:int,alignment:int)->int:
        if size<0: raise ReplayError(f'negative allocation size {size}')
        start=align_up(self.offset,alignment); end=start+size
        if self.limit is not None and end>self.limit: raise ReplayError(f'block overflow: {end}>{self.limit}')
        self.offset=end; return start
    def anchor(self,offset:int,label:str)->None:
        if offset<self.offset: raise ReplayError(f'anchor {label} rewinds cursor {self.offset}->{offset}')
        self.offset=offset

@dataclass
class Replay:
    pointer_size:int=4
    alias_block:int=4
    cursors:MutableMapping[int,Cursor]=field(default_factory=dict)
    allocations:list[Allocation]=field(default_factory=list)
    aliases:list[AliasCell]=field(default_factory=list)
    objects:MutableMapping[VirtualAddress,str]=field(default_factory=dict)
    alias_targets:MutableMapping[VirtualAddress,VirtualAddress]=field(default_factory=dict)
    evidence:MutableMapping[str,list[str]]=field(default_factory=dict)

    def cursor(self,block:int)->Cursor:
        return self.cursors.setdefault(block,Cursor())
    def set_block(self,block:int,offset:int=0,limit:int|None=None)->None:
        if block in self.cursors and self.cursors[block].offset!=offset:
            raise ReplayError(f'block {block} already initialized at {self.cursors[block].offset}')
        self.cursors[block]=Cursor(offset,limit)
    def anchor(self,block:int,offset:int,label:str)->None:
        self.cursor(block).anchor(offset,label); self.evidence.setdefault(label,[]).append(f'anchor b{block}+0x{offset:X}')
    def allocate_object(self,*,block:int,size:int,alignment:int,label:str,owner:str,key:str|None=None,expected_offset:int|None=None,kind:str='object')->VirtualAddress:
        start=self.cursor(block).allocate(size,alignment)
        if expected_offset is not None and start!=expected_offset:
            raise ReplayError(f'{label}: expected b{block}+0x{expected_offset:X}, replayed 0x{start:X}')
        a=VirtualAddress(block,start); k=key or label
        prior=self.objects.get(a)
        if prior is not None and prior!=k: raise ReplayError(f'object collision at {a}: {prior!r} vs {k!r}')
        self.objects[a]=k; self.allocations.append(Allocation(label,owner,a,size,alignment,kind)); return a
    def allocate_following(self,**kwargs:Any)->VirtualAddress:
        # Crucial retail rule: FOLLOWING does not allocate an alias cell.
        return self.allocate_object(kind='following',**kwargs)
    def allocate_insert(self,*,block:int,size:int,alignment:int,label:str,owner:str,key:str|None=None,expected_offset:int|None=None,alias_expected_offset:int|None=None,alias_before_object:bool=True)->tuple[VirtualAddress,VirtualAddress]:
        k=key or label
        if alias_before_object:
            ao=self.cursor(self.alias_block).allocate(self.pointer_size,self.pointer_size)
            if alias_expected_offset is not None and ao!=alias_expected_offset: raise ReplayError(f'{label}: expected alias 0x{alias_expected_offset:X}, replayed 0x{ao:X}')
            target=self.allocate_object(block=block,size=size,alignment=alignment,label=label,owner=owner,key=k,expected_offset=expected_offset,kind='insert')
        else:
            target=self.allocate_object(block=block,size=size,alignment=alignment,label=label,owner=owner,key=k,expected_offset=expected_offset,kind='insert')
            ao=self.cursor(self.alias_block).allocate(self.pointer_size,self.pointer_size)
            if alias_expected_offset is not None and ao!=alias_expected_offset: raise ReplayError(f'{label}: expected alias 0x{alias_expected_offset:X}, replayed 0x{ao:X}')
        alias=VirtualAddress(self.alias_block,ao)
        if alias in self.alias_targets: raise ReplayError(f'duplicate alias cell {alias}')
        self.alias_targets[alias]=target; self.aliases.append(AliasCell(label,owner,alias,target,k)); return target,alias
    def register_existing(self,address:VirtualAddress,key:str,*,evidence:str)->None:
        old=self.objects.get(address)
        if old is not None and old!=key: raise ReplayError(f'conflicting object evidence at {address}: {old!r}/{key!r}')
        self.objects[address]=key; self.evidence.setdefault(key,[]).append(evidence)
    def register_alias(self,alias:VirtualAddress,target:VirtualAddress,key:str,*,owner:str,evidence:str)->None:
        if alias in self.alias_targets and self.alias_targets[alias]!=target: raise ReplayError(f'conflicting alias evidence at {alias}')
        self.alias_targets[alias]=target; self.aliases.append(AliasCell(evidence,owner,alias,target,key)); self.evidence.setdefault(key,[]).append(evidence)
    def resolve(self,address:VirtualAddress)->tuple[VirtualAddress,str]:
        seen=set(); cur=address
        while cur in self.alias_targets:
            if cur in seen: raise ReplayError(f'alias cycle at {cur}')
            seen.add(cur); cur=self.alias_targets[cur]
        key=self.objects.get(cur)
        if key is None: raise ReplayError(f'unresolved packed target b{address.block}+0x{address.offset:X}')
        return cur,key
    def try_resolve(self,address:VirtualAddress)->tuple[VirtualAddress,str]|None:
        try:return self.resolve(address)
        except ReplayError:return None
    def assert_exact_anchor(self,*,block:int,expected:int,label:str)->None:
        got=self.cursor(block).offset
        if got!=expected: raise ReplayError(f'{label}: replay cursor b{block}=0x{got:X}, expected 0x{expected:X}, delta={got-expected}')
    def report(self)->dict[str,Any]:
        return {'pointer_size':self.pointer_size,'alias_block':self.alias_block,'blocks':{str(k):v.offset for k,v in sorted(self.cursors.items())},'allocation_count':len(self.allocations),'alias_cell_count':len(self.aliases),'object_count':len(self.objects),'unambiguous_aliases':len(self.alias_targets)}

def replay_events(events:Sequence[Mapping[str,Any]],*,initial_offsets:Mapping[int,int]|None=None,pointer_size:int=4,alias_block:int=4)->Replay:
    r=Replay(pointer_size=pointer_size,alias_block=alias_block)
    for b,o in (initial_offsets or {}).items(): r.set_block(int(b),int(o))
    for i,e in enumerate(events):
        op=str(e.get('op','')).lower(); label=str(e.get('label',f'event[{i}]')); owner=str(e.get('owner','<unknown>'))
        if op=='anchor': r.anchor(int(e['block']),int(e['offset']),label)
        elif op in {'following','object'}:
            r.allocate_following(block=int(e['block']),size=int(e['size']),alignment=int(e.get('alignment',1)),label=label,owner=owner,key=e.get('key'),expected_offset=e.get('expected_offset'))
        elif op=='insert':
            r.allocate_insert(block=int(e['block']),size=int(e['size']),alignment=int(e.get('alignment',1)),label=label,owner=owner,key=e.get('key'),expected_offset=e.get('expected_offset'),alias_expected_offset=e.get('alias_expected_offset'),alias_before_object=bool(e.get('alias_before_object',True)))
        else: raise ReplayError(f'{label}: unknown replay op {op!r}')
    return r
