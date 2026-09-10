#!/usr/bin/env python3
"""Disassemble the CoD4 PS3 EBOOT with TOC string/data annotations."""
import os
import re, struct, subprocess, sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import ps3elf

STR=ps3elf.strings(4)
TOCL=re.compile(r'^\s*([0-9a-f]+):\s+(?:[0-9a-f]{2} ){4}\s*(lwz|ld|lbz|lhz)\s+(\d+), (-?\d+)\(2\)')

def annotate(line):
    m=TOCL.match(line)
    if not m: return line
    d=int(m.group(4))
    tva=ps3elf.TOC_BASE+d
    raw=ps3elf.read(tva,4)
    if len(raw)!=4: return line
    val=struct.unpack('>I',raw)[0]
    note=''
    if val in STR: note=' ; "%s"'%STR[val][:70]
    elif 0x10000<=val<0x700000 or 0x10000000<=val<0x13000000: note=' ; -> 0x%X'%val
    return line.rstrip()+note

def run(start,end):
    p=subprocess.run(['llvm-objdump','-d','--triple=powerpc64',
                      '--start-address=%d'%start,'--stop-address=%d'%end,ps3elf.ELF],
                     capture_output=True,text=True)
    out=[]
    for l in p.stdout.splitlines():
        if not re.match(r'^\s*[0-9a-f]+:',l): continue
        out.append(annotate(l.replace('\t',' ')))
    return '\n'.join(out)

if __name__=='__main__':
    a=int(sys.argv[1],0); b=int(sys.argv[2],0)
    print(run(a,b))
