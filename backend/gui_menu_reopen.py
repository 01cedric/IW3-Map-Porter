"""Recover editor state without treating arbitrary binary strings as a valid UI."""
from gui_ui_layout import layout_value
from pathlib import Path
import hashlib,json,struct,zlib

MAGIC=b'IW3MENU1'
LIMIT=96*1024*1024

def digest(data):return hashlib.sha256(data).hexdigest()

def pack_state(zone,original,recovery,entries,original_trailer=b''):
    rows=[{k:v for k,v in e.items() if k!='preview_load_ff'} for e in entries]
    body=dict(schema=1,zone_sha256=digest(zone),original_sha256=digest(original),
              original_size=len(original),restore=recovery['parts'],entries=rows,original_trailer=original_trailer.hex())
    raw=json.dumps(body,separators=(',',':')).encode()
    if len(raw)>LIMIT:raise ValueError('UI reopening metadata exceeds its limit.')
    payload=zlib.compress(raw,9)
    return MAGIC+struct.pack('>I',len(payload))+payload

def unpack_state(doc):
    trailer=doc.trailer
    if not trailer.startswith(MAGIC):return None
    if len(trailer)<12:raise ValueError('Truncated UI reopening metadata.')
    n=struct.unpack_from('>I',trailer,8)[0]
    if n!=len(trailer)-12 or n>LIMIT:raise ValueError('Invalid UI reopening metadata length.')
    dec=zlib.decompressobj();raw=dec.decompress(trailer[12:],LIMIT+1)
    if len(raw)>LIMIT or not dec.eof or dec.unused_data:raise ValueError('Invalid compressed UI reopening metadata.')
    body=json.loads(raw)
    if body.get('schema')!=1 or body.get('zone_sha256')!=digest(doc.zone):raise ValueError('UI bytes do not match their reopening metadata.')
    size=body.get('original_size',0)
    if not 0<size<=LIMIT:raise ValueError('Invalid original UI size.')
    out=bytearray()
    for part in body['restore']:
        if isinstance(part,str):data=bytes.fromhex(part)
        elif isinstance(part,list) and len(part)==2:
            pos,n=part
            if not isinstance(pos,int) or not isinstance(n,int) or pos<0 or n<0 or pos+n>len(doc.zone):raise ValueError('Invalid original UI span.')
            data=doc.zone[pos:pos+n]
        else:raise ValueError('Invalid original UI recovery record.')
        if len(out)+len(data)>size:raise ValueError('Original UI recovery exceeds its size.')
        out+=data
    if len(out)!=size or digest(out)!=body['original_sha256']:raise ValueError('Original UI recovery hash differs.')
    return bytes(out),body['entries'],bytes.fromhex(body['original_trailer'])

def extract_previews(exported,original,profile,rows):
    from gui_menu_previews import TEMPLATE_ROOT,TEMPLATE_NAME
    events=profile['events']
    start=next(i for i,e in enumerate(events) if e[0]==3 and e[2]==layout_value(original, TEMPLATE_ROOT))-2
    end=next(i for i in range(start+3,len(events)) if events[i][0]==3 and events[i][1]==0x40000000 and events[i][3]==128)-2
    template=events[start:end]
    image=next(e[2] for e in template if e[0]==3 and e[3]==52)
    pixels=next(e[2] for e in template if e[0]==3 and e[1]>=0x70000000)
    result={}
    stock={s[0] for s in __import__('gui_mapmenu_profile').SLOTS}
    for row in rows:
        name=row['map_id']
        if name in stock:continue
        encoded=('loadscreen_'+name).encode()+b'\0'
        at=exported.rfind(encoded)
        if at<128:raise ValueError(f'{name}: embedded loading picture not found.')
        cursor=at-128;root=None;resource=None
        if exported[cursor:cursor+4]!=b'\xff'*4:raise ValueError('Invalid embedded material root.')
        for e in template:
            if e[0]!=3:continue
            pos,size=e[2:]
            if pos==layout_value(original, TEMPLATE_NAME):
                size=len(encoded)
                if exported[cursor:cursor+size]!=encoded:raise ValueError('Embedded preview name differs.')
            elif pos==pixels:
                if root is None:raise ValueError('Missing embedded image descriptor.')
                size=struct.unpack_from('>I',root,0x20)[0]
                if not 0<size<=4*1024*1024:raise ValueError('Invalid embedded image size.')
            data=exported[cursor:cursor+size]
            if len(data)!=size:raise ValueError('Truncated embedded image.')
            if pos==image:root=data[:44]+original[image+44:image+52]
            elif pos==pixels:resource=data
            cursor+=size
        if root is None or resource is None:raise ValueError('Incomplete embedded image.')
        result[name]=dict(root=root.hex(),pixels=resource.hex())
        row['preview_load_ff']='[Embedded in UI]'
    return result

def extract_legacy_rows(zone):
    from gui_mapmenu import ACTION
    from gui_mapmenu_profile import SLOTS
    rows=[]
    for i,m in enumerate(ACTION.finditer(zone.decode('latin1'))):
        if i>=32:raise ValueError('Export contains more than 32 map actions.')
        desc='';key=f'iw3_card_{i+1}_description'.encode()+b'\0map_desc_group\0'
        p=zone.find(key)
        if p>=0:
            p+=len(key)
            expected=b'\xff'*8+struct.pack('>6I',0,16,0,1,2,0xffffffff)
            if zone[p:p+32]!=expected:raise ValueError('Unsupported description expression.')
            p+=32;end=zone.find(b'\0',p,p+512)
            if end<0:raise ValueError('Invalid map description.')
            desc=zone[p:end].decode('ascii')
        rows.append(dict(slot=i+1,stock_map=SLOTS[i][0] if i<16 else 'Custom',map_id=m[1],display_name=m[2],description=desc,max_name_length=64))
    if not 16<=len(rows)<=32:raise ValueError('No supported exported map list found.')
    return rows

def open_source(doc,baseline_path=''):
    from gui_mapmenu import read_profile,inspect_zone,prepare_edit
    from gui_menu_previews import add_previews
    from gui_ui_relocation import rebuild
    from cod4porter.backend.v4_ffio import read_ps3_fastfile
    try:
        profile=read_profile(doc.zone)
        return doc.zone,profile,inspect_zone(doc.zone,profile),{},doc.trailer,doc.path
    except ValueError:pass
    state=unpack_state(doc)
    if state:
        original,rows,trailer=state;profile=read_profile(original)
        embedded=extract_previews(doc.zone,original,profile,rows)
        # Validate all recovered editor fields before exposing them to the GUI.
        prepare_edit(original,profile,rows)
        return original,profile,rows,embedded,trailer,''
    path=Path(baseline_path or '')
    if not path.is_file():raise ValueError('This older exported UI needs its untouched original once. Select it in "Original UI", then inspect again. New exports reopen without that original or a sidecar file.')
    baseline=read_ps3_fastfile(path);original=baseline.zone;profile=read_profile(original)
    rows=extract_legacy_rows(doc.zone);embedded=extract_previews(doc.zone,original,profile,rows)
    changes,inserts,_=prepare_edit(original,profile,rows)
    add_previews(original,profile,rows,changes,inserts,embedded)
    rebuilt,_=rebuild(original,profile,changes,inserts)
    # 22.1.12 had an incorrect size word, fixed in 22.1.13. Match all remaining
    # content byte-for-byte, permitting only all-zero container tail padding.
    end=profile['stream_end']+len(rebuilt)-len(original)
    if doc.zone[4:end]!=rebuilt[4:end] or any(doc.zone[end:]) or doc.trailer!=baseline.trailer:
        raise ValueError('This export does not match the supported 22.1.12/22.1.13 menu layout. No file was changed.')
    return original,profile,rows,embedded,baseline.trailer,str(path.resolve())
