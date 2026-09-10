from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import hashlib
import subprocess
import tempfile
import zipfile
import json
from typing import Iterable, Sequence

from .sound import SoundAliasPlan, SoundFilePlan, SoundFileType, SoundListPlan
from .pc_sound import SoundCatalog
from .backend.v4_iwd import IwdSoundEntry, normalize_sound_asset

@dataclass(frozen=True)
class StagedSound:
    asset_name: str
    payload: bytes
    format: int
    sample_count: int
    channels: int
    sample_rate: int
    source_sha256: str
    staged_sha256: str
    duration_ms: int = 0

@dataclass(frozen=True)
class SoundBindingReport:
    lists: tuple[SoundListPlan,...]
    owned_lists: tuple[SoundListPlan,...]
    external_native_names: tuple[str,...]
    source_list_count: int
    source_alias_count: int
    owned_list_count: int
    external_native_count: int
    custom_iwd_references: int
    inline_loaded_references: int
    staged_payload_count: int
    runtime_null_soundfile_references: int
    unresolved_owned: tuple[str,...]
    unreferenced_staged: tuple[str,...]
    @property
    def passed(self) -> bool:
        # Unreferenced files in an IWD are packaging surplus, not an unresolved SoundAliasList.
        # Runtime correctness requires every owned Sound reference to resolve; unused archive
        # members are reported but must not force a false failure.
        return not self.unresolved_owned


def _source_bytes(entry:IwdSoundEntry) -> bytes:
    with zipfile.ZipFile(entry.archive_path,'r') as z:
        return z.read(entry.entry_name)

def _run_ffmpeg(input_bytes:bytes,suffix:str,ffmpeg:str='ffmpeg')->bytes:
    # Retail PS3 IW3 LoadedSound payloads observed in mp_shipment use MPEG audio
    # preceded by ID3v2.3, not ID3v2.4.  Encode to a conservative 96 kbit/s
    # profile matching the measured retail LoadedSound population.
    with tempfile.TemporaryDirectory(prefix='cod4porter-sound-') as td:
        inp=Path(td)/('in'+suffix);out=Path(td)/'out.mp3';inp.write_bytes(input_bytes)
        proc=subprocess.run([ffmpeg,'-hide_banner','-loglevel','error','-y','-i',str(inp),'-vn','-acodec','libmp3lame','-b:a','96k','-id3v2_version','3',str(out)],capture_output=True,text=True)
        if proc.returncode or not out.exists():raise ValueError(f'ffmpeg staging failed: {proc.stderr.strip()}')
        b=out.read_bytes()
        if not b.startswith(b'ID3\x03\x00'):raise ValueError('ffmpeg PS3 payload is not ID3v2.3')
        return b

def _probe_mp3_duration_ms(payload:bytes,ffprobe:str='ffprobe')->int:
    # Shipment proves LoadedSound +0x0C is rounded playback duration in
    # milliseconds (e.g. header 840 vs ffprobe 839.927 ms), not sample count.
    with tempfile.TemporaryDirectory(prefix='cod4porter-probe-') as td:
        p=Path(td)/'probe.mp3';p.write_bytes(payload)
        proc=subprocess.run([ffprobe,'-v','error','-show_entries','format=duration','-of','json',str(p)],capture_output=True,text=True)
        if proc.returncode:raise ValueError(f'ffprobe duration failed: {proc.stderr.strip()}')
        try:duration=float(json.loads(proc.stdout)['format']['duration'])
        except Exception as exc:raise ValueError('ffprobe returned no MP3 duration') from exc
        ms=int(round(duration*1000.0))
        if ms<=0 or ms>0xFFFFFFFF:raise ValueError(f'invalid MP3 duration {ms} ms')
        return ms

def _synchsafe32(value:int)->bytes:
    if not 0<=value<0x10000000:raise ValueError('ID3 synchsafe size out of range')
    return bytes(((value>>21)&0x7f,(value>>14)&0x7f,(value>>7)&0x7f,value&0x7f))

def _id3_audio_start(data:bytes)->int:
    if len(data)>=10 and data[:3]==b'ID3':
        size=((data[6]&0x7f)<<21)|((data[7]&0x7f)<<14)|((data[8]&0x7f)<<7)|(data[9]&0x7f)
        start=10+size
        if start>len(data):raise ValueError('truncated ID3 tag')
        return start
    return 0

def _scan_mp3_frames(data:bytes)->tuple[bytes,tuple[int,...],int,int,int]:
    # Return a contiguous MP3 audio frame stream, frame offsets relative to its
    # beginning, total decoded samples, channel count and sample rate.  The
    # arithmetic mirrors the MPEG Layer III headers used by retail Shipment.
    cursor=_id3_audio_start(data);rates=[44100,48000,32000];br1=[0,32,40,48,56,64,80,96,112,128,160,192,224,256,320,0];br2=[0,8,16,24,32,40,48,56,64,80,96,112,128,144,160,0]
    first=None;offsets=[];total_samples=0;det_rate=det_ch=None;last_end=None
    while cursor<=len(data)-4:
        h=int.from_bytes(data[cursor:cursor+4],'big')
        if h&0xFFE00000!=0xFFE00000:
            if first is None:cursor+=1;continue
            break
        ver=(h>>19)&3;layer=(h>>17)&3;bri=(h>>12)&0xf;sri=(h>>10)&3;pad=(h>>9)&1
        if ver==1 or layer!=1 or bri in (0,15) or sri==3:
            if first is None:cursor+=1;continue
            break
        mpeg1=ver==3;rate=rates[sri]
        if ver==2:rate//=2
        elif ver==0:rate//=4
        kbps=(br1 if mpeg1 else br2)[bri];frame_len=(144000*kbps)//rate+pad if mpeg1 else (72000*kbps)//rate+pad
        if frame_len<4 or cursor+frame_len>len(data):break
        ch=1 if ((h>>6)&3)==3 else 2
        if first is None:first=cursor;det_rate=rate;det_ch=ch
        elif det_rate!=rate or det_ch!=ch:raise ValueError('MP3 changes channel/rate inside LoadedSound')
        offsets.append(cursor-first);total_samples+=1152 if mpeg1 else 576;cursor+=frame_len;last_end=cursor
    if first is None or not offsets or last_end is None:raise ValueError('MP3 contains no contiguous Layer III frames')
    return bytes(data[first:last_end]),tuple(offsets),total_samples,int(det_ch),int(det_rate)

def _make_ps3_loaded_mp3(encoded:bytes)->tuple[bytes,int,int,int,int]:
    # Retail mp_shipment PS3 IW3 LoadedSound contract:
    #   ID3v2.3 containing exactly one PRIV frame; PRIV payload is
    #   BE u32 totalDecodedSamples, BE u32 frameCount, then one BE u32
    #   byte offset for every MP3 frame. The complete ID3 region is padded
    #   to a 0x80-byte boundary. LoadedSound.duration is floor(samples*1000/rate).
    audio,offsets,samples,channels,rate=_scan_mp3_frames(encoded)
    priv=bytearray();priv += int(samples).to_bytes(4,'big');priv += len(offsets).to_bytes(4,'big')
    for off in offsets:priv += int(off).to_bytes(4,'big')
    frame=b'PRIV'+len(priv).to_bytes(4,'big')+b'\0\0'+bytes(priv)
    total_no_pad=10+len(frame);pad=(-total_no_pad)%0x80
    body=frame+b'\0'*pad
    tag=b'ID3\x03\x00\x00'+_synchsafe32(len(body))
    payload=tag+body+audio
    duration_ms=(samples*1000)//rate
    if duration_ms<=0:raise ValueError('PS3 LoadedSound duration is zero')
    return payload,duration_ms,samples,channels,rate

def stage_iwd_sounds(entries:Iterable[IwdSoundEntry],ffmpeg:str='ffmpeg')->dict[str,StagedSound]:
    out={}
    for e in entries:
        src=_source_bytes(e)
        if hashlib.sha256(src).hexdigest().upper()!=e.source_sha256:raise ValueError(f'IWD sound changed after inventory: {e.entry_name}')
        # Re-encode even an existing MP3 so the ID3 version/bitrate match the
        # PS3 retail LoadedSound contract.  The long custom STREAMED ambient is
        # staged for evidence but is not converted to LOADED by the runtime
        # binding path below.
        encoded=_run_ffmpeg(src,e.extension,ffmpeg)
        payload,duration_ms,encoded_samples,channels,rate=_make_ps3_loaded_mp3(encoded)
        if not payload.startswith(b'ID3\x03\x00'):raise ValueError(f'staged payload {e.asset_name} is not PS3 ID3v2.3')
        if not e.channels or not e.sample_rate or e.sample_count<=0:raise ValueError(f'incomplete sound metadata for {e.asset_name}')
        if channels!=e.channels or rate!=e.sample_rate:raise ValueError(f'PS3 MP3 format drift for {e.asset_name}: {channels}ch/{rate} != source {e.channels}ch/{e.sample_rate}')
        key=normalize_sound_asset(e.asset_name).casefold()
        staged=StagedSound(normalize_sound_asset(e.asset_name),payload,13,int(encoded_samples),int(channels),int(rate),e.source_sha256,hashlib.sha256(payload).hexdigest().upper(),duration_ms)
        old=out.get(key)
        if old is not None and old.staged_sha256!=staged.staged_sha256:raise ValueError(f'conflicting duplicate staged sound {e.asset_name}')
        out[key]=staged
    return out

def _stream_candidates(file):
    name=file.stream_name;directory=file.stream_directory
    if name:yield normalize_sound_asset(name)
    if name and directory:yield normalize_sound_asset(directory.rstrip('/\\')+'/'+name.lstrip('/\\'))

def _unique_alias_basename(list_name:str,alias_name:str|None,staged:dict[str,StagedSound])->str|None:
    for identity in dict.fromkeys(normalize_sound_asset(x) for x in (alias_name,list_name) if x):
        base=Path(identity).name.casefold();matches=[k for k in staged if Path(k).name.casefold()==base]
        if len(matches)==1:return matches[0]
    return None

def resolve_sound_bindings(catalog:SoundCatalog,staged:dict[str,StagedSound],*,ffmpeg:str='ffmpeg',preserve_all_lists:bool=False)->SoundBindingReport:
    plans=[];used=set();unresolved=[];custom=inline=runtime_null=0
    converted_inline={}
    for lst in catalog.lists:
        provisional=[];owned=bool(preserve_all_lists)
        for alias in lst.aliases:
            sf=alias.sound_file;plan=None;local=False
            if sf is not None:
                if sf.type in (SoundFileType.STREAMED,SoundFileType.PRIMED):
                    key=next((c.casefold() for c in _stream_candidates(sf) if c.casefold() in staged),None)
                    if key is None:key=_unique_alias_basename(lst.name,alias.alias_name,staged)
                    if key is not None:
                        st=staged[key];owned=True;custom+=1;used.add(key)
                        if preserve_all_lists:
                            # Preserve source ownership semantics for runtime builds. A PC
                            # STREAMED/PRIMED custom file must not be turned into a giant
                            # inline LoadedSound. The PS3 retail stream backend expects its
                            # streamed payload outside this zone; until a .pak/stream packer
                            # exists, null the member rather than feeding it to LoadedSound.
                            plan=None;runtime_null+=1
                        else:
                            plan=SoundFilePlan(sf,SoundFileType.LOADED,st.asset_name,st.payload,st.format,st.sample_count,st.channels,st.sample_rate,st.asset_name,st.duration_ms);local=True
                    elif preserve_all_lists:
                        # Stock streamed/primed files remain path-based references.  Their normal-block
                        # directory/name XStrings are replayed by SoundNode; no payload is fabricated.
                        plan=SoundFilePlan(sf,sf.type,None,b'',0,0,0,0,None)
                elif sf.type==SoundFileType.LOADED and sf.loaded_sound is not None:
                    l=sf.loaded_sound
                    lname=normalize_sound_asset(l.name or '') if l.name else ''
                    key=lname.casefold() if lname and lname.casefold() in staged else None
                    if key is not None:
                        st=staged[key];plan=SoundFilePlan(sf,SoundFileType.LOADED,st.asset_name,st.payload,st.format,st.sample_count,st.channels,st.sample_rate,st.asset_name,st.duration_ms);local=True;owned=True;custom+=1;used.add(key)
                    elif l.payload and l.payload.startswith(b'ID3') and l.channels>0 and l.sample_rate>0 and l.sample_count>0:
                        encoded=_run_ffmpeg(l.payload,'.mp3',ffmpeg);ps3_payload,duration_ms,encoded_samples,channels,rate=_make_ps3_loaded_mp3(encoded)
                        plan=SoundFilePlan(sf,SoundFileType.LOADED,l.name or alias.alias_name or lst.name,ps3_payload,13,encoded_samples,channels,rate,None,duration_ms);local=True;owned=True;inline+=1
                    elif preserve_all_lists and l.payload and l.channels>0 and l.sample_rate>0 and l.sample_count>0:
                        # CP12-A boot isolation deliberately does NOT duplicate hundreds of stock
                        # PC LoadedSound PCM payloads into the PS3 map zone. Keep the AliasList root
                        # and all alias metadata, but null this SoundFile member. Custom IWD payloads
                        # and already-PS3-compatible ID3 payloads above remain bound.
                        plan=None;owned=True;runtime_null+=1
                elif sf.type==SoundFileType.LOADED and preserve_all_lists:
                    # Shared/packed LoadedSound body not locally owned: preserve AliasList topology
                    # but use a null SoundFile pointer rather than a dangling PC/shared address.
                    plan=None;owned=True;runtime_null+=1
                elif sf.type==SoundFileType.UNKNOWN:
                    if preserve_all_lists:
                        plan=None;owned=True;runtime_null+=1
                    else:
                        plan=SoundFilePlan(sf,SoundFileType.UNKNOWN,None,b'',0,0,0,0,None)
            provisional.append((alias,plan,local))
        aps=[]
        for alias,plan,local in provisional:
            if owned and alias.sound_file is not None and plan is None and not preserve_all_lists:
                sf=alias.sound_file
                source='/'.join(x for x in (sf.stream_directory,sf.stream_name) if x) if sf.type in (SoundFileType.STREAMED,SoundFileType.PRIMED) else f'{sf.type.name}@0x{sf.root_offset:X}'
                unresolved.append(f'{lst.name}/{alias.alias_name or "<unnamed>"}: {source}')
            aps.append(SoundAliasPlan(alias,plan))
        plans.append(SoundListPlan(lst,owned,tuple(aps)))
    unreferenced=tuple(sorted(st.asset_name for k,st in staged.items() if k not in used))
    owned_lists=tuple(x for x in plans if x.owned_by_map);external=tuple(sorted({x.source.name for x in plans if not x.owned_by_map},key=str.casefold))
    return SoundBindingReport(tuple(plans),owned_lists,external,len(catalog.lists),sum(len(x.aliases) for x in catalog.lists),len(owned_lists),len(plans)-len(owned_lists),custom,inline,len(used),runtime_null,tuple(unresolved),unreferenced)

def stage_standalone_loaded_sound(source,ffmpeg:str='ffmpeg')->StagedSound:
    """Convert an owned PC LoadedSound payload to the measured PS3 ID3/MP3 contract.

    Existing ID3 payloads are retained. Otherwise the source is accepted only when its PCM metadata
    is complete enough to construct an exact RIFF/WAVE wrapper; arbitrary compressed bytes are never
    guessed as PCM.
    """
    payload=bytes(source.payload)
    name=normalize_sound_asset(source.name or f'loaded_{getattr(source,"source_asset_index",-1)}')
    if not payload:raise ValueError(f"standalone LoadedSound '{name}' has no payload")
    if source.channels<=0 or source.sample_rate<=0 or source.sample_count<=0:raise ValueError(f"standalone LoadedSound '{name}' has incomplete stream metadata")
    if payload.startswith(b'ID3'):
        out=payload
    else:
        declared_bits=int(source.bits_per_sample)
        # Some IW3 LoadedSound roots expose a platform/codec field at the historical
        # bits-per-sample slot even though the payload itself is plain PCM.  Never guess: prove
        # the PCM width from exact payload cardinality.  payloadBytes must divide exactly by
        # sampleCount*channels and yield one of the standard PCM widths; when block_size is
        # present it must agree with the derived frame width.
        denom=int(source.sample_count)*int(source.channels)
        derived_bits=(len(payload)*8//denom) if denom>0 and (len(payload)*8)%denom==0 else 0
        if derived_bits not in (8,16,24,32):
            raise ValueError(f"standalone LoadedSound '{name}' cannot prove PCM width from {len(payload)} bytes / {source.sample_count} samples / {source.channels} channels")
        align=source.channels*(derived_bits//8)
        if getattr(source,'block_size',0) not in (0,align):
            raise ValueError(f"standalone LoadedSound '{name}' derived PCM blockAlign {align} disagrees with source block_size {source.block_size}")
        bits=derived_bits
        expected=source.sample_count*align
        if len(payload)!=expected:raise ValueError(f"standalone LoadedSound '{name}' PCM bytes {len(payload)} != samples*blockAlign {expected}")
        import struct
        byte_rate=source.sample_rate*align
        fmt=struct.pack('<HHIIHH',1,source.channels,source.sample_rate,byte_rate,align,bits)
        wave=b'RIFF'+struct.pack('<I',36+len(payload))+b'WAVEfmt '+struct.pack('<I',16)+fmt+b'data'+struct.pack('<I',len(payload))+payload
        out=_run_ffmpeg(wave,'.wav',ffmpeg)
    # Re-encode/normalize every standalone payload to the PS3 PRIV seek-table
    # contract even if its source already carried some other ID3 tag.
    encoded=_run_ffmpeg(out,'.mp3',ffmpeg) if out.startswith(b'ID3') else out
    out,duration_ms,encoded_samples,channels,rate=_make_ps3_loaded_mp3(encoded)
    return StagedSound(name,out,13,int(encoded_samples),int(channels),int(rate),hashlib.sha256(payload).hexdigest().upper(),hashlib.sha256(out).hexdigest().upper(),duration_ms)
