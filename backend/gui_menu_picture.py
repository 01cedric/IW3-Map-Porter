"""Extract a ported PS3 loading picture and prepare bounded native UI pixels."""
import hashlib
import json
from pathlib import Path
import re
import struct
from PIL import Image
from cod4porter.load_zone import read_load_document
from cod4porter.assets.image import ImageAsset, ImageMip, build_image_root, PS3_ARGB8_LINEAR
from gui_materials import decode_texture

MAX_EDGE = 512


def prepare_picture(path, map_id=''):
    path=Path(path)
    doc=read_load_document(path)
    if not doc.rawfile.name.endswith('_load'):
        raise ValueError('The selected PS3 zone has no map loading identity.')
    actual=doc.rawfile.name[:-5]
    if not re.fullmatch(r'mp_[a-z0-9_]+',actual):raise ValueError('Invalid map identity in PS3 loading zone.')
    if map_id and actual!=map_id:
        raise ValueError(f'{map_id}: the selected loading zone belongs to {doc.rawfile.name}.')
    matches=[m for m in doc.materials if m.name=='$levelbriefing']
    if len(matches)!=1 or len(matches[0].textures)!=1:
        raise ValueError(f'{actual}: expected one $levelbriefing image.')
    img=matches[0].textures[0].image;desc=img.descriptor
    width,height,depth=struct.unpack_from('>HHH',desc,8)
    size=struct.unpack_from('>I',desc,0x1c)[0];fmt=desc[0]
    if img.map_type!=3 or depth!=1 or desc[2]!=2 or desc[3]!=0 or not desc[1]:
        raise ValueError('Loading picture must be a 2D texture with at least one mip.')
    if not 0<width<=4096 or not 0<height<=4096 or size!=len(img.resource):
        raise ValueError('Invalid loading picture dimensions or resource byte count.')
    if fmt in (0x86,0x87,0x88):
        count=max(1,(width+3)//4)*max(1,(height+3)//4)*(8 if fmt==0x86 else 16)
        base=img.resource[:count]
    elif fmt in (0x85,0xa5):
        if fmt==0xa5:
            pitch=struct.unpack_from('>I',desc,0x10)[0]
            if pitch<width*4 or len(img.resource)<pitch*height:raise ValueError('Invalid linear loading picture pitch.')
            base=b''.join(img.resource[y*pitch:y*pitch+width*4] for y in range(height))
        else:base=img.resource[:width*height*4]
    else:raise ValueError(f'Unsupported PS3 loading picture format 0x{fmt:02X}.')
    remap=struct.unpack_from('>I',desc,4)[0]
    rgba=decode_texture(base,width,height,fmt,remap)
    rgba.thumbnail((MAX_EDGE,MAX_EDGE),Image.Resampling.LANCZOS)
    output_width,output_height=rgba.size;pixels=rgba.tobytes()
    argb=bytearray(len(pixels))
    argb[0::4]=pixels[3::4];argb[1::4]=pixels[0::4];argb[2::4]=pixels[1::4];argb[3::4]=pixels[2::4]
    unpadded=len(argb);padding=(-unpadded)%128;resource=bytes(argb)+bytes(padding)
    mip=ImageMip(0,0,output_width,output_height,0,unpadded,hashlib.sha256(argb).hexdigest().upper())
    asset=ImageAsset('loadscreen_'+actual,3,'rgba8',PS3_ARGB8_LINEAR,2,output_width,output_height,1,1,1,
                     resource,unpadded,padding,(mip,))
    root=build_image_root(asset)
    report=dict(map_id=actual,material='loadscreen_'+actual,source=str(path.resolve()),
                source_sha256=hashlib.sha256(path.read_bytes()).hexdigest(),source_image=img.name,
                source_dimensions=[width,height],dimensions=list(rgba.size),resized=rgba.size!=(width,height),
                source_format=f'0x{fmt:02X}',output_format='linear ARGB8',pixel_bytes=len(resource),
                pixel_sha256=hashlib.sha256(resource).hexdigest(),max_edge=MAX_EDGE)
    return root,resource,rgba,report


def execute(settings,out,emit,artifact):
    source=Path(settings.get('ps3_load_ff') or '')
    if not source.is_file():raise ValueError('Select the ported PS3 _load.ff first.')
    emit('stage','Extracting and fitting the PS3 loading picture for the map menu')
    _,_,rgba,report=prepare_picture(source,settings.get('map_name','').strip())
    picture=out/'map-menu-picture.png';rgba.save(picture)
    report['preview_png']=str(picture.resolve())
    path=out/'map-menu-picture.json';path.write_text(json.dumps(report,indent=2)+'\n')
    artifact(picture,'map-menu-picture');artifact(path,'map-menu-picture-report')
    return 'passed',f"{report['map_id']}: loading picture prepared at {rgba.width} x {rgba.height}. Export the UI to embed it."
