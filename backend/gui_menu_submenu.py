"""Add a native menu-file child for custom maps; keep stock choices separate."""
from gui_ui_layout import layout_value
import struct


def prepare_submenu(zone,profile,entries):
    from gui_mapmenu import (prepare_edit,MENU_ROOT,ITEM_ARRAY,ORIGINAL_ITEM_COUNT,CLONE_START,CLONE_END,BUTTON_ROOT,SLOTS)
    if len(entries)<=16:return prepare_edit(zone,profile,entries)
    main,main_inserts,edits=prepare_edit(zone,profile,entries[:16],allow_no_changes=True)
    custom,custom_inserts,all_edits=prepare_edit(zone,profile,entries,custom_page=True)
    events=profile['events'];boundary=layout_value(zone, 1081291)
    start=next(i for i,e in enumerate(events) if e[0]==3 and e[2]==layout_value(zone, MENU_ROOT))-2
    finish=boundary+2
    assert events[boundary:finish]==[[1],[1]]
    headers=[(i,e[2]) for i,e in enumerate(events[start:finish],start) if e[0]==3 and e[3]==468]
    assert len(headers)==ORIGINAL_ITEM_COUNT
    removed=set()
    for _,ap,_,_,_,_,_ in layout_value(zone, SLOTS):
        button=next(j for j,(_,pos) in enumerate(headers) if pos==ap-468)
        removed.update(range(button-5,button+1))
    assert len(removed)==96
    skip=set()
    for j in removed:
        lo=headers[j][0]-1;hi=headers[j+1][0]-1 if j+1<len(headers) else boundary
        skip.update(range(lo,hi))
    kept=[j for j in range(len(headers)) if j not in removed]
    extras=[c for c in custom_inserts[boundary] if c.get('kind')!='scrollbar']
    # Extra stock descriptions are irrelevant in the custom-only menu.
    extras=[c for c in extras if c.get('kind')!='map-card' or any(f'iw3_card_{i}_'.encode().hex() in e[1] for i in range(17,len(entries)+1) for e in c['events'] if e[0]==5)]
    count=len(kept)+sum(6 if c.get('kind','row')=='row' else 1 for c in extras)
    header=bytearray(zone[layout_value(zone, MENU_ROOT):layout_value(zone, MENU_ROOT)+308]);struct.pack_into('>I',header,0xb0,count)
    custom[layout_value(zone, MENU_ROOT)]=bytes(header)
    custom[layout_value(zone, MENU_ROOT)+308]=b'settings_custom_map\0'
    custom[layout_value(zone, ITEM_ARRAY)]=b''.join(zone[layout_value(zone, ITEM_ARRAY)+4*j:layout_value(zone, ITEM_ARRAY)+4*j+4] for j in kept)+b'\xff'*(4*(count-len(kept)))
    # Escape returns to the still-open stock menu; selecting a map closes both.
    custom[layout_value(zone, 0x14B9B07)]=b'"close" "self" ; \0'
    for c in extras:
        if c.get('kind','row')=='row':
            p=layout_value(zone, 0x14BDD80)
            c['replacements'][p]=c['replacements'][p].replace(b'"close" "self" ;',b'"close" "self" ; "close" "settings_map" ;')
    # Use the native title item, retaining its material and layout.
    for e in events[start:finish]:
        if e[0]==3 and e[3]<100:
            raw=zone[e[2]:e[2]+e[3]]
            if raw in (b'@MPUI_CHOOSE_MAP\0',b'@MPUI_SELECT_MAP\0',b'@MENU_CHOOSE_MAP_CAP\0'):
                custom[e[2]]=b'^3Custom Maps^7\0'
    composite=[]
    for i in range(start,finish):
        if i in skip:continue
        if i==boundary:composite.extend([[6,c] for c in extras])
        composite.append(events[i])
    # Only retained native allocation fields belong to this clone's relocation scope.
    retained=[(e[2],e[2]+e[3]) for e in composite if e[0]==3]
    import bisect
    begins=[p for p,_ in retained]
    def belongs(p):
        j=bisect.bisect_right(begins,p)-1
        return j>=0 and p+4<=retained[j][1]
    fields=[f for f in profile['fixups'] if belongs(f[0])]
    submenu=dict(events=composite,replacements=custom,fixups=fields,kind='custom-menu')
    # Extend ui_mp/menus.txt from 331 to 332 registered menus, before its final pops.
    list_header=bytearray(zone[976:988]);assert struct.unpack_from('>I',list_header,4)[0]==331
    struct.pack_into('>I',list_header,4,332);main[976]=bytes(list_header)
    main[1004]=zone[1004:2328]+b'\xff'*4
    # The first material after the menu file starts in TEMP after two closing pops.
    first_material=next(i for i,e in enumerate(events) if e[0]==3 and e[3]==128 and e[1]==0x40000000)
    assert events[first_material-4:first_material]==[[1],[1],[0,0],[2,3]]
    main_inserts.setdefault(first_material-4,[]).append(submenu)
    # A yellow 17th row opens the custom menu. Use the established six-item row.
    row=next(c for c in custom_inserts[boundary] if c.get('kind','row')=='row')
    import copy
    opener=copy.deepcopy(row)
    opener['replacements'][layout_value(zone, 0x14BDD80)]=b'"play" "mouse_click" ; "open" "settings_custom_map" ; \0'
    opener['replacements'][layout_value(zone, 0x14BDE14)]=b'"play" "mouse_over" ; "setLocalVarInt" "ui_highlight" 17 ; "hide" "map_image_group" ; "hide" "map_name_group" ; "hide" "map_desc_group" ; \0'
    opener['replacements'][layout_value(zone, 0x14BDF52)]=b'"iw3_custom_maps" \0'
    opener['replacements'][layout_value(zone, 0x14BDF7F)]=b'^3Custom Maps^7\0'
    # Replace generated Y expressions with a fixed seventeenth-row position.
    # Rebuild the clone using only original events, with static Y geometry.
    lo=next(i for i,e in enumerate(events) if e[0]==3 and e[2]==layout_value(zone, CLONE_START))-1
    hi=next(i for i,e in enumerate(events) if e[0]==3 and e[2]==layout_value(zone, CLONE_END))-1
    opener['events']=events[lo:hi]
    for e in opener['events']:
        if e[0]==3 and e[3]==468:
            pos=e[2];h=bytearray(zone[pos:pos+468])
            for field in (8,0x20):struct.pack_into('>f',h,field,struct.unpack_from('>f',zone,pos+field)[0]+320)
            if pos==layout_value(zone, BUTTON_ROOT):struct.pack_into('>4f',h,0x64,1.0,1.0,0.0,1.0)
            opener['replacements'][pos]=bytes(h)
    main_inserts.setdefault(boundary,[]).append(opener)
    mh=bytearray(main.get(layout_value(zone, MENU_ROOT),zone[layout_value(zone, MENU_ROOT):layout_value(zone, MENU_ROOT)+308]));oldcount=struct.unpack_from('>I',mh,0xb0)[0]
    struct.pack_into('>I',mh,0xb0,oldcount+6);main[layout_value(zone, MENU_ROOT)]=bytes(mh)
    main[layout_value(zone, ITEM_ARRAY)]=main.get(layout_value(zone, ITEM_ARRAY),zone[layout_value(zone, ITEM_ARRAY):layout_value(zone, ITEM_ARRAY)+700])+b'\xff'*24
    # Preserve custom-aware lobby names/images and launch scripts outside settings_map.
    for pos,data in custom.items():
        if pos<layout_value(zone, MENU_ROOT) and pos not in (976,1004):main[pos]=data
    for at,scopes in custom_inserts.items():
        if at<start:main_inserts.setdefault(at,[]).extend(scopes)
    return main,main_inserts,all_edits
