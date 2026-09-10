from __future__ import annotations
import struct
from cod4porter.pc_sound import parse_sound_run_at,SoundFileLayout
from cod4porter.backend.v4_ffio import XAsset,XAssetList,FOLLOWING,encode_pc_packed

# Mixed inline + packed head fixture. The packed second head reuses the first list's normal-block head array.
start=0x100
z=bytearray(0x400)
# PC zone header: word0/1 + 9 block sizes. Block4 large enough for synthetic offsets.
struct.pack_into('<11I',z,0,0,0,0,0,0,0,0x1000,0,0,0,0)
# list0 root
p=start
struct.pack_into('<III',z,p,FOLLOWING,FOLLOWING,1);p+=0x0c
z[p:p+5]=b'snd0\0';p+=5
# one 0x5c alias; aliasName FOLLOWING, all other pointers NULL, finite zero scalar words
head0=p
struct.pack_into('<I',z,p,FOLLOWING);p+=0x5c
z[p:p+5]=b'snd0\0';p+=5
# list1 root: name FOLLOWING, head packed to local offset 8 (phase/basis consensus maps to first head allocation)
root1=p
struct.pack_into('<III',z,p,FOLLOWING,encode_pc_packed(4,8),1);p+=0x0c
z[p:p+5]=b'snd1\0';p+=5
assets=(XAsset(0,0,0x07,'sound',FOLLOWING),XAsset(1,8,0x07,'sound',FOLLOWING))
al=XAssetList('pc',0,0,(),assets,0x80,start)
doc=parse_sound_run_at(bytes(z),al,start,SoundFileLayout.LEGACY_0C)
assert len(doc.lists)==2
assert doc.lists[0].name=='snd0' and doc.lists[1].name=='snd1'
assert len(doc.lists[0].aliases)==1 and len(doc.lists[1].aliases)==1
assert doc.lists[1].aliases[0] is doc.lists[0].aliases[0]
assert doc.packed_reference_count==1 and doc.resolved_packed_reference_count==1
assert doc.equivalent_structural_phases==4
print({'passed':True,'end':hex(doc.stream_end),'basis':doc.block4_basis,'phases':doc.equivalent_structural_phases})

# Packed-only top-level shells. Their canonical head graph is physically before the shell and recovered by exact logical footprint.
z=bytearray(0x600);struct.pack_into('<11I',z,0,*([0,0,0,0,0,0,0x2000,0,0,0,0]))
# canonical head 0 @ physical 0x100, logical 0x100
h0=0x100;struct.pack_into('<I',z,h0,FOLLOWING);z[h0+0x5c:h0+0x5e]=b'a\0'
# canonical head 1 follows physically after h0+string; logical must be align(0x100+0x5c+2)=0x160
h1=h0+0x5c+2;struct.pack_into('<I',z,h1,FOLLOWING);z[h1+0x5c:h1+0x5e]=b'b\0'
shell=0x300
struct.pack_into('<III',z,shell,encode_pc_packed(4,0x15c),encode_pc_packed(4,0x100),1)
struct.pack_into('<III',z,shell+0x0c,encode_pc_packed(4,0x1bc),encode_pc_packed(4,0x160),1)
al=XAssetList('pc',0,0,(),assets,0x80,shell)
doc=parse_sound_run_at(bytes(z),al,shell,SoundFileLayout.LEGACY_0C)
assert [x.name for x in doc.lists]==['a','b']
assert [x.aliases[0].alias_name for x in doc.lists]==['a','b']
assert doc.packed_reference_count==4 and doc.resolved_packed_reference_count==4
print({'packed_only_passed':True,'basis':doc.block4_basis,'resolved':doc.resolved_packed_reference_count})

# Typed TEMP-root reuse: second SoundFile reuses LoadedSound root, and second alias reuses SndCurve root.
z=bytearray(0x1000);struct.pack_into('<11I',z,0,*([0,0,0,0,0,0,0x4000,0,0,0,0]))
p=0x300
struct.pack_into('<III',z,p,FOLLOWING,FOLLOWING,2);p+=0x0c
z[p:p+5]=b'list\0';p+=5
head=p
# alias0 and alias1 roots
# alias0 aliasName FOLLOWING, soundfile FOLLOWING, curve INSERT
struct.pack_into('<I',z,head+0,FOLLOWING);struct.pack_into('<I',z,head+16,FOLLOWING);struct.pack_into('<I',z,head+72,0xFFFFFFFE)
# alias1 aliasName FOLLOWING, soundfile FOLLOWING, curve packed target 220
struct.pack_into('<I',z,head+0x5c+0,FOLLOWING);struct.pack_into('<I',z,head+0x5c+16,FOLLOWING);struct.pack_into('<I',z,head+0x5c+72,encode_pc_packed(4,220))
p=head+2*0x5c
# alias0 children
z[p:p+3]=b'a0\0';p+=3
# SoundFile0 logical begins 196
sf0=p;z[p:p+4]=bytes([1,1,0,0]);struct.pack_into('<I',z,p+4,0xFFFFFFFE);p+=0x0c
# LoadedSound root; logical root alias target 208
ls=p;struct.pack_into('<I',z,p,FOLLOWING);struct.pack_into('<I',z,p+4,0x55);struct.pack_into('<I',z,p+0x0c,4);struct.pack_into('<I',z,p+0x10,44100);struct.pack_into('<I',z,p+0x14,16);struct.pack_into('<I',z,p+0x18,2);struct.pack_into('<I',z,p+0x1c,100);struct.pack_into('<I',z,p+0x20,0);struct.pack_into('<I',z,p+0x28,0xFFFFFFFE);p+=0x2c
z[p:p+3]=b'ls\0';p+=3
z[p:p+4]=b'ID3!';p+=4
# Curve root; logical root alias target 220
cv=p;struct.pack_into('<I',z,p,FOLLOWING);struct.pack_into('<I',z,p+4,2);p+=0x48
z[p:p+3]=b'cv\0';p+=3
# alias1 children
z[p:p+3]=b'a1\0';p+=3
# SoundFile1 is inline but LoadedSound child reuses root alias target 208
z[p:p+4]=bytes([1,1,0,0]);struct.pack_into('<I',z,p+4,encode_pc_packed(4,208));p+=0x0c
assets1=(XAsset(0,0,0x07,'sound',FOLLOWING),)
al1=XAssetList('pc',0,0,(),assets1,0x80,0x300)
doc=parse_sound_run_at(bytes(z),al1,0x300,SoundFileLayout.LEGACY_0C)
a0,a1=doc.lists[0].aliases
# Nested TEMP-XAsset roots are not part of the Sound-local normal/block-4 basis.  Inline roots
# are parsed exactly, while packed reuse words are preserved for the later global/shared binding
# stage instead of being forced into a false local basis (the real Getaway graph relies on this).
assert a0.sound_file.loaded_sound is not None and a0.sound_file.loaded_sound.payload==b'ID3!'
assert a0.volume_curve is not None and a0.volume_curve.name=='cv'
assert a1.sound_file.loaded_sound is None
assert getattr(a1.sound_file,'packed_loaded_pointer',None)==encode_pc_packed(4,208)
assert a1.volume_curve is None
assert getattr(a1,'packed_curve_pointer',None)==encode_pc_packed(4,220)
assert doc.packed_reference_count==0 and doc.resolved_packed_reference_count==0
print({'typed_temp_alias_deferred_passed':True,'loaded_raw':hex(a1.sound_file.packed_loaded_pointer),'curve_raw':hex(a1.packed_curve_pointer)})
