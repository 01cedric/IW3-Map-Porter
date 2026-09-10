from __future__ import annotations
import struct
import sys
from pathlib import Path

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))

from cod4porter.sound import *
from cod4porter.assets.sound import SoundNode
from cod4porter.graph import write_graph,AssetType
from cod4porter.backend.v4_ffio import parse_xasset_list,decode_ps3_pointer

ls=LoadedSound(0x900,'amb/test',13,7,44100,0,2,1000,0,b'ID3\x03\x00TEST')
sf=SoundFile(0x800,SoundFileType.LOADED,True,loaded_sound=ls)
plan_sf=SoundFilePlan(sf,SoundFileType.LOADED,'amb/test',b'ID3\x03\x00TEST',13,1000,2,44100,duration_ms=23)
cv=SoundCurve(0x700,'curve/test',2,tuple(float(i)/15 for i in range(16)))
sp=SpeakerMap(0xA00,False,'speaker/test',tuple(range(100)))
raw=tuple([0]*23)
a0=SoundAlias(0x1000,raw,'alias0',sound_file=sf,volume_curve=cv,speaker_map=sp)
a1=SoundAlias(0x105c,raw,'alias1',sound_file=sf,volume_curve=cv,speaker_map=sp)
lst=SoundAliasList(0,42,0x600,'snd_test',(a0,a1))
plan=SoundListPlan(lst,True,(SoundAliasPlan(a0,plan_sf),SoundAliasPlan(a1,plan_sf)))
n=SoundNode(plan,'sound.test')
g=write_graph([n]);z=g.zone.zone_bytes;r=g.context.results['sound:sound.test'];assert g.asset_types==(AssetType.SOUND,)
assert r['aliases']==2 and r['loaded_payloads']==2 and r['curves']==2 and r['speaker_maps']==2
al=parse_xasset_list(z,'ps3');assert al.assets[0].type_name=='sound'
# Retail Shipment evidence: SoundFile and names are B4. LoadedSound/SndCurve
# roots and MP3 data are nested TEMP/B0; reuse targets a B4 pointer/INSERT alias.
canon={(x.identity,x.block,x.size) for x in g.zone.canonical_allocations}
assert not any(x[0].startswith('sound:loadedsound-root:') for x in canon)
assert not any(x[0].startswith('sound:loadedsound-data:') for x in canon)
assert any(x[0].startswith('sound:sndcurve-root:') and x[1]==4 for x in canon)
assert len([x for x in g.zone.relocations if 'reused LoadedSound B4 pointer-field alias' in x.description])==1
assert len([x for x in g.zone.relocations if 'reused SndCurve' in x.description])==1
row=r['loaded_sound_rows'][0]
assert row['symbol_location'].block==4 and row['symbol_location'].offset%4==0
assert row['root_location'].block==0 and row['root_location'].offset%4==0
assert row['symbol_location']!=row['root_location']
assert row['name_location'].block==4
assert row['payload_location'].block==0
assert row['payload_location'].offset==row['root_location'].offset+0x1c
assert g.zone.block_sizes[3]==0
assert g.zone.block_sizes[0] >= 0x0c+0x1c+len(b'ID3\x03\x00TEST')
# Retail PS3 LoadedSound ABI: +0x08 codec/format is 13 and +0x0C is
# rounded duration milliseconds, not PCM sample count.
pos=z.find(b'ID3\x03\x00TEST');assert pos>0
root=row['root_physical']
assert struct.unpack_from('>I',z,root+8)[0]==13
assert struct.unpack_from('>I',z,root+12)[0]==23
assert struct.unpack_from('>I',z,root+24)[0]==0xffffffff
assert row['payload_physical']==pos
assert g.zone.block_sizes[4]>g.zone.block_sizes[0]
print({'passed':True,'zone_bytes':len(z),'block_sizes':g.zone.block_sizes,'aliases':r['aliases'],'canonical_aliases':len(canon)})
