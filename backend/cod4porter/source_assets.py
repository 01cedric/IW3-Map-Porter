from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Mapping, Sequence

from .backend.v4_assets import scan_rawfiles, scan_lightdefs
from .backend.v4_ffio import XAssetList, decode_pc_pointer
from .pc_fx import ImpactFx, parse_impact_fx
from .pc_xmodel import xasset_header_alias_index

PC_FX=0x19
PC_RAWFILE=0x1F
PC_STRINGTABLE=0x20
PC_LIGHTDEF=0x11
PC_IMAGE=0x06

@dataclass(frozen=True)
class SourceRawFile:
    source_asset_index:int
    root_offset:int
    name:str
    payload:bytes
    physical_end:int=-1
    name_runtime_fallback:bool=False
    name_pointer:int=0

@dataclass(frozen=True)
class SourceStringTable:
    source_asset_index:int
    root_offset:int
    name:str
    columns:int
    rows:int
    values:tuple[str|None,...]
    packed_cell_count:int
    resolved_local_packed_cells:int
    resolved_global_packed_cells:int
    physical_end:int=-1
    serialized_pointers:tuple[int,...]=()

@dataclass(frozen=True)
class SourceLightDef:
    source_asset_index:int
    root_offset:int
    name:str
    sampler:int
    lmap_lookup_start:int
    attenuation_image_name:str|None
    attenuation_image_source_asset_index:int|None
    attenuation_pointer_kind:str='null'
    attenuation_image_has_resource:bool|None=None


def _read_latin1_z(zone:bytes,cursor:int,max_bytes:int)->tuple[str,int]:
    if cursor<0 or cursor>=len(zone): raise ValueError('string cursor out of range')
    end=zone.find(b'\0',cursor,min(len(zone),cursor+max_bytes))
    if end<0: raise ValueError('unterminated string')
    raw=zone[cursor:end]
    if any(b<0x09 or b>0xFE or b in (0x0B,0x0C) for b in raw): raise ValueError('non-printable Latin1 string')
    return raw.decode('latin-1'),end+1


def _looks_table_name(name:str)->bool:
    if not name or len(name)>260 or name[0] in '$,': return False
    if any(not (c.isalnum() or c in '_-./\\@') for c in name): return False
    low=name.lower()
    return low.endswith('.csv') or low.endswith('.str') or '/' in low


def _u32(zone:bytes,off:int)->int:
    return int.from_bytes(zone[off:off+4],'little')


def _scan_stringtables(zone:bytes)->list[dict]:
    """Port of FIX114's conservative local packed-XString table replay."""
    out=[];root=0x2C;limit=len(zone)-0x11
    while root<=limit:
        if zone[root] not in (0xFE,0xFF): root+=1;continue
        np=decode_pc_pointer(_u32(zone,root))
        if np.kind not in ('following','insert'): root+=1;continue
        cols=_u32(zone,root+4);rows=_u32(zone,root+8)
        if cols==0 or cols>1024 or rows>1_000_000: root+=1;continue
        cells=cols*rows
        if cells>1_000_000: root+=1;continue
        vp=decode_pc_pointer(_u32(zone,root+0x0C))
        if cells and vp.kind not in ('following','insert'): root+=1;continue
        if not cells and vp.kind not in ('null','following','insert'): root+=1;continue
        try:
            cursor=root+0x10;name,cursor=_read_latin1_z(zone,cursor,512)
            if not _looks_table_name(name): root+=1;continue
            local_cursor=0x10+len(name.encode('latin-1'))+1
            if cursor+cells*4>len(zone): root+=1;continue
            ptrs=[_u32(zone,cursor+i*4) for i in range(cells)];cursor+=cells*4;local_cursor+=cells*4
            if any(decode_pc_pointer(x).kind not in ('null','following','insert','packed') for x in ptrs): root+=1;continue
            vals=[None]*cells;locals_=[];packed=[];ok=True
            for i,raw in enumerate(ptrs):
                p=decode_pc_pointer(raw)
                if p.kind=='null': continue
                if p.kind=='packed':
                    if p.block!=4:ok=False;break
                    packed.append((i,raw,p.offset));continue
                if p.kind=='insert':ok=False;break
                local=local_cursor
                val,cursor=_read_latin1_z(zone,cursor,16*1024);vals[i]=val
                locals_.append((local,val));local_cursor+=len(val.encode('latin-1'))+1
            if not ok: root+=1;continue
            resolved_local=0;unresolved=[]
            if packed:
                bases={target-local for _,_,target in packed for local,_ in locals_ if target>=local}
                passing=[]
                for basis in bases:
                    resolved=[];good=True
                    for _,_,target in packed:
                        local=target-basis if target>=basis else -1
                        matches=[v for o,v in locals_ if o==local]
                        if len(matches)!=1:good=False;break
                        resolved.append(matches[0])
                    if good:passing.append(tuple(resolved))
                if passing and all(x==passing[0] for x in passing):
                    for (i,_,_),v in zip(packed,passing[0]):vals[i]=v;resolved_local+=1
                else:unresolved=[raw for _,raw,_ in packed]
            out.append({'root':root,'end':cursor,'name':name.replace('\\','/'),'columns':cols,'rows':rows,'values':tuple(vals),'ptrs':tuple(ptrs),'packed':len(packed),'resolved_local':resolved_local,'unresolved':tuple(sorted(set(unresolved)))})
            root=max(root+1,cursor)
        except (ValueError,OverflowError,IndexError):
            root+=1
    uniq={x['root']:x for x in out}
    return [uniq[k] for k in sorted(uniq)]


def _parse_rawfile_at(zone:bytes,root:int,asset_index:int,packed_name_evidence:Mapping[int,str],runtime_compatible:bool)->SourceRawFile:
    if root<0 or root+0x0C>len(zone):raise ValueError(f'RawFile XAsset #{asset_index} root outside zone')
    name_raw=_u32(zone,root);length=_u32(zone,root+4);data_raw=_u32(zone,root+8)
    if length>16*1024*1024:raise ValueError(f'RawFile XAsset #{asset_index} implausible length {length}')
    np=decode_pc_pointer(name_raw);dp=decode_pc_pointer(data_raw)
    if np.kind not in ('following','insert','packed'):raise ValueError(f'RawFile XAsset #{asset_index} unsupported name pointer {np.kind}')
    if dp.kind not in ('following','insert'):raise ValueError(f'RawFile XAsset #{asset_index} unsupported data pointer {dp.kind}')
    cursor=root+0x0C;runtime_name=False
    if np.kind in ('following','insert'):
        name,cursor=_read_latin1_z(zone,cursor,512)
        name=name.replace('\\','/')
        if not name or len(name)>260 or any(not (c.isalnum() or c in '_-./\\@') for c in name):
            raise ValueError(f'RawFile XAsset #{asset_index} invalid inline name {name!r}')
    else:
        name=packed_name_evidence.get(name_raw)
        if name is None and runtime_compatible:
            name=f'__cp11_rawfile_{asset_index:04d}'
            runtime_name=True
        if name is None:
            raise ValueError(f'RawFile XAsset #{asset_index} packed name 0x{name_raw:08X} has no exact XString identity')
    if cursor+length>=len(zone)+1:raise ValueError(f"RawFile '{name}' payload outside zone")
    payload=zone[cursor:cursor+length]
    # IW3 RawFile::len excludes the source trailing NUL; an inline data pointer still traverses
    # len+1 physical bytes.  This is the byte that makes each Getaway RawFile end exactly at the
    # next top-level XAsset root (and the final zero-length RawFile end exactly at EOF).
    terminator=cursor+length
    if terminator>=len(zone) or zone[terminator]!=0:
        raise ValueError(f"RawFile '{name}' missing source payload terminator")
    physical_end=terminator+1
    return SourceRawFile(asset_index,root,name,payload,physical_end,runtime_name,name_raw)


def top_level_rawfiles(zone:bytes,asset_list:XAssetList,*,root_hints_by_asset_index:Mapping[int,int]|None=None,
        packed_name_evidence:Mapping[int,str]|None=None,runtime_compatible:bool=False)->tuple[SourceRawFile,...]:
    if asset_list.structural_index is not None:
        index=asset_list.structural_index;out=[]
        for row in index.rows(PC_RAWFILE):
            r=row['root'];n=_u32(zone,r+4);p=index.pointer(r+8)
            payload=zone[p:p+n] if p is not None else b''
            if len(payload)!=n:raise ValueError('RawFile payload length differs')
            out.append(SourceRawFile(row['index'],r,row['name'],payload,row['end'],False,_u32(zone,r)))
        return tuple(out)
    assets=sorted((a for a in asset_list.assets if a.type_id==PC_RAWFILE),key=lambda a:a.index)
    if not assets:return ()
    packed=[a for a in assets if decode_pc_pointer(a.serialized_pointer).kind=='packed']
    if packed:raise ValueError(f'RawFile full-fidelity closure has {len(packed)} packed/shared top-level header(s)')
    if any(decode_pc_pointer(a.serialized_pointer).kind not in ('following','insert') for a in assets):raise ValueError('RawFile unsupported top-level pointer kind')
    hints=dict(root_hints_by_asset_index or {});evidence=dict(packed_name_evidence or {})
    if hints:
        # Replay each consecutive source-XAsset RawFile run from a typed neighbouring boundary.
        # Only the first member of a run needs a hint; subsequent roots are the prior RawFile's
        # exact physical end (including the source NUL).  This proves source order without a zone scan.
        out=[];i=0
        while i<len(assets):
            j=i+1
            while j<len(assets) and assets[j].index==assets[j-1].index+1:j+=1
            group=assets[i:j];root=hints.get(group[0].index)
            if root is None:
                raise ValueError(f'RawFile source-order run #{group[0].index}-#{group[-1].index} has no typed physical root hint')
            for a in group:
                row=_parse_rawfile_at(zone,root,a.index,evidence,runtime_compatible);out.append(row);root=row.physical_end
            # If a hint exists for the next source asset, exact adjacency is required.
            next_hint=hints.get(group[-1].index+1)
            if next_hint is not None and root!=next_hint:
                raise ValueError(f'RawFile run #{group[0].index}-#{group[-1].index} ends 0x{root:X}, expected next typed root 0x{next_hint:X}')
            i=j
        return tuple(out)
    candidates=scan_rawfiles(zone)
    # Legacy generic fallback.  Production Getaway uses typed source-order hints above.
    runs=[];byroot={x.root:x for x in candidates}
    for x in candidates:
        cur=x;run=[x]
        for _ in range(1,len(assets)):
            nxt=byroot.get(cur.end) or byroot.get(cur.end+1)
            if nxt is None:break
            run.append(nxt);cur=nxt
        if len(run)==len(assets):runs.append(run)
    if len(runs)!=1:
        if len(candidates)==len(assets):runs=[sorted(candidates,key=lambda x:x.root)]
        else:raise ValueError(f'RawFile run ambiguous/incomplete: declared={len(assets)} candidates={len(candidates)} complete_runs={len(runs)}')
    return tuple(SourceRawFile(a.index,r.root,r.name.replace('\\','/'),r.payload,r.end+1,False) for a,r in zip(assets,runs[0]))

def top_level_stringtables(zone:bytes,asset_list:XAssetList,global_packed_xstrings:Mapping[int,str]|None=None,*,root_hints:Sequence[int]=(),runtime_compatible:bool=False)->tuple[SourceStringTable,...]:
    if asset_list.structural_index is not None:
        index=asset_list.structural_index;out=[]
        for row in index.rows(PC_STRINGTABLE):
            r=row['root'];cols=_u32(zone,r+4);rows=_u32(zone,r+8);p=index.pointer(r+12)
            pointers=tuple(_u32(zone,p+i*4) for i in range(cols*rows))
            values=tuple(index.pointer(p+i*4) for i in range(cols*rows))
            packed=sum(decode_pc_pointer(raw).kind=='packed' for raw in pointers)
            out.append(SourceStringTable(row['index'],r,row['name'],cols,rows,values,packed,0,packed,row['end'],pointers))
        return tuple(out)
    assets=sorted((a for a in asset_list.assets if a.type_id==PC_STRINGTABLE),key=lambda a:a.index)
    if not assets:return ()
    packed_headers=[a for a in assets if decode_pc_pointer(a.serialized_pointer).kind=='packed']
    if packed_headers:raise ValueError(f'StringTable full-fidelity closure has {len(packed_headers)} packed/shared top-level header(s)')
    if any(decode_pc_pointer(a.serialized_pointer).kind not in ('following','insert') for a in assets):raise ValueError('StringTable unsupported top-level pointer kind')
    # Prefer typed direct roots.  If the caller did not provide them, locate concrete .csv/.str
    # name terminators with C-speed bytes.find and test the fixed 0x10-byte root immediately before
    # the name.  This avoids a byte-by-byte 116-MiB zone scan while retaining the same parser.
    hints=list(root_hints)
    if not hints:
        pathchars=set(b"ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789_-./\\@")
        roots=set()
        for suffix in (b'.csv\0',b'.str\0'):
            pos=0
            while True:
                end=zone.find(suffix,pos)
                if end<0:break
                start=end
                while start>0 and zone[start-1] in pathchars and end-start<260:start-=1
                root=start-0x10
                if root>=0:roots.add(root)
                pos=end+1
        hints=sorted(roots)
    if hints:
        # Reuse the conservative parser by restricting its input to exact candidate roots.
        candidates=[]
        for root in hints:
            try:
                np=decode_pc_pointer(_u32(zone,root));cols=_u32(zone,root+4);rows=_u32(zone,root+8);vp=decode_pc_pointer(_u32(zone,root+0x0C))
                if np.kind not in ('following','insert') or cols==0 or cols>1024 or rows>1_000_000:continue
                cells=cols*rows
                if cells>1_000_000 or (cells and vp.kind not in ('following','insert')):continue
                cursor=root+0x10;name,cursor=_read_latin1_z(zone,cursor,512)
                if not _looks_table_name(name) or cursor+cells*4>len(zone):continue
                ptrs=[_u32(zone,cursor+i*4) for i in range(cells)];cursor+=cells*4;local_cursor=0x10+len(name.encode('latin-1'))+1+cells*4
                vals=[None]*cells;locals_=[];packed=[]
                for ci,raw in enumerate(ptrs):
                    pp=decode_pc_pointer(raw)
                    if pp.kind=='null':continue
                    if pp.kind=='packed':
                        if pp.block!=4:raise ValueError('StringTable packed cell not block4')
                        packed.append((ci,raw,pp.offset));continue
                    if pp.kind!='following':raise ValueError('StringTable inline cell not FOLLOWING')
                    local=local_cursor;val,cursor=_read_latin1_z(zone,cursor,16*1024);vals[ci]=val;locals_.append((local,val));local_cursor+=len(val.encode('latin-1'))+1
                resolved_local=0;unresolved=[]
                if packed:
                    bases={target-local for _,_,target in packed for local,_ in locals_ if target>=local};passing=[]
                    for basis in bases:
                        rr=[];good=True
                        for _,_,target in packed:
                            local=target-basis if target>=basis else -1;matches=[v for o,v in locals_ if o==local]
                            if len(matches)!=1:good=False;break
                            rr.append(matches[0])
                        if good:passing.append(tuple(rr))
                    if passing and all(x==passing[0] for x in passing):
                        for (ci,_,_),v in zip(packed,passing[0]):vals[ci]=v;resolved_local+=1
                    else:unresolved=[raw for _,raw,_ in packed]
                candidates.append({'root':root,'end':cursor,'name':name.replace('\\','/'),'columns':cols,'rows':rows,'values':tuple(vals),'ptrs':tuple(ptrs),'packed':len(packed),'resolved_local':resolved_local,'unresolved':tuple(sorted(set(unresolved)))})
            except (ValueError,OverflowError,IndexError):
                continue
    else:
        candidates=_scan_stringtables(zone)
    if len(candidates)!=len(assets):raise ValueError(f'StringTable scan found {len(candidates)} candidates for {len(assets)} source XAssets')
    evidence=dict(global_packed_xstrings or {})
    result=[]
    for a,c in zip(assets,candidates):
        vals=list(c['values']);global_count=0;unresolved=[]
        for i,(v,raw) in enumerate(zip(vals,c['ptrs'])):
            if v is not None:continue
            p=decode_pc_pointer(raw)
            if p.kind!='packed':continue
            exact=evidence.get(raw)
            if exact is None:unresolved.append(raw)
            else:vals[i]=exact;global_count+=1
        # mp/mapstable.csv has two strong structural identities that let us recover packed
        # XStrings without guessing a block-4 basis:
        #   * columns 1 and 2 are duplicated environment/theme fields in every row; when one
        #     side is inline and the other side is packed, the packed identity is proven.
        #   * column 0 follows the exact grammar mp_<suffix>, while columns 3/4/6 carry
        #     MPUI_<suffix>, loadscreen_mp_<suffix>, MPUI_DESC_MAP_<suffix>.  If all three
        #     suffixes agree, an unresolved packed map-name cell is exactly recoverable.
        # This closes Getaway's four unique packed words while remaining fail-closed for any
        # table that does not satisfy the full observed schema.
        structural_count=0
        if unresolved and c['name'].casefold()=='mp/mapstable.csv' and c['columns']==8:
            rows=c['rows'];raw_proofs={}
            # Prove duplicate col1/col2 semantics.  Every row either has equal concrete values
            # or equal serialized words; rows with one concrete side anchor the packed raw.
            duplicate_schema=True
            for r in range(rows):
                i1=r*8+1;i2=r*8+2
                v1,v2=vals[i1],vals[i2];raw1,raw2=c['ptrs'][i1],c['ptrs'][i2]
                if v1 is not None and v2 is not None and v1!=v2:duplicate_schema=False;break
                if v1 is None and v2 is None and raw1!=raw2:duplicate_schema=False;break
            if duplicate_schema:
                for r in range(rows):
                    i1=r*8+1;i2=r*8+2;v1,v2=vals[i1],vals[i2];raw1,raw2=c['ptrs'][i1],c['ptrs'][i2]
                    if v1 is not None and v2 is None and decode_pc_pointer(raw2).kind=='packed':
                        old=raw_proofs.get(raw2)
                        if old is not None and old!=v1:raise ValueError('mapstable packed environment identity conflict')
                        raw_proofs[raw2]=v1
                    if v2 is not None and v1 is None and decode_pc_pointer(raw1).kind=='packed':
                        old=raw_proofs.get(raw1)
                        if old is not None and old!=v2:raise ValueError('mapstable packed environment identity conflict')
                        raw_proofs[raw1]=v2
            # Column 7 is constant in this source table: one inline anchor plus one repeated packed
            # raw across every remaining row proves the shared identity.
            c7_known={vals[r*8+7] for r in range(rows) if vals[r*8+7] is not None}
            c7_raw={c['ptrs'][r*8+7] for r in range(rows) if vals[r*8+7] is None and decode_pc_pointer(c['ptrs'][r*8+7]).kind=='packed'}
            if len(c7_known)==1 and len(c7_raw)==1:
                raw_proofs[next(iter(c7_raw))]=next(iter(c7_known))
            # Apply only identities anchored by the table's own schema.
            for i,(v,raw) in enumerate(zip(vals,c['ptrs'])):
                if v is None and raw in raw_proofs:
                    vals[i]=raw_proofs[raw];structural_count+=1
            # Recover unresolved map-name cells from three mutually agreeing schema columns.
            for r in range(rows):
                i0=r*8
                if vals[i0] is not None or decode_pc_pointer(c['ptrs'][i0]).kind!='packed':continue
                v3,v4,v6=vals[i0+3],vals[i0+4],vals[i0+6]
                if not (isinstance(v3,str) and isinstance(v4,str) and isinstance(v6,str)):continue
                if not v3.startswith('MPUI_') or not v4.startswith('loadscreen_mp_') or not v6.startswith('MPUI_DESC_MAP_'):continue
                s3=v3[5:];s4=v4[len('loadscreen_mp_'):];s6=v6[len('MPUI_DESC_MAP_'):]
                if s3==s4==s6 and s3:
                    vals[i0]='mp_'+s3;structural_count+=1
            unresolved=[raw for i,(v,raw) in enumerate(zip(vals,c['ptrs'])) if v is None and decode_pc_pointer(raw).kind=='packed']
        runtime_count=0
        runtime_fallback_raws=()
        if unresolved and runtime_compatible:
            # Destination-local safety fallback only.  Preserve equality: every repeated raw PC
            # packed pointer maps to the same regenerated string.  This prevents dangling PC
            # pointers while keeping unresolved semantic identity explicit in the report.
            fallback_by_raw={}
            for raw in sorted(set(unresolved)):
                fallback_by_raw[raw]=f'__cp11_stringtable_{a.index:04d}_{raw:08x}'
            for i,(v,raw) in enumerate(zip(vals,c['ptrs'])):
                if v is None and raw in fallback_by_raw:
                    vals[i]=fallback_by_raw[raw];runtime_count+=1
            runtime_fallback_raws=tuple(sorted(fallback_by_raw))
            unresolved=[]
        if unresolved:raise ValueError(f"StringTable '{c['name']}' unresolved packed XStrings: {[hex(x) for x in unresolved[:12]]}")
        row=SourceStringTable(a.index,c['root'],c['name'],c['columns'],c['rows'],tuple(vals),c['packed'],c['resolved_local']+structural_count,global_count,c['end'],tuple(c['ptrs']))
        object.__setattr__(row,'runtime_fallback_cells',runtime_count)
        object.__setattr__(row,'runtime_fallback_raws',runtime_fallback_raws)
        object.__setattr__(row,'structural_recovery_cells',structural_count)
        result.append(row)
    return tuple(result)


def top_level_impactfx(zone:bytes,asset_list:XAssetList)->tuple[ImpactFx,...]:
    return tuple(parse_impact_fx(zone,asset_list))


def packed_xstring_evidence_from_tables(tables:Sequence[SourceStringTable])->dict[int,str]:
    """Return exact packed-word identities proven by regenerated StringTable cells.

    The serialized pointer word, rather than only the resolved text, is important here.  A later
    asset can legally use the same block-4 XString (or an interior substring of another inline
    string).  Getaway RawFile #766 and mapstable cell 112 both use ``0x4048EBF1`` for
    ``mp_getaway``; carrying that exact equality avoids inventing a runtime placeholder name.
    """
    evidence={}
    for table in tables:
        if len(table.serialized_pointers)!=len(table.values):
            raise ValueError(f"StringTable '{table.name}' lost serialized pointer cardinality")
        runtime_fallback_raws=set(getattr(table,'runtime_fallback_raws',()))
        for raw,value in zip(table.serialized_pointers,table.values):
            if value is None or raw in runtime_fallback_raws or decode_pc_pointer(raw).kind!='packed':continue
            old=evidence.get(raw)
            if old is not None and old!=value:
                raise ValueError(f'packed XString 0x{raw:08X} has conflicting table identities {old!r} and {value!r}')
            evidence[raw]=value
    return evidence


def top_level_lightdefs(zone:bytes,asset_list:XAssetList,image_name_by_source_asset_index:Mapping[int,str],*,
        root_hints:Sequence[int]=(),packed_name_evidence:Mapping[int,str]|None=None,runtime_compatible:bool=False)->tuple[SourceLightDef,...]:
    if asset_list.structural_index is not None:
        index=asset_list.structural_index;images={r['root']:r['index'] for r in index.rows(PC_IMAGE)};out=[]
        for row in index.rows(PC_LIGHTDEF):
            r=row['root'];image=index.pointer(r+4);kind=decode_pc_pointer(_u32(zone,r+4)).kind
            name=index.pointer(image+0x20) if image is not None else None
            has_resource=index.pointer(image+4) is not None if image is not None else None
            out.append(SourceLightDef(row['index'],r,row['name'],zone[r+8],int.from_bytes(zone[r+12:r+16],'little',signed=True),name,images.get(image),kind,has_resource))
        return tuple(out)
    assets=sorted((a for a in asset_list.assets if a.type_id==PC_LIGHTDEF),key=lambda a:a.index)
    if not assets:return ()
    if any(decode_pc_pointer(a.serialized_pointer).kind not in ('following','insert') for a in assets):
        raise ValueError('LightDef packed/shared top-level root identity is unresolved')
    packed_name_evidence=dict(packed_name_evidence or {})

    # Prefer source-order physical roots supplied by the immediately preceding fully replayed asset.
    # This avoids the old whole-zone LightDef shape scan, which matched shader/material byte patterns.
    # Each hinted root is still replayed as a complete typed LightDef + optional inline GfxImage.
    rows=[]
    for root in root_hints:
        try:
            if root<0 or root+0x10>len(zone):continue
            name_raw=_u32(zone,root);namep=decode_pc_pointer(name_raw);image_raw=_u32(zone,root+4);ip=decode_pc_pointer(image_raw)
            if namep.kind not in ('following','insert','packed'):continue
            if ip.kind not in ('null','packed','following','insert'):continue
            lmap=int.from_bytes(zone[root+0x0C:root+0x10],'little',signed=True)
            if lmap < -1 or lmap>1_000_000:continue
            cursor=root+0x10;name=None;name_runtime=False
            if namep.kind in ('following','insert'):
                name,cursor=_read_latin1_z(zone,cursor,512)
            else:
                name=packed_name_evidence.get(name_raw)
                if name is None and runtime_compatible:
                    name=f'__cp11_lightdef_{assets[len(rows)].index:04d}'
                    name_runtime=True
                if name is None:raise ValueError(f'LightDef root 0x{root:X} packed name 0x{name_raw:08X} has no exact identity evidence')
            image_name=None;semantic=None
            image_has_resource=None
            if ip.kind in ('following','insert'):
                from .backend.v4_assets import _skip_inline_pc_image
                image_root=cursor
                ii=_skip_inline_pc_image(zone,cursor)
                if ii is None:raise ValueError(f"LightDef '{name}' inline attenuation image is malformed")
                image_name,semantic,cursor=ii
                image_has_resource=decode_pc_pointer(_u32(zone,image_root+4)).kind in ('following','insert')
            row=type('LightRow',(),{})();row.root=root;row.end=cursor;row.name=name;row.image_pointer=image_raw;row.inline_image_name=image_name;row.inline_image_semantic=semantic;row.inline_image_has_resource=image_has_resource;row.sampler=zone[root+8];row.lmap_lookup_start=lmap;row.name_runtime_fallback=name_runtime
            rows.append(row)
        except (ValueError,IndexError,OverflowError):
            continue
    if root_hints and len(rows)!=len(assets):
        raise ValueError(f'LightDef source-order replay found {len(rows)} typed root(s) for {len(assets)} source XAssets at hints {[hex(x) for x in root_hints]}')
    if not root_hints:
        rows=scan_lightdefs(zone)
        if len(rows)!=len(assets):raise ValueError(f'LightDef scan found {len(rows)} candidates for {len(assets)} source XAssets')

    out=[]
    for a,r in zip(assets,rows):
        image_name=r.inline_image_name;image_index=None
        p=decode_pc_pointer(r.image_pointer)
        if p.kind=='packed':
            image_index=xasset_header_alias_index(asset_list,r.image_pointer,PC_IMAGE)
            if image_index is None:raise ValueError(f"LightDef '{r.name}' packed attenuation image is not an exact Image XAssetHeader alias")
            image_name=image_name_by_source_asset_index.get(image_index)
            if not image_name:raise ValueError(f"LightDef '{r.name}' Image XAsset #{image_index} has no exact semantic identity")
        elif p.kind in ('following','insert'):
            if not image_name:raise ValueError(f"LightDef '{r.name}' inline attenuation image has no name")
        elif p.kind=='null':image_name=None
        else:raise ValueError(f"LightDef '{r.name}' unsupported attenuation pointer {p.kind}")
        item=SourceLightDef(a.index,r.root,r.name,r.sampler,r.lmap_lookup_start,image_name,image_index,p.kind,getattr(r,'inline_image_has_resource',None))
        if getattr(r,'name_runtime_fallback',False):object.__setattr__(item,'name_runtime_fallback',True)
        out.append(item)
    return tuple(out)
