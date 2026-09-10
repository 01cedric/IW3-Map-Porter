#!/usr/bin/env python3
"""Compare the per-field shape of every loaded array between two zones of the same map."""
import os
import sys, json, struct, collections
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from zoneload import ZoneLoader
from stride import stride_at

def collect(path):
    z=open(path,'rb').read()
    L=ZoneLoader(z); L.trace=[]
    L.load()
    recs=[]
    for site,pos,size,blk,ptr in L.trace:
        recs.append((site,pos,size,blk))
    return z,recs

def kind(v, raw):
    if v==0xFFFFFFFF: return 'F'
    if v==0xFFFFFFFE: return 'I'
    if v==0: return 'Z'
    if 0x20000000<=v and (v>>29)<7 and (v&0x1FFFFFFF)<0x08000000: return 'P'
    f=struct.unpack('>f',raw)[0]
    if f==f and abs(f)!=float('inf') and (1e-8<abs(f)<1e9): return 'f'
    return 'V'

def profile(z, pos, size, stride):
    """Per-lane kind histogram across the array elements."""
    if stride<4 or size<stride: return None
    n=size//stride
    if n==0: return None
    n=min(n,20000)
    lanes=stride//4
    prof=[collections.Counter() for _ in range(lanes)]
    mx=[0]*lanes
    for i in range(n):
        b=pos+i*stride
        for j in range(lanes):
            raw=z[b+j*4:b+j*4+4]
            if len(raw)<4: break
            v=struct.unpack('>I',raw)[0]
            prof[j][kind(v,raw)]+=1
            if v not in (0xFFFFFFFF,0xFFFFFFFE) and v>mx[j]: mx[j]=v
    return [set(c) for c in prof], n, mx

def main(a,b):
    za,ra=collect(a); zb,rb=collect(b)
    ga=collections.defaultdict(list); gb=collections.defaultdict(list)
    for s,p,sz,bl in ra: ga[s].append((p,sz,bl))
    for s,p,sz,bl in rb: gb[s].append((p,sz,bl))
    sites=sorted(set(ga)|set(gb))
    print('sites: ours %d retail %d common %d'%(len(ga),len(gb),len(set(ga)&set(gb))))
    print('only in ours  :',[hex(s) for s in sorted(set(ga)-set(gb))])
    print('only in retail:',[hex(s) for s in sorted(set(gb)-set(ga))])
    issues=0
    for s in sorted(set(ga)&set(gb)):
        st=stride_at(s)
        if not st: continue
        if st[0]=='const':
            if st[1]<8: continue
            stride=st[1]
        elif st[1]<4: continue
        else: stride=st[1]
        pa=[]; pb=[]; ma=[]; mb=[]
        for p,sz,bl in ga[s][:60]:
            if bl in (1,2,3): continue
            r=profile(za,p,sz,stride)
            if r: pa.append(r[0]); ma.append(r[2])
        for p,sz,bl in gb[s][:60]:
            if bl in (1,2,3): continue
            r=profile(zb,p,sz,stride)
            if r: pb.append(r[0]); mb.append(r[2])
        if not pa or not pb: continue
        lanes=stride//4
        for j in range(lanes):
            ka=set().union(*[x[j] for x in pa]); kb=set().union(*[x[j] for x in pb])
            xa=max(x[j] for x in ma); xb=max(x[j] for x in mb)
            ptr_a = bool(ka & {'P','F','I'}); ptr_b = bool(kb & {'P','F','I'})
            flag=None
            if ptr_a!=ptr_b: flag='pointerness'
            elif not (ka & kb): flag='disjoint'
            elif ('f' in ka)!=('f' in kb) and ('V' in (ka|kb)): flag='float/int'
            elif xa and xb and (xa>1000*max(xb,1) or xb>1000*max(xa,1)): flag='magnitude'
            if flag:
                print('  site %#x stride %3d lane +%02X [%-11s]: ours %-16s max %-12d retail %-16s max %d'
                      %(s,stride,j*4,flag,''.join(sorted(ka)),xa,''.join(sorted(kb)),xb))
                issues+=1
    print('lane shape mismatches:',issues)

if __name__=='__main__':
    main(sys.argv[1],sys.argv[2])
