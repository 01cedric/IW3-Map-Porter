from __future__ import annotations
from dataclasses import dataclass,field,replace
from enum import Enum
import hashlib,math,struct
from typing import Mapping
from ..graph import AssetType,Dependency,GraphContext,nested_symbol
from ..zone_writer import RelocatingZoneWriter,PackedOffset,TEMP_BLOCK,VIRTUAL_BLOCK,LARGE_BLOCK,VERTEX_BLOCK,FOLLOWING,INSERT,NULL
from ..backend.v4_ffio import decode_pc_pointer,XAssetList
from ..pc_xmodel import xasset_header_alias_index
from ..pc_gfxworld import GfxWorldReport,GfxImagePayload,PC_CELL,PC_AABB,PC_PORTAL,PC_SURFACE,PC_DRAW,PC_WORLD_VERTEX,PC_SMODEL_INST,PC_CULL
from ..gfxworld_aabb import resolve_aabb_reuse,AabbReusePlan,ps3_children_offset
from ..gfxworld_portals import resolve_portals
from ..plane_format import ps3_plane_signbits
from ..vertex_format import pack_rsx_unit_vector,pack_rsx_unit_vector_from_floats,ps3_vertex_color

PS3_ROOT=0x2E4;PS3_AABB=0x28;PS3_VERTEX=0x10;PS3_VERTEX_LAYER=0x1C;PS3_SURFACE=0x34;PS3_DRAW=0x2C
PS3_GFX_TEXTURE_RUNTIME=0x18
PS3_SCENE_DYN_MODEL_RUNTIME=0x06
PS3_GFX_DRAW_SURF_RUNTIME=0x08
PS3_DPVS_ALIGNED_RUNTIME=0x04


class GfxWorldResourcePolicy(str,Enum):
    """Pixel-payload policy for GfxWorld-owned image shells.

    ``RETAIL_DELAYED`` matches the inspected PS3 Shipment main-zone contract: metadata keeps
    resourceSize/data=FOLLOWING but delayLoadPixels is set and no pixel bytes are consumed from
    the main FastFile. ``SELF_CONTAINED`` is an explicit diagnostic A/B mode. ``SOURCE`` preserves
    each PC source delay flag and embeds only non-delayed resources.
    """
    SOURCE='source'
    RETAIL_DELAYED='retail-delayed'
    FASTFILE_DELAYED='fastfile-delayed'
    SELF_CONTAINED='self-contained'
PS3_B8=0x81;PS3_ARGB8=0x85;PS3_DXT1=0x86;PS3_DXT3=0x87;PS3_DXT5=0x88;REMAP_COMP=0x0001AAE4;REMAP_B8=0x0001A9FF

def _new_image_emit_stats(policy:GfxWorldResourcePolicy)->dict:
    return {
        'policy':policy.value,
        'images':0,'delayed_images':0,'embedded_images':0,'external_native_images':0,
        'declared_resource_bytes':0,'embedded_resource_bytes':0,'omitted_resource_bytes':0,
        'entries':[],'external_entries':[],
    }

def u16(z,o):return struct.unpack_from('<H',z,o)[0]
def u32(z,o):return struct.unpack_from('<I',z,o)[0]
def i32(z,o):return struct.unpack_from('<i',z,o)[0]
def f32(z,o):return struct.unpack_from('<f',z,o)[0]
def w16(b,o,v):struct.pack_into('>H',b,o,v&0xffff)
def w32(b,o,v):struct.pack_into('>I',b,o,v&0xffffffff)
def wi32(b,o,v):struct.pack_into('>i',b,o,v)
def wf(b,o,v):
    if not math.isfinite(v):raise ValueError(f'non-finite GfxWorld float {v}')
    struct.pack_into('>f',b,o,v)
def copy_floats(z,b,do,po,n):
    for x in range(0,n,4):wf(b,do+x,f32(z,po+x))
def branch(rep,name):
    for x in rep.full.branches:
        if x.name==name:return x
    raise ValueError(f'missing GfxWorld branch {name}')

def pack_half2(x,y):
    if not math.isfinite(x) or not math.isfinite(y):raise ValueError('non-finite lmap coord')
    hx=struct.unpack('<H',struct.pack('<e',x))[0];hy=struct.unpack('<H',struct.pack('<e',y))[0]
    return (hy<<16)|hx

# The PC PackedUnitVec encoder (three byte components plus a scale byte) has no PS3
# destination: measured against Retail, every PackedUnitVec field on PS3 - vertex normals,
# vertex tangents and GfxStaticModelDrawInst placement axes alike - uses the RSX X11Y11Z10N
# word from cod4porter.vertex_format.  It was removed rather than left as a trap.

# The world-vertex and model-vertex streams use the same two RSX attribute encodings;
# ``cod4porter.vertex_format`` owns them and records the Retail measurement behind each.
# The historical name stays exported because the Version09 byte oracles reference it.
pack_rsx_world_unit_vector=pack_rsx_unit_vector

def ps3_vertex_layer_offset(source_offset:int,first_vertex:int,total_bytes:int)->int:
    """Map a PC surface layer offset into the expanded PS3 vertex-layer stream.

    Retail Shipment establishes ``target = source + firstVertex * 0x1C``.  The
    current converter supports the common zero-extra-layer PC layout used by
    Nuked/Getaway; maps with additional PC layer streams fail closed until those
    bytes can be interleaved into the PS3 destination exactly.
    """
    if source_offset<0 or first_vertex<0:
        raise ValueError('negative GfxWorld vertex-layer source offset/firstVertex')
    if source_offset:
        raise ValueError(
            f'GfxWorld PC extra vertex-layer offset {source_offset} is not yet supported'
        )
    target=source_offset+first_vertex*PS3_VERTEX_LAYER
    if target<0 or (target and target>=total_bytes):
        raise ValueError(f'GfxWorld PS3 vertex-layer offset {target} outside {total_bytes} bytes')
    return target

@dataclass(frozen=True)
class _ImagePlan:
    source:GfxImagePayload
    format:int
    resource:bytes
    unpadded:int
    sha256:str
    level_count:int
    resource_size:int|None=None
    source_sha256:str=''
    materialized:bool=True
    # A source-owned, delayed GfxImage with no pixel bytes is a database/native
    # reference, not an invitation to substitute an unrelated texture payload.
    external_native:bool=False

    @property
    def declared_resource_size(self)->int:
        return len(self.resource) if self.resource_size is None else int(self.resource_size)

def _mip_bytes(fmt:int,w:int,h:int,d:int=1)->int:
    if fmt==21:return max(1,w)*max(1,h)*max(1,d)*4
    if fmt==50:return max(1,w)*max(1,h)*max(1,d)
    if fmt==0x31545844:return max(1,(w+3)//4)*max(1,(h+3)//4)*8*max(1,d)
    if fmt in (0x33545844,0x35545844):return max(1,(w+3)//4)*max(1,(h+3)//4)*16*max(1,d)
    raise ValueError(f'unsupported inline GfxWorld PC image format 0x{fmt:08X}')

def _effective_levels(img:GfxImagePayload)->int:
    if img.level_count>0:return img.level_count
    if img.map_type!=5 or not img.resource_bytes:
        raise ValueError(f'GfxWorld image {img.name} has zero levelCount without a provable cubemap full-mip payload')
    levels=max(img.width,img.height,img.depth).bit_length()
    face=sum(_mip_bytes(img.pc_format,max(1,img.width>>m),max(1,img.height>>m),max(1,img.depth>>m)) for m in range(levels))
    if img.resource_bytes!=face*6:
        raise ValueError(f'GfxWorld cubemap {img.name} zero-level payload {img.resource_bytes} != full mip proof {face*6}')
    return levels

def _ceil_log2(v:int)->int:
    if v<=1:return 0
    return (v-1).bit_length()

def _swizzle_2d_pot(data:bytes,width:int,height:int,bpp:int)->bytes:
    """Linear -> RSX Z-order/Morton layout for a 2D power-of-two texture.

    This mirrors RPCS3 rsx::convert_linear_swizzle<T,false>.  RSX texture formats
    without CELL_GCM_TEXTURE_LN (0x20) consume this layout, including IW3 PS3
    GfxWorld B8 (0x81) and A8R8G8B8 (0x85) images.  Rectangular POT textures are
    supported; no row pitch/padding is present in IW3's source blobs.
    """
    if width<=0 or height<=0 or bpp<=0:raise ValueError('invalid swizzle geometry')
    if width&(width-1) or height&(height-1):raise ValueError(f'RSX swizzle requires POT dimensions, got {width}x{height}')
    if len(data)!=width*height*bpp:raise ValueError(f'RSX swizzle byte size {len(data)} != {width}x{height}x{bpp}')
    logw=_ceil_log2(width);logh=_ceil_log2(height)
    xmask=0x55555555;ymask=0xAAAAAAAA
    limit=min(logw,logh);limit_mask=1<<(limit<<1)
    xmask=(xmask | (~(limit_mask-1)&0xffffffff))&0xffffffff
    ymask=(ymask & (limit_mask-1))&0xffffffff
    y_incr=limit_mask;offs_y=0;offs_x0=0
    src=memoryview(data);out=bytearray(len(data));dst=memoryview(out)
    for y in range(height):
        offs_x=offs_x0;row=y*width
        if bpp==1:
            for x in range(width):
                dst[offs_y+offs_x]=src[row+x]
                offs_x=((offs_x-xmask)&0xffffffff)&xmask
        else:
            for x in range(width):
                si=(row+x)*bpp;di=(offs_y+offs_x)*bpp
                dst[di:di+bpp]=src[si:si+bpp]
                offs_x=((offs_x-xmask)&0xffffffff)&xmask
        offs_y=((offs_y-ymask)&0xffffffff)&ymask
        if offs_y==0:offs_x0+=y_incr
    return bytes(out)

def _unswizzle_2d_pot(data:bytes,width:int,height:int,bpp:int)->bytes:
    """Inverse of _swizzle_2d_pot; used by validation/tests."""
    if width<=0 or height<=0 or bpp<=0:raise ValueError('invalid unswizzle geometry')
    if width&(width-1) or height&(height-1):raise ValueError(f'RSX unswizzle requires POT dimensions, got {width}x{height}')
    if len(data)!=width*height*bpp:raise ValueError('RSX unswizzle byte size mismatch')
    logw=_ceil_log2(width);logh=_ceil_log2(height)
    xmask=0x55555555;ymask=0xAAAAAAAA
    limit=min(logw,logh);limit_mask=1<<(limit<<1)
    xmask=(xmask | (~(limit_mask-1)&0xffffffff))&0xffffffff
    ymask=(ymask & (limit_mask-1))&0xffffffff
    y_incr=limit_mask;offs_y=0;offs_x0=0
    src=memoryview(data);out=bytearray(len(data));dst=memoryview(out)
    for y in range(height):
        offs_x=offs_x0;row=y*width
        if bpp==1:
            for x in range(width):
                dst[row+x]=src[offs_y+offs_x]
                offs_x=((offs_x-xmask)&0xffffffff)&xmask
        else:
            for x in range(width):
                si=(offs_y+offs_x)*bpp;di=(row+x)*bpp
                dst[di:di+bpp]=src[si:si+bpp]
                offs_x=((offs_x-xmask)&0xffffffff)&xmask
        offs_y=((offs_y-ymask)&0xffffffff)&ymask
        if offs_y==0:offs_x0+=y_incr
    return bytes(out)

def _swizzle_uncompressed_mips(raw:bytes,img:GfxImagePayload,levels:int,bpp:int)->bytes:
    """Swizzle every uncompressed mip independently, preserving IW3 face/mip order."""
    faces=6 if img.map_type==5 else 1
    out=bytearray();cur=0
    for _face in range(faces):
        face_start=len(out)
        for m in range(levels):
            w=max(1,img.width>>m);h=max(1,img.height>>m);d=max(1,img.depth>>m)
            if d!=1:raise ValueError(f'GfxWorld image {img.name}: 3D uncompressed swizzle is not implemented')
            n=w*h*bpp
            if cur+n>len(raw):raise ValueError(f'GfxWorld image {img.name}: truncated mip {m}')
            out+=_swizzle_2d_pot(raw[cur:cur+n],w,h,bpp);cur+=n
        if faces==6:
            out+=b'\0'*((- (len(out)-face_start))%0x80)
    if cur!=len(raw):raise ValueError(f'GfxWorld image {img.name}: {len(raw)-cur} unconsumed source bytes')
    if faces==1:out+=b'\0'*((-len(out))%0x80)
    return bytes(out)


# ---------------------------------------------------------------------------
# Retail PS3 image-payload ceiling
#
# Measured 2026-09-04 over 2843 GfxImages in five Retail PS3 zones (mp_crash,
# mp_shipment, common_mp, ui_mp, code_post_gfx_mp): NOT ONE payload exceeds
# 4 MiB, and no uncompressed GfxWorld image is wider than 1024.
#
#     mp_crash     *lightmap0_primary   1024x2048 B8     2 MiB
#                  *lightmap0_secondary  512x2048 ARGB8  4 MiB
#     mp_shipment  *lightmap0_primary   1024x1024 B8     1 MiB
#                  *lightmap0_secondary  512x1024 ARGB8  2 MiB
#
# A ported PC map brings PC-sized lightmaps: mp_getaway declares four pages with
# 2048x2048 primaries and 1024x2048 secondaries - three 8 MiB payloads, 39.8 MB
# of lightmap against Retail's 6.3 MB, and the console freezes hard inside
# GfxWorld.  A ceiling that no shipping zone ever crosses is a ceiling worth
# respecting, so oversized uncompressed GfxWorld images are halved until they
# meet it.  Lightmap UVs are normalized, so a resampled page still maps to the
# same surfaces - the lighting simply gets softer, which is exactly the trade
# Retail already made against the PC originals.
PS3_MAX_IMAGE_PAYLOAD_BYTES = 4 * 1024 * 1024
PS3_MAX_UNCOMPRESSED_WIDTH = 1024


def _halve_box(data:bytes,width:int,height:int,bpp:int)->bytes:
    """2x2 box filter. Rows are halved with slice assignment, not per-texel indexing."""
    hw=width//2;hh=height//2;stride=width*bpp;out=bytearray(hw*hh*bpp)
    for y in range(hh):
        r0=data[(2*y)*stride:(2*y+1)*stride];r1=data[(2*y+1)*stride:(2*y+2)*stride]
        row=bytearray(hw*bpp)
        for ch in range(bpp):
            step=2*bpp
            row[ch::bpp]=bytes(
                (a+b+c+d)>>2 for a,b,c,d in zip(r0[ch::step],r0[ch+bpp::step],r1[ch::step],r1[ch+bpp::step])
            )
        out[y*hw*bpp:(y+1)*hw*bpp]=row
    return bytes(out)


def _oversized_uncompressed(img:GfxImagePayload)->bool:
    return (
        img.pc_format in (21,50) and img.map_type==3 and img.depth==1
        and max(1,img.level_count)==1 and img.width>=2 and img.height>=2
        and (img.width>PS3_MAX_UNCOMPRESSED_WIDTH or img.resource_bytes>PS3_MAX_IMAGE_PAYLOAD_BYTES)
    )


def _apply_ps3_image_caps(img:GfxImagePayload,source:bytes,*,materialize:bool)->tuple[GfxImagePayload,bytes,int]:
    """Halve an uncompressed GfxWorld image until it fits the measured Retail ceiling."""
    if not _oversized_uncompressed(img):
        return img,source,0
    bpp=4 if img.pc_format==21 else 1
    width=img.width;height=img.height;data=source;steps=0
    while (width>PS3_MAX_UNCOMPRESSED_WIDTH or width*height*bpp>PS3_MAX_IMAGE_PAYLOAD_BYTES) and width>=2 and height>=2:
        if materialize:
            data=_halve_box(data,width,height,bpp)
        width//=2;height//=2;steps+=1
    if not steps:
        return img,source,0
    expected=width*height*bpp
    if materialize and len(data)!=expected:
        raise ValueError(f'GfxWorld image {img.name}: reduced payload {len(data)} != {expected}')
    return replace(img,width=width,height=height,resource_bytes=expected),(data if materialize else b''),steps


def _resource_plan_metadata(img:GfxImagePayload,levels:int)->tuple[int,int]:
    """Return (PS3 format, declared converted resource bytes) without materializing pixels."""
    if img.pc_format==21:fmt=PS3_ARGB8;bpp=4
    elif img.pc_format==50:fmt=PS3_B8;bpp=1
    elif img.pc_format==0x31545844:fmt=PS3_DXT1;bpp=0
    elif img.pc_format==0x33545844:fmt=PS3_DXT3;bpp=0
    elif img.pc_format==0x35545844:fmt=PS3_DXT5;bpp=0
    else:raise ValueError(f'unsupported inline GfxWorld PC image format 0x{img.pc_format:08X}')
    if not img.resource_bytes:return fmt,0
    faces=6 if img.map_type==5 else 1
    if bpp:
        per_face=sum(max(1,img.width>>m)*max(1,img.height>>m)*max(1,img.depth>>m)*bpp for m in range(levels))
        expected=per_face*faces
        if expected!=img.resource_bytes:
            raise ValueError(f'GfxWorld image {img.name} uncompressed payload {img.resource_bytes} != expected {expected}')
        size=((per_face+0x7f)&~0x7f)*faces if faces==6 else ((expected+0x7f)&~0x7f)
    elif faces==6:
        if img.resource_bytes%6:raise ValueError(f'GfxWorld cubemap {img.name} payload not divisible by six faces')
        face=img.resource_bytes//6;size=((face+0x7f)&~0x7f)*6
    else:size=(img.resource_bytes+0x7f)&~0x7f
    return fmt,size


def _convert_image(z:bytes,img:GfxImagePayload,*,materialize:bool=True)->_ImagePlan:
    source=z[img.resource_offset:img.resource_offset+img.resource_bytes]
    if len(source)!=img.resource_bytes:raise ValueError(f'GfxWorld image {img.name} resource range is truncated')
    if not img.resource_bytes:
        # IW3 PC maps can own a GfxImage/LoadDef shell whose pixels are deliberately
        # absent because the image identity is supplied by a stock/support zone.  On
        # PS3 the measured nested form is a zero-body shell with a comma-prefixed
        # XString.  A non-delayed LoadDef with dimensions/format, on the other hand,
        # describes an image which needs pixels; treating that as native would hide a
        # missing IWD/FF binding and can leave the loader waiting for nonexistent data.
        name=img.name.strip()
        if not name or name.casefold().startswith('packed:'):
            raise ValueError(f'GfxWorld image {img.label} has no resource and no exact native image identity')
        has_loaddef_metadata=bool(img.level_count or img.flags or img.width or img.height or img.depth or img.pc_format)
        if has_loaddef_metadata and not img.delay_load_pixels:
            raise ValueError(
                f'GfxWorld image {img.name!r} declares texture metadata but has zero resource bytes; '
                'bind the matching IWD/FF payload before PS3 conversion'
            )
        source_sha=hashlib.sha256(source).hexdigest().upper()
        # Retain the source format in diagnostics when a delayed LoadDef supplied one;
        # the external PS3 shell itself intentionally carries no texture descriptor.
        format_by_pc={21:PS3_ARGB8,50:PS3_B8,0x31545844:PS3_DXT1,0x33545844:PS3_DXT3,0x35545844:PS3_DXT5}
        fmt=format_by_pc.get(img.pc_format,0)
        return _ImagePlan(img,fmt,b'',0,'',max(1,img.level_count),0,source_sha,False,True)
    source_sha=hashlib.sha256(source).hexdigest().upper()
    img,source,reduction_steps=_apply_ps3_image_caps(img,source,materialize=materialize)
    levels=_effective_levels(img) if img.resource_bytes else max(1,img.level_count)
    fmt,declared_size=_resource_plan_metadata(img,levels)
    if not materialize:
        # RETAIL_DELAYED keeps only shell metadata.  Do not spend tens of seconds swizzling
        # pixels which are deliberately absent from the main zone.
        return _ImagePlan(img,fmt,b'',len(source),'',levels,declared_size,source_sha,False)
    if img.pc_format==21:
        if len(source)%4:raise ValueError(f'ARGB image {img.name} size')
        # PC D3DFMT_A8R8G8B8 is BGRA in little-endian memory. Convert each texel to
        # PS3 A,R,G,B byte order first, then Morton-swizzle because 0x85 lacks LN.
        linear=bytearray(len(source))
        for o in range(0,len(source),4):
            bb,g,r,a=source[o:o+4];linear[o:o+4]=bytes((a,r,g,bb))
        unp=len(linear);out=bytearray(_swizzle_uncompressed_mips(bytes(linear),img,levels,4))
    elif img.pc_format==50:
        # CELL_GCM_TEXTURE_B8 (0x81) also lacks CELL_GCM_TEXTURE_LN (0x20).
        # The byte value itself is unchanged; only addressing becomes Morton order.
        unp=len(source);out=bytearray(_swizzle_uncompressed_mips(source,img,levels,1))
    else:out=bytearray(source);unp=len(out)
    # Compressed formats retain the previous byte-preserving path. Their PS3 format
    # identifiers describe block-compressed storage and are not converted as texels here.
    if img.pc_format not in (21,50):
        if img.map_type==5 and out:
            face=len(out)//6;padded=bytearray()
            for f in range(6):
                chunk=out[f*face:(f+1)*face];padded+=chunk;padded+=b'\0'*((-len(chunk))%0x80)
            out=padded
        else:out.extend(b'\0'*((-len(out))%0x80))
    if len(out)!=declared_size:raise ValueError(f'GfxWorld image {img.name} converted size {len(out)} != planned {declared_size}')
    return _ImagePlan(img,fmt,bytes(out),unp,hashlib.sha256(out).hexdigest().upper(),levels,declared_size,source_sha,True)

def _image_shell(plan:_ImagePlan, *, delay_override:int|None=None)->bytes:
    if plan.external_native:
        d=bytearray(0x34);w32(d,0x30,FOLLOWING);return bytes(d)
    i=plan.source
    if i.map_type not in (3,4,5) or not i.width or not i.height or not i.depth:raise ValueError(f'invalid GfxWorld image {i.label}')
    d=bytearray(0x34);wi32(d,0,i.map_type);d[4]=plan.format;d[5]=plan.level_count;d[6]=3 if i.map_type==4 else 2;d[7]=1 if i.map_type==5 else 0;w32(d,8,REMAP_B8 if plan.format==PS3_B8 else REMAP_COMP);w16(d,0xc,i.width);w16(d,0xe,i.height);w16(d,0x10,i.depth);d[0x1c]=i.semantic;w32(d,0x20,plan.declared_resource_size);w16(d,0x24,i.width);w16(d,0x26,i.height);w16(d,0x28,i.depth);d[0x2a]=i.category;d[0x2b]=i.delay_load_pixels if delay_override is None else delay_override;w32(d,0x2c,FOLLOWING if plan.declared_resource_size else 0);w32(d,0x30,FOLLOWING);return bytes(d)

@dataclass
class GfxWorldNode:
    pc_zone:bytes;report:GfxWorldReport;symbol:str
    material_symbols_by_raw:Mapping[int,str]=field(default_factory=dict)
    xmodel_symbols_by_raw:Mapping[int,str]=field(default_factory=dict)
    lightdef_symbols_by_raw:Mapping[int,str]=field(default_factory=dict)
    pc_asset_list:XAssetList|None=None
    material_symbols_by_asset_index:Mapping[int,str]=field(default_factory=dict)
    xmodel_symbols_by_asset_index:Mapping[int,str]=field(default_factory=dict)
    lightdef_symbols_by_asset_index:Mapping[int,str]=field(default_factory=dict)
    # Source GfxWorld may own sunSprite/sunFlare Materials inline (FOLLOWING/INSERT).
    # Map source root-field offsets (0x180/0x184) to the separately emitted typed Material XAsset.
    inline_material_symbols_by_root_field:Mapping[int,str]=field(default_factory=dict)
    inline_material_symbols_by_matmem_index:Mapping[int,str]=field(default_factory=dict)
    runtime_default_material_symbol:str|None=None
    image_resource_policy:GfxWorldResourcePolicy|str=GfxWorldResourcePolicy.FASTFILE_DELAYED
    # 'source' keeps every PC primary light.  'sun-only' keeps the sun and drops the
    # rest.  Measured 2026-09-04: Retail PS3 mp_crash ships 5 primary lights and
    # mp_shipment 2, while a ported PC map like mp_getaway declares 102.  Every
    # per-light structure scales with that count - the serialized shadow/light-region
    # graph and the block-1 runtime reservation (ns * 0x1000 * 4 bytes, 1.64 MB at 102
    # lights against 49 KB at 5).  Dropping the non-sun lights keeps the world lit by
    # its lightmaps and only costs local light contribution on dynamic entities.
    primary_light_policy:str='source'
    # 'source' writes the PC vertex-layer stream unchanged.  'omit' declares zero layer
    # bytes and zeroes every surface layer offset - a diagnostic that removes the one
    # GfxWorld region all four hardware freezes landed in.
    vertex_layer_policy:str='source'
    # 'source' fails closed on GfxWorld portals, which are still unconverted.  'omit'
    # writes every cell with portalCount 0 so a multi-cell map can be built for
    # structural comparison; the map then has no PVS portals.
    portal_policy:str='source'
    # GfxWorld.name, ClipMap.name and GameWorldMp.name all alias one block-4 XString on
    # Retail PS3, and that string is the ComWorld's inline BSP path - 'maps/mp/<map>.d3dbsp',
    # not GfxWorld.baseName.  CM_LoadMap looks the ClipMap up by exactly that path, so
    # aliasing the base name instead makes the game refuse the map with
    # "Couldn't find the bsp for this map".  GfxWorld.baseName (+0x04) stays FOLLOWING with
    # the short name inline, exactly as Retail writes it.
    inline_draw_xmodel_symbols:Mapping[int,str]=field(default_factory=dict)
    shared_map_name_symbol:str|None=None
    type:AssetType=field(init=False,default=AssetType.GFXWORLD);root_block:int=field(init=False,default=TEMP_BLOCK)
    def __post_init__(self):
        if isinstance(self.image_resource_policy,str):self.image_resource_policy=GfxWorldResourcePolicy(self.image_resource_policy)
        materialize=self.image_resource_policy is not GfxWorldResourcePolicy.RETAIL_DELAYED
        self._images={i.label:_convert_image(self.pc_zone,i,materialize=materialize) for i in self.report.full.images}
        self._image_emit_stats={}
        self._aabb=resolve_aabb_reuse(self.pc_zone,self.report)
        self._portals=resolve_portals(self.pc_zone,self.report,self._aabb) if self.portal_policy!='omit' else {}
        self._reordered,self._newbase,self._minidx,self._span=self._build_indices()
        self._primary,self._layers=self._build_vertices()
        if self.vertex_layer_policy=='omit':self._layers=b''
    def _resolve(self,raw,kind):
        if raw==0:return None
        if kind=='material' and self.runtime_default_material_symbol is not None:return self.runtime_default_material_symbol
        direct={'material':self.material_symbols_by_raw,'xmodel':self.xmodel_symbols_by_raw,'lightdef':self.lightdef_symbols_by_raw}[kind]
        if raw in direct:return direct[raw]
        if self.pc_asset_list is not None:
            typ={'material':4,'xmodel':3,'lightdef':0x13}[kind];idx=xasset_header_alias_index(self.pc_asset_list,raw,typ)
            if idx is not None:
                by={'material':self.material_symbols_by_asset_index,'xmodel':self.xmodel_symbols_by_asset_index,'lightdef':self.lightdef_symbols_by_asset_index}[kind]
                if idx in by:return by[idx]
        raise ValueError(f'GfxWorld unresolved {kind} pointer 0x{raw:08X}')
    @property
    def dependencies(self):
        found={}
        def add(raw,kind,typ,desc):
            if not raw:return
            p=decode_pc_pointer(raw)
            if p.kind=='null':return
            if p.kind in ('following','insert') and kind=='material':return # inline material is regenerated separately and caller maps it by raw if needed
            try:s=self._resolve(raw,kind)
            except ValueError:return
            found[(s,typ)]=Dependency(s,typ,desc)
        r=self.report.root_offset;z=self.pc_zone
        for off,desc in ((0x180,'GfxWorld sun sprite'),(0x184,'GfxWorld sun flare')):
            raw=u32(z,r+off); pk=decode_pc_pointer(raw).kind
            if pk in ('following','insert'):
                sym=self.inline_material_symbols_by_root_field.get(off) or self.runtime_default_material_symbol
                if sym is None: raise ValueError(f'{desc} inline Material has no typed destination binding')
                found[(sym,AssetType.MATERIAL)]=Dependency(sym,AssetType.MATERIAL,desc+' inline material')
            else:
                add(raw,'material',AssetType.MATERIAL,desc)
        # materialMemory/surfaces/draws
        b=branch(self.report,'brush-model/material-memory roots');mm=b.start+self.report.model_count*0x38
        for i in range(self.report.material_memory_count):
            raw=u32(z,mm+i*8); pk=decode_pc_pointer(raw).kind
            if pk in ('following','insert'):
                sym=self.inline_material_symbols_by_matmem_index.get(i) or self.runtime_default_material_symbol
                if sym is None: raise ValueError(f'GfxWorld materialMemory[{i}] inline Material has no typed destination binding')
                found[(sym,AssetType.MATERIAL)]=Dependency(sym,AssetType.MATERIAL,f'GfxWorld materialMemory[{i}] inline material')
            else:
                add(raw,'material',AssetType.MATERIAL,'GfxWorld materialMemory')
        dp=branch(self.report,'DPVS serialized graph');cur=dp.start+(self.report.static_surface_count+self.report.static_surface_count_no_decal)*2+self.report.static_model_count*0x1c
        for i in range(self.report.surface_count):add(u32(z,cur+i*PC_SURFACE+0x10),'material',AssetType.MATERIAL,'GfxWorld surface')
        cur+=self.report.surface_count*PC_SURFACE+self.report.cull_group_count*PC_CULL
        for i in range(self.report.static_model_count):add(u32(z,cur+i*PC_DRAW+0x38),'xmodel',AssetType.XMODEL,'GfxWorld smodelDraw')
        sun_raw=u32(z,r+0xc8)
        if decode_pc_pointer(sun_raw).kind in ('following','insert'):
            sun=branch(self.report,'sunLight');def_raw=u32(z,sun.start+0x3c)
            if decode_pc_pointer(def_raw).kind=='packed':add(def_raw,'lightdef',AssetType.LIGHTDEF,'GfxWorld sunLight.def')
        for symbol in self.inline_draw_xmodel_symbols.values():
            found[(symbol,AssetType.XMODEL)]=Dependency(symbol,AssetType.XMODEL,'GfxWorld inline shared XModel')
        return tuple(found.values())
    def _build_indices(self):
        z=self.pc_zone;ib=branch(self.report,'indices');db=branch(self.report,'DPVS serialized graph');sr=db.start+(self.report.static_surface_count+self.report.static_surface_count_no_decal)*2+self.report.static_model_count*0x1c
        out=bytearray(self.report.index_count*2);cursor=0;nb={};mi={};sp={}
        for i in range(self.report.surface_count):
            s=sr+i*PC_SURFACE;tri=u16(z,s+0xa);cnt=tri*3;base=i32(z,s+0xc);vc=u16(z,s+8);vals=[]
            if base<0 or base>self.report.index_count-cnt:raise ValueError('GfxWorld surface index base')
            for j in range(cnt):
                v=u16(z,ib.start+(base+j)*2)
                if v>=vc:raise ValueError('GfxWorld local index')
                vals.append(v);struct.pack_into('>H',out,(cursor+j)*2,v)
            first=min(vals) if vals else 0;span=max(vals)-first+1 if vals else 0;fv=i32(z,s+4)
            if fv<0 or fv+first+span>self.report.vertex_count:raise ValueError('GfxWorld vertex window')
            nb[i]=cursor;mi[i]=first;sp[i]=span;cursor+=cnt
        if cursor!=self.report.index_count:raise ValueError(f'GfxWorld index coverage {cursor}!={self.report.index_count}')
        return bytes(out),nb,mi,sp
    def _build_vertices(self):
        z=self.pc_zone
        vb=branch(self.report,'vd.vertices')
        p=bytearray(self.report.vertex_count*PS3_VERTEX)
        for i in range(self.report.vertex_count):
            s=vb.start+i*PC_WORLD_VERTEX;po=i*PS3_VERTEX
            # PS3 primary world-vertex stream: position/binormal-sign words only.
            for j in range(4):wf(p,po+j*4,f32(z,s+j*4))
        # PS3 expands the PC interleaved world vertex into a 0x10 primary stream
        # plus a 0x1C RSX layer record per vertex.  The four-byte PC vld blob is
        # not the PS3 destination size; it is a separate source-side runtime field.
        layers=bytearray(self.report.vertex_count*PS3_VERTEX_LAYER)
        for i in range(self.report.vertex_count):
            s=vb.start+i*PC_WORLD_VERTEX;d=i*PS3_VERTEX_LAYER
            # GfxColor is four independent RSX attribute bytes, not a word: PC keeps
            # D3DCOLOR order (blue, green, red, alpha), PS3 keeps red, green, blue, alpha.
            layers[d:d+4]=ps3_vertex_color(z[s+0x10:s+0x14])
            for j in range(4):wf(layers,d+4+j*4,f32(z,s+0x14+j*4))
            w32(layers,d+0x14,pack_rsx_world_unit_vector(z[s+0x24:s+0x28]))
            w32(layers,d+0x18,pack_rsx_world_unit_vector(z[s+0x28:s+0x2c]))
        return bytes(p),bytes(layers)
    def _copy_sunparse(self,b,ps,pc):
        z=self.pc_zone;b[ps:ps+0x40]=z[pc:pc+0x40]
        for o in range(0x40,0x70,4):wf(b,ps+o,f32(z,pc+o))
        b[ps+0x70]=z[pc+0x70]
        for o in range(0x74,0x80,4):wf(b,ps+o,f32(z,pc+o))
    def _copy_gridroot(self,b,ps,pc):
        z=self.pc_zone;b[ps]=z[pc];w32(b,ps+4,u32(z,pc+4))
        for i in range(3):w16(b,ps+8+i*2,u16(z,pc+8+i*2));w16(b,ps+0xe+i*2,u16(z,pc+0xe+i*2))
        wi32(b,ps+0x14,i32(z,pc+0x14));wi32(b,ps+0x18,i32(z,pc+0x18));ra=i32(z,pc+0x14)
        if ra not in (0,1,2):raise ValueError('lightgrid row axis')
        rows=u16(z,pc+0xe+ra*2)-u16(z,pc+8+ra*2)+1;w32(b,ps+0x1c,FOLLOWING if rows>0 else 0)
        for o in (0x20,0x28,0x30):
            v=i32(z,pc+o);wi32(b,ps+o,v);w32(b,ps+o+4,FOLLOWING if v>0 else 0)
    def _copy_sunflare(self,b,ps,pc):
        z=self.pc_zone;b[ps]=z[pc]
        for o in range(0xc,0x60,4):w32(b,ps+o,u32(z,pc+o))
    def _root(self):
        z=self.pc_zone;r=self.report.root_offset;q=self.report;b=bytearray(PS3_ROOT);w32(b,4,FOLLOWING);wi32(b,8,q.plane_count);wi32(b,0xc,q.node_count);wi32(b,0x10,q.index_count);w32(b,0x14,FOLLOWING if q.index_count else 0);wi32(b,0x1c,q.surface_count);b[0x20]=z[r+0x1c];wi32(b,0x24,q.sky_surface_count);w32(b,0x28,FOLLOWING if q.sky_surface_count else 0);w32(b,0x2c,INSERT if 'skyImage' in self._images else 0);b[0x30]=z[r+0x2c];wi32(b,0x34,q.vertex_count);w32(b,0x38,FOLLOWING);wi32(b,0x44,len(self._layers));w32(b,0x48,FOLLOWING if self._layers else 0);self._copy_sunparse(b,0x54,r+0x48);w32(b,0xd4,FOLLOWING if any(x.name=='sunLight' for x in q.full.branches) else 0);copy_floats(z,b,0xd8,r+0xcc,12);wi32(b,0xe4,q.sun_primary_light_index);wi32(b,0xe8,self.effective_primary_light_count);wi32(b,0xec,q.cull_group_count);wi32(b,0xf0,q.reflection_probe_count);w32(b,0xf4,FOLLOWING if q.reflection_probe_count else 0);w32(b,0xf8,FOLLOWING if q.reflection_probe_count else 0);wi32(b,0xfc,q.cell_count);w32(b,0x100,FOLLOWING if q.plane_count else 0);w32(b,0x104,FOLLOWING if q.node_count else 0);w32(b,0x108,FOLLOWING if q.cell_count else 0);wi32(b,0x10c,q.cell_bits_count);w32(b,0x110,FOLLOWING if q.cell_count else 0);wi32(b,0x114,q.lightmap_count);w32(b,0x118,FOLLOWING if q.lightmap_count else 0);self._copy_gridroot(b,0x11c,r+0x110);w32(b,0x154,FOLLOWING if q.lightmap_count else 0);w32(b,0x158,FOLLOWING if q.lightmap_count else 0);wi32(b,0x15c,q.model_count);w32(b,0x160,FOLLOWING if q.model_count else 0);copy_floats(z,b,0x164,r+0x158,24);w32(b,0x17c,q.checksum);wi32(b,0x180,q.material_memory_count);w32(b,0x184,FOLLOWING if q.material_memory_count else 0);self._copy_sunflare(b,0x188,r+0x17c);copy_floats(z,b,0x1e8,r+0x1dc,0x40);w32(b,0x228,INSERT if 'outdoorImage' in self._images else 0);w32(b,0x22c,FOLLOWING if q.cell_count else 0);w32(b,0x230,FOLLOWING if q.dynamic_model_count else 0);w32(b,0x234,FOLLOWING if q.dynamic_brush_count else 0);nonsun=max(0,self.effective_primary_light_count-q.sun_primary_light_index-1);w32(b,0x238,FOLLOWING if nonsun else 0);w32(b,0x23c,FOLLOWING if nonsun and q.dynamic_model_count else 0);w32(b,0x240,FOLLOWING if nonsun and q.dynamic_brush_count else 0);w32(b,0x244,FOLLOWING if q.dynamic_model_count else 0);w32(b,0x248,FOLLOWING if self.effective_primary_light_count else 0);w32(b,0x24c,FOLLOWING if self.effective_primary_light_count else 0)
        pd=r+0x244;ps=0x250
        # PS3 GfxWorldDpvsStatic has no staticSurfaceCountNoDecal: PC word 2 is dropped and the
        # six surface-range words move down by one, so emissiveSurfsEnd survives at ps+0x1C.
        # Measured against Retail PS3 mp_shipment (smodelCount 1343, staticSurfaceCount 2526,
        # litSurfsBegin 0, litSurfsEnd 2525, decalSurfs 2525..2526, emissiveSurfs 2526..2526).
        wi32(b,ps+0x00,i32(z,pd+0x00));wi32(b,ps+0x04,i32(z,pd+0x04))
        for i in range(6):wi32(b,ps+0x08+i*4,i32(z,pd+0x0c+i*4))
        wi32(b,ps+0x20,i32(z,pd+0x24));wi32(b,ps+0x24,i32(z,pd+0x28))
        for i in range(3):w32(b,ps+0x28+i*4,FOLLOWING if q.static_model_count else 0);w32(b,ps+0x34+i*4,FOLLOWING if q.static_surface_count else 0)
        smv=i32(z,pd+0x24);sv=i32(z,pd+0x28);w32(b,ps+0x40,FOLLOWING if smv else 0);w32(b,ps+0x44,FOLLOWING if q.static_surface_count else 0);w32(b,ps+0x48,FOLLOWING if q.static_model_count else 0);w32(b,ps+0x4c,FOLLOWING if q.surface_count else 0);w32(b,ps+0x50,FOLLOWING if q.cull_group_count else 0);w32(b,ps+0x54,FOLLOWING if q.static_model_count else 0);w32(b,ps+0x58,FOLLOWING if q.static_surface_count else 0);w32(b,ps+0x5c,FOLLOWING if sv else 0);w32(b,ps+0x60,0)
        pd=r+0x2ac;ps=0x2b4
        for i in range(4):wi32(b,ps+i*4,i32(z,pd+i*4))
        a=i32(z,pd);bb=i32(z,pd+4);w32(b,ps+0x10,FOLLOWING if a and q.cell_count else 0);w32(b,ps+0x14,FOLLOWING if bb and q.cell_count else 0)
        for i in range(3):w32(b,ps+0x18+i*4,FOLLOWING if a else 0);w32(b,ps+0x24+i*4,FOLLOWING if bb else 0)
        return bytes(b)
    @property
    def effective_primary_light_count(self)->int:
        q=self.report
        if self.primary_light_policy=='sun-only':
            return min(q.primary_light_count,max(1,q.sun_primary_light_index+1))
        return q.primary_light_count

    def _clamp_primary_light(self,value:int)->int:
        """Map a source primary-light index onto the kept set.

        Index 0 is 'no primary light' and stays 0; everything above the sun collapses
        onto the sun so no surface, static model or light-grid entry can address a
        light that is no longer serialized.
        """
        if self.primary_light_policy!='sun-only':return value
        limit=self.effective_primary_light_count-1
        return value if value<=limit else max(0,limit)

    def _write_image(self,w,label):
        p=self._images[label];policy=self.image_resource_policy
        # Unit validators exercise this primitive directly, outside ``write``.  Keep the
        # accounting self-contained instead of relying on the top-level writer to initialize it.
        if not self._image_emit_stats:
            self._image_emit_stats=_new_image_emit_stats(policy)
        has_resource=p.declared_resource_size>0
        if p.external_native:
            delay=0;embed=False;defer=False
        elif policy is GfxWorldResourcePolicy.RETAIL_DELAYED:
            delay=1 if has_resource else p.source.delay_load_pixels;embed=False;defer=False
        elif policy is GfxWorldResourcePolicy.FASTFILE_DELAYED:
            delay=1 if has_resource else p.source.delay_load_pixels;embed=False;defer=has_resource
        elif policy is GfxWorldResourcePolicy.SELF_CONTAINED:
            delay=0 if has_resource else p.source.delay_load_pixels;embed=has_resource;defer=False
        elif policy is GfxWorldResourcePolicy.SOURCE:
            delay=p.source.delay_load_pixels;embed=bool(has_resource and not delay);defer=False
        else:raise ValueError(f'unknown GfxWorld image-resource policy {policy!r}')
        # Every GfxImage pointee is an XAsset: its 0x34 shell is allocated in
        # TEMP, then Load_GfxImage pushes default-normal B4 for the name.  This
        # is also true when the owning pointer is embedded in a B4 lightmap row.
        w.push_block(TEMP_BLOCK);w.align_logical_only(4);shell_location=w.current_location;shell_physical=w.physical_position
        w.write_in_block(_image_shell(p,delay_override=delay),f'GfxWorld image {label} TEMP root')
        loaddef_location=None
        if has_resource:
            w.push_block(TEMP_BLOCK);w.align_logical_only(4);loaddef_location=w.current_location
            w.reserve_logical_only(0x10,f'GfxWorld image {label} LoadDef TEMP object');w.pop_block()
        w.pop_block()
        serialized_name=(','+p.source.name.lstrip(',')) if p.external_native else p.source.name
        w.push_block(VIRTUAL_BLOCK);name_location=w.current_location;w.write_latin1z(serialized_name,f'GfxWorld image {label} name');w.pop_block()
        resource_physical=None
        if embed:
            if not p.materialized or len(p.resource)!=p.declared_resource_size:
                raise ValueError(f'GfxWorld image {label} policy requires materialized resource bytes')
            w.push_block(VERTEX_BLOCK);w.align_logical_only(0x80);resource_physical=w.physical_position;w.write_in_block(p.resource,f'GfxWorld image {label} resource');w.pop_block()
        row={'label':label,'name':serialized_name,'source_name':p.source.name,'external_native':p.external_native,'shell_physical':shell_physical,'shell_location':{'block':shell_location.block,'offset':shell_location.offset},'loaddef_location':None if loaddef_location is None else {'block':loaddef_location.block,'offset':loaddef_location.offset},'name_location':{'block':name_location.block,'offset':name_location.offset},'resource_physical':resource_physical,'delay_load_pixels':int(delay),'declared_resource_bytes':p.declared_resource_size,'embedded':embed,'deferred':defer,'resource_materialized':p.materialized,'resource_sha256':p.sha256 or None,'source_resource_sha256':p.source_sha256}
        if defer:
            if not p.materialized or len(p.resource)!=p.declared_resource_size:
                raise ValueError(f'GfxWorld image {label} delayed policy requires materialized resource bytes')
            w.defer_payload(p.resource,alignment=0x80,description=f'GfxWorld image {label} delayed resource',result_row=row)
        self._image_emit_stats['images']+=1
        self._image_emit_stats['declared_resource_bytes']+=p.declared_resource_size
        self._image_emit_stats['embedded_resource_bytes']+=p.declared_resource_size if embed else 0
        self._image_emit_stats.setdefault('deferred_resource_bytes',0)
        self._image_emit_stats['deferred_resource_bytes']+=p.declared_resource_size if defer else 0
        self._image_emit_stats['omitted_resource_bytes']+=p.declared_resource_size if not embed and not defer else 0
        self._image_emit_stats['delayed_images']+=int(bool(delay))
        self._image_emit_stats['embedded_images']+=int(embed)
        self._image_emit_stats['external_native_images']+=int(p.external_native)
        # Resource validators/FIFO checks operate only on resource-bearing image plans.
        # Keep native shells visible, but separate, so a zero-byte external reference is
        # never mistaken for a delayed-payload FIFO row.
        self._image_emit_stats['external_entries' if p.external_native else 'entries'].append(row)
    def _relocate(self,w,c,physical,raw,kind,desc):
        if raw==0:return
        sym=self._resolve(raw,kind);w.register_pointer_relocation(physical,c.reference_symbol(sym),kind='packed_alias',description=desc)
    def _write_sunlight(self,w,c):
        z=self.pc_zone;r=self.report.root_offset;raw=u32(z,r+0xc8)
        if decode_pc_pointer(raw).kind not in ('following','insert'):return
        b=branch(self.report,'sunLight');p=b.start;out=bytearray(0x40);out[:4]=z[p:p+4]
        for o in range(4,0x3c,4):w32(out,o,u32(z,p+o))
        dr=u32(z,p+0x3c);dp=decode_pc_pointer(dr);w32(out,0x3c,FOLLOWING if dp.kind in ('following','insert') else 0)
        w.push_block(VIRTUAL_BLOCK);w.align_logical_only(4);phys=w.physical_position;w.write_in_block(out,'GfxWorld sunLight');w.pop_block()
        if dp.kind=='packed':self._relocate(w,c,phys+0x3c,dr,'lightdef','GfxWorld.sunLight.def')
        # inline LightDef is expected to be emitted by the source branch but not duplicated here; fail rather than copy untyped bytes.
        if dp.kind in ('following','insert'):raise ValueError('inline GfxWorld sun LightDef requires typed LightDef subnode binding')
    def _write_reflections(self,w):
        q=self.report
        if not q.reflection_probe_count:return
        b=branch(q,'reflectionProbes');roots=b.start;w.push_block(VIRTUAL_BLOCK);w.align_logical_only(4)
        for i in range(q.reflection_probe_count):
            p=roots+i*0x10;d=bytearray(0x10);copy_floats(self.pc_zone,d,0,p,12);label=f'reflectionProbe[{i}]';w32(d,0xc,FOLLOWING if label in self._images else 0);w.write_in_block(d,label)
        w.pop_block();w.push_block(TEMP_BLOCK)
        for i in range(q.reflection_probe_count):
            label=f'reflectionProbe[{i}]'
            if label in self._images:self._write_image(w,label)
        w.pop_block()
    def _write_planes_nodes(self,w):
        q=self.report;z=self.pc_zone;w.push_block(LARGE_BLOCK);w.align_logical_only(4);phys=w.physical_position;w.define_symbol(nested_symbol(self.symbol,'planes'));b=branch(q,'planes')
        for i in range(q.plane_count):
            p=b.start+i*0x14;d=bytearray(0x14)
            for j in range(4):wf(d,j*4,f32(z,p+j*4))
            # type and the two pad bytes copy through; signbits does not - PS3 marks an axial
            # plane with 8 where PC stores the (always zero) sign mask.  See plane_format.
            d[0x10]=z[p+0x10];d[0x11]=ps3_plane_signbits([f32(z,p+j*4) for j in range(3)],z[p+0x10]);d[0x12:0x14]=z[p+0x12:p+0x14]
            w.write_in_block(d,'GfxWorld plane')
        w.align_logical_only(2);b=branch(q,'nodes')
        for i in range(q.node_count):w.write_u16(u16(z,b.start+i*2),'GfxWorld node')
        w.pop_block();return phys
    def _write_cells(self,w):
        q=self.report;z=self.pc_zone;b=branch(q,'cells/AABB/portals');roots=b.start;w.push_block(LARGE_BLOCK);w.align_logical_only(4);phys=w.physical_position;states=[]
        cell_symbol=nested_symbol(self.symbol,'cells')
        w.define_symbol(cell_symbol)
        for ci in range(q.cell_count):
            p=roots+ci*PC_CELL;d=bytearray(PC_CELL);copy_floats(z,d,0,p,24);ac=i32(z,p+0x18);po=i32(z,p+0x20);cu=i32(z,p+0x28);pr=z[p+0x30];wi32(d,0x18,ac);w32(d,0x1c,FOLLOWING if ac else 0);wi32(d,0x20,0 if self.portal_policy=='omit' else po);w32(d,0x24,0 if self.portal_policy=='omit' else (FOLLOWING if po else 0));wi32(d,0x28,cu);w32(d,0x2c,FOLLOWING if cu else 0);d[0x30]=pr;w32(d,0x34,FOLLOWING if pr else 0);w.write_in_block(d,'GfxWorld cell');states.append((ci,ac,po,cu,pr))
        cur=roots+q.cell_count*PC_CELL
        for ci,ac,po,cu,pr in states:
            if ac:
                w.align_logical_only(4);ar=cur;cur+=ac*PC_AABB;inline=[]
                for ai in range(ac):
                    p=ar+ai*PC_AABB;bind=self._aabb.bindings_by_record[p];d=bytearray(PS3_AABB);copy_floats(z,d,0,p,24);w16(d,0x18,u16(z,p+0x18));w16(d,0x1a,u16(z,p+0x1a));w16(d,0x1c,u16(z,p+0x1c));w16(d,0x1e,bind.smodel_count);w32(d,0x20,FOLLOWING if bind.smodel_count and bind.pointer_kind in ('following','insert') else 0)
                    child_offset=ps3_children_offset(i32(z,p+0x28),u16(z,p+0x18),ai,ac)
                    wi32(d,0x24,child_offset);ph=w.physical_position;w.write_in_block(d,'GfxWorld AABB')
                    if bind.pointer_kind=='packed':w.register_pointer_relocation(ph+0x20,nested_symbol(self.symbol,f'aabb.{bind.canonical_inline_data_offset:08X}'),bind.target_byte_offset,kind='packed_pointer',description='AABB smodel reuse')
                    elif bind.smodel_count:inline.append(bind)
                for bind in inline:
                    data=bind.inline_data_offset
                    if data!=cur:raise ValueError('AABB inline cursor mismatch')
                    w.align_logical_only(2);w.define_symbol(nested_symbol(self.symbol,f'aabb.{data:08X}'))
                    for j in range(bind.smodel_count):w.write_u16(u16(z,data+j*2),'AABB smodel')
                    cur+=bind.smodel_count*2
            if po:
                proots=cur;cur+=po*PC_PORTAL
                if self.portal_policy!='omit':
                    w.align_logical_only(4)
                    for j in range(po):
                        p=proots+j*PC_PORTAL;bind=self._portals[p];d=bytearray(0x44)
                        # writable (+0..0x0B) contains transient visibility state/pointers.
                        # Reset it; preserve the plane, side bytes, vertices and hull axes.
                        copy_floats(z,d,0xc,p+0xc,16);d[0x1c:0x20]=z[p+0x1c:p+0x20]
                        w32(d,0x24,FOLLOWING if bind.vertex_count else 0);d[0x28]=bind.vertex_count
                        copy_floats(z,d,0x2c,p+0x2c,24)
                        ph=w.physical_position;w.write_in_block(d,'GfxWorld portal')
                        w.register_pointer_relocation(ph+0x20,cell_symbol,bind.target_cell*PC_CELL,kind='packed_pointer',description='Portal neighbour cell')
                for j in range(po):
                    qp=proots+j*PC_PORTAL;vc=z[qp+0x28]
                    if vc and decode_pc_pointer(u32(z,qp+0x24)).kind in ('following','insert'):
                        if self.portal_policy!='omit':
                            if self._portals[qp].vertices_offset!=cur:raise ValueError('Portal vertex cursor mismatch')
                            w.align_logical_only(4);d=bytearray(vc*12);copy_floats(z,d,0,cur,len(d));w.write_in_block(d,'Portal vertices')
                        cur+=vc*0xc
            if cu:w.align_logical_only(4);[w.write_i32(i32(z,cur+i*4),'GfxCell cull') for i in range(cu)];cur+=cu*4
            if pr:w.write_in_block(z[cur:cur+pr],'GfxCell probes');cur+=pr
        if cur!=b.end:raise ValueError('GfxWorld cell cursor mismatch')
        w.pop_block();return phys
    def _write_lighting(self,w):
        q=self.report;z=self.pc_zone;b=branch(q,'lightmaps/LightGrid');cur=b.start;w.push_block(LARGE_BLOCK);w.align_logical_only(4);phys=w.physical_position;roots=cur;cur+=q.lightmap_count*8
        for i in range(q.lightmap_count):
            d=bytearray(8);w32(d,0,FOLLOWING if f'lightmap[{i}].primary' in self._images else 0);w32(d,4,FOLLOWING if f'lightmap[{i}].secondary' in self._images else 0);w.write_in_block(d,'lightmap root')
        for i in range(q.lightmap_count):
            for lab in (f'lightmap[{i}].primary',f'lightmap[{i}].secondary'):
                if lab in self._images:self._write_image(w,lab)
        for p in self._images.values():
            if p.source.label.startswith('lightmap['):cur=max(cur,p.source.end_offset)
        grid=q.root_offset+0x110;ra=i32(z,grid+0x14);rows=u16(z,grid+0xe+ra*2)-u16(z,grid+8+ra*2)+1;w.align_logical_only(2)
        for i in range(rows):w.write_u16(u16(z,cur+i*2),'LightGrid row')
        cur+=rows*2;rb=i32(z,grid+0x20);w.write_in_block(z[cur:cur+rb],'LightGrid raw');cur+=rb;ec=i32(z,grid+0x28);w.align_logical_only(4)
        for i in range(ec):p=cur+i*4;w.write_u16(u16(z,p),'LightGrid colorIndex');w.write_byte(self._clamp_primary_light(z[p+2]),'LightGrid primaryLight');w.write_byte(z[p+3],'LightGrid trace')
        cur+=ec*4;cc=i32(z,grid+0x30);w.align_logical_only(4)
        for i in range(cc):w.write_in_block(z[cur+i*0xa8:cur+(i+1)*0xa8],'LightGrid color')
        cur+=cc*0xa8
        if cur!=b.end:raise ValueError('lighting cursor mismatch')
        w.pop_block();return phys
    def _write_models_materialmemory(self,w,c):
        q=self.report;z=self.pc_zone;b=branch(q,'brush-model/material-memory roots');cur=b.start;w.push_block(LARGE_BLOCK);w.align_logical_only(4);phys=w.physical_position
        for i in range(q.model_count):
            # PS3 GfxBrushModel keeps the PC 0x38 size but widens the two 16-bit surface
            # fields to 32 bits and drops surfaceCountNoDecal: +0x30 u32 surfaceCount,
            # +0x34 u32 startSurfIndex.  Measured on Retail PS3 mp_shipment (2526/0 for the
            # worldspawn model, 0/0xFFFFFFFF for all 31 others) and mp_crash (5563/0, 33x
            # 0/0xFFFFFFFF).  The PC 0xFFFF "no surfaces" sentinel widens to 0xFFFFFFFF.
            p=cur+i*0x38;d=bytearray(0x38);copy_floats(z,d,0x18,p+0x18,0x18)
            wi32(d,0x30,u16(z,p+0x30));start=u16(z,p+0x32);w32(d,0x34,0xFFFFFFFF if start==0xFFFF else start)
            w.write_in_block(d,'brush model')
        cur+=q.model_count*0x38;w.pop_block();w.push_block(LARGE_BLOCK);w.align_logical_only(4);owned=[]
        for i in range(q.material_memory_count):
            p=cur+i*8;raw=u32(z,p);pk=decode_pc_pointer(raw).kind;sym=None
            if raw:
                if pk in ('following','insert'):
                    sym=self.inline_material_symbols_by_matmem_index.get(i) or self.runtime_default_material_symbol
                    if sym is None:raise ValueError(f'GfxWorld materialMemory[{i}] inline Material has no typed destination binding')
                else:sym=self._resolve(raw,'material')
            site=f'materialMemory[{i}]';d=bytearray(8);wi32(d,4,i32(z,p+4));marker=c.owned_marker(self.symbol,sym,site) if sym else None;logical=w.current_location
            if marker is not None:w32(d,0,marker);owned.append((sym,site));c.bind_owned_pointer_field(w,self.symbol,sym,site,logical)
            ph=w.physical_position;w.write_in_block(d,'materialMemory')
            if sym and marker is None:w.register_pointer_relocation(ph,c.reference_symbol(sym),kind='packed_alias',description=f'{site} Material graph reference')
        # GfxWorld loads the complete materialMemory array before resolving each MaterialPtr.
        for sym,site in owned:c.write_owned_child(w,self.symbol,sym,site)
        cur+=q.material_memory_count*8;w.pop_block()
        if cur!=b.end:raise ValueError('model/material cursor mismatch')
        return phys,owned
    def _write_shadow(self,w):
        q=self.report;z=self.pc_zone;b=branch(q,'shadow/light-region graph');cur=b.start;w.push_block(VIRTUAL_BLOCK);w.align_logical_only(4);phys=w.physical_position;roots=cur;cur+=q.primary_light_count*0xc;sh=[]
        # The source cursor must still walk every light so the branch ends exactly where
        # the parser said it does; only the kept lights are serialized into the zone.
        keep=self.effective_primary_light_count
        for i in range(q.primary_light_count):
            p=roots+i*0xc;s=u16(z,p);m=u16(z,p+2);sh.append((s,m))
            if i>=keep:continue
            d=bytearray(0xc);w16(d,0,s);w16(d,2,m);w32(d,4,FOLLOWING if s else 0);w32(d,8,FOLLOWING if m else 0);w.write_in_block(d,'shadow root')
        for i,(s,m) in enumerate(sh):
            if i<keep:
                for j in range(s):w.write_u16(u16(z,cur+j*2),'shadow surf')
            cur+=s*2
            if i<keep:
                for j in range(m):w.write_u16(u16(z,cur+j*2),'shadow model')
            cur+=m*2
        w.align_logical_only(4);rr=cur;cur+=q.primary_light_count*8;hcs=[]
        for i in range(q.primary_light_count):
            hc=i32(z,rr+i*8);hcs.append(hc)
            if i>=keep:continue
            d=bytearray(8);wi32(d,0,hc);w32(d,4,FOLLOWING if hc else 0);w.write_in_block(d,'light region')
        for i,hc in enumerate(hcs):
            emit=i<keep
            hr=cur;cur+=hc*0x50;acs=[]
            for j in range(hc):
                p=hr+j*0x50;ac=i32(z,p+0x48);acs.append(ac)
                if not emit:continue
                d=bytearray(0x50);copy_floats(z,d,0,p,0x48);wi32(d,0x48,ac);w32(d,0x4c,FOLLOWING if ac else 0);w.write_in_block(d,'light hull')
            for ac in acs:
                if emit:
                    for j in range(ac):p=cur+j*0x14;d=bytearray(0x14);copy_floats(z,d,0,p,0x14);w.write_in_block(d,'light axis')
                cur+=ac*0x14
        if cur!=b.end:raise ValueError('shadow cursor mismatch')
        w.pop_block();return phys
    def _write_dpvs(self,w,c):
        q=self.report;z=self.pc_zone;b=branch(q,'DPVS serialized graph');cur=b.start;w.push_block(VIRTUAL_BLOCK);w.align_logical_only(2)
        # Load_GfxWorldDpvsStatic reads dpvs.sortedSurfIndex with dpvs.staticSurfaceCount
        # entries (dpvs+0x04), not the PC sum of staticSurfaceCount and
        # staticSurfaceCountNoDecal.  The PC stream still carries both lists, so only the
        # source cursor advances by the sum.
        sc=q.static_surface_count+q.static_surface_count_no_decal
        for i in range(q.static_surface_count):w.write_u16(u16(z,cur+i*2),'sorted surface')
        cur+=sc*2;w.align_logical_only(4)
        for i in range(q.static_model_count):p=cur+i*PC_SMODEL_INST;d=bytearray(PC_SMODEL_INST);copy_floats(z,d,0,p,0x18);d[0x18:0x1c]=z[p+0x18:p+0x1c];w.write_in_block(d,'smodel inst')
        cur+=q.static_model_count*PC_SMODEL_INST;surface_phys=w.physical_position
        self._surface_vertex_layer_offsets=[]
        for i in range(q.surface_count):
            p=cur+i*PC_SURFACE;ph=w.physical_position;d=bytearray(PS3_SURFACE);fv=i32(z,p+4);first=self._minidx[i];span=self._span[i]
            # PC offsets address the source-side auxiliary stream. PS3 expands every base
            # world vertex into a 0x1C layer record, so Retail maps add firstVertex*0x1C.
            layer_off=0 if self.vertex_layer_policy=='omit' else ps3_vertex_layer_offset(i32(z,p),fv,len(self._layers))
            self._surface_vertex_layer_offsets.append(layer_off)
            w32(d,0,layer_off);w32(d,4,fv);w32(d,8,first);w16(d,0xc,span);w16(d,0xe,u16(z,p+0xa));w32(d,0x10,self._newbase[i]);group=bytearray(z[p+0x14:p+0x18]);group[2]=self._clamp_primary_light(group[2]);d[0x18:0x1c]=group;copy_floats(z,d,0x1c,p+0x18,24);w.write_in_block(d,'dpvs surface');self._relocate(w,c,ph+0x14,u32(z,p+0x10),'material','surface material')
        cur+=q.surface_count*PC_SURFACE
        for i in range(q.cull_group_count):p=cur+i*PC_CULL;d=bytearray(PC_CULL);copy_floats(z,d,0,p,0x18);wi32(d,0x18,i32(z,p+0x18));wi32(d,0x1c,i32(z,p+0x1c));w.write_in_block(d,'cull group')
        cur+=q.cull_group_count*PC_CULL;draw_phys=w.physical_position;draw_owned=[]
        for i in range(q.static_model_count):
            p=cur+i*PC_DRAW;ph=w.physical_position;d=bytearray(PS3_DRAW);wf(d,0,f32(z,p));copy_floats(z,d,4,p+4,12);w32(d,0x10,pack_rsx_unit_vector_from_floats(f32(z,p+0x10),f32(z,p+0x14),f32(z,p+0x18)));w32(d,0x14,pack_rsx_unit_vector_from_floats(f32(z,p+0x1c),f32(z,p+0x20),f32(z,p+0x24)));w32(d,0x18,pack_rsx_unit_vector_from_floats(f32(z,p+0x28),f32(z,p+0x2c),f32(z,p+0x30)));wf(d,0x1c,f32(z,p+0x34));d[0x24]=z[p+0x44];d[0x25]=self._clamp_primary_light(z[p+0x45])
            child=self.inline_draw_xmodel_symbols.get(i)
            if child is not None:
                site=f'draw[{i}].xmodel';marker=c.owned_marker(self.symbol,child,site)
                if marker is not None:
                    w32(d,0x20,marker);loc=w.current_location
                    c.bind_owned_pointer_field(w,self.symbol,child,site,PackedOffset(loc.block,loc.offset+0x20))
                    draw_owned.append((child,site))
                else:w.register_pointer_relocation(ph+0x20,c.reference_symbol(child),kind='packed_alias',description='shared smodel XModel')
                w.write_in_block(d,'smodel draw')
            else:
                w.write_in_block(d,'smodel draw');self._relocate(w,c,ph+0x20,u32(z,p+0x38),'xmodel','smodel XModel')
        cur+=q.static_model_count*PC_DRAW
        for child,site in draw_owned:c.write_owned_child(w,self.symbol,child,site)
        for index,name,source_start,source_end in q.full.inline_draw_xmodels:
            if cur!=source_start:raise ValueError('Inline draw XModel source cursor mismatch')
            cur=source_end
        if cur!=b.end:raise ValueError('DPVS cursor mismatch')
        w.pop_block();return surface_phys,draw_phys
    def _runtime(self,w,phase=None):
        q=self.report;z=self.pc_zone;total=0;contract_allocations=list(getattr(self,'_runtime_contract_allocations',())) if phase else []
        def reserve(fn,tag):
            nonlocal total
            if phase is not None and phase!=tag:return
            w.push_block(1);s=w.current_location.offset;fn();e=w.current_location.offset;w.pop_block();total+=e-s
        def contract(count,stride,label,alignment,array_count=1):
            if not count:return
            w.align_logical_only(alignment);loc=w.current_location;size=count*stride*array_count
            w.reserve_in_block(size,label)
            contract_allocations.append({'label':label,'offset':loc.offset,'alignment':alignment,'count':count,'stride':stride,'array_count':array_count,'bytes':size})
        # Block 1 is loader-allocated PS3 runtime memory.  These are target ABI
        # structures, not serialized PC pointer cells, so their PS3 sizes must be
        # used even though no physical FastFile bytes are emitted here.
        reserve(lambda:contract(q.reflection_probe_count,PS3_GFX_TEXTURE_RUNTIME,'reflection runtime',4),'reflection')
        reserve(lambda:(w.align_logical_only(4),w.reserve_in_block(q.cell_count*0x100*4,'sceneEnt bits') if q.cell_count else None),'cells')
        def lm():
            if q.lightmap_count:
                contract(q.lightmap_count,PS3_GFX_TEXTURE_RUNTIME,'lightmap primary runtime',4)
                contract(q.lightmap_count,PS3_GFX_TEXTURE_RUNTIME,'lightmap secondary runtime',4)
        reserve(lm,'lightmaps')
        def tail():
            cw=(q.cell_count+31)//32
            if q.cell_count:w.align_logical_only(4);w.reserve_in_block(q.cell_count*cw*4,'cell caster bits')
            contract(q.dynamic_model_count,PS3_SCENE_DYN_MODEL_RUNTIME,'scene dyn model',4)
            if q.dynamic_brush_count:w.align_logical_only(4);w.reserve_in_block(q.dynamic_brush_count*4,'scene dyn brush')
            ns=max(0,self.effective_primary_light_count-q.sun_primary_light_index-1)
            if ns:
                w.align_logical_only(4);w.reserve_in_block(ns*0x1000*4,'primary light shadow')
                if q.dynamic_model_count:w.align_logical_only(4);w.reserve_in_block(q.dynamic_model_count*ns*4,'dyn model shadow')
                if q.dynamic_brush_count:w.align_logical_only(4);w.reserve_in_block(q.dynamic_brush_count*ns*4,'dyn brush shadow')
            if q.dynamic_model_count:w.reserve_in_block(q.dynamic_model_count,'nonSun light')
        reserve(tail,'tail')
        def dpvs():
            pd=q.root_offset+0x244;smv=i32(z,pd+0x24);sv=i32(z,pd+0x28)
            for _ in range(3):
                if q.static_model_count:contract(smv,4,'smodel vis',128)
            for _ in range(3):
                if q.static_surface_count:contract(sv,4,'surface vis',128)
            contract(smv,PS3_DPVS_ALIGNED_RUNTIME,'lod',128,array_count=2)
            contract(q.static_surface_count,PS3_GFX_DRAW_SURF_RUNTIME,'surface materials runtime',4)
            contract(sv,PS3_DPVS_ALIGNED_RUNTIME,'sun shadow',128)
            d=q.root_offset+0x2ac;a=i32(z,d);bb=i32(z,d+4)
            if a and q.cell_count:w.align_logical_only(4);w.reserve_in_block(a*q.cell_count*4,'dyn cell0')
            if bb and q.cell_count:w.align_logical_only(4);w.reserve_in_block(bb*q.cell_count*4,'dyn cell1')
            for _ in range(3):
                if a:w.align_logical_only(16);w.reserve_in_block(32*a,'dyn vis0')
            for _ in range(3):
                if bb:w.align_logical_only(16);w.reserve_in_block(32*bb,'dyn vis1')
        reserve(dpvs,'dpvs');self._runtime_contract_allocations=tuple(contract_allocations);return total
    def write(self,w:RelocatingZoneWriter,c:GraphContext):
        if w.current_block_index!=TEMP_BLOCK:raise ValueError('GfxWorld root must be TEMP')
        self._image_emit_stats=_new_image_emit_stats(self.image_resource_policy)
        q=self.report;z=self.pc_zone;root=bytearray(self._root());root_refs=[];root_owned=[]
        for out_off,src_rel,kind,desc,site in ((0x18c,0x180,'material','sun sprite','sunSprite'),(0x190,0x184,'material','sun flare','sunFlare')):
            raw=u32(z,q.root_offset+src_rel)
            if not raw:continue
            pk=decode_pc_pointer(raw).kind
            if pk in ('following','insert'):
                sym=self.inline_material_symbols_by_root_field.get(src_rel) or self.runtime_default_material_symbol
                if sym is None:raise ValueError(f'GfxWorld {desc} inline Material has no typed destination binding')
            else:sym=self._resolve(raw,kind)
            marker=c.owned_marker(self.symbol,sym,site)
            if marker is not None:w32(root,out_off,marker);root_owned.append((sym,site,out_off))
            else:root_refs.append((out_off,sym,desc))
        rp=w.physical_position;rl=w.define_symbol(self.symbol);c.register_root(self.symbol,rp)
        for sym,site,out_off in root_owned:c.bind_owned_pointer_field(w,self.symbol,sym,site,PackedOffset(rl.block,rl.offset+out_off))
        w.write_in_block(bytes(root),'GfxWorld root');name=nested_symbol(self.symbol,'name')
        w.register_pointer_relocation(rp,self.shared_map_name_symbol or name,
                                      description='GfxWorld.name shared ComWorld BSP-path alias' if self.shared_map_name_symbol else 'GfxWorld.name')
        for out_off,sym,desc in root_refs:w.register_pointer_relocation(rp+out_off,c.reference_symbol(sym),kind='packed_alias',description=f'{desc} Material graph reference')
        # Retail loads the sole map-name XString into B4 immediately after the
        # TEMP GfxWorld root. ClipMap and MapEnts reuse this exact allocation.
        w.push_block(LARGE_BLOCK);w.define_symbol(name);w.write_latin1z(q.base_name,'GfxWorld baseName');w.pop_block()
        w.push_block(VIRTUAL_BLOCK);w.align_logical_only(2);index_phys=w.physical_position;w.write_in_block(self._reordered,'GfxWorld indices');w.align_logical_only(4);sb=branch(q,'skyStartSurfs')
        for i in range(q.sky_surface_count):w.write_u32(u32(z,sb.start+i*4),'sky surface')
        w.pop_block()
        if 'skyImage' in self._images:w.define_insert_pointer_alias(nested_symbol(self.symbol,'insert.skyImage'));self._write_image(w,'skyImage')
        self._runtime_contract_allocations=()
        self._write_sunlight(w,c);self._write_reflections(w);runtime=self._runtime(w,'reflection')
        planes=self._write_planes_nodes(w);runtime+=self._runtime(w,'cells');cells=self._write_cells(w)
        lighting=self._write_lighting(w);runtime+=self._runtime(w,'lightmaps');models,matmem_owned=self._write_models_materialmemory(w,c)
        # Both PS3 world-vertex streams are persistent block-4 allocations.  B6
        # is reserved for XModel secondary vertices and must not grow here.
        w.push_block(LARGE_BLOCK);w.align_logical_only(16);vphys=w.physical_position;w.write_in_block(self._primary,'GfxWorld primary vertices');w.pop_block()
        # Load_GfxWorldVertexData leaves vd.vertices in the enclosing virtual block, but
        # Load_GfxWorldVertexLayerData does 'li r3, 6; bl DB_PushStreamPos' before reading
        # vertexLayerDataSize bytes, and allocates them with DB_AllocStreamPos(0) - the
        # physical block, unaligned.  Keeping the layer stream in the virtual block overruns
        # it by the whole stream and shifts every later virtual-block offset.
        lphys=w.physical_position
        if self._layers:
            w.push_block(VERTEX_BLOCK);lphys=w.physical_position
            w.write_in_block(self._layers,'GfxWorld vertex layers');w.pop_block()
        # Inline root-owned sun Materials are loaded after the two vertex streams.
        [c.write_owned_child(w,self.symbol,sym,site) for sym,site,_out_off in root_owned]
        if 'outdoorImage' in self._images:w.define_insert_pointer_alias(nested_symbol(self.symbol,'insert.outdoorImage'));self._write_image(w,'outdoorImage')
        runtime+=self._runtime(w,'tail');shadow=self._write_shadow(w);runtime+=self._runtime(w,'dpvs');surf,draw=self._write_dpvs(w,c)
        c.register_result('gfxworld:'+self.symbol,{'root_physical':rp,'root_location':rl,'index_physical':index_phys,'plane_physical':planes,'cell_physical':cells,'lighting_physical':lighting,'model_physical':models,'vertex_physical':vphys,'vertex_layer_physical':lphys,'shadow_physical':shadow,'surface_physical':surf,'draw_physical':draw,'runtime_bytes':runtime,'runtime_contract_allocations':self._runtime_contract_allocations,'index_count':q.index_count,'vertex_count':q.vertex_count,'surface_count':q.surface_count,'static_model_count':q.static_model_count,'aabb_packed_reuse':self._aabb.packed_pointer_count,'index_sha256':hashlib.sha256(self._reordered).hexdigest().upper(),'vertex_sha256':hashlib.sha256(self._primary).hexdigest().upper(),'layer_sha256':hashlib.sha256(self._layers).hexdigest().upper(),'surface_vertex_layer_offsets':tuple(self._surface_vertex_layer_offsets),'surface_vertex_layer_offset_unique':len(set(self._surface_vertex_layer_offsets)),'surface_vertex_layer_offset_max':max(self._surface_vertex_layer_offsets,default=0),'surface_vertex_layer_formula':'sourceOffset + firstVertex * 0x1C','image_count':len(self._images),'owned_material_sites':[site for _,site in matmem_owned]+[site for _,site,_ in root_owned],'image_resource_policy':self.image_resource_policy.value,'image_resource_stats':dict(self._image_emit_stats)})
