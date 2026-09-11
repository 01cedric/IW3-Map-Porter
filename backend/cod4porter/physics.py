from __future__ import annotations

from dataclasses import dataclass, field
import math
import struct
from typing import Sequence

from .graph import AssetType, GraphContext
from .plane_format import ps3_plane_signbits
from .zone_writer import RelocatingZoneWriter, TEMP_BLOCK, VIRTUAL_BLOCK, FOLLOWING, NULL

PHYS_PRESET_ROOT_SIZE = 0x2C
PHYS_GEOM_LIST_ROOT_SIZE = 0x2C
PHYS_GEOM_INFO_SIZE = 0x44
BRUSH_WRAPPER_SIZE = 0x50
BRUSH_SIDE_SIZE = 0x0C
PLANE_SIZE = 0x14


def _finite(v: float, label: str) -> float:
    if not math.isfinite(v):
        raise ValueError(f'{label} must be finite')
    return float(v)


def _vec(values: Sequence[float], n: int, label: str) -> tuple[float, ...]:
    if len(values) != n:
        raise ValueError(f'{label} must have {n} elements')
    return tuple(_finite(float(x), f'{label}[{i}]') for i, x in enumerate(values))


def _put_vec3(buf: bytearray, off: int, values: Sequence[float]) -> None:
    vals = _vec(values, 3, 'vec3')
    for i, v in enumerate(vals):
        struct.pack_into('>f', buf, off + i * 4, v)


@dataclass
class PhysPreset:
    name: str
    type_id: int
    mass: float
    bounce: float
    friction: float
    bullet_force_scale: float
    explosive_force_scale: float
    pieces_spread_fraction: float
    pieces_upward_velocity: float
    temp_default_to_cylinder: bool
    sound_alias_prefix: str | None = None
    source_asset_index: int = -1

    def validate(self) -> None:
        if not self.name or '\x00' in self.name:
            raise ValueError('PhysPreset needs a valid name')
        if self.sound_alias_prefix is not None and '\x00' in self.sound_alias_prefix:
            raise ValueError('PhysPreset sound alias prefix contains NUL')
        for label in ('mass','bounce','friction','bullet_force_scale','explosive_force_scale','pieces_spread_fraction','pieces_upward_velocity'):
            _finite(getattr(self, label), f'{self.name}.{label}')


@dataclass(frozen=True)
class PhysPlane:
    normal: Sequence[float]
    distance: float
    type: int
    sign_bits: int = 0


@dataclass(frozen=True)
class PhysBrushSide:
    plane: PhysPlane | None
    material_index: int
    first_adjacent_side_offset: int
    edge_count: int


@dataclass(frozen=True)
class PhysBrush:
    minimum: Sequence[float]
    maximum: Sequence[float]
    contents: int
    sides: Sequence[PhysBrushSide]
    axial_material_indices: Sequence[int]
    base_adjacent_sides: bytes
    first_adjacent_side_offsets: Sequence[int]
    axial_edge_counts: Sequence[int]
    total_edge_count: int
    planes: Sequence[PhysPlane]


@dataclass(frozen=True)
class PhysGeom:
    type: int
    orientation: Sequence[float]
    offset: Sequence[float]
    half_lengths: Sequence[float]
    brush: PhysBrush | None = None


@dataclass(frozen=True)
class PhysGeomList:
    mass_center_of_mass: Sequence[float]
    mass_moments_of_inertia: Sequence[float]
    mass_products_of_inertia: Sequence[float]
    geoms: Sequence[PhysGeom]


def build_phys_preset_root(p: PhysPreset) -> bytes:
    p.validate()
    out = bytearray(PHYS_PRESET_ROOT_SIZE)
    struct.pack_into('>I', out, 0x00, FOLLOWING)
    struct.pack_into('>i', out, 0x04, p.type_id)
    struct.pack_into('>f', out, 0x08, _finite(p.mass, 'mass'))
    struct.pack_into('>f', out, 0x0C, _finite(p.bounce, 'bounce'))
    struct.pack_into('>f', out, 0x10, _finite(p.friction, 'friction'))
    struct.pack_into('>f', out, 0x14, _finite(p.bullet_force_scale, 'bullet_force_scale'))
    struct.pack_into('>f', out, 0x18, _finite(p.explosive_force_scale, 'explosive_force_scale'))
    # Physics sound initialization dereferences this XString unconditionally.
    # Missing source identities must use an owned empty string, never NULL.
    struct.pack_into('>I', out, 0x1C, FOLLOWING)
    struct.pack_into('>f', out, 0x20, _finite(p.pieces_spread_fraction, 'pieces_spread_fraction'))
    struct.pack_into('>f', out, 0x24, _finite(p.pieces_upward_velocity, 'pieces_upward_velocity'))
    out[0x28] = 1 if p.temp_default_to_cylinder else 0
    return bytes(out)


def build_phys_geom_list_root(x: PhysGeomList) -> bytes:
    out = bytearray(PHYS_GEOM_LIST_ROOT_SIZE)
    struct.pack_into('>I', out, 0x00, len(x.geoms))
    struct.pack_into('>I', out, 0x04, FOLLOWING if x.geoms else NULL)
    _put_vec3(out, 0x08, x.mass_center_of_mass)
    _put_vec3(out, 0x14, x.mass_moments_of_inertia)
    _put_vec3(out, 0x20, x.mass_products_of_inertia)
    return bytes(out)


def build_phys_geom_root(x: PhysGeom) -> bytes:
    orientation = _vec(x.orientation, 9, 'PhysGeom.orientation')
    offset = _vec(x.offset, 3, 'PhysGeom.offset')
    half = _vec(x.half_lengths, 3, 'PhysGeom.half_lengths')
    out = bytearray(PHYS_GEOM_INFO_SIZE)
    struct.pack_into('>I', out, 0x00, FOLLOWING if x.brush is not None else NULL)
    struct.pack_into('>i', out, 0x04, x.type)
    for i, v in enumerate(orientation): struct.pack_into('>f', out, 0x08 + i*4, v)
    _put_vec3(out, 0x2C, offset)
    _put_vec3(out, 0x38, half)
    return bytes(out)


def build_brush_root(x: PhysBrush) -> bytes:
    if len(x.axial_material_indices) != 6 or len(x.first_adjacent_side_offsets) != 6 or len(x.axial_edge_counts) != 6:
        raise ValueError('Brush axial arrays must have six entries')
    if len(x.base_adjacent_sides) != x.total_edge_count:
        raise ValueError('Brush baseAdjacentSides length != totalEdgeCount')
    if x.planes and len(x.planes) != len(x.sides):
        raise ValueError('Brush planes must be empty or one per side')
    out = bytearray(BRUSH_WRAPPER_SIZE)
    _put_vec3(out,0x00,x.minimum)
    struct.pack_into('>I',out,0x0C,x.contents & 0xFFFFFFFF)
    _put_vec3(out,0x10,x.maximum)
    struct.pack_into('>I',out,0x1C,len(x.sides))
    struct.pack_into('>I',out,0x20,FOLLOWING if x.sides else NULL)
    for i,v in enumerate(x.axial_material_indices): struct.pack_into('>h',out,0x24+i*2,int(v))
    struct.pack_into('>I',out,0x30,FOLLOWING if x.base_adjacent_sides else NULL)
    for i,v in enumerate(x.first_adjacent_side_offsets): struct.pack_into('>h',out,0x34+i*2,int(v))
    for i,v in enumerate(x.axial_edge_counts):
        if not 0 <= int(v) <= 255: raise ValueError('axial edge count outside byte')
        out[0x40+i] = int(v)
    struct.pack_into('>I',out,0x48,x.total_edge_count & 0xFFFFFFFF)
    struct.pack_into('>I',out,0x4C,FOLLOWING if x.planes else NULL)
    return bytes(out)


def build_brush_side(x: PhysBrushSide) -> bytes:
    if not 0 <= x.edge_count <= 255: raise ValueError('edge_count outside byte')
    out=bytearray(BRUSH_SIDE_SIZE)
    struct.pack_into('>I',out,0,FOLLOWING if x.plane is not None else NULL)
    struct.pack_into('>I',out,4,x.material_index & 0xFFFFFFFF)
    struct.pack_into('>h',out,8,x.first_adjacent_side_offset)
    out[0x0A]=x.edge_count
    return bytes(out)


def build_plane(x: PhysPlane) -> bytes:
    if not 0 <= x.type <= 255 or not 0 <= x.sign_bits <= 255: raise ValueError('plane byte fields outside range')
    out=bytearray(PLANE_SIZE)
    _put_vec3(out,0,x.normal)
    struct.pack_into('>f',out,0x0C,_finite(x.distance,'plane.distance'))
    # PS3 marks an axial plane with signbits 8; only a non-axial plane carries the sign mask.
    out[0x10]=x.type; out[0x11]=ps3_plane_signbits(x.normal,x.type) if x.type<=3 else x.sign_bits
    return bytes(out)


def write_inline_phys_geom_list(w: RelocatingZoneWriter, x: PhysGeomList, owner: str) -> dict:
    # PhysGeomList and all of its pointees use IW3's default-normal block.
    # AlignStream padding advances only the logical destination cursor and is
    # deliberately absent from the physical FastFile byte stream.
    w.align_logical_only(4)
    root_physical=w.physical_position
    root_location=w.current_location
    w.write_in_block(build_phys_geom_list_root(x),f'{owner} PhysGeomList root')
    if x.geoms:w.align_logical_only(4)
    for i,g in enumerate(x.geoms): w.write_in_block(build_phys_geom_root(g),f'{owner} PhysGeomInfo[{i}]')
    brush_count=side_count=0
    for gi,g in enumerate(x.geoms):
        b=g.brush
        if b is None: continue
        brush_count += 1; side_count += len(b.sides)
        w.align_logical_only(4)
        w.write_in_block(build_brush_root(b),f'{owner} PhysGeom[{gi}] BrushWrapper')
        if b.sides:w.align_logical_only(4)
        for si,s in enumerate(b.sides): w.write_in_block(build_brush_side(s),f'{owner} PhysGeom[{gi}] side[{si}]')
        for si,s in enumerate(b.sides):
            if s.plane is not None:
                w.align_logical_only(4)
                w.write_in_block(build_plane(s.plane),f'{owner} PhysGeom[{gi}] side[{si}] following plane')
        if b.base_adjacent_sides: w.write_in_block(bytes(b.base_adjacent_sides),f'{owner} PhysGeom[{gi}] baseAdjacentSide bytes')
        if b.planes:w.align_logical_only(4)
        for pi,p in enumerate(b.planes): w.write_in_block(build_plane(p),f'{owner} PhysGeom[{gi}] plane[{pi}]')
    return {'root_physical':root_physical,'root_location':root_location,'geom_count':len(x.geoms),'brush_count':brush_count,'side_count':side_count}


@dataclass
class PhysPresetNode:
    preset: PhysPreset
    symbol: str
    type: AssetType = field(init=False, default=AssetType.PHYSPRESET)
    root_block: int = field(init=False, default=TEMP_BLOCK)
    dependencies: tuple = field(init=False, default=())

    def write(self, w: RelocatingZoneWriter, c: GraphContext) -> None:
        root_phys=w.physical_position
        loc=w.define_symbol(self.symbol); c.register_root(self.symbol,root_phys)
        w.write_in_block(build_phys_preset_root(self.preset),f"PhysPreset '{self.preset.name}' root")
        # PhysPreset is a TEMP asset, but generated IW3 asset loaders push the
        # default-normal block for all members after the root.
        w.push_block(VIRTUAL_BLOCK)
        try:
            name_location=w.current_location
            w.write_latin1z(self.preset.name,f"PhysPreset '{self.preset.name}' name")
            w.write_latin1z(self.preset.sound_alias_prefix or '',f"PhysPreset '{self.preset.name}' sound alias prefix")
        finally:
            w.pop_block()
        c.register_result('physpreset:'+self.symbol,{
            'source_asset_index':self.preset.source_asset_index,
            'name':self.preset.name,'root_physical':root_phys,'root_location':loc,'name_location':name_location,'physical_end':w.physical_position})

# ---------------- PC little-endian source parser ----------------
from .backend.v4_ffio import decode_pc_pointer

def _pc_u32(z:bytes,o:int)->int:return struct.unpack_from('<I',z,o)[0]
def _pc_i32(z:bytes,o:int)->int:return struct.unpack_from('<i',z,o)[0]
def _pc_i16(z:bytes,o:int)->int:return struct.unpack_from('<h',z,o)[0]
def _pc_f32(z:bytes,o:int)->float:return struct.unpack_from('<f',z,o)[0]
def _pc_inline(raw:int)->bool:return decode_pc_pointer(raw).kind in ('following','insert')

def _pc_string(z:bytes,cursor:int,max_bytes:int=512)->tuple[str,int]:
    end=z.find(b'\0',cursor,min(len(z),cursor+max_bytes))
    if end<0:raise ValueError('unterminated PC XString')
    return z[cursor:end].decode('latin-1'),end+1

def parse_pc_phys_preset(z:bytes,root:int)->tuple[PhysPreset,int,dict]:
    if root<0 or root+PHYS_PRESET_ROOT_SIZE>len(z):raise ValueError('PhysPreset root out of range')
    name_raw=_pc_u32(z,root);sound_raw=_pc_u32(z,root+0x1c);cursor=root+PHYS_PRESET_ROOT_SIZE
    nk=decode_pc_pointer(name_raw).kind
    if _pc_inline(name_raw):name,cursor=_pc_string(z,cursor);name_proven=True
    elif nk=='packed':name=f'__pc_phys_{name_raw:08X}';name_proven=False
    else:raise ValueError('PhysPreset invalid name pointer')
    sound=None;sound_proven=True
    if _pc_inline(sound_raw):sound,cursor=_pc_string(z,cursor)
    elif decode_pc_pointer(sound_raw).kind=='packed':sound_proven=False
    vals=[_pc_f32(z,root+o) for o in (0x08,0x0c,0x10,0x14,0x18,0x20,0x24)]
    if not all(math.isfinite(x) for x in vals):raise ValueError('PhysPreset non-finite scalar')
    p=PhysPreset(name,_pc_i32(z,root+4),*vals[:5],vals[5],vals[6],z[root+0x28]!=0,sound)
    meta={'serialized_name_pointer':name_raw,'name_identity_proven':name_proven,'serialized_sound_alias_prefix_pointer':sound_raw,'sound_alias_prefix_identity_proven':sound_proven,'root_offset':root,'physical_end':cursor}
    return p,cursor,meta

def _parse_pc_plane(z:bytes,o:int)->PhysPlane:
    return PhysPlane([_pc_f32(z,o+i*4) for i in range(3)],_pc_f32(z,o+0x0c),z[o+0x10],z[o+0x11])

def parse_pc_phys_geom_list(z:bytes,root:int)->tuple[PhysGeomList,int]:
    if root<0 or root+PHYS_GEOM_LIST_ROOT_SIZE>len(z):raise ValueError('PhysGeomList root out of range')
    count=_pc_u32(z,root);gptr=_pc_u32(z,root+4)
    if count>4096:raise ValueError('PhysGeomList implausible count')
    if count and not _pc_inline(gptr):raise ValueError('PhysGeomList geoms not inline')
    mass=[_pc_f32(z,root+8+i*4) for i in range(9)];cursor=root+PHYS_GEOM_LIST_ROOT_SIZE;geom_array=cursor;cursor+=count*PHYS_GEOM_INFO_SIZE
    geoms=[]
    for gi in range(count):
        gr=geom_array+gi*PHYS_GEOM_INFO_SIZE;bptr=_pc_u32(z,gr);brush=None
        if _pc_inline(bptr):
            br=cursor
            minimum=[_pc_f32(z,br+i*4) for i in range(3)];contents=_pc_u32(z,br+0x0c);maximum=[_pc_f32(z,br+0x10+i*4) for i in range(3)]
            side_count=_pc_u32(z,br+0x1c);sptr=_pc_u32(z,br+0x20);baseptr=_pc_u32(z,br+0x30);total_edges=_pc_u32(z,br+0x48);planesptr=_pc_u32(z,br+0x4c)
            if side_count>65536 or total_edges>16*1024*1024:raise ValueError('PhysBrush implausible counts')
            axial=[_pc_i16(z,br+0x24+i*2) for i in range(6)];first=[_pc_i16(z,br+0x34+i*2) for i in range(6)];ec=[z[br+0x40+i] for i in range(6)]
            cursor+=BRUSH_WRAPPER_SIZE;sides=[];side_roots=cursor
            if side_count:
                if not _pc_inline(sptr):raise ValueError('PhysBrush sides not inline')
                cursor+=side_count*BRUSH_SIDE_SIZE
            # Following plane payloads are serialized after all side roots.
            side_plane_meta=[]
            for si in range(side_count):
                sr=side_roots+si*BRUSH_SIDE_SIZE;pp=_pc_u32(z,sr);plane=None
                if _pc_inline(pp):plane=_parse_pc_plane(z,cursor);cursor+=PLANE_SIZE
                elif decode_pc_pointer(pp).kind=='packed':raise ValueError('packed PhysBrush side plane unresolved')
                sides.append(PhysBrushSide(plane,_pc_u32(z,sr+4),_pc_i16(z,sr+8),z[sr+0x0a]))
            base=b''
            if total_edges:
                if not _pc_inline(baseptr):raise ValueError('PhysBrush baseAdjacentSides not inline')
                base=z[cursor:cursor+total_edges];cursor+=total_edges
            planes=[]
            if side_count and _pc_inline(planesptr):
                planes=[_parse_pc_plane(z,cursor+i*PLANE_SIZE) for i in range(side_count)];cursor+=side_count*PLANE_SIZE
            elif decode_pc_pointer(planesptr).kind=='packed':
                # BrushWrapper.planes is a packed pointer whenever the PC zone reuses the
                # plane array it already wrote for the brush sides: the sides were emitted
                # in order, each with its own inline cplane_s, so those planes are exactly
                # the contiguous array this pointer addresses.  Stock mp_crash does this
                # and Version 15 refused the map outright.  No stream bytes are consumed
                # here - only the reuse is recorded, and the PS3 writer serializes the same
                # planes inline behind a FOLLOWING marker, which the loader accepts.
                if not side_count or any(x.plane is None for x in sides):
                    raise ValueError('packed PhysBrush planes without a complete inline side-plane array')
                planes=[x.plane for x in sides]
            brush=PhysBrush(minimum,maximum,contents,sides,axial,base,first,ec,total_edges,planes)
        elif decode_pc_pointer(bptr).kind=='packed':raise ValueError('packed PhysGeom BrushWrapper unresolved')
        vals=[_pc_f32(z,gr+8+i*4) for i in range(15)]
        geoms.append(PhysGeom(_pc_i32(z,gr+4),vals[:9],vals[9:12],vals[12:15],brush))
    return PhysGeomList(mass[:3],mass[3:6],mass[6:9],geoms),cursor

# ---------------- Top-level PC PhysPreset catalog / XModel binding ----------------
from dataclasses import replace as _dc_replace
from typing import Mapping as _Mapping
from collections import defaultdict
from .backend.v4_ffio import XAssetList as _XAssetList

PC_PHYSPRESET_TYPE=0x01

def _looks_phys_name(s:str)->bool:
    return bool(s) and len(s)<=256 and any(c.isalpha() for c in s) and all(0x20<=ord(c)<=0x7e for c in s)

def _looks_pc_phys_preset(z:bytes,root:int):
    if root<0 or root+PHYS_PRESET_ROOT_SIZE>len(z):return None
    try:
        name_raw=_pc_u32(z,root);nk=decode_pc_pointer(name_raw).kind
        if nk not in ('following','insert','packed'):return None
        typ=_pc_i32(z,root+4)
        if typ<-1 or typ>64:return None
        vals=[_pc_f32(z,root+o) for o in (0x08,0x0c,0x10,0x14,0x18,0x20,0x24)]
        if not all(math.isfinite(x) for x in vals):return None
        sound_raw=_pc_u32(z,root+0x1c)
        if decode_pc_pointer(sound_raw).kind not in ('null','following','insert','packed'):return None
        p,end,meta=parse_pc_phys_preset(z,root)
        if meta['name_identity_proven'] and not _looks_phys_name(p.name):return None
        return p,end,meta
    except Exception:return None

def _model_order_authoritative(xs)->bool:
    rows=sorted((x for x in xs if getattr(x.model,'source_asset_index',-1)>=0 and x.physical_end is not None),key=lambda x:x.model.source_asset_index)
    if not rows:return False
    ai=root=end=-1
    for x in rows:
        if x.model.source_asset_index<=ai or x.root_offset<=root or x.physical_end<=x.root_offset or x.root_offset<end:return False
        ai=x.model.source_asset_index;root=x.root_offset;end=x.physical_end
    return True

def top_level_phys_presets(z:bytes,asset_list:_XAssetList,xmodels=(),excluded_inline_roots=())->tuple[PhysPreset,...]:
    if asset_list.structural_index is not None:
        index=asset_list.structural_index;out=[]
        for row in index.rows(PC_PHYSPRESET_TYPE):
            hit=_looks_pc_phys_preset(z,row['root'])
            if hit is None:raise ValueError(f"Unsupported PhysPreset payload at structural root {row['root']:X}")
            p,end,meta=hit;p.source_asset_index=row['index'];p.source_root_offset=row['root'];p.source_physical_end=row['end']
            p.name=row['name'];p.name_identity_proven=True;p.serialized_name_pointer=meta['serialized_name_pointer']
            p.serialized_sound_alias_prefix_pointer=meta['serialized_sound_alias_prefix_pointer']
            p.sound_alias_prefix_identity_proven=True
            p.sound_alias_prefix=index.pointer(row['root']+0x1c)
            out.append(p)
        return tuple(out)
    assets=sorted((a for a in asset_list.assets if a.type_id==PC_PHYSPRESET_TYPE),key=lambda a:a.index)
    if not assets:return ()
    if any(decode_pc_pointer(a.serialized_pointer).kind not in ('following','insert') for a in assets):raise ValueError('Top-level PC PhysPreset XAssets must be inline')
    excluded=set(excluded_inline_roots)
    models=sorted((x for x in xmodels if getattr(x.model,'source_asset_index',-1)>=0 and x.physical_end is not None),key=lambda x:x.model.source_asset_index)
    authoritative=_model_order_authoritative(models)
    windows={}
    for a in assets:
        lower=next((x for x in reversed(models) if x.model.source_asset_index<a.index),None) if authoritative else None
        upper=next((x for x in models if x.model.source_asset_index>a.index),None) if authoritative else None
        lo=lower.physical_end if lower is not None else asset_list.asset_data_offset;hi=upper.root_offset if upper is not None else len(z)
        windows.setdefault((lo,hi,lower.model.source_asset_index if lower else None,upper.model.source_asset_index if upper else None),[]).append(a)
    selected=[]
    for (lo,hi,_lower_asset_index,_upper_asset_index),wa in sorted(windows.items()):
        candidates=[]
        # The source-order interval owns the candidates; the bounded scan below is authoritative.
        # General bounded scan. PhysPreset is only 0x2C and windows are normally tight between XModels.
        for root in range(max(asset_list.asset_data_offset,lo),min(len(z)-PHYS_PRESET_ROOT_SIZE,hi-1)+1):
            if root in excluded:continue
            hit=_looks_pc_phys_preset(z,root)
            if hit is None:continue
            p,end,meta=hit
            if end<=hi:candidates.append((root,p,end,meta))
        # In an authoritative source-order window the first inline top-level object starts at
        # the lower physical boundary modulo only normal zero alignment padding. Prefer that
        # physical ownership proof before considering byte windows embedded inside the object.
        # This rejects e.g. Getaway's fake 'al' PhysPreset at +0x1C inside metal_dinnerware.
        prefix_candidates=[]
        for cand in candidates:
            root=cand[0];gap=root-lo
            if 0<=gap<=15 and all(b==0 for b in z[lo:root]):prefix_candidates.append(cand)
        if authoritative and len(wa)==1 and len(prefix_candidates)==1:
            candidates=prefix_candidates
        elif (authoritative and _lower_asset_index is not None and
              [a.index for a in wa]==list(range(_lower_asset_index+1,_lower_asset_index+1+len(wa)))):
            # Consecutive PhysPreset XAssets immediately after the lower model
            # have a causal physical start. Walk their actual owned lengths;
            # later TechniqueSet bytes in this model window are not presets.
            by_root={c[0]:c for c in candidates}
            chain=[];cursor=lo
            for _ in wa:
                candidate=by_root.get(cursor)
                if candidate is None:break
                chain.append(candidate);cursor=candidate[2]
            if len(chain)==len(wa):candidates=chain

        # De-dup exact roots, then eliminate weak packed-name shells that physically begin
        # inside a stronger inline-name PhysPreset's owned span. A top-level asset root cannot
        # start inside the bytes consumed by another top-level root. This specifically prevents
        # packed-pointer-looking u32 windows inside inline XStrings from becoming fake presets,
        # without guessing an asset identity or a block-4 basis.
        by={x[0]:x for x in candidates};candidates=[by[k] for k in sorted(by)]
        strong_spans=[(root,end) for root,p,end,meta in candidates if meta['name_identity_proven']]
        if strong_spans:
            filtered=[]
            for cand in candidates:
                root,p,end,meta=cand
                if meta['name_identity_proven']:
                    filtered.append(cand);continue
                if any(lo < root < hi for lo,hi in strong_spans):
                    continue
                filtered.append(cand)
            candidates=filtered
        if len(candidates)!=len(wa):
            sample=', '.join(f'0x{x[0]:X}:{x[1].name}' for x in candidates[:12])
            raise ValueError(f'PhysPreset source-order window 0x{lo:X}..0x{hi:X} has {len(candidates)} candidates for {len(wa)} XAssets: {sample}')
        for a,c in zip(sorted(wa,key=lambda q:q.index),candidates):
            p=c[1];p.source_asset_index=a.index
            # attach provenance used by name/sound closure
            p.source_root_offset=c[0];p.source_physical_end=c[2];p.serialized_name_pointer=c[3]['serialized_name_pointer'];p.name_identity_proven=c[3]['name_identity_proven'];p.serialized_sound_alias_prefix_pointer=c[3]['serialized_sound_alias_prefix_pointer'];p.sound_alias_prefix_identity_proven=c[3]['sound_alias_prefix_identity_proven']
            selected.append(p)
    return tuple(sorted(selected,key=lambda p:p.source_asset_index))

def phys_signature(p:PhysPreset)->tuple:
    return (p.type_id,tuple(round(float(x),7) for x in (p.mass,p.bounce,p.friction,p.bullet_force_scale,p.explosive_force_scale,p.pieces_spread_fraction,p.pieces_upward_velocity)),bool(p.temp_default_to_cylinder))

def recover_packed_phys_names(top:Sequence[PhysPreset],xmodels,*,runtime_compatible:bool=False)->int:
    """Recover packed PhysPreset XStrings only from exact semantic twins.

    Runtime-compatible mode may assign a deterministic *destination-local* identity when the
    source XString itself cannot be proven.  That name is never presented as the recovered PC
    semantic name: ``name_identity_proven`` remains false and ``name_runtime_fallback`` is set.
    Pointer topology remains safe because top-level users bind by regenerated XAssetHeader and
    inline presets remain owned by their XModel.
    """
    proven=defaultdict(set)
    for p in top:
        if getattr(p,'name_identity_proven',True):proven[phys_signature(p)].add(p.name)
    for x in xmodels:
        p=x.model.inline_phys_preset
        if p is not None and getattr(p,'name_identity_proven',True):proven[phys_signature(p)].add(p.name)
    recovered=0
    rows=list(top)+[x.model.inline_phys_preset for x in xmodels if x.model.inline_phys_preset is not None]
    for ordinal,p in enumerate(rows):
        if getattr(p,'name_identity_proven',True):continue
        names=proven.get(phys_signature(p),set())
        if len(names)==1:
            p.name=next(iter(names));p.name_identity_proven=True;recovered+=1;continue
        if not runtime_compatible:
            raise ValueError(f'packed PhysPreset name has {len(names)} proven identities for signature {phys_signature(p)}')
        source_index=int(getattr(p,'source_asset_index',-1))
        root=int(getattr(p,'source_root_offset',getattr(p,'root_offset',-1)))
        if source_index>=0:
            p.name=f'__cp11_phys_asset_{source_index:04d}'
        elif root>=0:
            p.name=f'__cp11_phys_inline_{root:08x}'
        else:
            p.name=f'__cp11_phys_inline_{ordinal:04d}'
        setattr(p,'name_runtime_fallback',True)
        # Deliberately leave identity false: this is loader-safe destination identity, not proof.
        p.name_identity_proven=False
    return recovered

def _xasset_header_alias(asset_list:_XAssetList,raw:int,expected:int)->int|None:
    from .pc_xmodel import xasset_header_alias_index
    return xasset_header_alias_index(asset_list,raw,expected)

def resolve_xmodel_phys_presets(asset_list:_XAssetList,top:Sequence[PhysPreset],xmodels,*,runtime_compatible:bool=False)->dict:
    """Resolve XModel::physPreset ownership without collapsing nested INSERT aliases.

    IW3 source zones can own a PhysPreset inline inside an XModel (INSERT) and later XModels can
    reuse the loader-created InsertPointer cell via a packed block-4 pointer.  Those nested aliases
    are not top-level PhysPreset XAsset headers, so xasset_header_alias_index cannot resolve them.

    We resolve in source-XAsset order.  A packed reference that is not a top-level alias may reuse
    the latest preceding inline XModel PhysPreset only while no top-level PhysPreset or newer inline
    PhysPreset has intervened.  This is deliberately conservative and matches the three measured
    mp_getaway reuse groups; otherwise the reference remains unresolved/fail-closed.
    """
    inserted={}
    for a in asset_list.assets:
        if a.type_id!=PC_PHYSPRESET_TYPE:continue
        p=decode_pc_pointer(a.serialized_pointer)
        if p.kind!='packed' or p.block!=4:continue
        if _xasset_header_alias(asset_list,a.serialized_pointer,PC_PHYSPRESET_TYPE) is not None:continue
        old=inserted.get(p.offset)
        if old is not None and old!=a.index:raise ValueError(f'PhysPreset InsertPointer alias 0x{p.offset:X} conflict')
        inserted[p.offset]=a.index
    by_index={p.source_asset_index:p for p in top}
    resolved=inline=packed=nested_reuse=0;unresolved=[]
    top_source_indices=set(by_index)
    x_by_source={}
    # Source order is required only for inline/nested INSERT ownership.  Tiny unit fixtures and
    # callers that already point directly at a top-level PhysPreset XAssetHeader do not need to
    # fabricate a source_asset_index merely to resolve that exact alias.  Resolve those direct
    # aliases up front; fail closed for ownership forms whose correctness actually depends on
    # source order.
    pre_resolved=set()
    for ordinal,x in enumerate(xmodels):
        source_index=getattr(x.model,'source_asset_index',None)
        raw=x.model.phys_preset_pointer;p=decode_pc_pointer(raw)
        if source_index is not None:
            if source_index in x_by_source:
                raise ValueError(f'duplicate XModel source_asset_index {source_index}')
            x_by_source[source_index]=x
            continue
        if p.kind=='null':
            pre_resolved.add(id(x));continue
        if p.kind=='packed' and p.block==4:
            ai=_xasset_header_alias(asset_list,raw,PC_PHYSPRESET_TYPE)
            if ai is not None and ai in by_index:
                x.model.resolved_phys_preset_asset_index=ai
                x.model.resolved_phys_preset_mode='xasset_header'
                packed+=1;resolved+=1;pre_resolved.add(id(x));continue
        unresolved.append({'model':x.model.name,'raw':raw,'reason':'source_asset_index required for nested/inline PhysPreset ownership'})
        pre_resolved.add(id(x))
    # Process only relevant ownership events in exact source XAsset order.
    last_inline_owner=None
    packed_target_owner={}
    for source_index in sorted(top_source_indices | set(x_by_source)):
        if source_index in top_source_indices:
            # A top-level PhysPreset establishes a different ownership domain; do not infer a
            # nested XModel alias across it.
            last_inline_owner=None
            continue
        x=x_by_source[source_index]
        raw=x.model.phys_preset_pointer;p=decode_pc_pointer(raw)
        if p.kind=='null':continue
        if p.kind in ('following','insert'):
            if x.model.inline_phys_preset is None:
                unresolved.append({'model':x.model.name,'reason':'inline pointer but no parsed preset'});last_inline_owner=None;continue
            inline+=1;resolved+=1
            # Preserve enough evidence for the destination XModel writer to emit INSERT rather
            # than FOLLOWING and to expose the corresponding logical alias cell.
            x.model.inline_phys_preset_pointer_kind=p.kind
            last_inline_owner=x.model.source_asset_index
            continue
        if p.kind!='packed' or p.block!=4:
            unresolved.append({'model':x.model.name,'raw':raw,'reason':'unsupported pointer'});last_inline_owner=None;continue
        packed+=1;ai=_xasset_header_alias(asset_list,raw,PC_PHYSPRESET_TYPE);mode='xasset_header'
        if ai is None and p.offset in inserted:ai=inserted[p.offset];mode='insert_alias'
        if ai is not None and ai in by_index:
            x.model.resolved_phys_preset_asset_index=ai;x.model.resolved_phys_preset_mode=mode;resolved+=1
            last_inline_owner=None
            continue
        # Nested XModel InsertPointer reuse.  Require one consistent source target -> owner mapping
        # and a live preceding inline owner.  Never guess across an ownership boundary.
        owner=packed_target_owner.get(raw)
        if owner is None:
            owner=last_inline_owner
            if owner is not None:packed_target_owner[raw]=owner
        if owner is not None and owner in x_by_source and x_by_source[owner].model.inline_phys_preset is not None:
            x.model.resolved_nested_phys_preset_owner_asset_index=owner
            x.model.resolved_phys_preset_mode='nested_xmodel_insert_alias'
            nested_reuse+=1;resolved+=1
            continue
        unresolved.append({'model':x.model.name,'raw':raw,'offset':p.offset})
    exact_unresolved=tuple(unresolved)
    runtime_null_fallbacks=0
    if unresolved and not runtime_compatible:
        raise ValueError(f'XModel PhysPreset closure unresolved ({len(unresolved)}): {unresolved[:6]}')
    if unresolved and runtime_compatible:
        runtime_null_fallbacks=len(unresolved);unresolved=[]
    return {'references':inline+packed,'resolved':resolved+runtime_null_fallbacks,'exact_resolved':resolved,
            'inline':inline,'packed':packed,'nested_reuse':nested_reuse,'insert_aliases':inserted,
            'nested_target_owners':dict(packed_target_owner),'unresolved':unresolved,
            'exact_unresolved':exact_unresolved,'runtime_null_fallbacks':runtime_null_fallbacks}


def resolve_phys_preset_sound_prefixes(top:Sequence[PhysPreset],xmodels,packed_string_evidence:Mapping[int,str],*,runtime_compatible:bool=False)->dict:
    """Resolve only packed PhysPreset sndAliasPrefix XStrings proven elsewhere by the exact raw pointer."""
    resolved=already=0;unresolved=[]
    rows=list(top)+[x.model.inline_phys_preset for x in xmodels if x.model.inline_phys_preset is not None]
    seen=set()
    for p in rows:
        if id(p) in seen:continue
        seen.add(id(p))
        raw=int(getattr(p,'serialized_sound_alias_prefix_pointer',0))
        ptr=decode_pc_pointer(raw)
        if ptr.kind=='null':
            p.sound_alias_prefix=None;setattr(p,'sound_alias_prefix_identity_proven',True);already+=1;continue
        if getattr(p,'sound_alias_prefix_identity_proven',p.sound_alias_prefix is not None):
            already+=1;continue
        if ptr.kind!='packed':
            unresolved.append({'preset':p.name,'raw':raw,'reason':f'unsupported unresolved pointer kind {ptr.kind}'})
            continue
        value=packed_string_evidence.get(raw)
        if value is None:
            unresolved.append({'preset':p.name,'raw':raw,'reason':'no exact global packed-XString evidence'})
            continue
        p.sound_alias_prefix=value;setattr(p,'sound_alias_prefix_identity_proven',True);resolved+=1
    exact_unresolved=tuple(unresolved)
    runtime_empty_fallbacks=0
    if unresolved and not runtime_compatible:
        raise ValueError(f'PhysPreset sndAliasPrefix closure unresolved ({len(unresolved)}): {unresolved[:8]}')
    if unresolved and runtime_compatible:
        # A packed PC pointer cannot be retained, and NULL crashes physics sound
        # initialization. An owned empty XString takes the engine's empty-prefix
        # path. Retain the unresolved source identity as explicit fidelity debt.
        unresolved_ids={(x['preset'],x['raw']) for x in unresolved}
        for p in rows:
            raw=int(getattr(p,'serialized_sound_alias_prefix_pointer',0))
            if (p.name,raw) in unresolved_ids:
                p.sound_alias_prefix=''
                setattr(p,'sound_alias_prefix_identity_proven',False)
                setattr(p,'sound_alias_prefix_runtime_empty',True)
        runtime_empty_fallbacks=len(unresolved)
        unresolved=[]
    return {'resolved_from_global_xstring':resolved,'already_proven_or_null':already,'unresolved':unresolved,
            'exact_unresolved':exact_unresolved,'runtime_null_fallbacks':0,
            'runtime_empty_fallbacks':runtime_empty_fallbacks}
