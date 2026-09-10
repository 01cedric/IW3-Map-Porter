"""Bounded IW3 material preview using verified generated BC texture resources.

The remap equation follows IW4Studio's RsxTextureSwizzleDecoder (MIT,
Jacob Schroeder, commit a40c917e6f46553f419b01d7c7ed5a3a5a875b07).
See src/IW3MapPorter.Core/Vendor/IW4Studio/LICENSE.IW4Studio.txt.
No IW4 Material or shader-constant layout is applied to IW3 data.
"""
from __future__ import annotations
import hashlib
import io
import json
from pathlib import Path
import struct
import numpy as np
from PIL import Image


def remap_rgba(rgba, payload):
    """RSX A-R-G-B control/source table, output in R-G-B-A; BC formats only."""
    source = [rgba[..., 3], rgba[..., 0], rgba[..., 1], rgba[..., 2]]
    output = []
    for slot in (1, 2, 3, 0):
        control = (payload >> (8 + slot * 2)) & 3
        output.append(np.zeros_like(source[0]) if control == 0 else np.full_like(source[0], 255) if control == 1 else source[(payload >> (slot * 2)) & 3])
    return np.stack(output, axis=-1)


def decode_bc(data, width, height, fmt, remap=0xAAE4):
    if fmt not in (0x86, 0x87, 0x88): raise ValueError('This preview supports BC1/BC2/BC3 only.')
    if not 1 <= width <= 16384 or not 1 <= height <= 16384: raise ValueError('Invalid texture size.')
    fourcc = {0x86: b'DXT1', 0x87: b'DXT3', 0x88: b'DXT5'}[fmt]
    size = max(1, (width + 3) // 4) * max(1, (height + 3) // 4) * (8 if fmt == 0x86 else 16)
    if len(data) != size: raise ValueError('BC mip has an invalid length.')
    header = struct.pack('<7I', 124, 0x81007, height, width, size, 0, 1) + bytes(44)
    header += struct.pack('<II4s5I', 32, 4, fourcc, 0, 0, 0, 0, 0) + struct.pack('<5I', 0x1000, 0, 0, 0, 0)
    with Image.open(io.BytesIO(b'DDS ' + header + data)) as image:
        rgba = np.asarray(image.convert('RGBA')).copy()
    return Image.fromarray(remap_rgba(rgba, remap))


def decode_texture(data, width, height, fmt, remap=0xAAE4):
    if fmt not in (0xA5,0x85):
        return decode_bc(data,width,height,fmt,remap)
    if not 1 <= width <= 16384 or not 1 <= height <= 16384 or len(data)!=width*height*4:
        raise ValueError('Linear ARGB8 mip has invalid dimensions or byte count.')
    if fmt==0x85:
        from cod4porter.assets.gfxworld import _unswizzle_2d_pot
        data=_unswizzle_2d_pot(data,width,height,4)
    argb=np.frombuffer(data,dtype=np.uint8).reshape(height,width,4)
    return Image.fromarray(remap_rgba(argb[...,[1,2,3,0]],remap))


def export_materials(machine, materials, settings, zone, out):
    from worldview import Mem
    from gui_texture_sources import IwdTextures
    R = Mem(machine.m); rows = []; textures = {}; resources = {}; decoded = {}; bindings = {}
    ff = Path(settings['ps3_ff'])
    report_path = ff.with_name(ff.stem + '.python-port-report.json')
    if settings.get('report_path') and Path(settings['report_path']).is_file(): report_path = Path(settings['report_path'])
    status = 'No matching port report; selected PC IWD images are used when available.'
    if report_path.is_file():
        report = json.loads(report_path.read_text(encoding='utf-8-sig'))
        actual = hashlib.sha256(ff.read_bytes()).hexdigest().upper()
        if report.get('main', {}).get('fastfile_sha256', '').upper() == actual:
            for row in report.get('material_image_resources', []): resources[row['name'].lstrip(',').casefold()] = row
            bindings={r['name'].casefold():r['textures'] for r in report.get('material_texture_bindings',[])}
            status = 'Port report and fastfile hash match.'
        else: status = 'Port report does not match this fastfile; its resource offsets were ignored.'
    from cod4porter.texture_library import library_archives
    source = IwdTextures(settings.get('iwd_paths', []))
    library = IwdTextures(library_archives(settings['texture_library_directory']) if settings.get('texture_library_directory') else [])
    inventory=machine.inventory()
    default_assets={(typ,name.casefold()) for typ,name,*_ in getattr(machine,'default_clones',[])}
    default_assets-={(typ,name.casefold()) for typ,name in getattr(machine,'default_replaced',[])}
    def source_pixels(name):
        if source.entries is None:source._index()
        from cod4porter.backend.v4_iwd import normalize_iwi_name
        key=normalize_iwi_name(name.lstrip(',')).casefold()
        # Explicit map archives override a stock library, matching conversion.
        return source.read(name) if key in source.entries else library.read(name)

    try:
        for hdr, name in materials.items():
            row = {'native_default_material':(4,name.casefold()) in default_assets, 'name': name, 'header': int(hdr), 'textures': [], 'shader_execution': False}
            try:
                ts = R.u32(hdr + 0x70); table = R.u32(hdr + 0x74); count = R.u8(hdr + 0x32)
                if count > 64: raise ValueError('Invalid material texture count.')
                row['technique_set'] = R.cstr(R.u32(ts)) if ts >= 0x10000 else None
                for index in range(count):
                    item = table + index * 12; semantic = R.u8(item + 7); image_hdr = R.u32(item + 8)
                    texture = {'semantic': semantic, 'sampler': R.u8(item + 6), 'name_hash': R.u32(item)}
                    row['textures'].append(texture)
                    if semantic == 11:
                        texture['status'] = 'Water definition; not a standard image pointer.'; continue
                    if image_hdr < 0x10000: texture['status'] = 'No valid image pointer.'; continue
                    image_name = R.cstr(R.u32(image_hdr + 0x30)); texture['runtime_image'] = image_name
                    exact=[b for b in bindings.get(name.lstrip(',').casefold(),[]) if b['index']==index and b['semantic']==semantic and b['name_hash']==texture['name_hash']]
                    if len(exact)==1:image_name=exact[0]['image']
                    texture['image']=image_name
                    texture['native_default_image']=(6,image_name.lstrip(',').casefold()) in default_assets

                    if semantic != 2 or name in textures: continue
                    info = resources.get(image_name.lstrip(',').casefold())
                    key=(image_name.casefold(),R.u32(image_hdr+8))
                    try:
                        if key not in decoded:
                            try:
                                if info is None: raise ValueError('No verified PS3 image resource in the selected port report.')
                                mip = next(m for m in info['mips'] if m['face'] == 0 and m['level'] == 0)
                                if info.get('face_count', 1) != 1: raise ValueError('A cubemap is not a diffuse 2D texture.')
                                start = int(info['resource_physical']) + int(mip['offset']); size = int(mip['bytes'])
                                if start < 0 or start + size > len(zone): raise ValueError('Mip is outside the zone.')
                                raw = zone[start:start + size]
                                if hashlib.sha256(raw).hexdigest().upper() != mip['sha256'].upper(): raise ValueError('Mip hash does not match.')
                                png = decode_texture(raw, mip['width'], mip['height'], info['ps3_format'], key[1])
                                origin={'kind':'ps3-fastfile','sha256':mip['sha256']}
                            except (ValueError,KeyError,StopIteration,TypeError) as exc:
                                texture['ps3_resource_status']=str(exc)
                                png,origin=source_pixels(image_name)
                            filename='texture_'+hashlib.sha256(repr(key).encode()).hexdigest()[:20]+'.png'
                            png.save(Path(out)/filename);decoded[key]=(filename,origin)
                        filename,origin=decoded[key];textures[name]=filename
                        texture['preview']=filename;texture['source']=origin
                    except (ValueError,KeyError,StopIteration,TypeError,OSError) as exc:texture['status']=str(exc)
                row['preview_status']='textured' if name in textures else 'material color; diffuse resource unavailable'
            except Exception as exc: row['error'] = str(exc)
            rows.append(row)
    finally: source.close(); library.close()
    fx = [{'name': name, 'header': hdr, 'playback': False} for (typ, name), hdr in inventory.items() if typ == 27]
    doc = {'source_status': status, 'diffuse_textures_exported': len(textures), 'unique_textures_exported':len(decoded),
           'materials': rows, 'fx_assets': fx, 'iwd_errors':source.errors+library.errors,
           'native_default_images':sorted(name for typ,name in default_assets if typ==6),
           'texture_library_directory':settings.get('texture_library_directory',''),
           'missing_diffuse_materials':[row['name'] for row in rows if row['name'] not in textures],
           'shader_execution': False, 'fx_playback': False,
           'limitations': ['Diffuse preview only; no IW3 pass/shader execution.',
                           'PC IWD fallback previews source pixels and does not prove PS3 texture fidelity.',
                           'FX inventory is not a particle simulation.']}
    (Path(out) / 'material-rsx-report.json').write_text(json.dumps(doc, indent=2), encoding='utf-8')
    return textures
