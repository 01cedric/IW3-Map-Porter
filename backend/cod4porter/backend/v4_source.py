from __future__ import annotations
from dataclasses import asdict
from pathlib import Path
import hashlib, json, re
from typing import Iterable
from .v4_ffio import read_pc_fastfile, parse_zone_header, parse_xasset_list, decode_pc_pointer, summarize_fastfile
from .v4_iwd import inspect_iwds

KNOWN_DEBT_TOKENS={
    'sound_curve_pool_index': ('PcSoundAliasCatalog.cs', ['ResolvePackedCurves','resolves beyond the PC XAsset pool','assetIndex']),
    'neutral_images': (None, ['__fix103_neutral_semantic_']),
    'water_default_fallback': ('Ps3FinalMainFastFileExporter.cs', ["water compatibility", "stock ',default'"]),
    'platformstate_zero': (None, ['0x38-byte neutral zero state']),
    'material_state_neutralization': (None, ['NeutralizedOutOfRangePcMaterialStateIndexCount']),
    'stringtable_compatibility': (None, ['FIX105 StringTable recovery']),
    'lightdef_null_attenuation': (None, ['attenuation=NULL']),
    'owned_fx_fallback': (None, ['ownedFxReferenceFallbacks']),
}

def file_sha256(path:Path)->str:
    h=hashlib.sha256()
    with path.open('rb') as f:
        for c in iter(lambda:f.read(1024*1024),b''): h.update(c)
    return h.hexdigest().upper()


def inspect_pc_source(pc_ff:str|Path,iwd_paths:Iterable[str|Path]=(),expected:dict|None=None,deep_assets:bool=False,deep_sound:bool=False)->dict:
    pc_ff=Path(pc_ff); doc=read_pc_fastfile(pc_ff); header=parse_zone_header(doc.zone,'pc'); al=parse_xasset_list(doc.zone,'pc')
    counts={}
    ptrk={}
    top=[]
    for a in al.assets:
        counts[a.type_name]=counts.get(a.type_name,0)+1
        p=decode_pc_pointer(a.serialized_pointer); ptrk[p.kind]=ptrk.get(p.kind,0)+1
        top.append({'index':a.index,'type':a.type_name,'type_id':a.type_id,'pointer_raw':f'0x{a.serialized_pointer:08X}','pointer_kind':p.kind,
                    'packed_block':p.block,'packed_offset':p.offset})
    images,sounds=inspect_iwds(iwd_paths) if list(iwd_paths) else ({},[])
    iwi_counts={}
    cube=[]
    for e in images.values():
        iwi_counts[e.image.format_name]=iwi_counts.get(e.image.format_name,0)+1
        if e.image.is_cubemap: cube.append(e.canonical_name)
    checks=[]
    if expected:
        def chk(name,actual,key): checks.append({'name':name,'passed':actual==expected.get(key),'actual':actual,'expected':expected.get(key)})
        if 'script_strings' in expected: chk('script strings',len(al.script_strings),'script_strings')
        if 'xassets' in expected: chk('XAssets',len(al.assets),'xassets')
        if 'iwi' in expected: chk('IWI',len(images),'iwi')
        if 'audio' in expected: chk('IWD audio',len(sounds),'audio')
    result={
        'kind':'pc_source_deep','pc_ff':str(pc_ff.resolve()),'fastfile':summarize_fastfile(pc_ff,'pc'),
        'zone_header':asdict(header),'script_strings':len(al.script_strings),'xassets':len(al.assets),'asset_counts':counts,
        'top_level_pointer_kinds':ptrk,'top_level_assets':top,
        'iwd':{'paths':[str(Path(p).resolve()) for p in iwd_paths],'images':len(images),'image_formats':iwi_counts,'cubemaps':cube,'audio':len(sounds),
               'audio_extensions':{ext:sum(1 for s in sounds if s.extension==ext) for ext in sorted({s.extension for s in sounds})}},
        'expected_checks':checks,'expected_passed':all(c['passed'] for c in checks),
        'source_byte_evidence':True,
    }
    if deep_assets:
        from .v4_assets import scan_asset_families
        result['asset_family_scan']=scan_asset_families(doc.zone)
    if deep_sound:
        from .v4_sound import scan_sound_shells
        result['sound_shell_scan']=scan_sound_shells(doc.zone)
    return result


def audit_csharp_source(source_root:str|Path)->dict:
    root=Path(source_root)
    if not root.exists(): return {'passed':False,'blockers':[f'source root does not exist: {root}'],'findings':[]}
    files=list(root.rglob('*.cs')); by_name={p.name:p for p in files}; findings=[]; blockers=[]
    sound=by_name.get('PcSoundAliasCatalog.cs')
    if sound:
        text=sound.read_text(encoding='utf-8',errors='replace'); region=_extract_method(text,'ResolvePackedCurves')
        bad=[x for x in ('resolves beyond the PC XAsset pool','asset #','assetIndex','assetList.Assets[') if x in region]
        findings.append({'id':'sound_curve_pool_index','present':bool(bad),'file':str(sound.relative_to(root)),'tokens':bad})
    else: blockers.append('PcSoundAliasCatalog.cs not found')
    for debt,(preferred,tokens) in KNOWN_DEBT_TOKENS.items():
        if debt=='sound_curve_pool_index': continue
        hits=[]
        candidates=[by_name[preferred]] if preferred and preferred in by_name else files
        for p in candidates:
            txt=p.read_text(encoding='utf-8',errors='replace')
            present=[t for t in tokens if t in txt]
            if present: hits.append({'file':str(p.relative_to(root)),'tokens':present})
        findings.append({'id':debt,'present':bool(hits),'hits':hits[:20]})
    return {'passed':not blockers,'files_scanned':len(files),'blockers':blockers,'findings':findings}


def _extract_method(text:str,name:str)->str:
    m=re.search(r'(?m)^[ \t]*(?:public|private|internal|protected)[^\n;{}]*\b'+re.escape(name)+r'\s*\(',text)
    if not m:return ''
    brace=text.find('{',m.end())
    if brace<0:return text[m.start():m.start()+30000]
    depth=0; in_string=False; verbatim=False; esc=False
    for i in range(brace,len(text)):
        ch=text[i]
        if in_string:
            if verbatim:
                if ch=='"' and i+1<len(text) and text[i+1]=='"': continue
                if ch=='"': in_string=False;verbatim=False
            else:
                if esc:esc=False
                elif ch=='\\':esc=True
                elif ch=='"':in_string=False
            continue
        if ch=='@' and i+1<len(text) and text[i+1]=='"': in_string=True;verbatim=True;continue
        if ch=='"':in_string=True;continue
        if ch=='{':depth+=1
        elif ch=='}':
            depth-=1
            if depth==0:return text[m.start():i+1]
    return text[m.start():]


def build_tree_manifest(root:str|Path)->dict[str,str]:
    root=Path(root); out={}
    for p in sorted(x for x in root.rglob('*') if x.is_file()): out[str(p.relative_to(root)).replace('\\','/')]=file_sha256(p)
    return out


def compare_tree_manifest(root:str|Path,manifest:dict[str,str])->dict:
    current=build_tree_manifest(root); missing=sorted(set(manifest)-set(current)); added=sorted(set(current)-set(manifest)); changed=sorted(k for k in set(current)&set(manifest) if current[k]!=manifest[k])
    return {'passed':not missing and not added and not changed,'expected_files':len(manifest),'current_files':len(current),'missing':missing,'added':added,'changed':changed}
