"""Validate native UI expression evaluation and map launch-script dispatch.

C runtime, string storage, thread identity and command execution are stubbed.
The reference ELF's menu parser, conditionals, local variables and expression
operators execute as native PowerPC instructions in the loader interpreter.
"""
from pathlib import Path
import argparse
import os
import sys
import struct
parser=argparse.ArgumentParser(description=__doc__)
parser.add_argument('--ui',required=True,type=Path)
parser.add_argument('--elf',required=True,type=Path)
parser.add_argument('--report',required=True,type=Path)
args=parser.parse_args()
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'backend'))
from gui_emulator import validate_elf
validate_elf(str(args.elf))
os.environ['IW3_PS3_ELF']=str(args.elf.resolve())
sys.path.insert(0,str(ROOT/'backend/tools/ps3_loader_emulator'))
from zoneload import ZoneLoader
from gui_menu_scroll import row_y
l=ZoneLoader(bytes(64));m=l.m;c=l.cpu
def cs(a):
 b=bytearray()
 while m.u8(a):b.append(m.u8(a));a+=1
 return bytes(b)
def concatenate(c):
 dest,n,src=c.r[3:6];s=cs(dest)+cs(src)
 m.write(dest,s[:n-1]+b'\0');c.r[3]=len(s)
l.stubs[0x1e1830]=concatenate
l.stubs[0x1711d8]=lambda c: c.r.__setitem__(3,1)
def copyz(c):
 dest,src,n=c.r[3:6];m.write(dest,cs(src)[:n-1]+b'\0');c.r[3]=dest
l.stubs[0x1e13a8]=copyz
pool={}
def intern(c):
 s=cs(c.r[3]);a=pool.setdefault(s,0x30100000+len(pool)*1024);m.write(a,s+b'\0');c.r[3]=a
l.stubs[0x1d1678]=intern
l.stubs[0x10c00]=lambda c:c.r.__setitem__(3,int(cs(c.r[3]),10))
l.stubs[0x10ab0]=lambda c:c.r.__setitem__(3,len(cs(c.r[3])))
context=0x11d81138;item=0x30000100;script=0x30001000
a=0x30010000
# Evaluate every row at every legal viewport offset with the game's evaluator.
from gui_menu_scroll import thumb_y,focus_scroll
checks=0
for count in (19,32):
 for top in range(count-15):
  m.write(script,f'"setLocalVarInt" "iw3_map_scroll" {top} ; \0'.encode());c.run(0x1be0a0,[context,item,script],maxsteps=300000)
  for index in range(count):
   t=row_y(index);m.w32(a,len(t));m.w32(a+4,a+16)
   for i,(kind,typ,val) in enumerate(t):
    p=a+0x100+i*0x100;m.w32(a+16+4*i,p)
    if isinstance(val,str):m.write(p+16,val.encode()+b'\0');val=p+16
    m.write(p,struct.pack('>3I',kind,typ,val))
   c.run(0x1ac568,[0,a],maxsteps=300000)
   want=34+20*(index-top)+(10000 if not top<=index<top+16 else 0)
   assert c.f[1]==want,(count,top,index,c.f[1],want)
   checks+=1
print('PASS native scrolling positions:',checks)
# Keep native command tokenization/conditional dispatch; stub command execution.
import shlex,re,json
from gui_mapmenu import read_profile,inspect_zone,prepare_edit,START_ACTION
from cod4porter.backend.v4_ffio import read_ps3_fastfile
z=read_ps3_fastfile(args.ui).zone;profile=read_profile(z);rows=inspect_zone(z,profile)
for i in range(16):rows.append(dict(slot=17+i,map_id='mp_'+('x'*58)+f'{i:02}',display_name='Custom '+str(i)))
launch=prepare_edit(z,profile,rows)[0][START_ACTION]
dvars={};queued=[]
def getstring(c):
 name=cs(c.r[3]).decode();val=dvars.get(name,'').encode();addr=0x30200000;m.write(addr,val+b'\0');c.r[3]=addr
def execute(c):
 text=cs(c.r[5]).decode().strip()
 for cmd in text.split(';'):
  bits=shlex.split(cmd)
  if not bits:continue
  if bits[0]=='set':dvars[bits[1]]=' '.join(bits[2:])
  elif bits[0]=='setfromdvar':dvars[bits[1]]=dvars.get(bits[2],'')
  else:queued.append(cmd.strip())
 c.r[3]=0
def enqueue(c):queued.append(cs(c.r[5]).decode().strip());c.r[3]=0
def play(c):
 pointer=c.r[5];start=m.u32(pointer);raw=cs(start);match=re.match(rb'\s*("[^"]*"|[^\s;]+)',raw)
 assert match;m.w32(pointer,start+match.end());c.r[3]=0
l.stubs.update({0x1d5498:getstring,0x15fec0:execute,0x1bd3d0:enqueue,0x8d950:lambda c:c.r.__setitem__(3,0),0x1bf708:play})
launch_results=[]
for selected,want in [('mp_crash','xpartygo'),('mp_creek','xpartygo'),(rows[16]['map_id'],'map '+rows[16]['map_id']),(rows[-1]['map_id'],'map '+rows[-1]['map_id'])]:
 dvars.clear();dvars.update(ui_mapname=selected,ui_gametype='war');queued.clear();m.write(script,launch)
 c.run(0x1be0a0,[context,item,script],maxsteps=500000)
 assert dvars.get('iw3_menu_launch')==want,(selected,dvars,queued)
 assert dvars.get('g_gametype')=='war'
 assert 'wait; wait; vstr iw3_menu_launch' in queued,queued
 launch_results.append(dict(selected=selected,launch=dvars['iw3_menu_launch'],queued=list(queued)))
print('PASS native launch dispatch:',len(launch_results),'script bytes:',len(launch))
# Evaluate the local lobby's edited material expression using native string evaluation.
from gui_menu_lobbies import local_preview_tokens
def boundedformat(c):
 dest,n,fmt=c.r[3:6];pattern=cs(fmt);assert pattern in (b'%s',b'%s%s'),pattern
 result=cs(c.r[6])+(cs(c.r[7]) if pattern==b'%s%s' else b'');m.write(dest,result[:n-1]+b'\0');c.r[3]=len(result)
def finddvar(c):
 name=cs(c.r[3]).decode();ptr=0x30210000
 if name not in dvars:c.r[3]=0;return
 m.write(ptr,bytes(80));m.write(ptr+10,b'\x07');m.w32(ptr+12,ptr+80);m.write(ptr+80,dvars[name].encode()+b'\0');c.r[3]=ptr
l.stubs.update({0x1e0e58:boundedformat,0x1d4e40:finddvar})
picture_results=[]
for selected in ('mp_crash','mp_nuked','mp_getaway','mp_csgo_mirage'):
 dvars['ui_mapname']=selected
 t=local_preview_tokens('loadscreen_', 'ui_mapname');m.w32(a,len(t));m.w32(a+4,a+16)
 for i,(kind,typ,val) in enumerate(t):
  p=a+0x100+i*0x100;m.w32(a+16+4*i,p)
  if isinstance(val,str):m.write(p+16,val.encode()+b'\0');val=p+16
  m.write(p,struct.pack('>3I',kind,typ,val))
 c.run(0x1ac660,[0,a],maxsteps=300000)
 value=cs(c.r[3]).decode()
 assert value=='loadscreen_'+selected,(selected,value)
 picture_results.append(dict(selected=selected,material=value))
print('PASS native local lobby picture lookup:',len(picture_results))
# Native lobby title evaluation must preserve literal names and stock localize keys.
from gui_menu_cards import title_tokens
name_results=[]
for selected in ('Nuketown', 'Getaway', 'Mirage', '@MPUI_CARGOSHIP', 'A'*64):
 dvars['ui_mapname_text']=selected
 t=title_tokens('', 'ui_mapname_text');m.w32(a,len(t));m.w32(a+4,a+16)
 for i,(kind,typ,val) in enumerate(t):
  p=a+0x100+i*0x100;m.w32(a+16+4*i,p)
  if isinstance(val,str):m.write(p+16,val.encode()+b'\0');val=p+16
  m.write(p,struct.pack('>3I',kind,typ,val))
 c.run(0x1ac660,[0,a],maxsteps=300000)
 result=cs(c.r[3]).decode();assert result==selected,(selected,result)
 name_results.append(dict(display_name=selected,result=result))
print('PASS native lobby titles:',len(name_results))
# Execute native StartServer, keeping the real UI parser and handler while
# replacing only Dvar storage, formatting and the engine command-buffer sink.
from gui_mapmenu import LOCAL_START_ACTION
local_launch=prepare_edit(z,profile,rows)[0][LOCAL_START_ACTION]
def setint(c):dvars[cs(c.r[3]).decode()]=str(c.r[4]);c.r[3]=0
def setstring(c):dvars[cs(c.r[3]).decode()]=cs(c.r[4]).decode();c.r[3]=0
def formatmap(c):
 fmt=cs(c.r[3]).decode();assert fmt.strip()=='wait ; wait ; map %s',repr(fmt)
 text=fmt % cs(c.r[4]).decode();ptr=0x30300000;m.write(ptr,text.encode()+b'\0');c.r[3]=ptr
native_queued=[]
def native_queue(c):native_queued.append(cs(c.r[4]).decode().strip());c.r[3]=0
l.stubs.update({0x1d92d0:setint,0x1d8e78:setstring,0x1e0c80:formatmap,0x15ec18:native_queue})
local_results=[]
for selected in ('mp_crash','mp_nuked','mp_getaway','mp_csgo_mirage'):
 dvars.clear();dvars.update(ui_mapname=selected,ui_gametype='war');queued.clear();native_queued.clear()
 for offset,base,text in ((0x1e780,0x30400000,'war'),(0x1e784,0x30401000,selected)):
  m.w32(context+offset,base);m.w32(base+12,base+64);m.write(base+64,text.encode()+b'\0')
 m.write(script,local_launch);c.run(0x1be0a0,[context,item,script],maxsteps=500000)
 assert native_queued==['wait ; wait ; map '+selected],native_queued
 assert dvars.get('g_gametype')=='war' and dvars.get('cg_thirdPerson')=='0',dvars
 assert 'echo IW3MENU_LOCAL_BEGIN' in queued and 'echo IW3MENU_LOCAL_HANDLER_RETURNED' in queued,queued
 local_results.append(dict(selected=selected,native_commands=list(native_queued),diagnostics=list(queued)))
print('PASS native local StartServer:',len(local_results))
args.report.parent.mkdir(parents=True,exist_ok=True)
args.report.write_text(json.dumps(dict(passed=True,position_checks=checks,local_title_checks=name_results,local_picture_checks=picture_results,local_startserver_checks=local_results,launch_checks=launch_results,script_bytes=len(launch),stubs='C runtime, string storage, thread identity and command execution; expression evaluator and UI script parser execute native instructions.',hardware_verified=False),indent=2)+'\n')
