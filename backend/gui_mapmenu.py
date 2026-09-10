"""PS3 private-match menu rebuild using a native-loader allocation profile."""
from __future__ import annotations
from gui_ui_layout import layout_value
import hashlib
import json
import re
import struct
import zlib
from pathlib import Path
from cod4porter.backend.v4_ffio import read_ps3_fastfile, build_ps3_fastfile, require_retail_ps3_adler32
from gui_mapmenu_profile import SLOTS
from gui_ui_relocation import rebuild
from gui_menu_previews import add_previews
from gui_menu_scroll import (RECT_Y, SCROLL_VAR, expression_events, row_y, thumb_y, set_expression, focus_scroll)

PROFILE = Path(__file__).parent / 'reference/ui-menu-layout-v1.json.zlib'
MAP_ID = re.compile(r'mp_[a-z0-9_]{1,60}\Z')
NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9 ._()+!&'-]{0,63}\Z")
ACTION = re.compile(r'"setdvar"\s+"ui_mapname"\s+"(mp_[a-z0-9_]+)"\s*;\s*"setdvar"\s+"ui_mapname_text"\s+"([^"\x00]+)"')
MENU_ROOT = 0x14B9975
ITEM_ARRAY = 0x14B9B21
ORIGINAL_ITEM_COUNT = 175
CLONE_START = 0x14BCFFA
CLONE_END = 0x14BDF8C
BUTTON_ROOT = 0x14BDBAC
MAX_MAPS = 32
START_ACTION = 0x14892D0
LOCAL_START_ACTION = 0x14A9806
MENU_OPEN = 0x14B9AB6


def read_profile(zone):
    from gui_revision import ui_identity
    identity = ui_identity(zone)
    filename = identity.get('editor_profile')
    profile_path = PROFILE.parent / filename if filename else PROFILE
    profile = json.loads(zlib.decompress(profile_path.read_bytes()))
    if hashlib.sha256(zone).hexdigest() != profile['source_sha256']:
        from gui_revision import ui_identity
        identity = ui_identity(zone)
        raise ValueError(f"{identity['label']} ({identity['zone_sha256'][:16]}): a matching menu relocation profile is required. Region and update labels cannot substitute for this profile. No output was written.")
    return profile


def inspect_zone(zone, profile=None):
    if profile is None: read_profile(zone)
    rows = []
    for i, (stock, offset, size, hp, hn, lp, ln) in enumerate(layout_value(zone, SLOTS)):
        match = ACTION.search(zone[offset:offset+size].decode('ascii'))
        if not match:
            raise ValueError('The map-selection script does not match its profile.')
        rows.append(dict(slot=i+1, stock_map=stock, map_id=match[1], display_name=match[2], description='', max_name_length=64))
    return rows


def cstring(zone, offset):
    return zone[offset:zone.index(0, offset)+1]


def scripts(map_id, name, index):
    action = f'"play" "mouse_click" ; "setdvar" "ui_mapname" "{map_id}" ; "setdvar" "ui_mapname_text" "{name}" ; "close" "self" ; \0'
    hover = (f'"play" "mouse_over" ; "setLocalVarInt" "ui_highlight" {index} ; '
             '"setLocalVarString" "ui_choicegroup" "map_selection" ; '
             '"hide" "map_image_group" ; "hide" "map_name_group" ; '
             f'"hide" "map_desc_group" ; "setdvar" "ui_mapname2_text" "{name}" ; \0')
    return action.encode(), hover.encode()


def prepare_edit(zone, profile, entries, *, allow_no_changes=False, custom_page=False):
    current = inspect_zone(zone, profile)
    if not isinstance(entries, list) or not 16 <= len(entries) <= MAX_MAPS:
        raise ValueError('Keep the 16 original rows and add up to 16 custom rows (32 map choices total).')
    if [e.get('slot') for e in entries] != list(range(1,len(entries)+1)):
        raise ValueError('Menu rows must have consecutive slot numbers starting at 1.')
    ids = []; edits = []; replacements = {}; clones = []; inserts = {}
    scrolling = len(entries) > 16
    events = profile['events']
    starti = next(i for i,e in enumerate(events) if e[0]==3 and e[2]==layout_value(zone, CLONE_START))-1
    endi = next(i for i,e in enumerate(events) if e[0]==3 and e[2]==layout_value(zone, CLONE_END))-1
    clone_events = events[starti:endi]
    clone_fields = [f for f in profile['fixups'] if layout_value(zone, CLONE_START) <= f[0] < layout_value(zone, CLONE_END)]
    for i, entry in enumerate(entries):
        map_id = str(entry.get('map_id','')).strip(); name = str(entry.get('display_name','')).strip()
        if not MAP_ID.fullmatch(map_id):
            raise ValueError(f'Row {i+1}: use a map ID beginning with mp_, followed by lowercase letters, digits and underscores (63 characters maximum).')
        ids.append(map_id)
        unchanged = i<16 and map_id==current[i]['map_id'] and name==current[i]['display_name']
        if not unchanged and not NAME.fullmatch(name):
            raise ValueError(f'Row {i+1}: enter a plain English display name, 1–64 characters, without quotes, semicolons or backslashes.')
        action, hover = scripts(map_id,name,i+1)
        if i<16:
            stock,ap,an,hp,hn,lp,ln=layout_value(zone, SLOTS)[i]
            if unchanged:
                action, hover = cstring(zone, ap), cstring(zone, hp)
            if scrolling:
                hover = focus_scroll(i, len(entries)) + hover
            replacements.update({ap:action,hp:hover,lp:name.encode()+b'\0'})
            # The focus-test XString follows the hover script, before its expression.
            test=hp+hn+1
            replacements[test]=f'"{map_id}" \0'.encode()
            # The button header is immediately before its inline action script.
            header=bytearray(zone[ap-468:ap]);struct.pack_into('>f',header,0x124,0.375*min(1,26/len(name)))
            replacements[ap-468]=bytes(header)
        else:
            hover = focus_scroll(i-16 if custom_page else i, len(entries)-16 if custom_page else len(entries)) + hover
            cr={layout_value(zone, 0x14BDD80):action,layout_value(zone, 0x14BDE14):hover,layout_value(zone, 0x14BDF52):f'"{map_id}" \0'.encode(),layout_value(zone, 0x14BDF7F):name.encode()+b'\0'}
            # Clone the entire selectable row: background, focus highlight, button
            # glyph and action item. Its three comparison operands select this row.
            for offset in (layout_value(zone, 0x14BD602),layout_value(zone, 0x14BD8A4),layout_value(zone, 0x14BDB40)):
                data=bytearray(zone[offset:offset+12]);assert struct.unpack('>3I',data)==(1,0,1)
                struct.pack_into('>I',data,8,i+1);cr[offset]=bytes(data)
            for e in clone_events:
                if e[0]==3 and e[3]==468:
                    offset=e[2];header=bytearray(zone[offset:offset+468])
                    tokens = row_y(i-16 if custom_page else i, struct.unpack_from('>f', header, 8)[0])
                    set_expression(header, RECT_Y, tokens)
                    for field in (8,0x20):struct.pack_into('>f',header,field,10000.0)
                    if offset==layout_value(zone, BUTTON_ROOT):struct.pack_into('>f',header,0x124,0.375*min(1,26/len(name)))
                    cr[offset]=bytes(header)
            expanded_events = []
            for k, event in enumerate(clone_events):
                if k and event[0]==2 and k+1<len(clone_events) and clone_events[k+1][0]==3 and clone_events[k+1][3]==468:
                    expanded_events += expression_events(row_y(i-16 if custom_page else i, base))
                expanded_events.append(event)
                if event[0]==3 and event[3]==468:
                    base = struct.unpack_from('>f',zone,event[2]+8)[0]
            expanded_events += expression_events(row_y(i-16 if custom_page else i, base))
            clones.append(dict(events=expanded_events,replacements=cr,fixups=clone_fields))
        if not unchanged or entry.get('description'): edits.append(dict(slot=i+1,map_id=map_id,display_name=name,description=entry.get('description',''),added=i>=16))
    if len(ids)!=len(set(ids)):raise ValueError('Map IDs must be unique across the menu.')
    if not edits and not allow_no_changes:raise ValueError('No changes. Edit a name or add a map before exporting.')
    if scrolling:
        # Add rect-Y expressions after each stock row item's existing data.
        headers = [(k,e[2]) for k,e in enumerate(events) if e[0]==3 and e[3]==468]
        for i, (_,ap,_,_,_,_,_) in enumerate(layout_value(zone, SLOTS)):
            button = next(j for j,(_,pos) in enumerate(headers) if pos==ap-468)
            for j in range(button-5, button+1):
                k,pos=headers[j];boundary=headers[j+1][0]-1
                header=bytearray(replacements.get(pos,zone[pos:pos+468]))
                base=struct.unpack_from('>f',zone,pos+8)[0]-20*i
                if base not in (34,35): raise ValueError('Stock row geometry differs from the scrolling profile.')
                tokens=row_y(i,base);set_expression(header,RECT_Y,tokens)
                replacements[pos]=bytes(header)
                inserts.setdefault(boundary,[]).append(dict(events=expression_events(tokens), replacements={},fixups=[],kind='expression'))
        # Narrow non-focusable track and proportional thumb alongside the list.
        first_end=next(k for k,e in enumerate(clone_events) if e[0]==3 and e[2]==layout_value(zone, 0x14BD1EE))-1
        for thumb in (False,True):
            header=bytearray(zone[layout_value(zone, CLONE_START):layout_value(zone, CLONE_START)+468])
            height=320.0*16/len(entries) if thumb else 320.0
            for start in (4,0x1c):struct.pack_into('>4f',header,start,232.0,34.0,4.0,height)
            struct.pack_into('>4f',header,0x64,0.8,0.8,0.8,0.8 if thumb else 0.2)
            bar_events=list(clone_events[:first_end])
            if thumb:
                tokens=thumb_y(len(entries));set_expression(header,RECT_Y,tokens)
                bar_events+=expression_events(tokens)
            fields=[f for f in clone_fields if f[0]<layout_value(zone, 0x14BD1EE)]
            clones.append(dict(events=bar_events,replacements={layout_value(zone, CLONE_START):bytes(header)},fixups=fields,kind='scrollbar'))
        header=bytearray(zone[layout_value(zone, MENU_ROOT):layout_value(zone, MENU_ROOT)+308]);struct.pack_into('>I',header,0xb0,ORIGINAL_ITEM_COUNT+6*(len(entries)-16)+2);replacements[layout_value(zone, MENU_ROOT)]=bytes(header)
        replacements[layout_value(zone, ITEM_ARRAY)]=zone[layout_value(zone, ITEM_ARRAY):layout_value(zone, ITEM_ARRAY)+4*ORIGINAL_ITEM_COUNT]+b'\xff'*(24*(len(entries)-16)+8)
        replacements[layout_value(zone, MENU_OPEN)]=f'"setLocalVarInt" "{SCROLL_VAR}" 0 ; '.encode()+cstring(zone,layout_value(zone, MENU_OPEN))
    # Direct map startup uses the same selected map/game-type inputs as the
    # native StartServer UI handler. Stock and unlisted maps retain xpartygo.
    # Direct custom startup must not assume party preloading has registered the
    # map. Save/disable the dvar before dispatch; queue restoration after map.
    # The next launch also restores an interrupted override before saving again.
    stock_ids={row['map_id'] for row in current}
    custom_ids=[name for name in ids if name not in stock_ids]
    if custom_ids:
        launch=['"play" "mouse_click" ; ',
                '"execNowOnDvarStringValue" "iw3_preload_override" "1" "setfromdvar useSvMapPreloading iw3_preload_saved" ; ',
                '"execNow" "set iw3_preload_override 0" ; ',
                '"execNow" "set iw3_custom_launch 0" ; ',
                '"execNow" "set iw3_preload_restore echo IW3MENU_STOCK_LAUNCH" ; ',
                '"exec" "selectStringTableEntryInDvar mp/didyouknow.csv 0 didyouknow" ; ',
                '"execNow" "set iw3_menu_launch xpartygo" ; ']
        for name in custom_ids:
            launch.append(f'"execNowOnDvarStringValue" "ui_mapname" "{name}" "set iw3_menu_launch map {name}" ; ')
            launch.append(f'"execNowOnDvarStringValue" "ui_mapname" "{name}" "set iw3_custom_launch 1" ; ')
        launch += ['"execNowOnDvarStringValue" "iw3_custom_launch" "1" "setfromdvar iw3_preload_saved useSvMapPreloading" ; ',
                   '"execNowOnDvarStringValue" "iw3_custom_launch" "1" "set iw3_preload_override 1" ; ',
                   '"execNowOnDvarStringValue" "iw3_custom_launch" "1" "set useSvMapPreloading 0" ; ',
                   '"execNowOnDvarStringValue" "iw3_custom_launch" "1" "set iw3_preload_restore setfromdvar useSvMapPreloading iw3_preload_saved" ; ',
                   '"execNow" "setfromdvar g_gametype ui_gametype" ; ',
                   '"exec" "wait; wait; vstr iw3_menu_launch; vstr iw3_preload_restore; set iw3_preload_override 0" ; ']
        script=''.join(launch).encode()+b'\0'
        if len(script)>0x1400:raise ValueError('Map launch script exceeds the native 5120-byte script buffer.')
        replacements[layout_value(zone, START_ACTION)]=script
    from gui_menu_cards import add_map_cards
    add_map_cards(zone, profile, entries, current, replacements, inserts, clones)
    if custom_ids:
        from gui_menu_lobbies import fix_local_lobby_previews, instrument_start
        fix_local_lobby_previews(zone, replacements)
        replacements[layout_value(zone, START_ACTION)] = instrument_start(replacements[layout_value(zone, START_ACTION)], 'PRIVATE', False)
        replacements[layout_value(zone, LOCAL_START_ACTION)] = instrument_start(cstring(zone, layout_value(zone, LOCAL_START_ACTION)), 'LOCAL', True)
    # The captured menu loader pops VIRTUAL here after consuming all item pointers.
    insertion = layout_value(zone, 1081291)
    assert events[insertion]==[1]
    inserts.setdefault(insertion,[]).extend(clones)
    return replacements, inserts, edits


def execute(command, settings, out, emit, artifact):
    source = Path(settings.get('map_menu_ff') or '')
    if not source.is_file():raise ValueError('Select a PS3 ui_mp.ff first.')
    emit('stage','Inspecting PS3 map-selection menu')
    doc=read_ps3_fastfile(source)
    from gui_revision import ui_identity, installation_metadata
    revision = dict(file=ui_identity(doc.zone), installation=installation_metadata(source))
    revision_path=out/'ps3-revision.json'
    revision_path.write_text(json.dumps(revision,indent=2)+'\n',encoding='utf-8')
    artifact(revision_path,'report')
    emit('log', 'Automatic UI identification: '+revision['file']['label']+'; update '+revision['file']['update'])
    from gui_menu_reopen import open_source,pack_state
    original,profile,rows,embedded,original_trailer,original_source=open_source(doc,settings.get('map_menu_original_ff',''))
    revision['original_ui'] = ui_identity(original)
    revision_path.write_text(json.dumps(revision,indent=2)+'\n',encoding='utf-8')
    report=dict(revision=revision,original_source=original_source,source=str(source.resolve()),source_sha256=doc.file_sha256,profile=revision['original_ui']['label'],slots=rows,
                runtime_json_required=False,hardware_verified=False,max_maps=MAX_MAPS,
                limitations=['Automatically selects the captured reference or BLUS-30072 UI layout by exact zone hash, including reopened exports. Other UI revisions require their own capture.',
                             'Uses the normal FastFile directory, without EBOOT changes or folder autodetection.',
                             '16 stock rows plus up to 16 added rows in a separate colored Custom Maps submenu. Stock previews are preserved; custom lobby previews are embedded from converted PS3 _load.ff files; custom map-selection cards include pictures, titles, descriptions and a fixed porter credit. System-link and split-screen lobby pictures also resolve embedded loadscreen materials.',
                             'Names support 64 ASCII characters; longer labels use smaller text.',
                             'Loading-screen/server-browser names may use other assets.',
                             'Shared native menu strings can also affect split-screen labels/actions.',
                             'New exports reopen directly; older 22.1.12/22.1.13 exports require their untouched original once.',
                             'Online private custom maps save and disable useSvMapPreloading before direct map startup, with queued restoration and next-launch recovery; stock private maps retain xpartygo. Menu-command behavior requires console validation. Local system-link/split-screen menus retain native StartServer with launch diagnostics. The 22.1.13 stream-size and full-frame fixes are preserved; this new submenu still requires a PS3 test.'])
    if command=='mapmenu-write':
        from gui_menu_submenu import prepare_submenu
        replacements,inserts,edits=prepare_submenu(original,profile,settings.get('map_menu_entries',[]))
        report['embedded_previews']=add_previews(original,profile,settings.get('map_menu_entries',[]),replacements,inserts,embedded)
        emit('stage','Rebuilding allocations, menu items, lobby images and packed references')
        recovery={}
        zone,checks=rebuild(original,profile,replacements,inserts,recovery=recovery)
        # Relocation preserves the original stream suffix, but expansion changes
        # its position. Re-pad the container tail to full retail 64-KiB frames.
        # This is outside the asset stream: do not shift assets or grow logical blocks.
        tail_padding=(-len(zone)) & 0xFFFF
        zone += bytes(tail_padding)
        checks['container_tail_padding_added']=tail_padding
        target=out/'ui_mp.ff'
        if target.resolve()==source.resolve():raise ValueError('Output must differ from the original UI.')
        temp=target.with_suffix('.ff.tmp')
        try:
            trailer=pack_state(zone,original,recovery,settings['map_menu_entries'],original_trailer)
            temp.write_bytes(build_ps3_fastfile(zone,trailer=trailer));readback=read_ps3_fastfile(temp)
            if readback.zone!=zone or readback.trailer!=trailer:raise ValueError('UI FastFile readback failed.')
            require_retail_ps3_adler32(readback)
            if any(block.raw_bytes!=0x10000 for block in readback.blocks):
                raise ValueError('UI FastFile contains a short retail decompression frame.')
            temp.replace(target)
        finally:temp.unlink(missing_ok=True)
        report.update(changes=edits,output_sha256=readback.file_sha256,readback_passed=True,relocation=checks,
                      map_count=len(settings['map_menu_entries']),item_count=struct.unpack_from('>I', replacements.get(layout_value(original, MENU_ROOT), original[layout_value(original, MENU_ROOT):layout_value(original, MENU_ROOT)+308]), 0xb0)[0])
        artifact(target,'map-menu-fastfile')
    path=out/'map-menu-inspection.json';path.write_text(json.dumps(report,indent=2)+'\n',encoding='utf-8')
    artifact(path,'map-menu-inspection' if command=='mapmenu-inspect' else 'report')
    if command=='mapmenu-inspect':return 'passed','UI loaded. Edit names or add maps, then export a separate ui_mp.ff.'
    return 'unproven',f'{report["map_count"]} map choices rebuilt. Relocation and FastFile readback completed; private-match startup still requires a PS3 test.'
