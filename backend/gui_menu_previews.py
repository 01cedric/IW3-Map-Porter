"""Embed converted map loading pictures as native lobby preview materials."""
from gui_ui_layout import layout_value
from pathlib import Path
import hashlib
import struct
from gui_menu_picture import prepare_picture

TEMPLATE_ROOT = 0x1926001
TEMPLATE_NAME = TEMPLATE_ROOT + 128
MAX_PREVIEW_BYTES = 16 * 1024 * 1024


def add_previews(zone, profile, entries, replacements, inserts, embedded=None):
    """Copy pixels/descriptors from PS3 _load.ff using the UI's own 2D material.

    The lobby requests loadscreen_<ui_mapname> before the map's _load zone is
    loaded. $levelbriefing in _load.ff alone cannot satisfy that UI lookup.
    """
    stock = {'mp_convoy','mp_backlot','mp_bloc','mp_bog','mp_countdown','mp_crash',
             'mp_crossfire','mp_citystreets','mp_farm','mp_overgrown','mp_pipeline',
             'mp_shipment','mp_showdown','mp_strike','mp_vacant','mp_cargoship'}
    selected=[row for row in entries if row['map_id'].strip() not in stock]
    if not selected:return []
    events=profile['events']
    start=next(i for i,e in enumerate(events) if e[0]==3 and e[2]==layout_value(zone, TEMPLATE_ROOT))-2
    end=next(i for i in range(start+3,len(events)) if events[i][0]==3 and events[i][1]==0x40000000 and events[i][3]==128)-2
    template=events[start:end]
    if template[:2]!=[[0,0],[2,3]] or template[-2:]!=[[1],[1]]:
        raise ValueError('Preview material allocation scope differs from the verified UI.')
    image=next(e[2] for e in template if e[0]==3 and e[3]==52)
    pixels=next(e[2] for e in template if e[0]==3 and e[1]>=0x70000000)
    finish=next(e[2] for e in events[end:] if e[0]==3)
    fixups=[f for f in profile['fixups'] if layout_value(zone, TEMPLATE_ROOT)<=f[0]<finish]
    report=[];total=0;clones=[]
    for row in selected:
        name=row['map_id'].strip();path=Path(row.get('preview_load_ff') or '')
        restored=(embedded or {}).get(name) if not row.get('preview_load_ff') or row.get('preview_load_ff')=='[Embedded in UI]' else None
        if restored is not None:
            root=bytes.fromhex(restored['root']);resource=bytes.fromhex(restored['pixels']);size=len(resource)
            if len(root)!=52 or struct.unpack_from('>I',root,0x20)[0]!=size or not 0<size<=4*1024*1024:raise ValueError('Invalid embedded preview.')
            total+=size
            if total>MAX_PREVIEW_BYTES:raise ValueError('Embedded previews exceed the 16 MiB export limit.')
            clones.append(dict(events=template,replacements={layout_value(zone, TEMPLATE_NAME):('loadscreen_'+name).encode()+b'\0',image:root,pixels:resource},fixups=fixups,kind='preview'))
            report.append(dict(map_id=name,material='loadscreen_'+name,source='embedded UI',pixel_bytes=size,pixel_sha256=hashlib.sha256(resource).hexdigest()))
            continue
        if not path.is_file():
            raise ValueError(f'{name}: select its converted PS3 _load.ff using "Set loading picture". The lobby needs an embedded preview; no output was written.')
        root,resource,_,picture_report=prepare_picture(path,name)
        size=len(resource)
        total+=size
        if total>MAX_PREVIEW_BYTES:raise ValueError('Embedded previews exceed the 16 MiB export limit.')
        root=root[:44]+zone[image+44:image+52]
        clones.append(dict(events=template,replacements={layout_value(zone, TEMPLATE_NAME):('loadscreen_'+name).encode()+b'\0',image:root,pixels:resource},fixups=fixups,kind='preview'))
        report.append(picture_report)
    # Extend the XAssetList and its native type/pointer records, then append the
    # complete material assets before the final VIRTUAL block pop.
    header=bytearray(zone[36:52]);count=struct.unpack_from('>I',header,8)[0]
    if count!=101:raise ValueError('Unexpected source UI asset count.')
    struct.pack_into('>I',header,8,count+len(clones));replacements[36]=bytes(header)
    replacements[52]=zone[52:52+count*8]+struct.pack('>2I',4,0xffffffff)*len(clones)
    inserts.setdefault(len(events)-1,[]).extend(clones)
    return report
