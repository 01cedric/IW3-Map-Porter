from __future__ import annotations

import argparse
from dataclasses import asdict
import hashlib
import json
from pathlib import Path
import shutil
import struct
import tempfile
import traceback
import wave
import zipfile
import zlib

from .v4_ffio import *
from .v4_load import parse_load_zone
from .v4_iwd import *
from .v4_proofs import *
from .v4_source import audit_csharp_source, compare_tree_manifest
from .v4_gate import *
from .v4_sound import *
from .v4_assets import *
from .v4_images import *
from .v4_retail import *
from .v4_pipeline import run_pipeline

HERE = Path(__file__).resolve().parent
DEFAULT_REF = HERE.parents[1] / 'reference' / 'legacy'
FIX89_MAIN_SHA = '0F94852CA8A3882D049B95CCD6C46DA7E6088F64D3E319A1D2E042041953ADF2'
FIX89_LOAD_SHA = '428A9C45E0FB167CE8C6495760E8E2A524F8CE93A76D445D23053A31B4B84E50'

class Suite:
    def __init__(self): self.rows=[]
    def add(self,name,fn):
        try:
            value=fn()
            ok = value if isinstance(value,bool) else True
            detail='' if isinstance(value,bool) else value
            self.rows.append({'name':name,'passed':bool(ok),'detail':detail})
        except Exception as exc:
            self.rows.append({'name':name,'passed':False,'detail':f'{type(exc).__name__}: {exc}','trace':traceback.format_exc()})
    def eq(self,name,actual,expected): self.add(name,lambda: actual==expected or (_ for _ in ()).throw(AssertionError(f'{actual!r} != {expected!r}')))
    def true(self,name,actual): self.eq(name,bool(actual),True)
    def false(self,name,actual): self.eq(name,bool(actual),False)
    def result(self):
        return {'revision':'FIX114-backend-v4','passed':all(x['passed'] for x in self.rows),'check_count':len(self.rows),'failed':[x for x in self.rows if not x['passed']],'checks':self.rows}


def _parse_manifest(path:Path)->dict[str,str]:
    out={}
    for line in path.read_text(encoding='utf-8').splitlines():
        if not line.strip(): continue
        h,p=line.split(None,1); p=p.strip()
        if p.startswith('./'):p=p[2:]
        out[p]=h.upper()
    return out


def _synthetic_pc_zone()->bytes:
    # PC v5 header + XAssetList with one ScriptString and one TechniqueSet shell.
    header=struct.pack('<11I', 0x80,0,*([0]*9))
    root=struct.pack('<4I',1,FOLLOWING,1,FOLLOWING)
    body=struct.pack('<I',FOLLOWING)+b'synthetic\0'
    pool=struct.pack('<II',0x05,FOLLOWING)
    asset=b'asset\0'
    return header+root+body+pool+asset


def _write_pc_ff(path:Path,zone:bytes):
    path.write_bytes(MAGIC+struct.pack('<I',5)+zlib.compress(zone,9))


def _make_iwi(name='images/test.iwi',cube=False,distinct=True)->bytes:
    fmt=0x0B; flags=0x02 | (0x04 if cube else 0); w=h=4; depth=1
    faces=6 if cube else 1; payload=b''
    for face in range(faces):
        byte=(face+1 if distinct else 1)&0xFF
        payload += bytes([byte])*8
    total=28+len(payload)
    hdr=bytearray(28); hdr[:3]=b'IWi';hdr[3]=6;hdr[4]=fmt;hdr[5]=flags
    struct.pack_into('<HHH',hdr,6,w,h,depth); struct.pack_into('<4I',hdr,12,total,total,total,total)
    return bytes(hdr)+payload


def _wav_bytes()->bytes:
    import io
    bio=io.BytesIO()
    with wave.open(bio,'wb') as w:
        w.setnchannels(1);w.setsampwidth(1);w.setframerate(8000);w.writeframes(bytes(range(64)))
    return bio.getvalue()


def _make_iwd(path:Path,cube=False):
    with zipfile.ZipFile(path,'w',zipfile.ZIP_DEFLATED) as z:
        z.writestr('images/test.iwi',_make_iwi(cube=False))
        if cube:z.writestr('images/cube.iwi',_make_iwi(cube=True))
        z.writestr('sound/foo/bar.wav',_wav_bytes())


def _machine_all_pass_docs():
    docs=[]
    for crit,_ in CRITERIA:
        for source in sorted(REQUIRED_SOURCES[crit]):
            docs.append(evidence(crit,True,source,{'selftest':crit,'provenance':source}))
    return docs


def run(reference_root:Path)->dict:
    s=Suite()
    baseline=reference_root/'hardware_baseline/Fix89B'
    main=baseline/'mp_getaway.ff'; load=baseline/'mp_getaway_load.ff'

    # ----- Real hardware baseline: container, XAssetList and strict _load -----
    main_doc=read_ps3_fastfile(main); load_doc=read_ps3_fastfile(load)
    s.eq('Fix89B main SHA-256',main_doc.file_sha256,FIX89_MAIN_SHA)
    s.eq('Fix89B load SHA-256',load_doc.file_sha256,FIX89_LOAD_SHA)
    main_sum=summarize_fastfile(main,'ps3'); load_sum=summarize_fastfile(load,'ps3')
    s.eq('Fix89B main XAsset count',main_sum['xassets'],445)
    s.eq('Fix89B main ScriptString count',main_sum['script_strings'],281)
    s.eq('Fix89B load XAsset count',load_sum['xassets'],5)
    s.eq('Fix89B load ScriptString count',load_sum['script_strings'],0)
    lp=parse_load_zone(load_doc.zone); lpd=asdict(lp)
    s.eq('Fix89B _load XAsset order',lp.asset_type_order,['techset','material','material','material','rawfile'])
    s.eq('Fix89B _load material names',[x.name for x in lp.materials],['$victorybackdrop',',$defeatbackdrop','$levelbriefing'])
    s.eq('Fix89B _load resource bytes',lp.resource_bytes,1223424)
    s.eq('Fix89B _load trailing capacity',lp.trailing_capacity,20891)
    s.eq('Fix89B _load trailing capacity is all zero',lp.trailing_nonzero,0)
    s.eq('Victory image geometry',(lp.materials[0].textures[0]['image']['width'],lp.materials[0].textures[0]['image']['height'],lp.materials[0].textures[0]['image']['mip_count']),(1024,1024,11))
    s.eq('Victory image bytes',lp.materials[0].textures[0]['image']['resource_size'],699136)
    s.eq('Briefing image geometry',(lp.materials[2].textures[0]['image']['width'],lp.materials[2].textures[0]['image']['height'],lp.materials[2].textures[0]['image']['mip_count']),(1024,1024,1))
    s.eq('Briefing image bytes',lp.materials[2].textures[0]['image']['resource_size'],524288)
    s.eq('Victory sampler',lp.materials[0].textures[0]['sampler'],10)
    s.eq('Briefing sampler',lp.materials[2].textures[0]['sampler'],226)
    s.eq('Victory stateBits',lp.materials[0].state_bits,[[0xFFFFFFFF,0x19289165,0xE00E0002]])
    s.eq('Briefing stateBits',lp.materials[2].state_bits,[[0xFFFFFFFF,0x18128812,0xE00E0002]])
    # Use the real hardware-good _load zone for the deterministic codec test.  Re-encoding
    # the 88 MiB main zone twice is unnecessarily expensive for a release selftest; the same
    # framing/block writer is exercised here byte-for-byte on a real PS3 FastFile.
    re1=build_ps3_fastfile(load_doc.zone,load_doc.trailer); re2=build_ps3_fastfile(load_doc.zone,load_doc.trailer)
    s.true('PS3 writer deterministic',re1==re2)
    with tempfile.TemporaryDirectory() as td:
        pp=Path(td)/'round.ff';pp.write_bytes(re1); rd=read_ps3_fastfile(pp)
        s.true('PS3 writer zone roundtrip',rd.zone==load_doc.zone)
        s.true('PS3 writer trailer roundtrip',rd.trailer==load_doc.trailer)

    # ----- Synthetic PC FastFile -----
    with tempfile.TemporaryDirectory() as td:
        p=Path(td)/'pc.ff'; zone=_synthetic_pc_zone(); _write_pc_ff(p,zone)
        d=read_pc_fastfile(p); al=parse_xasset_list(d.zone,'pc')
        s.true('synthetic PC FastFile decompression',d.zone==zone)
        s.eq('synthetic PC ScriptString',al.script_strings[0].value,'synthetic')
        s.eq('synthetic PC XAsset type',al.assets[0].type_name,'techset')
        pcptr=decode_pc_pointer(encode_pc_packed(4,0x1234)); s.eq('PC packed pointer roundtrip',(pcptr.block,pcptr.offset),(4,0x1234))
        sizes=(0x100,)*7; ps3ptr=decode_ps3_pointer(encode_ps3_packed(4,0x40,sizes),sizes);s.eq('PS3 packed pointer roundtrip',(ps3ptr.block,ps3ptr.offset),(4,0x40))

    # ----- IWI/IWD/Cubemap/Sound bindings -----
    img=parse_iwi('images/test.iwi',_make_iwi()); s.eq('IWI v6 DXT1 parse',(img.format_name,img.width,img.height,img.face_count),('dxt1',4,4,1))
    cube=parse_iwi('images/cube.iwi',_make_iwi(cube=True)); faces=cubemap_face_fingerprints(cube)
    s.eq('cubemap has six distinct face fingerprints',len(set(faces)),6)
    perm=[faces[i] for i in (2,0,5,1,4,3)]; cp=prove_cubemap_bijection(cube,perm)
    s.true('cubemap exact 6/6 bijection',cp['passed'])
    bad=prove_cubemap_bijection(cube,[faces[0]]*6);s.false('cubemap ambiguity fails closed',bad['passed'])
    with tempfile.TemporaryDirectory() as td:
        iwd=Path(td)/'x.iwd';_make_iwd(iwd,True);images,sounds=inspect_iwds([iwd])
        s.eq('IWD image inventory',len(images),2);s.eq('IWD audio inventory',len(sounds),1)
        sb=bind_sound_payloads((x for x in ['foo/bar']),sounds)
        s.true('sound exact asset binding',sb['passed']);s.eq('sound generator request count retained',sb['request_count'],1)
        su=bind_sound_payloads(['foo/missing'],sounds);s.false('unresolved/unused sound payload fails closed',su['passed'])

    # ----- Generic block/alias proof engine -----
    alloc=[Allocation('a','k',4,4),Allocation('b','k',8,4)]
    req=[PackedRequest('ra',100,'k'),PackedRequest('rb',104,'k')]
    bp=solve_uniform_basis(alloc,req,phases=(0,),minimum_constraints=2)
    s.true('uniform packed block basis positive proof',bp['passed']);s.eq('uniform packed block basis value',bp['base'],100)
    one=solve_uniform_basis(alloc,[req[0]],phases=(0,),minimum_constraints=2);s.false('one packed pointer cannot prove basis',one['passed'])
    amb=solve_uniform_basis([Allocation('a','k',4),Allocation('b','k',4)],[PackedRequest('x',100,'k'),PackedRequest('y',100,'k')],minimum_constraints=2)
    s.false('ambiguous packed binding fails closed',amb['passed'])
    led=TypedAliasLedger();led.add(4,0x20,'sndcurve_root','curveA');s.eq('typed alias exact resolve',led.resolve(4,0x20,'sndcurve_root'),'curveA');s.eq('typed alias type separation',led.resolve(4,0x20,'loadedsound_root'),None)
    l2=TypedAliasLedger();l2.add(4,0x20,'sndcurve_root','curveA');ca=consensus_alias([led,l2],4,0x20,'sndcurve_root',2);s.true('typed alias consensus',ca['passed'])
    conflict=TypedAliasLedger();conflict.add(4,0x20,'sndcurve_root','curveB');cc=consensus_alias([led,conflict],4,0x20,'sndcurve_root',2);s.false('typed alias disagreement blocks',cc['passed'])

    inline=[{'name_hash':1,'name_start':2,'name_end':3,'sampler':4,'semantic':5,'image_identity':'img'}]
    packed=[{'description':'p','name_hash':1,'name_start':2,'name_end':3,'sampler':4,'semantic':5}]
    s.true('TextureDef complete-signature identity binding',bind_texture_signatures(inline,packed)['passed'])
    inline2=inline+[dict(inline[0],image_identity='other')];s.false('TextureDef ambiguous identity blocks',bind_texture_signatures(inline2,packed)['passed'])

    # ----- Packed GfxImage two-stage proof -----
    in_tex=[{'name_hash':10,'name_start':1,'name_end':2,'sampler':3,'semantic':4,'image_identity':'inlineA'}]
    alloc_rows=[
      {'identity':'imageA','kind':IMAGE_ALIAS_KIND,'size':4,'alignment':4},
      {'identity':'loadA','kind':LOADDEF_ALIAS_KIND,'size':4,'alignment':4},
      {'identity':'imageB','kind':IMAGE_ALIAS_KIND,'size':4,'alignment':4},
    ]
    # base=0x100: imageA at 0x100, loaddef at 0x104 (must never bind), imageB at 0x108.
    packed_img=[
      {'description':'p0','name_hash':20,'name_start':1,'name_end':2,'sampler':3,'semantic':4,'packed_offset':0x100},
      {'description':'p1','name_hash':21,'name_start':1,'name_end':2,'sampler':3,'semantic':4,'packed_offset':0x108},
    ]
    ip=resolve_packed_images(in_tex,packed_img,alloc_rows,phases=(0,),minimum_constraints=2)
    s.true('packed GfxImage two-target alias proof',ip['passed']);s.eq('packed GfxImage identities',[b['identity'] for b in ip['bindings']],['imageA','imageB'])
    shifted=[dict(packed_img[0]),dict(packed_img[1],packed_offset=0x10C)]
    s.false('packed GfxImage inconsistent common basis blocks',resolve_packed_images(in_tex,shifted,alloc_rows,phases=(0,),minimum_constraints=2)['passed'])
    loaddef_target=[dict(packed_img[0],packed_offset=0x104),packed_img[1]]
    s.false('GfxImageLoadDef alias cannot satisfy GfxImage root pointer',resolve_packed_images(in_tex,loaddef_target,alloc_rows,phases=(0,),minimum_constraints=2)['passed'])
    sig_packed=[{'description':'sig','name_hash':10,'name_start':1,'name_end':2,'sampler':3,'semantic':4}]
    sigres=resolve_packed_images(in_tex,sig_packed,alloc_rows)
    s.true('packed GfxImage exact TextureDef signature short-circuit',sigres['passed']);s.eq('packed GfxImage signature identity',sigres['bindings'][0]['identity'],'inlineA')

    # ----- Material state / PlatformState -----
    stale=solve_material_state_slots([0,3,0xFF],2,[1,0,0]);s.true('stale inactive material state slot may become FF',stale['passed']);s.eq('stale slot rewritten',stale['resolved_state_bits_entry'][1],0xFF)
    active=solve_material_state_slots([0,3,0xFF],2,[1,0x1234,0]);s.false('active out-of-range material state slot blocks',active['passed'])
    unknown=solve_material_state_slots([0,3,0xFF],2,[1,None,0]);s.false('unknown out-of-range material state slot blocks',unknown['passed'])
    cat=PlatformStateCatalog();cat.add('sig',bytes(range(56)),'retailA');pr=cat.resolve('sig');s.true('unique 0x38 PlatformState evidence resolves',pr['passed'])
    cat.add('sig',bytes(reversed(range(56))),'retailB');s.false('conflicting PlatformState evidence blocks',cat.resolve('sig')['passed'])

    # ----- Water 0x44 -> 0x48 and endian-normalized payload proof -----
    root=bytearray(0x44);struct.pack_into('<I',root,0,0x3F800000);struct.pack_into('<II',root,4,FOLLOWING,FOLLOWING);struct.pack_into('<ii',root,0x0C,2,3)
    for i in range(11):struct.pack_into('<I',root,0x14+i*4,0x3F000000+i)
    struct.pack_into('<I',root,0x40,FOLLOWING)
    h0=struct.pack('<4f',1.0,2.0,3.0,4.0);wt=struct.pack('<4f',5.0,6.0,7.0,8.0);pcw=parse_pc_water(bytes(root),h0,wt)
    psroot=build_ps3_water_root(pcw,FOLLOWING,FOLLOWING);psh0=b''.join(struct.pack('>f',x) for x in (1,2,3,4));pswt=b''.join(struct.pack('>f',x) for x in (5,6,7,8))
    wo=water_observation(pcw,psroot,psh0,pswt);s.true('Water PC 0x44 -> PS3 0x48 proof',wo['passed']);s.eq('Water extra PS3 pointer evidence',wo['extra_pointer'],FOLLOWING)
    psbad=bytearray(psh0);psbad[3]^=1;s.false('Water payload mismatch blocks',water_observation(pcw,psroot,bytes(psbad),pswt)['passed'])
    s.eq('Water normalized LE/BE payload fingerprint',normalized_pc_float_payload_to_ps3_sha256(h0),ps3_float_payload_sha256(psh0))

    # ----- Retail/hardware evidence builders -----
    pse=build_platformstate_retail_evidence({'required_signatures':['sigA'],'observations':[{'signature':'sigA','platform_state_hex':'00'*56,'source':'retail'}]})
    s.true('PlatformState retail evidence builder',pse['evidence'][0]['passed'])
    pse_bad=build_platformstate_retail_evidence({'required_signatures':['sigA','sigB'],'observations':[{'signature':'sigA','platform_state_hex':'00'*56}]})
    s.false('PlatformState incomplete retail coverage blocks',pse_bad['evidence'][0]['passed'])
    wse=build_water_retail_evidence({'pc_root_hex':bytes(root).hex(),'pc_h0_hex':h0.hex(),'pc_wterm_hex':wt.hex(),'ps3_root_hex':psroot.hex(),'ps3_h0_hex':psh0.hex(),'ps3_wterm_hex':pswt.hex(),'source_description':'selftest'})
    s.true('Water retail evidence builder',wse['evidence'][0]['passed'])
    with tempfile.TemporaryDirectory() as td:
        iw=Path(td)/'cube.iwi';iw.write_bytes(_make_iwi(cube=True));cr=build_cubemap_evidence({'source_iwi':str(iw),'target_face_hashes':perm})
        s.true('Cubemap PC-source evidence builder',cr[0]['evidence'][0]['passed']);s.true('Cubemap retail bijection evidence builder',cr[1]['evidence'][0]['passed'])

    # ----- Sound ABI/model -----
    s.eq('Sound ABI sizes',(ALIAS_LIST,ALIAS,SOUNDFILE_LEGACY,SOUNDFILE_EXTENDED,LOADED_SOUND,SND_CURVE,SPEAKER_MAP),(0x0C,0x5C,0x0C,0x20,0x2C,0x48,0x198))
    sev=[SoundAllocationEvent('head','head_array',8),SoundAllocationEvent('curveAlias','temp_xasset_root_alias:sndcurve',4)]
    sr=[PackedRequest('headref',0x100,'head_array'),PackedRequest('curveref',0x108,'temp_xasset_root_alias:sndcurve')]
    sm=build_sound_block4_model(sev,sr,minimum_constraints=2,phases=(0,));s.true('Sound typed block4 model',sm['passed'])
    sl=build_typed_sound_alias_ledger([{'kind':'sndcurve_root','offset':0x10,'identity':'c'},{'kind':'loadedsound_root','offset':0x14,'identity':'l'},{'kind':'loadedsound_data','offset':0x18,'identity':'d'}])
    s.eq('Sound root/member alias ledgers separated',(sl.resolve(4,0x10,'sndcurve_root'),sl.resolve(4,0x14,'loadedsound_root'),sl.resolve(4,0x18,'loadedsound_data')),('c','l','d'))

    # ----- Direct RawFile/StringTable/FX/ImpactFX/LightDef family scanners -----
    rz=bytearray(b'\x00'*0x2C)
    raw_payload=b'hello rawfile'
    rz += struct.pack('<III',FOLLOWING,len(raw_payload),FOLLOWING)+b'maps/mp/test.gsc\0'+raw_payload
    rr=scan_rawfiles(bytes(rz));s.eq('RawFile structural scanner count',len(rr),1);s.eq('RawFile payload exact',rr[0].payload,raw_payload)

    sz=bytearray(b'\x00'*0x2C)
    # 2 columns x 1 row, inline value pointer table, one string and one null cell.
    sz += struct.pack('<IIII',FOLLOWING,2,1,FOLLOWING)+b'tables/test.csv\0'+struct.pack('<II',FOLLOWING,NULL)+b'value\0'
    st=scan_stringtables(bytes(sz));s.eq('StringTable structural scanner count',len(st),1);s.eq('StringTable row-major values',st[0].values,('value',None))

    fz=bytearray(b'\x00'*0x80);fr=0x30
    struct.pack_into('<I',fz,fr,FOLLOWING)
    fz[fr+FX_ROOT:fr+FX_ROOT+9]=b'fx/test\0'
    fa=scan_fx_alias_shells(bytes(fz),0x2C);s.eq('FX name-only shared shell scanner count',len(fa),1);s.eq('FX shell semantic name',fa[0].name,'fx/test')

    # Complete owned FX graph: one type-10 visual with an inline sound-alias string.
    fxroot=bytearray(FX_ROOT);struct.pack_into('<I',fxroot,0,FOLLOWING);struct.pack_into('<III',fxroot,0x10,1,0,0);struct.pack_into('<I',fxroot,0x1C,FOLLOWING)
    elem=bytearray(FX_ELEM);elem[0xB0]=10;elem[0xB1]=1;struct.pack_into('<I',elem,0xBC,FOLLOWING)
    owned_zone=bytes(fxroot)+b'fx/owned_test\0'+bytes(elem)+b'sound_alias\0'
    owned=scan_owned_fx_graphs(owned_zone,0)
    s.eq('owned FX full ElemDef walk count',len(owned),1);s.eq('owned FX physical end',owned[0].end,len(owned_zone))

    # Synthetic ImpactFx with one FX target repeated across the exact 396-entry table.
    zh=struct.pack('<11I',0x4000,0,*([0]*9)); xl=struct.pack('<4I',0,NULL,2,FOLLOWING)
    pool=struct.pack('<II',0x19,FOLLOWING)+struct.pack('<II',0x1A,FOLLOWING)
    impact_root=struct.pack('<II',FOLLOWING,FOLLOWING)+b'impactfx\0'
    fx_alias_raw=encode_pc_packed(4,4)  # first XAssetHeader field in block4 alias pool
    iz=zh+xl+pool+impact_root+(struct.pack('<I',fx_alias_raw)*IMPACT_REFS)
    imp=scan_impactfx(iz);s.eq('ImpactFx exact table scanner count',len(imp),1);s.eq('ImpactFx resolved reference cardinality',sum(x is not None for x in imp[0].targets),396)

    # Synthetic LightDef with no attenuation dependency.
    zh=struct.pack('<11I',0x200,0,*([0]*9)); xl=struct.pack('<4I',0,NULL,1,FOLLOWING);pool=struct.pack('<II',0x11,FOLLOWING)
    ldroot=struct.pack('<II',FOLLOWING,NULL)+bytes([7,0,0,0])+struct.pack('<i',0)+b'light/test\0'
    lds=scan_lightdefs(zh+xl+pool+ldroot);s.eq('LightDef root scanner count',len(lds),1);s.eq('LightDef null attenuation retained',lds[0].image_pointer,NULL)

    # PhysPreset source inventory and exact inline sndAliasPrefix closure.
    zh=struct.pack('<11I',0x200,0,*([0]*9));xl=struct.pack('<4I',0,NULL,1,FOLLOWING);pool=struct.pack('<II',0x01,FOLLOWING)
    pr=bytearray(PHYSPRESET_ROOT);struct.pack_into('<I',pr,0,FOLLOWING);struct.pack_into('<i',pr,4,0);struct.pack_into('<I',pr,0x1C,FOLLOWING)
    pz=zh+xl+pool+bytes(pr)+b'phys/test\0'+b'wood\0'
    ps=scan_physpresets(pz);s.eq('PhysPreset structural scanner count',len(ps),1);s.eq('PhysPreset inline sound alias prefix',ps[0].sound_alias_prefix,'wood');s.eq('PhysPreset packed sound request count',len(physpreset_sound_requests(ps)),0)

    # ----- Source-tree audit/integrity -----
    source_audit=audit_csharp_source(reference_root)
    s.true('FIX113 source audit executable',source_audit['passed'])
    debt={x['id']:x['present'] for x in source_audit['findings']}
    s.true('FIX113 known Curve pool-index debt detected',debt.get('sound_curve_pool_index'))
    s.true('FIX113 neutral-image debt detected',debt.get('neutral_images'))
    manifest=_parse_manifest(HERE/'FIX113_REFERENCE_MANIFEST.sha256'); integrity=compare_tree_manifest(reference_root,manifest)
    s.true('FIX113 reference tree bit-identical',integrity['passed']);s.eq('FIX113 reference file count',integrity['current_files'],543)

    # ----- 26-point Full Fidelity gate -----
    all_docs=_machine_all_pass_docs();gg=evaluate_full_fidelity(all_docs)
    s.true('26/26 machine-evidence gate can pass',gg['full_fidelity_passed']);s.eq('Full Fidelity criterion count',gg['required_count'],26)
    only_source=evaluate_full_fidelity(evidence('fx_owned_exact',True,'pc_source_deep',{'owned_graphs':4}))
    s.false('FX requires independent source + conversion provenance',only_source['full_fidelity_passed'])
    fxrow=next(r for r in only_source['rows'] if r['criterion']=='fx_owned_exact');s.true('FX gate reports missing conversion provenance','conversion_audit' in fxrow['missing_sources'])
    bare=evaluate_full_fidelity(evidence('water_exact',True,'retail_observation',{}));s.false('bare PASS without machine proof rejected',bare['full_fidelity_passed']);s.true('bare PASS recorded as rejected evidence',bool(bare['rejected_evidence']))
    manual=evaluate_full_fidelity({'criterion':'water_exact','passed':True,'source':'manual','proof':{'x':1}});s.false('manual PASS rejected',manual['full_fidelity_passed'])
    neg=list(all_docs)+[evidence('water_exact',False,'retail_observation',{'mismatch':True})];ng=evaluate_full_fidelity(neg);s.false('trusted negative proof dominates positive',ng['full_fidelity_passed']);s.true('water criterion blocked by negative', 'water_exact' in ng['failed'])

    # Conversion report translator: exact count closure and explicit zero debts only.
    fc={
      'SourceXAssetCount':767,'ClassifiedSourceXAssetCount':767,
      'SourceSoundXAssetCount':160,'EmittedSoundXAssetCount':160,
      'SourceCustomAudioPayloadCount':17,'BoundCustomAudioPayloadCount':17,
      'SourcePhysPresetSoundAliasReferenceCount':9,'ResolvedPhysPresetSoundAliasReferenceCount':9,
      'PackedImageReferenceCount':12,'ResolvedPackedImageReferenceCount':12,
      'OwnedFxCount':4,'ExactOwnedFxCount':4,
      'ImpactFxReferenceCount':396,'ResolvedImpactFxReferenceCount':396,
      'LightDefAttenuationReferenceCount':1,'ResolvedLightDefAttenuationReferenceCount':1,
      'RequiredRawFileCount':7,'ExactRawFileCount':7,
      'RequiredStringTableCount':1,'ExactStringTableCount':1,
      'UnresolvedMaterialStateSlotCount':0,'NeutralImageFallbackCount':0,'PackedSourcePointerLeakCount':0,
      'UnresolvedPlatformStateCount':0,'WaterFallbackCount':0,'UnprovenCubemapCount':0,'UnsupportedCustomTechniqueSetCount':0,'UnresolvedOutputXAssetReferenceCount':0,
      'XModelSourceVertexCount':561815,'XModelOutputVertexCount':561815,'XModelSourceTriangleCount':543202,'XModelOutputTriangleCount':543202,
      'GfxWorldSourceSurfaceCount':4183,'GfxWorldOutputSurfaceCount':4183,'GfxWorldSourceVertexCount':133901,'GfxWorldOutputVertexCount':133901,
      'ClipMapSourceBrushCount':6639,'ClipMapOutputBrushCount':6639,'ClipMapSourceTriangleCount':18708,'ClipMapOutputTriangleCount':18708,
    }
    conv=evidence_from_conversion_report({'FidelityClosure':fc}); cv={x['criterion']:x['passed'] for x in conv['evidence']}
    s.true('conversion Source XAsset count closure',cv['source_xasset_closure']);s.true('conversion Sound count closure',cv['sound_graph_closure']);s.true('conversion packed Image closure',cv['packed_image_identity_exact']);s.true('conversion material state zero blocker',cv['material_state_slots_exact']);s.true('conversion XModel closure',cv['xmodel_exact']);s.true('conversion GfxWorld closure',cv['gfxworld_exact']);s.true('conversion ClipMap closure',cv['clipmap_exact'])
    fc_bad=dict(fc);fc_bad['ResolvedPackedImageReferenceCount']=11;cvbad=evidence_from_conversion_report({'FidelityClosure':fc_bad});img_ev=next(x for x in cvbad['evidence'] if x['criterion']=='packed_image_identity_exact');s.false('conversion count mismatch blocks packed images',img_ev['passed'])
    empty_conv=evidence_from_conversion_report({'FidelityClosure':{'someFreeFormPass':True}});s.eq('free-form conversion pass creates no evidence',len(empty_conv['evidence']),0)

    # ----- One-pass orchestration fail-closed -----
    with tempfile.TemporaryDirectory() as td:
        config={'output_dir':str(Path(td)/'out'),'ps3_load':str(load),'source_root':str(reference_root)}
        pipe=run_pipeline(config)
        s.true('pipeline itself runs without parser errors',pipe['backend_pipeline_passed'])
        s.false('pipeline without PC/conversion/retail evidence cannot Full-Fidelity pass',pipe['full_fidelity_passed'])
        s.true('pipeline reports missing evidence',bool(pipe['full_fidelity_gate']['missing']))
        s.true('pipeline independent _load evidence passes',next(r for r in pipe['full_fidelity_gate']['rows'] if r['criterion']=='load_zone_exact')['passed'])
        s.true('pipeline deterministic write evidence passes',next(r for r in pipe['full_fidelity_gate']['rows'] if r['criterion']=='deterministic_write')['passed'])

    return s.result()


def main()->int:
    ap=argparse.ArgumentParser();ap.add_argument('--reference-root',default=str(DEFAULT_REF));ap.add_argument('--output',default=str(HERE/'FIX114_V4_SELFTEST.json'))
    a=ap.parse_args();result=run(Path(a.reference_root));Path(a.output).write_text(json.dumps(result,indent=2,ensure_ascii=False),encoding='utf-8')
    print(json.dumps({'passed':result['passed'],'check_count':result['check_count'],'failed':[x['name'] for x in result['failed']]},indent=2))
    return 0 if result['passed'] else 1

if __name__=='__main__':raise SystemExit(main())
