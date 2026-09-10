from __future__ import annotations
from dataclasses import dataclass
from enum import IntEnum
from typing import Sequence

class SoundFileType(IntEnum):
    UNKNOWN=0; LOADED=1; STREAMED=2; PRIMED=3

@dataclass
class LoadedSound:
    root_offset:int;name:str|None;format:int;payload_bytes:int;sample_rate:int;bits_per_sample:int;channels:int;sample_count:int;block_size:int;payload:bytes;source_asset_index:int=-1
@dataclass
class SoundFile:
    root_offset:int;type:SoundFileType;exists:bool;serialized_a:int=0;serialized_b:int=0;stream_directory:str|None=None;stream_name:str|None=None;loaded_sound:LoadedSound|None=None
@dataclass
class SoundCurve:
    root_offset:int;name:str|None;knot_count:int;knots:tuple[float,...];source_asset_index:int=-1
@dataclass
class SpeakerMap:
    root_offset:int;is_default:bool;name:str|None;channel_map_words:tuple[int,...]
@dataclass
class SoundAlias:
    head_offset:int;raw_words:tuple[int,...];alias_name:str|None=None;subtitle:str|None=None;secondary_alias_name:str|None=None;chain_alias_name:str|None=None;sound_file:SoundFile|None=None;volume_curve:SoundCurve|None=None;speaker_map:SpeakerMap|None=None
@dataclass
class SoundAliasList:
    sound_asset_ordinal:int;source_asset_index:int;root_offset:int;name:str;aliases:tuple[SoundAlias,...]
@dataclass
class SoundFilePlan:
    source:SoundFile;output_type:SoundFileType;loaded_name:str|None;payload:bytes;format:int;sample_count:int;channels:int;sample_rate:int;staged_asset_name:str|None=None;duration_ms:int=0
@dataclass
class SoundAliasPlan:
    source:SoundAlias;sound_file:SoundFilePlan|None
@dataclass
class SoundListPlan:
    source:SoundAliasList;owned_by_map:bool;aliases:tuple[SoundAliasPlan,...]
