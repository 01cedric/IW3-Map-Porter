from __future__ import annotations
import hashlib,struct,tempfile,wave,zipfile
from pathlib import Path
import sys

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))

from cod4porter.sound import *
from cod4porter.pc_sound import SoundCatalog,SoundFileLayout
from cod4porter.sound_binding import StagedSound,resolve_sound_bindings,stage_iwd_sounds
from cod4porter.backend.v4_iwd import inspect_iwds

raw=tuple([0]*23)
sf=SoundFile(1,SoundFileType.STREAMED,True,stream_directory='ambient',stream_name='custom')
a=SoundAlias(2,raw,'custom',sound_file=sf)
l=SoundAliasList(0,7,3,'custom',(a,))
cat=SoundCatalog((l,),SoundFileLayout.LEGACY_0C,0,0,None,0,0,0,0,0)
st=StagedSound('ambient/custom',b'ID3\x03\x00x',13,100,2,44100,'A','B',2)
r=resolve_sound_bindings(cat,{'ambient/custom':st})
assert r.passed and len(r.owned_lists)==1 and r.custom_iwd_references==1
# ``preserve_all_lists`` remains a diagnostic compatibility mode. Version 10
# deliberately does not use it: custom lists take the normal fail-closed path
# above and every non-null source SoundFile receives a concrete LoadedSound.
r_rt=resolve_sound_bindings(cat,{'ambient/custom':st},preserve_all_lists=True)
assert r_rt.passed and r_rt.owned_lists[0].aliases[0].sound_file is None and r_rt.runtime_null_soundfile_references==1

# Real staging path: tiny PCM WAV in an IWD is converted by ffmpeg to an ID3 LoadedSound payload.
with tempfile.TemporaryDirectory() as td:
    wavp=Path(td)/'x.wav'
    with wave.open(str(wavp),'wb') as w:
        w.setnchannels(1);w.setsampwidth(2);w.setframerate(8000);w.writeframes(b'\0\0'*800)
    iwd=Path(td)/'map.iwd'
    with zipfile.ZipFile(iwd,'w',zipfile.ZIP_DEFLATED) as z:z.write(wavp,'sound/test/x.wav')
    imgs,sounds=inspect_iwds([iwd]);assert len(sounds)==1
    staged=stage_iwd_sounds(sounds);s=staged['test/x'];assert s.payload.startswith(b'ID3\x03\x00') and s.format==13 and s.channels==1 and s.sample_rate==8000 and s.duration_ms>0
    tag=((s.payload[6]&127)<<21)|((s.payload[7]&127)<<14)|((s.payload[8]&127)<<7)|(s.payload[9]&127);assert (10+tag)%128==0 and s.payload[10:14]==b'PRIV'
    psz=int.from_bytes(s.payload[14:18],'big');priv=s.payload[20:20+psz];samples=int.from_bytes(priv[:4],'big');frames=int.from_bytes(priv[4:8],'big');assert len(priv)==8+frames*4 and s.duration_ms==(samples*1000)//s.sample_rate
print({'passed':True,'owned':r.owned_list_count,'ffmpeg_stage':True})
