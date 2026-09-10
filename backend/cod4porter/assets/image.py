from __future__ import annotations
from dataclasses import dataclass,field
from enum import Enum
import hashlib,struct
from ..graph import AssetType,GraphContext
from ..zone_writer import RelocatingZoneWriter,TEMP_BLOCK,VIRTUAL_BLOCK,VERTEX_BLOCK,NULL,FOLLOWING,INSERT

PS3_DXT1=0x86;PS3_DXT3=0x87;PS3_DXT5=0x88;PS3_ARGB8=0x85;PS3_ARGB8_LINEAR=0xA5;RESOURCE_ALIGN=0x80

@dataclass(frozen=True)
class ImageMip:
    face:int;level:int;width:int;height:int;offset:int;size:int;sha256:str
@dataclass(frozen=True)
class ImageAsset:
    name:str;map_type:int;source_format:str;ps3_format:int;semantic:int;width:int;height:int;depth:int;level_count:int;face_count:int;resource:bytes;unpadded:int;padding:int;mips:tuple[ImageMip,...]
    @property
    def is_cubemap(self):return self.face_count==6

def from_iwi(iwi,semantic:int=2)->ImageAsset:
    fmt=str(iwi.format_name).lower()
    if fmt in ('rgb8','rgba8'):
        return _from_bitmap_iwi(iwi,semantic)
    fm={'dxt1':PS3_DXT1,'dxt3':PS3_DXT3,'dxt5':PS3_DXT5}
    if fmt not in fm:raise ValueError(f'unsupported IWI format {fmt}')
    block=8 if fmt=='dxt1' else 16;buf=bytearray();mips=[]
    # v4 IwiImage mips already contain face/level/data.
    for m in sorted(iwi.mips,key=lambda x:(x.face,x.level)):
        data=bytes(m.data);exp=max(1,(m.width+3)//4)*max(1,(m.height+3)//4)*block
        if len(data)!=exp:raise ValueError(f'IWI {iwi.name} face {m.face} mip {m.level}: {len(data)} != {exp}')
        off=len(buf);buf+=data;mips.append(ImageMip(m.face,m.level,m.width,m.height,off,len(data),hashlib.sha256(data).hexdigest().upper()))
    unp=len(buf);pad=(-unp)%RESOURCE_ALIGN;buf+=b'\0'*pad
    return ImageAsset(iwi.name,5 if iwi.is_cubemap else 3,fmt,fm[fmt],semantic,iwi.width,iwi.height,iwi.depth,iwi.level_count,iwi.face_count,bytes(buf),unp,pad,tuple(mips))


def _from_bitmap_iwi(iwi,semantic:int)->ImageAsset:
    """Preserve 2D bitmap pixels: linear single mip, Morton-swizzled POT chain."""
    if iwi.face_count!=1 or iwi.depth!=1 or len(iwi.mips)!=iwi.level_count:
        raise ValueError(f'IWI {iwi.name}: bitmap conversion requires a 2D image')
    swizzled=iwi.level_count>1
    if swizzled and (iwi.width&(iwi.width-1) or iwi.height&(iwi.height-1)):
        raise ValueError(f'IWI {iwi.name}: bitmap mip chains require power-of-two dimensions')
    if swizzled and iwi.level_count!=max(iwi.width,iwi.height).bit_length():
        raise ValueError(f'IWI {iwi.name}: incomplete bitmap mip chain')
    bpp=3 if iwi.format_name=='rgb8' else 4
    buf=bytearray();mips=[]
    for level,m in enumerate(sorted(iwi.mips,key=lambda x:x.level)):
        if m.face!=0 or m.level!=level or (m.width,m.height)!=(max(1,iwi.width>>level),max(1,iwi.height>>level)):
            raise ValueError(f'IWI {iwi.name}: inconsistent bitmap mip metadata')
        if len(m.data)!=m.width*m.height*bpp:
            raise ValueError(f'IWI {iwi.name}: invalid bitmap byte count')
        raw=bytearray(m.width*m.height*4)
        raw[0::4]=m.data[3::4] if bpp==4 else b'\xff'*(m.width*m.height)
        raw[1::4]=m.data[2::bpp];raw[2::4]=m.data[1::bpp];raw[3::4]=m.data[0::bpp]
        if swizzled:
            from .gfxworld import _swizzle_2d_pot
            raw=_swizzle_2d_pot(bytes(raw),m.width,m.height,4)
        mips.append(ImageMip(0,level,m.width,m.height,len(buf),len(raw),hashlib.sha256(raw).hexdigest().upper()))
        buf.extend(raw)
    unp=len(buf);pad=(-unp)%RESOURCE_ALIGN
    fmt=PS3_ARGB8 if swizzled else PS3_ARGB8_LINEAR
    return ImageAsset(iwi.name,3,iwi.format_name,fmt,semantic,iwi.width,iwi.height,1,iwi.level_count,1,bytes(buf)+b'\0'*pad,unp,pad,tuple(mips))



def neutral_bc1_image(name:str,semantic:int)->ImageAsset:
    """Create a tiny owned PS3 BC1 compatibility resource without inventing an external asset name."""
    endpoint=0x841F if semantic==0x05 else 0x0000 if semantic==0x07 else 0xFFFF
    lo=endpoint&0xff; hi=(endpoint>>8)&0xff
    raw=bytes((lo,hi,lo,hi,0,0,0,0))
    pad=(-len(raw))%RESOURCE_ALIGN;resource=raw+b'\0'*pad
    mip=ImageMip(0,0,4,4,0,len(raw),hashlib.sha256(raw).hexdigest().upper())
    return ImageAsset(name,3,'dxt1',PS3_DXT1,int(semantic)&0xff,4,4,1,1,1,resource,len(raw),pad,(mip,))

class ImageResourcePolicy(str,Enum):
    SELF_CONTAINED='self-contained'
    RETAIL_DELAYED='retail-delayed'
    FASTFILE_DELAYED='fastfile-delayed'
    # Deterministic build-level policy.  MaterialGraph resolves this into a
    # mixture of SELF_CONTAINED (block 6) and FASTFILE_DELAYED (block 3)
    # ImageNodes so neither physical allocation becomes one oversized extent.
    BALANCED_SPLIT='balanced-split'


def build_image_root(a:ImageAsset,*,delay_load_pixels:int=0)->bytes:
    if delay_load_pixels not in (0,1):raise ValueError('Image delayLoadPixels must be 0 or 1')
    root=bytearray(0x34);struct.pack_into('>i',root,0,a.map_type);d=memoryview(root)[4:0x2C]
    d[0]=a.ps3_format;d[1]=a.level_count;d[2]=2;d[3]=1 if a.is_cubemap else 0;struct.pack_into('>I',d,4,0x0001AAE4)
    pitch=a.width*4 if a.ps3_format==PS3_ARGB8_LINEAR else 0
    struct.pack_into('>HHH',d,8,a.width,a.height,a.depth);d[0x0E]=0;d[0x0F]=0;struct.pack_into('>II',d,0x10,pitch,0);d[0x18]=a.semantic
    struct.pack_into('>I',d,0x1C,len(a.resource));struct.pack_into('>HHH',d,0x20,a.width,a.height,a.depth);d[0x26]=3;d[0x27]=delay_load_pixels
    # Retail delayed Image shells retain the FOLLOWING data marker and declared byte count even
    # though the resource payload itself lives outside the main zone.  This mirrors the audited
    # GfxWorld image-shell policy and keeps the loader-facing structure intact.
    struct.pack_into('>II',root,0x2C,FOLLOWING if a.resource else NULL,FOLLOWING)
    return bytes(root)


@dataclass
class ImageNode:
    image:ImageAsset;symbol:str;resource_policy:ImageResourcePolicy|str=ImageResourcePolicy.SELF_CONTAINED
    type:AssetType=field(init=False,default=AssetType.IMAGE);root_block:int=field(init=False,default=TEMP_BLOCK);dependencies:tuple=field(init=False,default=())
    def __post_init__(self):
        if isinstance(self.resource_policy,str):self.resource_policy=ImageResourcePolicy(self.resource_policy)
    def write(self,w:RelocatingZoneWriter,c:GraphContext):
        has_resource=bool(self.image.resource)
        embedded=bool(has_resource and self.resource_policy is ImageResourcePolicy.SELF_CONTAINED)
        deferred=bool(has_resource and self.resource_policy is ImageResourcePolicy.FASTFILE_DELAYED)
        delay=1 if has_resource and self.resource_policy in (ImageResourcePolicy.RETAIL_DELAYED,ImageResourcePolicy.FASTFILE_DELAYED) else 0
        root_physical=w.physical_position;root_location=w.define_symbol(self.symbol);c.register_root(self.symbol,root_physical)
        w.write_in_block(build_image_root(self.image,delay_load_pixels=delay),f'image {self.image.name} root')
        loaddef_location=None
        if has_resource:
            # texture.loadDef points to a 0x10-byte loader object in the active
            # TEMP frame. Its fields are already represented in the serialized
            # image shell, so only the logical allocator high-water advances.
            w.push_block(TEMP_BLOCK);w.align_logical_only(4);loaddef_location=w.current_location
            w.reserve_logical_only(0x10,'GfxImageLoadDef TEMP object');w.pop_block()
        w.push_block(VIRTUAL_BLOCK);name_physical=w.physical_position;name_location=w.current_location;w.write_latin1z(self.image.name,'image name');w.pop_block();resource_physical=-1
        if embedded:
            w.push_block(VERTEX_BLOCK);w.align_logical_only(RESOURCE_ALIGN);resource_physical=w.physical_position
            w.write_in_block(self.image.resource,'image resource');w.pop_block()
        row={
            'name':self.image.name,'root_physical':root_physical,'name_physical':name_physical,
            'name_location':name_location,
            'loaddef_location':loaddef_location,
            'width':self.image.width,'height':self.image.height,'depth':self.image.depth,
            'level_count':self.image.level_count,'face_count':self.image.face_count,
            'source_format':self.image.source_format,'ps3_format':self.image.ps3_format,
            'unpadded_resource_bytes':self.image.unpadded,'padding_bytes':self.image.padding,
            'mips':[
                {
                    'face':m.face,'level':m.level,'width':m.width,'height':m.height,
                    'offset':m.offset,'bytes':m.size,'sha256':m.sha256,
                }
                for m in self.image.mips
            ],
            'resource_physical':resource_physical,'resource_policy':self.resource_policy.value,
            'resource_bytes':len(self.image.resource),'declared_resource_bytes':len(self.image.resource),
            'embedded_resource_bytes':len(self.image.resource) if embedded else 0,
            'deferred_resource_bytes':len(self.image.resource) if deferred else 0,
            'omitted_resource_bytes':len(self.image.resource) if has_resource and not embedded and not deferred else 0,
            'delay_load_pixels':delay,'embedded':embedded,'deferred':deferred,
            'sha256':hashlib.sha256(self.image.resource).hexdigest().upper(),'root_location':root_location,
        }
        if deferred:
            w.defer_payload(self.image.resource,alignment=RESOURCE_ALIGN,description=f'image {self.image.name} delayed resource',result_row=row)
        declared=len(self.image.resource);embedded_bytes=declared if embedded else 0
        c.register_result('image:'+self.symbol,row)

@dataclass
class ExternalImageNode:
    name:str;symbol:str
    type:AssetType=field(init=False,default=AssetType.IMAGE);root_block:int=field(init=False,default=TEMP_BLOCK);dependencies:tuple=field(init=False,default=())
    def write(self,w,c):
        rp=w.physical_position;rl=w.define_symbol(self.symbol);c.register_root(self.symbol,rp)
        # A name-only GfxImage is a shared/native asset lookup, not a partially
        # initialized 2D image.  Retail PS3 writes a completely zeroed 0x30-byte
        # body and only a FOLLOWING XString marker at +0x30.  Setting mapType=3
        # while format/dimensions/loadDef stay zero reaches OnImageLoaded with an
        # internally inconsistent texture shell and can fail in the first XModel.
        root=bytearray(0x34);struct.pack_into('>I',root,0x30,FOLLOWING);w.write_in_block(bytes(root),'external image root')
        w.push_block(w.options.insert_block_index);np=w.physical_position;nl=w.current_location;w.write_latin1z(','+self.name.lstrip(','),'external image name');w.pop_block();c.register_result('image.external:'+self.symbol,{'root_physical':rp,'name_physical':np,'name_location':nl,'root_location':rl})
