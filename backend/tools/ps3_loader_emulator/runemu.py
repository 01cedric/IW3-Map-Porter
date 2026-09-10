import os
import sys, json
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from zoneload import ZoneLoader
path=sys.argv[1]
z=open(path,'rb').read()
L=ZoneLoader(z)
ok=False
try:
    rows=L.load(); ok=True
    print('OK assets',len(rows),'pos %#x / %#x'%(L.pos,len(z)))
except Exception as e:
    print('FAILED:',type(e).__name__,e)
    rows=getattr(L,'assets',[])
    print('file pos %#x of %#x'%(L.pos,len(z)),'assets completed',len(rows))
    if rows:
        last=rows[-1]
        print('last completed asset:',last['index'],last['name'],'file %#x..%#x'%(last['file_start'],last['file_end']))
    bnd=None
    for site,tgt in reversed(L.cpu.calls):
        if 0xb0000<=site<0xe1000 and not (0xb0000<=tgt<0xe1000):
            bnd=(site,tgt); break
    print('boundary call:',(hex(bnd[0]),hex(bnd[1])) if bnd else None)
    print('calls tail:',[(hex(a),hex(b)) for a,b in L.cpu.calls[-6:]])
json.dump({'ok':ok,'assets':rows,'high':L.high,'sizes':L.sizes,'pos':L.pos,'len':len(z),
           'deferred':getattr(L,'deferred_count',0),'deferred_bytes':getattr(L,'deferred_bytes',0)},
          open(path+'.emu.json','w'))
