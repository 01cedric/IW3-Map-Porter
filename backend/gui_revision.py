"""Identify supplied installations without equating a product ID with a binary ABI."""
from pathlib import Path
import hashlib
import json
import re
import struct

CATALOG = Path(__file__).parent / 'reference/ps3-regions.json'

def normalize_title_id(value):
    text = str(value).strip().upper().replace('-', '')
    if not re.fullmatch(r'[A-Z]{4}\d{5}', text):
        raise ValueError('Expected a nine-character PS3 software title ID.')
    return text

def read_sfo(path):
    data = Path(path).read_bytes()
    if len(data) < 20 or data[:4] != b'\0PSF':
        raise ValueError('Invalid PARAM.SFO header.')
    version, keys, values, count = struct.unpack_from('<4I', data, 4)
    if count > 4096 or not 20 + count * 16 <= keys <= values <= len(data):
        raise ValueError('Invalid PARAM.SFO table bounds.')
    result = {}
    for i in range(count):
        key, fmt, size, capacity, offset = struct.unpack_from('<HHIII', data, 20 + i * 16)
        begin = keys + key
        if not keys <= begin < values or size > capacity or values + offset + capacity > len(data):
            raise ValueError('Invalid PARAM.SFO entry bounds.')
        end = data.find(b'\0', begin, values)
        if end < 0: raise ValueError('Unterminated PARAM.SFO key.')
        name = data[begin:end].decode('ascii')
        if name in result: raise ValueError('Duplicate PARAM.SFO key.')
        raw = data[values + offset:values + offset + size]
        if fmt == 0x204:
            result[name] = raw.rstrip(b'\0').decode('utf-8')
        elif fmt == 0x404 and size == 4:
            result[name] = struct.unpack('<I', raw)[0]
    return result

def installation_metadata(path):
    """Use only an ancestor SFO: a sibling update is not proof of the running binary."""
    path = Path(path).resolve()
    result = dict(title_id=None, app_version=None, source_kind='unknown', sfo=None)
    for parent in (path.parent, *path.parent.parents):
        candidate = parent / 'PARAM.SFO'
        if candidate.is_file():
            sfo = read_sfo(candidate)
            title = sfo.get('TITLE_ID')
            if title: result['title_id'] = normalize_title_id(title)
            result.update(app_version=sfo.get('APP_VER'), sfo=str(candidate),
                          source_kind={'DG':'disc', 'GD':'installed_game_data'}.get(sfo.get('CATEGORY'), 'unknown'))
            break
    catalog = json.loads(CATALOG.read_text())
    result['catalogued_title'] = result['title_id'] in catalog['software_title_ids']
    result['update_1_40_metadata'] = result['app_version'] in ('01.40', '1.40')
    result['running_executable_verified'] = False
    return result

def ui_identity(zone):
    digest = hashlib.sha256(zone).hexdigest()
    catalog = json.loads(CATALOG.read_text())
    entry = catalog['ui_files'].get(digest, {})
    return dict(zone_sha256=digest, label=entry.get('label','Uncatalogued UI revision'),
                title_id=entry.get('title_id'), update=entry.get('update','unknown'),
                editor_profile=entry.get('editor_profile'),
                status='profile_available' if entry.get('editor_profile') else 'profile_required')

def elf_segments(data):
    if len(data) < 64 or data[:6] != b'\x7fELF\x02\x02' or struct.unpack_from('>H',data,18)[0] != 21:
        raise ValueError('Supply a decrypted PPC64 big-endian ELF; encrypted EBOOT.BIN is not supported.')
    phoff = struct.unpack_from('>Q',data,32)[0]
    size,count = struct.unpack_from('>HH',data,54)
    if size < 56 or count > 64 or phoff + size*count > len(data): raise ValueError('Invalid ELF program headers.')
    segments=[]
    for i in range(count):
        typ,flags,off,va,pa,n,mem,align=struct.unpack_from('>IIQQQQQQ',data,phoff+size*i)
        if typ != 1: continue
        if off+n > len(data) or n > mem: raise ValueError('ELF segment exceeds file or memory bounds.')
        if n: segments.append((off,va,n,flags))
    return segments

def inspect_elf(path):
    """Resolve the BSP error call from this ELF's string/TOC, never region offsets.

    Diagnostic addresses only. Does not select or rebase the native loader emulator.
    """
    data=Path(path).read_bytes();segments=elf_segments(data)
    def offset(va,n=4):
        for off,base,size,flags in segments:
            if base <= va and va+n <= base+size:return off+va-base
        return None
    entry=struct.unpack_from('>Q',data,24)[0];at=offset(entry,8)
    if at is None:raise ValueError('ELF entry descriptor is outside file-backed memory.')
    code,toc=struct.unpack_from('>II',data,at)
    if offset(code) is None or offset(toc) is None:raise ValueError('Unsupported ELF entry descriptor.')
    needle=b"Couldn't find the bsp for this map."
    strings=[]
    for off,base,n,flags in segments:
        pos=off
        while True:
            pos=data.find(needle,pos,off+n)
            if pos<0:break
            strings.append(base+pos-off);pos+=len(needle)
    slots={}
    for off,base,n,flags in segments:
        for p in range(off,off+n-3,4):
            if struct.unpack_from('>I',data,p)[0] in strings:
                delta=base+p-off-toc
                if -32768<=delta<=32767:slots[delta & 0xffff]=base+p-off
    calls=[]
    for off,base,n,flags in segments:
        if not flags & 1:continue
        for p in range(off,off+n-7,4):
            word,branch=struct.unpack_from('>II',data,p)
            # lwz r4, displacement(r2), followed by a relative bl.
            if word & 0xffff0000 != 0x80820000 or word & 0xffff not in slots:continue
            if branch & 0xfc000003 != 0x48000001:continue
            delta=branch & 0x03fffffc
            if delta & 0x02000000:delta-=0x04000000
            va=base+p-off+4;target=va+delta
            if offset(target) is None:continue
            calls.append(dict(address=f'0x{va:08X}',original_instruction=f'{branch:08X}',
                              error_function=f'0x{target:08X}',format_string_register='r4',
                              first_format_argument_register='r5'))
    return dict(path=str(Path(path).resolve()),sha256=hashlib.sha256(data).hexdigest(),
                metadata=installation_metadata(path),toc=f'0x{toc:08X}',
                bsp_error_breakpoints=calls if len(calls)==1 else [],
                diagnostic_status='unique_string_toc_call' if len(calls)==1 else 'unresolved_or_ambiguous',
                candidate_count=len(calls),emulator_profile='not_selected',
                note='Static diagnostic identification; compare live instruction before trapping. No EBOOT modifications.')
