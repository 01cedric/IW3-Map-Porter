#!/usr/bin/env python3
"""Recover the per-element stride used at a Load_Stream call site (r5 = f(count))."""
import os
import struct,sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import ps3elf
DATA=ps3elf.DATA; TEXT=0x10240; TEND=0x656478
o=ps3elf.va2off(TEXT); n=(TEND-TEXT)//4
W=struct.unpack('>%dI'%n, DATA[o:o+n*4])
def word(va):
    i=(va-TEXT)//4
    return W[i] if 0<=i<len(W) else 0
def stride_at(site, back=14):
    """Return ('const', N) or ('stride', N) or None."""
    terms={}          # reg -> multiple of count (r4)
    consts={}
    for i in range(back,0,-1):
        w=word(site-i*4); op=w>>26
        if op==14:      # addi
            d=(w>>21)&31; a=(w>>16)&31; si=w&0xFFFF
            si=si-0x10000 if si&0x8000 else si
            if a==0: consts[d]=si; terms.pop(d,None)
            else: terms.pop(d,None); consts.pop(d,None)
        elif op==7:     # mulli
            d=(w>>21)&31; a=(w>>16)&31; si=w&0xFFFF
            si=si-0x10000 if si&0x8000 else si
            base = 1 if a==4 else terms.get(a)
            if base: terms[d]=base*si; consts.pop(d,None)
            else: terms.pop(d,None)
        elif op==21:    # rlwinm rA,rS,sh,mb,me  (slwi)
            rs=(w>>21)&31; ra=(w>>16)&31; sh=(w>>11)&31; mb=(w>>6)&31; me=(w>>1)&31
            base = 1 if rs==4 else terms.get(rs)
            if base is not None and mb==0 and me==31-sh:
                terms[ra]=base*(1<<sh); consts.pop(ra,None)
            else:
                terms.pop(ra,None); consts.pop(ra,None)
        elif op==31:
            xo=(w>>1)&0x3FF; d=(w>>21)&31; a=(w>>16)&31; b=(w>>11)&31
            if xo==40:       # subf rD = rB - rA
                x=terms.get(b,1 if b==4 else None); y=terms.get(a,1 if a==4 else None)
                if x is not None and y is not None: terms[d]=x-y
                else: terms.pop(d,None)
            elif xo==266:    # add
                x=terms.get(a,1 if a==4 else None); y=terms.get(b,1 if b==4 else None)
                if x is not None and y is not None: terms[d]=x+y
                else: terms.pop(d,None)
            elif xo==986:    # extsw rA,rS
                base=terms.get(d,1 if d==4 else None)
                if base is not None: terms[a]=base
                else: terms.pop(a,None)
            elif xo==444:    # or/mr
                base=terms.get(d,1 if d==4 else None)
                if d==b and base is not None: terms[a]=base
                else: terms.pop(a,None)
            elif xo==235:    # mullw
                x=terms.get(a,1 if a==4 else None); y=consts.get(b)
                if x is not None and y is not None: terms[d]=x*y
                else: terms.pop(d,None)
            else:
                terms.pop(d,None)
    if 5 in terms: return ('stride',terms[5])
    if 5 in consts: return ('const',consts[5])
    return None
if __name__=='__main__':
    for a in sys.argv[1:]:
        print(a, stride_at(int(a,0)))
