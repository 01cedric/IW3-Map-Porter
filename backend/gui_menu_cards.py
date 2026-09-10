"""Custom map cards and lobby titles using native menu items and expressions."""
from gui_ui_layout import layout_value
import struct
import textwrap
from gui_menu_scroll import expression_events
from gui_mapmenu_profile import SLOTS

CREDIT = "Ported by 01cedric's IW3MapPorter"
TITLE_TEMPLATE = 0x14BDF8C
DESCRIPTION_TEMPLATE = 0x14BE248
IMAGE_TEMPLATE = 0x14BE4FA
MENU_ROOT = 0x14B9975
ITEM_ARRAY = 0x14B9B21
LOCAL_TITLES = (
    (0x14A8244, (0x14A8470,0x14A847C,0x14A8488,0x14A8494,0x14A84A0,0x14A84BD,0x14A84C9,0x14A84D5,0x14A84E1,0x14A84ED,0x14A84F9,0x14A8505,0x14A8511,0x14A851D)),
    (0x14B203C, tuple(0x14B2268+12*i for i in range(14))),
)


def description_text(value):
    value = str(value or '').replace('\r\n','\n').replace('\r','\n').strip()
    if len(value)>160 or any(c!='\n' and not 32<=ord(c)<=126 for c in value) or '^' in value:
        raise ValueError('Map descriptions support up to 160 plain English characters and line breaks, without color codes.')
    lines=[]
    for paragraph in value.split('\n'):
        lines.extend(textwrap.wrap(paragraph,width=40,break_long_words=True,break_on_hyphens=False) or [''])
    if len(lines)>4:raise ValueError('Map description needs more than four lines. Shorten it or remove extra line breaks.')
    return '\n'.join(lines)


def title_tokens(empty, name):
    return [(0,16,0),(1,2,empty),(0,5,0),(0,16,0),(1,2,empty),
            (0,5,0),(0,16,0),(0,16,0),(0,31,0),(1,2,name),
            (0,1,0),(0,1,0),(0,1,0),(0,1,0)]


def inline_string(value):
    return [[2,0],[5,(value.encode('ascii')+b'\0').hex()]]


def card_item(zone, template, name, group, text, field, *, credit=False, title=False):
    header=bytearray(zone[template:template+468])
    if struct.unpack_from('>I',header)[0]!=0xFFFFFFFF or struct.unpack_from('>I',header,0x34)[0]!=0xFFFFFFFF:
        raise ValueError('Map-card template must have inline item and group names.')
    tokens=[(0,16,0),(1,2,text)]
    struct.pack_into('>2I',header,field,2,0xFFFFFFFF)
    if template==layout_value(zone, DESCRIPTION_TEMPLATE):
        for base in (4,0x1c):
            struct.pack_into('>4f',header,base,8,160 if credit else 60,262,18 if credit else 80)
        if credit:struct.pack_into('>f',header,0x124,0.28)
    if title:struct.pack_into('>f',header,0x124,0.375*min(1,26/max(len(text),1)))
    events=[[2,3],[5,header.hex()]]+inline_string(name)+inline_string(group)+expression_events(tokens)
    return dict(events=events,replacements={},fixups=[],kind='map-card')


def fix_lobby_titles(zone, profile, entries, replacements, inserts):
    # Reuse an existing empty XString. The local system-link spelling of the
    # CSV is inline and unshared; shrinking that one payload cannot alter a
    # different table lookup. Other packed strings retain their original data.
    events=profile['events']
    empty=next(e for e in events if e[0]==3 and e[3]==1 and zone[e[2]:e[2]+1]==b'\0')
    empty_raw=0x80000001+empty[1]-0x60000000
    for header_pos, positions in layout_value(zone, LOCAL_TITLES):
        if struct.unpack_from('>2I',zone,positions[3])!=(0,56):
            raise ValueError('Local lobby title lookup differs from its verified profile.')
        tokens=title_tokens(empty_raw,0xFFFFFFFF)
        if struct.unpack_from('>I',zone,positions[4]+8)[0]==0xFFFFFFFF:
            inline=next(e for e in events if e[0]==3 and e[2]==positions[4]+12)
            raw=0x80000001+inline[1]-0x60000000
            if any(f[1]==raw for f in profile['fixups']):
                raise ValueError('Local title table name is shared and needs a different relocation profile.')
            tokens[4]=(1,2,0xFFFFFFFF)
            replacements[inline[2]]=b'\0'
        for pos,token in zip(positions,tokens):replacements[pos]=struct.pack('>3I',*token)
        end=next(i+1 for i,e in enumerate(events) if e[0]==3 and e[2]==positions[9])
        inserts.setdefault(end,[]).append(dict(events=inline_string('ui_mapname_text'),replacements={},fixups=[],kind='title-string'))
        header=bytearray(replacements.get(header_pos,zone[header_pos:header_pos+468]))
        scale=0.375*min(1,26/max(len(str(e.get('display_name',''))) for e in entries))
        struct.pack_into('>f',header,0x124,scale);replacements[header_pos]=bytes(header)


def add_map_cards(zone, profile, entries, current, replacements, inserts, clones):
    cards=[]
    row_clones=[c for c in clones if c.get('kind','row')=='row']
    for i,entry in enumerate(entries):
        description=description_text(entry.get('description',''))
        changed=i>=16 or entry['map_id']!=current[i]['map_id'] or entry['display_name']!=current[i]['display_name'] or bool(description)
        if not changed:continue
        slot=i+1;name=str(entry['display_name']);map_id=str(entry['map_id'])
        stem=f'iw3_card_{slot}'
        for suffix,template,group,text,field in (
            ('title',layout_value(zone, TITLE_TEMPLATE),'map_name_group',name,0x19c),
            ('description',layout_value(zone, DESCRIPTION_TEMPLATE),'map_desc_group',description,0x19c),
            ('picture',layout_value(zone, IMAGE_TEMPLATE),'map_image_group','loadscreen_'+map_id,0x1a4),
            ('credit',layout_value(zone, DESCRIPTION_TEMPLATE),'map_desc_group',CREDIT,0x19c)):
            cards.append(card_item(zone,template,stem+'_'+suffix,group,text,field,credit=suffix=='credit',title=suffix=='title'))
        show=''.join(f'"show" "{stem}_{suffix}" ; ' for suffix in ('title','description','picture','credit')).encode()
        if i<16:
            hp=layout_value(zone, SLOTS)[i][3]
            hover=replacements[hp].rstrip(b'\0')
            # An unchanged stock row with an edited description still has its
            # stock show commands. Hide that card before showing the new one.
            hover+=b'"hide" "map_image_group" ; "hide" "map_name_group" ; "hide" "map_desc_group" ; '
            replacements[hp]=hover+show+b'\0'
        else:
            cr=row_clones[i-16]['replacements'];cr[layout_value(zone, 0x14BDE14)]=cr[layout_value(zone, 0x14BDE14)].rstrip(b'\0')+show+b'\0'
    if not cards:return
    header=bytearray(replacements.get(layout_value(zone, MENU_ROOT),zone[layout_value(zone, MENU_ROOT):layout_value(zone, MENU_ROOT)+308]))
    old_count=struct.unpack_from('>I',header,0xb0)[0]
    struct.pack_into('>I',header,0xb0,old_count+len(cards));replacements[layout_value(zone, MENU_ROOT)]=bytes(header)
    replacements[layout_value(zone, ITEM_ARRAY)]=replacements.get(layout_value(zone, ITEM_ARRAY),zone[layout_value(zone, ITEM_ARRAY):layout_value(zone, ITEM_ARRAY)+4*175])+b'\xff'*(4*len(cards))
    clones.extend(cards)
    fix_lobby_titles(zone,profile,entries,replacements,inserts)
