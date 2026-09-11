from __future__ import annotations

from dataclasses import dataclass
import hashlib
import struct
from typing import Iterable, Mapping, Sequence

from .backend.v4_ffio import XAssetList, decode_pc_pointer, FOLLOWING, INSERT
from .assets.image import ImageAsset, ImageMip, PS3_DXT1, PS3_DXT3, PS3_DXT5

PC_IMAGE = 0x06
ROOT = 0x24
LOADDEF = 0x10
RESOURCE_ALIGN = 0x80


def _u16(z: bytes, o: int) -> int: return struct.unpack_from('<H', z, o)[0]
def _u32(z: bytes, o: int) -> int: return struct.unpack_from('<I', z, o)[0]
def _i32(z: bytes, o: int) -> int: return struct.unpack_from('<i', z, o)[0]

def _read_name(z: bytes, cursor: int, max_len: int = 1024) -> tuple[str, int]:
    if cursor < 0 or cursor >= len(z): raise ValueError('image name starts outside zone')
    end = z.find(b'\0', cursor, min(len(z), cursor + max_len + 1))
    if end < 0: raise ValueError(f'unterminated image name at 0x{cursor:X}')
    raw = z[cursor:end]
    if not raw or len(raw) > max_len: raise ValueError(f'invalid image name length at 0x{cursor:X}')
    if any(b < 0x20 or b > 0x7E for b in raw): raise ValueError(f'non-printable image name at 0x{cursor:X}')
    name = raw.decode('latin-1').replace('\\','/').lstrip('/')
    return name, end + 1


def _ptr_allowed(raw: int) -> bool:
    p = decode_pc_pointer(raw)
    return p.kind in ('null','following','insert','packed')

@dataclass(frozen=True)
class PcImageLoadDef:
    root_offset: int
    level_count: int
    flags: int
    width: int
    height: int
    depth: int
    pc_format: int
    resource: bytes

@dataclass(frozen=True)
class PcImageRecord:
    source_asset_index: int
    root_offset: int
    end_offset: int
    name: str
    map_type: int
    loaddef_pointer: int
    picmip0: int
    picmip1: int
    no_picmip: int
    semantic: int
    track: int
    category: int
    delay_load_pixels: int
    name_pointer: int
    loaddef: PcImageLoadDef | None

    @property
    def owns_resource(self) -> bool:
        return self.loaddef is not None and bool(self.loaddef.resource)

@dataclass(frozen=True)
class PcImageCatalog:
    records_by_asset_index: Mapping[int, PcImageRecord]
    exact_name_by_asset_index: Mapping[int, str]
    inline_asset_count: int
    packed_asset_count: int
    physical_run_start: int | None
    physical_run_end: int | None


def parse_image_at(zone: bytes, root: int, source_asset_index: int = -1) -> PcImageRecord:
    if root < 0 or root + ROOT > len(zone): raise ValueError('GfxImage root exceeds zone')
    map_type = _i32(zone, root)
    loadp = _u32(zone, root + 4)
    pic0, pic1, no_pic, semantic, track = zone[root+8], zone[root+9], zone[root+10], zone[root+11], zone[root+12]
    category, delay = zone[root+0x1E], zone[root+0x1F]
    namep = _u32(zone, root + 0x20)
    if not 0 <= map_type <= 5: raise ValueError(f'GfxImage mapType {map_type} outside IW3 bounds')
    if no_pic > 1 or delay > 1: raise ValueError('GfxImage boolean metadata outside IW3 bounds')
    if not _ptr_allowed(loadp) or not _ptr_allowed(namep): raise ValueError('GfxImage contains unsupported pointer kind')
    cursor = root + ROOT
    np = decode_pc_pointer(namep)
    if np.kind == 'following':
        name, cursor = _read_name(zone, cursor)
    elif np.kind == 'packed':
        name = ''
    elif np.kind == 'null':
        name = ''
    else:
        # XString ownership uses FOLLOWING, not INSERT; fail closed on an unmeasured root.
        raise ValueError('GfxImage name uses INSERT')
    load = None
    lp = decode_pc_pointer(loadp)
    if lp.kind in ('following','insert'):
        if cursor + LOADDEF > len(zone): raise ValueError('GfxImageLoadDef exceeds zone')
        level = zone[cursor]
        flags = zone[cursor+1]
        width, height, depth = _u16(zone,cursor+2), _u16(zone,cursor+4), _u16(zone,cursor+6)
        fmt = _u32(zone,cursor+8)
        n = _u32(zone,cursor+0x0C)
        if n > len(zone) - cursor - LOADDEF: raise ValueError('GfxImage resource exceeds zone')
        if not width or not height or not depth: raise ValueError('owned GfxImage has zero dimensions')
        resource_start = cursor + LOADDEF
        resource = zone[resource_start:resource_start+n]
        load = PcImageLoadDef(cursor, level, flags, width, height, depth, fmt, resource)
        cursor = resource_start + n
    elif lp.kind not in ('null','packed'):
        raise ValueError(f'unsupported GfxImage loadDef pointer {lp.kind}')
    return PcImageRecord(source_asset_index, root, cursor, name, map_type, loadp, pic0, pic1, no_pic, semantic, track, category, delay, namep, load)


def _candidate_roots(zone: bytes, start: int = 0) -> list[int]:
    """Find high-confidence physical roots by their inline XString marker at +0x20.

    This intentionally indexes only roots that own their name. A top-level Image whose name is
    packed is still resolvable through exact semantic evidence, but is not a useful physical-run
    anchor.
    """
    marker = b'\xff\xff\xff\xff'
    out=[]; pos=max(0,start+0x20)
    while True:
        pos=zone.find(marker,pos)
        if pos < 0: break
        root=pos-0x20;pos+=1
        if root < start or root+ROOT>len(zone): continue
        try:
            r=parse_image_at(zone,root)
            if not r.name: continue
            # At least one meaningful ownership/descriptor signal, preventing generic string-adjacent false positives.
            lp=decode_pc_pointer(r.loaddef_pointer)
            if lp.kind not in ('null','following','insert','packed'): continue
            out.append(root)
        except (ValueError,struct.error,OverflowError,IndexError):
            continue
    return out


def _run_at(zone: bytes, root: int, count: int) -> tuple[PcImageRecord,...] | None:
    rows=[]; cursor=root
    try:
        for _ in range(count):
            r=parse_image_at(zone,cursor)
            if not r.name: return None
            rows.append(r);cursor=r.end_offset
    except (ValueError,struct.error,OverflowError,IndexError):
        return None
    return tuple(rows)


def locate_top_level_images(
    zone: bytes,
    asset_list: XAssetList,
    *,
    expected_names_by_asset_index: Mapping[int,str] | None = None,
    iwd_names: Iterable[str] = (),
) -> PcImageCatalog:
    if asset_list.structural_index is not None:
        from dataclasses import replace
        index=asset_list.structural_index;records={};names={};rows=index.rows(PC_IMAGE);packed_count=0
        for row in rows:
            r=row['root'];a=asset_list.assets[row['index']]
            names[row['index']]=row['name']
            if decode_pc_pointer(a.serialized_pointer).kind=='packed':packed_count+=1;continue
            rec=parse_image_at(zone,r,row['index'])
            records[row['index']]=replace(rec,name=row['name'])
        return PcImageCatalog(records,names,len(records),packed_count,min((r['start'] for r in rows),default=None),max((r['end'] for r in rows),default=None))
    assets=sorted((a for a in asset_list.assets if a.type_id==PC_IMAGE),key=lambda a:a.index)
    if not assets:return PcImageCatalog({},dict(expected_names_by_asset_index or {}),0,0,None,None)
    inline=[a for a in assets if decode_pc_pointer(a.serialized_pointer).kind in ('following','insert')]
    packed=[a for a in assets if decode_pc_pointer(a.serialized_pointer).kind=='packed']
    bad=[a for a in assets if decode_pc_pointer(a.serialized_pointer).kind not in ('following','insert','packed')]
    if bad:raise ValueError(f'{len(bad)} top-level Image XAssets use unsupported root pointer kinds')
    expected={int(k):v.replace('\\','/').lstrip('/') for k,v in (expected_names_by_asset_index or {}).items() if v}
    if not inline:
        names={a.index:expected[a.index] for a in packed if a.index in expected}
        return PcImageCatalog({},names,0,len(packed),None,None)

    roots=_candidate_roots(zone,asset_list.asset_data_offset)
    rootset=set(roots);runs=[];seen=set()
    for root in roots:
        # only starts not immediately reachable as the next image from another candidate are preferred later;
        # exact chain parsing is the actual proof.
        run=_run_at(zone,root,len(inline))
        if run is None:continue
        key=tuple(x.root_offset for x in run)
        if key in seen:continue
        seen.add(key);runs.append(run)
    if not runs:raise ValueError(f'no complete physical run for {len(inline)} inline top-level Image XAssets')

    iwd_set={x.replace('\\','/').lstrip('/').casefold() for x in iwd_names}
    scored=[]
    for run in runs:
        ok=True; exact=0; iwd=0
        for a,r in zip(inline,run):
            e=expected.get(a.index)
            if e:
                if r.name.casefold()!=e.casefold():ok=False;break
                exact+=1
            if r.name.casefold() in iwd_set:iwd+=1
        if ok:scored.append((exact,iwd,run))
    if not scored:raise ValueError(f'{len(runs)} Image physical runs exist, but none satisfy exact source-index semantic evidence')
    best_score=max((x[0],x[1]) for x in scored)
    best=[x[2] for x in scored if (x[0],x[1])==best_score]
    if len(best)!=1:
        sample=[(hex(r[0].root_offset),[x.name for x in r[:3]]) for r in best[:8]]
        raise ValueError(f'top-level Image physical run ambiguous: {len(runs)} candidates -> {len(best)} tied at exact/iwd score {best_score}; {sample}')
    selected=best[0]
    records={}
    names=dict(expected)
    for a,r in zip(inline,selected):
        rr=PcImageRecord(a.index,r.root_offset,r.end_offset,r.name,r.map_type,r.loaddef_pointer,r.picmip0,r.picmip1,r.no_picmip,r.semantic,r.track,r.category,r.delay_load_pixels,r.name_pointer,r.loaddef)
        records[a.index]=rr;names[a.index]=r.name
    return PcImageCatalog(records,names,len(inline),len(packed),selected[0].root_offset,selected[-1].end_offset)


def _mip_meta(width:int,height:int,depth:int,level_count:int,face_count:int,resource:bytes,pc_format:int) -> tuple[ImageMip,...]:
    # GfxImageLoadDef resource order is already the physical stream used by the game. The writer only
    # needs the complete resource; these records are diagnostic hashes and are not used to reorder bytes.
    if face_count not in (1,6): return ()
    block = 8 if pc_format==0x31545844 else 16 if pc_format in (0x33545844,0x35545844) else None
    if block is None:return ()
    expected=[]
    for level in range(level_count):
        w=max(1,width>>level);h=max(1,height>>level);n=max(1,(w+3)//4)*max(1,(h+3)//4)*block
        expected.append((level,w,h,n))
    if sum(x[3]*face_count for x in expected)!=len(resource):return ()
    rows=[];off=0
    # The exact PC loadDef face/mip ordering is retained byte-for-byte. We expose a linear diagnostic
    # segmentation only; no byte movement is performed here.
    for face in range(face_count):
        for level,w,h,n in expected:
            data=resource[off:off+n];rows.append(ImageMip(face,level,w,h,off,n,hashlib.sha256(data).hexdigest().upper()));off+=n
    return tuple(rows)


def image_asset_from_pc(record: PcImageRecord) -> ImageAsset:
    ld=record.loaddef
    if ld is None or not ld.resource:raise ValueError(f"PC GfxImage '{record.name}' has no owned resource")
    fmtmap={
        0x31545844:('dxt1',PS3_DXT1),
        0x33545844:('dxt3',PS3_DXT3),
        0x35545844:('dxt5',PS3_DXT5),
        21:('argb8',0x85),
        50:('l8',0x81),
    }
    if ld.pc_format not in fmtmap:raise ValueError(f"PC GfxImage '{record.name}' unsupported format 0x{ld.pc_format:08X}")
    source_fmt,ps3_fmt=fmtmap[ld.pc_format]
    src=ld.resource
    if ld.pc_format==21:
        if len(src)%4:raise ValueError('ARGB8 resource byte count is not divisible by four')
        conv=bytearray(len(src))
        for o in range(0,len(src),4):
            b,g,r,a=src[o:o+4];conv[o:o+4]=bytes((a,r,g,b))
        src=bytes(conv)
    # L8 and BC payloads are byte-preserving on the measured PS3 path.
    unp=len(src);pad=(-unp)%RESOURCE_ALIGN;resource=src+b'\0'*pad
    faces=6 if record.map_type==5 else 1
    if record.map_type not in (3,5):raise ValueError(f"PC GfxImage '{record.name}' mapType {record.map_type} has no measured PS3 conversion")
    mips=_mip_meta(ld.width,ld.height,ld.depth,ld.level_count,faces,src,ld.pc_format)
    return ImageAsset(record.name,record.map_type,source_fmt,ps3_fmt,record.semantic,ld.width,ld.height,ld.depth,ld.level_count,faces,resource,unp,pad,mips)
