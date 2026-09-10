import os
#!/usr/bin/env python3
"""Dump one EBOOT function by entry address."""
import struct,sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import ps3elf, importlib.util
spec=importlib.util.spec_from_file_location('disasm',os.path.join(os.path.dirname(os.path.abspath(__file__)),'ppcdis.py'))
_d=importlib.util.module_from_spec(spec); spec.loader.exec_module(_d)
DATA=ps3elf.DATA; TEXT=0x10240; TEND=0x656478
_o=ps3elf.va2off(TEXT); _n=(TEND-TEXT)//4
W=struct.unpack('>%dI'%_n, DATA[_o:_o+_n*4])
def word(va): return W[(va-TEXT)//4]
ENTRIES=[]
for i,w in enumerate(W):
    if w==0x7C0802A6:
        va=TEXT+i*4
        prev=W[i-1] if i else 0
        # stdu r1,-N(r1) is 0xF821xxxx
        ENTRIES.append(va-4 if (prev&0xFFFF0000)==0xF8210000 else va)
ENTRIES=sorted(set(ENTRIES))
import bisect
def span(entry):
    i=bisect.bisect_left(ENTRIES,entry)
    if i<len(ENTRIES) and ENTRIES[i]==entry:
        return entry, (ENTRIES[i+1] if i+1<len(ENTRIES) else TEND)
    j=bisect.bisect_right(ENTRIES,entry)-1
    return ENTRIES[j], (ENTRIES[j+1] if j+1<len(ENTRIES) else TEND)
if __name__=='__main__':
    a=int(sys.argv[1],0)
    s,e=span(a)
    print('; function %#x .. %#x  (%d instrs)'%(s,e,(e-s)//4))
    print(_d.run(s,e))
