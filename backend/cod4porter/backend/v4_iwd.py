from __future__ import annotations
from dataclasses import dataclass, asdict, replace
from pathlib import Path
import hashlib, io, struct, zipfile, re
from typing import Iterable

IWI_HEADER=28
IWI_FORMATS={1:'rgba8',2:'rgb8',3:'luminance_alpha8',4:'luminance8',5:'alpha8',0x0B:'dxt1',0x0C:'dxt3',0x0D:'dxt5'}
IWI_PIXEL_BYTES={1:4,2:3,3:2,4:1,5:1}

def sha256(data:bytes)->str: return hashlib.sha256(data).hexdigest().upper()
def normalize_iwi_name(value:str)->str:
    n=value.strip().replace('\\','/').lstrip('/')
    if n.lower().startswith('images/'): n=n[7:]
    if n.lower().endswith('.iwi'): n=n[:-4]
    return n

def normalize_sound_asset(value:str)->str:
    n=value.strip().replace('\\','/').lstrip('/')
    if n.lower().startswith('sound/'): n=n[6:]
    if n.lower().endswith('.wav') or n.lower().endswith('.mp3'): n=n.rsplit('.',1)[0]
    return n

@dataclass(frozen=True)
class IwiMip:
    face:int; level:int; width:int; height:int; data:bytes
    @property
    def sha256(self): return sha256(self.data)

@dataclass(frozen=True)
class IwiImage:
    name:str; version:int; format_id:int; format_name:str; flags:int; width:int; height:int; depth:int
    picmip_sizes:tuple[int,int,int,int]; mips:tuple[IwiMip,...]
    encoded_format_id:int|None=None
    @property
    def face_count(self): return 6 if self.flags & 0x04 else 1
    @property
    def is_cubemap(self): return self.face_count==6
    @property
    def level_count(self): return len({m.level for m in self.mips})

@dataclass(frozen=True)
class IwdImageEntry:
    archive_path:str; entry_name:str; canonical_name:str; archive_bytes:int; content_sha256:str; image:IwiImage

@dataclass(frozen=True)
class IwdSoundEntry:
    archive_path:str; entry_name:str; asset_name:str; extension:str; bytes:int; source_sha256:str
    is_pcm_wave:bool; channels:int; sample_rate:int; bits_per_sample:int; sample_count:int; has_id3:bool


def _levels(width:int,height:int,block_bytes:int,mipmapped:bool,pixel_bytes:int=0):
    out=[]; level=0; w=width; h=height
    while True:
        bx=max(1,(w+3)//4); by=max(1,(h+3)//4)
        out.append((level,w,h,w*h*pixel_bytes if pixel_bytes else bx*by*block_bytes))
        if not mipmapped or (w==1 and h==1): break
        w=max(1,w//2); h=max(1,h//2); level+=1
    return out


def parse_iwi(name:str,data:bytes)->IwiImage:
    if len(data)<IWI_HEADER: raise ValueError(f"IWI {name!r} shorter than v6 header")
    if data[:3]!=b'IWi' or data[3]!=6: raise ValueError(f"IWI {name!r} is not COD4 v6")
    fmt=data[4]
    if fmt in range(6,11):
        from ..iwi_wavelet_adapter import decode_wavelet_iwi
        try:
            decoded=parse_iwi(name,decode_wavelet_iwi(data))
        except ValueError as error:
            raise ValueError(f'IWI {name!r}: {error}') from error
        if decoded.format_id!=1 or decoded.encoded_format_id is not None:
            raise ValueError(f'IWI {name!r}: wavelet decoder did not return a BGRA bitmap')
        return replace(decoded,encoded_format_id=fmt)
    if fmt not in IWI_FORMATS: raise ValueError(f"IWI {name!r} unsupported format 0x{fmt:02X}")
    flags=data[5]; width,height,depth=struct.unpack_from('<HHH',data,6)
    if not width or not height or depth!=1: raise ValueError(f"IWI {name!r} invalid/unsupported dimensions {width}x{height}x{depth}")
    pic=struct.unpack_from('<4I',data,12)
    if pic[0]!=len(data): raise ValueError(f"IWI {name!r} says {pic[0]} bytes but has {len(data)}")
    faces=6 if flags&0x04 else 1; mipmapped=(flags&0x02)==0; block=8 if fmt==0x0B else 16
    levels=_levels(width,height,block,mipmapped,IWI_PIXEL_BYTES.get(fmt,0))
    expected=sum(x[3]*faces for x in levels)
    if len(data)-IWI_HEADER!=expected: raise ValueError(f"IWI {name!r} payload {len(data)-IWI_HEADER} != expected {expected}")
    raw={}; cursor=IWI_HEADER
    for lev,w,h,nbytes in sorted(levels,key=lambda x:x[0],reverse=True):
        f=[]
        for face in range(faces):
            f.append(data[cursor:cursor+nbytes]); cursor+=nbytes
        raw[lev]=f
    if cursor!=len(data): raise ValueError('IWI parser did not reach EOF')
    mips=[]
    for face in range(faces):
        for lev,w,h,nbytes in sorted(levels):
            mips.append(IwiMip(face,lev,w,h,raw[lev][face]))
    return IwiImage(normalize_iwi_name(name),6,fmt,IWI_FORMATS[fmt],flags,width,height,depth,tuple(pic),tuple(mips))


def image_fingerprint(image:IwiImage)->str:
    h=hashlib.sha256()
    h.update(bytes([image.version,image.format_id,image.flags])); h.update(struct.pack('<HHHH',image.width,image.height,image.depth,len(image.mips)))
    for m in image.mips:
        h.update(struct.pack('<iiiii',m.face,m.level,m.width,m.height,len(m.data))); h.update(m.data)
    return h.hexdigest().upper()


def inspect_iwds(paths:Iterable[str|Path],*,include_sounds:bool=True, image_names:set[str]|None=None)->tuple[dict[str,IwdImageEntry],list[IwdSoundEntry]]:
    images:dict[str,IwdImageEntry]={}; sounds=[]
    for raw_path in paths:
        path=Path(raw_path)
        with zipfile.ZipFile(path,'r') as z:
            for zi in z.infolist():
                en=zi.filename.replace('\\','/')
                lower=en.lower()
                if lower.endswith('.iwi'):
                    if image_names is not None and normalize_iwi_name(en).casefold() not in image_names:continue
                    data=z.read(zi); image=parse_iwi(en,data); canonical=image.name.casefold(); fp=image_fingerprint(image)
                    cand=IwdImageEntry(str(path.resolve()),en,image.name,zi.file_size,fp,image)
                    old=images.get(canonical)
                    if old is None: images[canonical]=cand
                    elif old.content_sha256!=cand.content_sha256:
                        raise ValueError(f"conflicting duplicate IWI {image.name!r}: {old.archive_path} vs {cand.archive_path}")
                elif include_sounds and (lower.endswith('.wav') or lower.endswith('.mp3')):
                    data=z.read(zi); ext=Path(en).suffix.lower(); asset=normalize_sound_asset(en)
                    if ext=='.wav': channels,rate,bits,samples,pcm=_parse_wave(en,data)
                    else: channels,rate,samples=_parse_mp3(en,data); bits=0; pcm=False
                    sounds.append(IwdSoundEntry(str(path.resolve()),en,asset,ext,len(data),sha256(data),pcm,channels,rate,bits,samples,data[:3]==b'ID3'))
    return images,sounds


def _parse_wave(name:str,b:bytes):
    if len(b)<12 or b[:4]!=b'RIFF' or b[8:12]!=b'WAVE': raise ValueError(f"WAV {name!r} not RIFF/WAVE")
    p=12; fmt=channels=bits=align=0; rate=0; data_bytes=-1
    while p<=len(b)-8:
        ident=b[p:p+4]; size=struct.unpack_from('<I',b,p+4)[0]; p+=8
        if p+size>len(b): raise ValueError(f"WAV {name!r} truncated chunk")
        if ident==b'fmt ':
            if size<16: raise ValueError(f"WAV {name!r} short fmt")
            fmt,channels,rate,_,align,bits=struct.unpack_from('<HHIIHH',b,p)
        elif ident==b'data': data_bytes=size
        p+=size+(size&1)
    pcm=fmt==1
    if not pcm or not channels or not rate or not bits or not align or data_bytes<0: raise ValueError(f"WAV {name!r} must be PCM")
    return channels,rate,bits,data_bytes//align,True


def _parse_mp3(name:str,data:bytes):
    cursor=0
    if len(data)>=10 and data[:3]==b'ID3':
        tag=((data[6]&0x7F)<<21)|((data[7]&0x7F)<<14)|((data[8]&0x7F)<<7)|(data[9]&0x7F); cursor=10+tag
        if cursor>len(data): raise ValueError(f"MP3 {name!r} truncated ID3")
    samples=0; detected_rate=0; detected_ch=0; frames=0
    rates=[44100,48000,32000]; br1=[0,32,40,48,56,64,80,96,112,128,160,192,224,256,320,0]; br2=[0,8,16,24,32,40,48,56,64,80,96,112,128,144,160,0]
    while cursor<=len(data)-4:
        header=int.from_bytes(data[cursor:cursor+4],'big')
        if header&0xFFE00000!=0xFFE00000: cursor+=1; continue
        ver=(header>>19)&3; layer=(header>>17)&3; bri=(header>>12)&0xF; sri=(header>>10)&3; pad=(header>>9)&1
        if ver==1 or layer!=1 or bri in (0,15) or sri==3: cursor+=1; continue
        mpeg1=ver==3; sr=rates[sri]
        if ver==2: sr//=2
        elif ver==0: sr//=4
        kbps=(br1 if mpeg1 else br2)[bri]; fl=(144000*kbps)//sr+pad if mpeg1 else (72000*kbps)//sr+pad
        if fl<4 or cursor+fl>len(data): break
        ch=1 if ((header>>6)&3)==3 else 2
        if frames==0: detected_rate=sr; detected_ch=ch
        elif detected_rate!=sr or detected_ch!=ch: raise ValueError(f"MP3 {name!r} changes stream format")
        samples+=1152 if mpeg1 else 576; frames+=1; cursor+=fl
    if frames==0: raise ValueError(f"MP3 {name!r} contains no decodable frames")
    return detected_ch,detected_rate,samples


def cubemap_face_fingerprints(image:IwiImage, all_mips:bool=True)->tuple[str,...]:
    if not image.is_cubemap: raise ValueError('image is not a cubemap')
    out=[]
    for face in range(6):
        h=hashlib.sha256()
        ms=[m for m in image.mips if m.face==face and (all_mips or m.level==0)]
        for m in sorted(ms,key=lambda x:x.level):
            h.update(struct.pack('<iiii',m.level,m.width,m.height,len(m.data))); h.update(m.data)
        out.append(h.hexdigest().upper())
    return tuple(out)


def prove_cubemap_bijection(source:IwiImage,target_face_hashes:Iterable[str])->dict:
    src=cubemap_face_fingerprints(source,True); tgt=tuple(x.upper() for x in target_face_hashes)
    if len(tgt)!=6: return {'passed':False,'reason':'target must contain six face hashes'}
    mapping={}; used=set()
    for si,h in enumerate(src):
        cand=[ti for ti,x in enumerate(tgt) if x==h and ti not in used]
        if len(cand)!=1: return {'passed':False,'reason':f'source face {si} has {len(cand)} unique target matches','source_hashes':src,'target_hashes':tgt}
        mapping[si]=cand[0]; used.add(cand[0])
    return {'passed':len(used)==6,'mapping':mapping,'source_hashes':src,'target_hashes':tgt}


def bind_sound_payloads(requests:Iterable[str],sounds:Iterable[IwdSoundEntry])->dict:
    # Materialize once: callers may pass generators.  The old implementation
    # consumed a generator in the loop and then reported request_count=0.
    # Keeping the exact request set is also useful evidence for closure gates.
    requests=list(requests)
    entries=list(sounds); by_asset={}
    by_base={}
    for s in entries:
        by_asset.setdefault(s.asset_name.casefold(),[]).append(s)
        by_base.setdefault(Path(s.asset_name).name.casefold(),[]).append(s)
    bindings=[]; used=set(); blockers=[]
    for raw in requests:
        req=normalize_sound_asset(raw); exact=by_asset.get(req.casefold(),[])
        cand=exact
        mode='exact_asset'
        if not cand:
            cand=by_base.get(Path(req).name.casefold(),[]); mode='unique_exact_basename'
        if len(cand)!=1:
            blockers.append({'request':raw,'canonical':req,'candidate_count':len(cand),'mode':mode}); continue
        s=cand[0]; key=(s.archive_path,s.entry_name.casefold())
        if key in used:
            blockers.append({'request':raw,'canonical':req,'reason':'payload already selected by another request','entry':s.entry_name}); continue
        used.add(key); bindings.append({'request':raw,'asset_name':s.asset_name,'entry_name':s.entry_name,'sha256':s.source_sha256,'mode':mode})
    unused=[{'asset_name':s.asset_name,'entry_name':s.entry_name} for s in entries if (s.archive_path,s.entry_name.casefold()) not in used]
    return {'passed':not blockers and not unused,'bindings':bindings,'blockers':blockers,'unused_audio':unused,'request_count':len(requests),'audio_count':len(entries)}
