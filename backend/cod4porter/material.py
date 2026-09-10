from __future__ import annotations
from dataclasses import dataclass,field
from typing import Sequence,Mapping
import struct,math
from .backend.v4_ffio import decode_pc_pointer

PC_TO_PS3_SLOTS=(0,1,2,3,4,5,6,7,8,9,10,11,12,13,21,22,23,24,25,26,27,28,29,30,31,32)

@dataclass(frozen=True)
class MaterialTexture:
    # ``semantic`` is the MaterialTextureDef *slot* role.  ``inline_image_semantic`` is
    # the byte at +0x0B of the owning PC GfxImage root, i.e. the image's own semantic.
    # They agree for 1019 of mp_getaway's 1023 inline bindings; where they differ the
    # image is shared by two slots with different sampling roles and only the owner is
    # authoritative for the GfxImage asset that gets written.
    name_hash:int;name_start:int;name_end:int;sampler:int;semantic:int;resource_pointer:int;inline_image_name:str|None=None;inline_image_root:int|None=None;inline_image_semantic:int|None=None
@dataclass(frozen=True)
class WaterComplex: real:float;imaginary:float
@dataclass(frozen=True)
class MaterialWater:
    texture_index:int;float_time:float;h0_pointer:int;wterm_pointer:int;m:int;n:int;scalars:tuple[float,...];h0:tuple[WaterComplex,...];wterm:tuple[float,...];image_pointer:int;setup_image_name:str|None=None;setup_image_root:int|None=None
@dataclass(frozen=True)
class MaterialRecord:
    root_offset:int;name:str;game_flags:int;sort_key:int;atlas_rows:int;atlas_cols:int;draw_surf:int;surface_type_bits:int;hash_index:int
    state_bits_entry:bytes;declared_state_bits_count:int;serialized_state_bits_pointer:int;state_bits_resolution_kind:str
    texture_count:int;constant_count:int;state_flags:int;camera_region:int;technique_set_pointer:int;textures:tuple[MaterialTexture,...];constants:tuple[bytes,...];state_bits:tuple[tuple[int,int],...];water_by_texture:Mapping[int,MaterialWater]=field(default_factory=dict)
    @property
    def contains_water(self):return bool(self.water_by_texture)

@dataclass(frozen=True)
class MaterialStateConversion:
    state_bits_entry:bytes;state_bits:tuple[tuple[int,int],...];dropped_pc_slot_refs:int;invalid_pc_state_indices:int;neutralized_out_of_range:int

def convert_state(record:MaterialRecord, technique_pointers:Sequence[int]|None=None)->MaterialStateConversion:
    if len(record.state_bits_entry)!=34:raise ValueError('PC material stateBitsEntry must be 34 bytes')
    out=bytearray([0xFF]*26);states=[];index={};inactive=0
    def map_pair(pair):
        if pair in index:return index[pair]
        if len(states)>=255:raise ValueError('state table exceeds uint8')
        i=len(states);states.append(pair);index[pair]=i;return i
    for ps3slot,pcslot in enumerate(PC_TO_PS3_SLOTS):
        pcidx=record.state_bits_entry[pcslot]
        if pcidx==0xFF:continue
        if pcidx>=len(record.state_bits):
            p=decode_pc_pointer(record.serialized_state_bits_pointer)
            absent=record.declared_state_bits_count==0 and len(record.state_bits)==0 and p.kind=='null'
            if absent:inactive+=1;continue
            if technique_pointers is None or len(technique_pointers)!=34:
                raise ValueError(f"Material '{record.name}' PC slot {pcslot} state #{pcidx} outside table and TechniqueSet activity unavailable")
            if decode_pc_pointer(int(technique_pointers[pcslot])).kind!='null':
                raise ValueError(f"Material '{record.name}' active slot {pcslot} references missing state #{pcidx}")
            inactive+=1;continue
        out[ps3slot]=map_pair(record.state_bits[pcidx])
    dropped=sum(1 for i in range(14,21) if record.state_bits_entry[i]!=0xFF)+(1 if record.state_bits_entry[33]!=0xFF else 0)+inactive
    return MaterialStateConversion(bytes(out),tuple(states),dropped,0,0)
