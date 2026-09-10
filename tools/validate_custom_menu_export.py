import os,sys,json,struct,time,argparse
from pathlib import Path
parser=argparse.ArgumentParser(description='Replay a Custom Maps export through the reference PS3 native loader.')
parser.add_argument('--ui',required=True,type=Path)
parser.add_argument('--elf',required=True,type=Path)
parser.add_argument('--report',required=True,type=Path)
args=parser.parse_args()
root=Path(__file__).resolve().parents[1]/'backend';sys.path.insert(0,str(root))
from gui_emulator import validate_elf
validate_elf(args.elf)
os.environ['IW3_PS3_ELF']=str(args.elf.resolve());sys.path.insert(0,str(root/'tools/ps3_loader_emulator'))
from zoneload import ZoneLoader
from cod4porter.backend.v4_ffio import read_ps3_fastfile
z=read_ps3_fastfile(args.ui).zone
l=ZoneLoader(z,verbose=True);old=l.stubs[0xdfa28];menus={};loc={}
def string(p):return l.m.read(p,8192).split(b'\0')[0].decode('ascii','replace') if p else ''
def link(cpu):
 typ=cpu.r[4]&0xffffffff;p=cpu.r[5]&0xffffffff
 if typ==24:loc[string(l.m.u32(p+4))]=string(l.m.u32(p))
 if typ==23:
  name=string(l.m.u32(p))
  if name in ('settings_map','settings_custom_map'):
   n=l.m.u32(p+0xb0);a=l.m.u32(p+0x130);items=[]
   for i in range(n):
    item=l.m.u32(a+4*i);action=string(l.m.u32(item+0x150));name0=string(l.m.u32(item))
    text='';count=l.m.u32(item+0x19c);ptr=l.m.u32(item+0x1a0)
    if count==2:
     record=l.m.u32(ptr+4)
     if l.m.u32(record)==1 and l.m.u32(record+4)==2:text=string(l.m.u32(record+8))
    if action or text or name0.startswith('iw3_card_'):items.append(dict(index=i,name=name0,action=action,text=text))
   menus[name]=dict(count=n,items=items)
 old(cpu)
l.stubs[0xdfa28]=link
t=time.monotonic();l.load()
assert not l.unknown and all(a<=b for a,b in zip(l.high,l.sizes))
assert struct.unpack_from('>I',z)[0]==l.pos-36
assert set(menus)=={'settings_map','settings_custom_map'}
main=menus['settings_map']['items'];custom=menus['settings_custom_map']['items']
assert sum('setdvar" "ui_mapname"' in i['action'] for i in main)==16
custom_choices=[i for i in custom if 'setdvar" "ui_mapname"' in i['action']]
assert 1<=len(custom_choices)<=16
assert sum('"open" "settings_custom_map"' in i['action'] for i in main)==1
assert sum(i['name'].startswith('iw3_card_') for i in custom)==4*len(custom_choices)
assert any(i['text']=='^3Custom Maps^7' for i in custom)
r=dict(passed=True,menus=menus,localization=loc,consumed=l.pos,high=l.high,declared=l.sizes,assets=l.assets,seconds=time.monotonic()-t,hardware_verified=False)
args.report.write_text(json.dumps(r,indent=2));print('PASS',r['seconds'],flush=True)
