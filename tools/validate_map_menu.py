"""Reproduce the 32-choice UI relocation check with user-supplied game files."""
from pathlib import Path
import argparse
import json
import os
import struct
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'backend'))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--ui', required=True, type=Path, help='Original supported ui_mp.ff')
    parser.add_argument('--elf', required=True, type=Path, help='Matching cod4.ELF loader reference')
    parser.add_argument('--report', required=True, type=Path)
    parser.add_argument('--entries', type=Path, help='Optional exported editor rows with PS3 loading picture paths')
    args = parser.parse_args()
    from gui_emulator import validate_elf
    validate_elf(str(args.elf))
    os.environ['IW3_PS3_ELF'] = str(args.elf.resolve())
    sys.path.insert(0, str(ROOT / 'backend/tools/ps3_loader_emulator'))
    from zoneload import ZoneLoader
    from gui_mapmenu import read_profile, inspect_zone, prepare_edit, rebuild
    from cod4porter.backend.v4_ffio import read_ps3_fastfile

    original = read_ps3_fastfile(args.ui).zone
    profile = read_profile(original)
    if rebuild(original, profile)[0] != original:
        raise ValueError('Unchanged expanded stream is not byte-exact.')
    rows = inspect_zone(original, profile)
    rows[0]['display_name'] = 'Ambush with a much longer display name'
    for i in range(16):
        rows.append(dict(slot=17+i, map_id=f'mp_custom_{i}',
                         display_name=f'Custom Map {i} with an extended display name'))
    if args.entries: rows=json.loads(args.entries.read_text())
    replacements, inserts, _ = prepare_edit(original, profile, rows)
    previews=[]
    if args.entries:
        from gui_menu_previews import add_previews
        previews=add_previews(original,profile,rows,replacements,inserts)
    zone, relocation = rebuild(original, profile, replacements, inserts)
    loader = ZoneLoader(zone, verbose=True)
    menus = []
    images = []
    materials = []
    local_lobbies = []
    cards = []
    original_link = loader.stubs[0xdfa28]

    def cstring(address):
        raw = bytearray()
        for i in range(16384):
            value = loader.m.u8(address+i)
            if not value:
                return raw.decode('ascii', errors='replace')
            raw.append(value)
        raise ValueError('Unterminated native menu string.')

    def link(cpu):
        typ, root = cpu.r[4] & 0xFFFFFFFF, cpu.r[5] & 0xFFFFFFFF
        if typ == 8:
            name=cstring(loader.m.u32(root+0x30))
            if name in {p['material'] for p in previews}:
                import hashlib
                size=loader.m.u32(root+0x20);resource=loader.m.u32(root+0x2c)
                images.append(dict(name=name,bytes=size,sha256=hashlib.sha256(loader.m.read(resource,size)).hexdigest()))
        if typ == 4:
            name=cstring(loader.m.u32(root))
            if name in {p['material'] for p in previews}: materials.append(name)
        if typ == 23 and cstring(loader.m.u32(root)) in ('menu_gamesetup_systemlink', 'menu_gamesetup_splitscreen'):
            name=cstring(loader.m.u32(root));found=[];starts=[];titles=[]
            count, table=loader.m.u32(root+0xb0),loader.m.u32(root+0x130)
            for i in range(count):
                item=loader.m.u32(table+4*i);n=loader.m.u32(item+0x1a4);ptr=loader.m.u32(item+0x1a8)
                if n == 12:
                    tokens=[]
                    for j in range(n):
                        record=loader.m.u32(ptr+4*j)
                        kind,typ0,value=struct.unpack('>3I',loader.m.read(record,12))
                        tokens.append([kind,typ0,cstring(value) if kind==1 and typ0==2 else value])
                    if any(t[2]=='ui_mapname' for t in tokens):found.append(tokens)
                ntext=loader.m.u32(item+0x19c);textptr=loader.m.u32(item+0x1a0)
                if ntext==14:
                    tt=[]
                    for j in range(ntext):
                        record=loader.m.u32(textptr+4*j)
                        kind,typ0,value=struct.unpack('>3I',loader.m.read(record,12))
                        tt.append([kind,typ0,cstring(value) if kind==1 and typ0==2 else value])
                    if any(t[2]=='ui_mapname_text' for t in tt):titles.append(tt)
                action=loader.m.u32(item+0x150)
                if action and 'StartServer' in cstring(action):starts.append(cstring(action))
            from gui_menu_lobbies import local_preview_tokens
            expected=[list(t) for t in local_preview_tokens('loadscreen_','ui_mapname')]
            if found != [expected] or len(starts)!=1 or 'IW3MENU_LOCAL_HANDLER_RETURNED' not in starts[0]:
                raise ValueError('Local lobby image expression or native StartServer diagnostics differ from the edited graph.')
            from gui_menu_cards import title_tokens
            if titles != [[list(t) for t in title_tokens('', 'ui_mapname_text')]]:raise ValueError('Native local title expression differs from selected display-name lookup.')
            local_lobbies.append(dict(name=name,material_expression=found[0],title_expression=titles[0],start_action=starts[0]))
        if typ == 23 and cstring(loader.m.u32(root)) == 'settings_map':
            count, table = loader.m.u32(root+0xb0), loader.m.u32(root+0x130)
            items = []
            for i in range(count):
                item=loader.m.u32(table+i*4);nameptr=loader.m.u32(item)
                name=cstring(nameptr) if nameptr else ''
                if name.startswith('iw3_card_'):
                    field=0x1a4 if name.endswith('_picture') else 0x19c
                    n=loader.m.u32(item+field);ptr=loader.m.u32(item+field+4)
                    assert n==2,(name,n)
                    record=loader.m.u32(ptr+4);assert loader.m.u32(record)==1 and loader.m.u32(record+4)==2
                    cards.append(dict(name=name,group=cstring(loader.m.u32(item+0x34)),text=cstring(loader.m.u32(record+8)),rect=struct.unpack('>4f',loader.m.read(item+4,16))))
            for i in range(count):
                item = loader.m.u32(table+i*4)
                action = loader.m.u32(item+0x150)
                if action:
                    text = cstring(action)
                    if 'ui_mapname' in text:
                        items.append(dict(index=i, action=text,
                                          x=struct.unpack('>f', loader.m.read(item+4,4))[0],
                                          y=struct.unpack('>f', loader.m.read(item+8,4))[0],
                                          rect_y_expression_count=loader.m.u32(item+0x1b4),
                                          rect_y_expression=loader.m.u32(item+0x1b8),
                                          focus=cstring(loader.m.u32(item+0x158))))
            menus.append(dict(item_count=count, map_buttons=items))
        original_link(cpu)

    loader.stubs[0xdfa28] = link
    started = time.monotonic()
    print('Running native UI loader; this validation can take several minutes.', flush=True)
    loader.load()
    original_rows=inspect_zone(original,profile)
    edited_cards=[(i,e) for i,e in enumerate(rows) if i>=16 or e['map_id']!=original_rows[i]['map_id'] or e['display_name']!=original_rows[i]['display_name'] or e.get('description')]
    from gui_menu_cards import CREDIT,description_text
    for i,e in edited_cards:
        expected={'title':e['display_name'],'description':description_text(e.get('description','')),'picture':'loadscreen_'+e['map_id'],'credit':CREDIT}
        for suffix,text in expected.items():
            match=[c for c in cards if c['name']==f'iw3_card_{i+1}_{suffix}']
            if len(match)!=1 or match[0]['text']!=text:raise ValueError('Native map card text/image differs from requested content.')
    if len(cards)!=4*len(edited_cards):raise ValueError('Native custom card item count differs.')
    if not menus or menus[0]['item_count'] != 175+6*(len(rows)-16)+2+len(cards) or len(menus[0]['map_buttons']) != len(rows):
        raise ValueError('Native menu item/button count differs from the rebuilt graph.')
    for i, item in enumerate(menus[0]['map_buttons'][16:]):
        if rows[16+i]['map_id']+'"' not in item['action'] or item['x'] != 0 or item['rect_y_expression_count'] == 0 or 'iw3_map_scroll' not in item['focus']:
            raise ValueError('A custom map has the wrong action or scrolling expression.')
    if any(used > declared for used, declared in zip(loader.high, loader.sizes)) or loader.unknown:
        raise ValueError('Native loader bounds or unresolved-call check failed.')
    if len(local_lobbies)!=2:raise ValueError('Both local lobbies must be inspected.')
    for preview in previews:
        expected=dict(name=preview['material'],bytes=preview['pixel_bytes'],sha256=preview['pixel_sha256'])
        if expected not in images or preview['material'] not in materials:raise ValueError('Native preview material/pixel readback differs from the selected loading zone.')
    report = dict(cards=cards,local_lobbies=local_lobbies,previews=previews,images=images,materials=materials,passed=True, noop_byte_exact=True, seconds=time.monotonic()-started,
                  consumed=loader.pos, expanded_size=len(zone), high=loader.high,
                  declared=loader.sizes, menus=menus, relocation=relocation,
                  console_startup_verified=False)
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(json.dumps(report, indent=2)+'\n', encoding='utf-8')
    print(f'PASS: {len(rows)} choices, {len(previews)} embedded pictures, scrolling expressions and native block bounds.')


if __name__ == '__main__':
    main()
