"""Address helpers for the extracted CoD4 PS3 EBOOT."""
import re, struct, subprocess
import os
# The decrypted CoD4 PS3 EBOOT, as an ELF.  Override with IW3_PS3_ELF.
ELF=os.environ.get('IW3_PS3_ELF') or os.path.join(os.path.dirname(os.path.abspath(__file__)),'eboot.elf')
if not os.path.exists(ELF):
    raise SystemExit('EBOOT not found: %s\nSet IW3_PS3_ELF to the decrypted EBOOT (ELF form).'%ELF)
DATA=open(ELF,'rb').read()
SEGS=[(0x000000,0x66D5B8,0x10000),(0x670000,0x5440C,0x680000),
      (0x6D0000,0xD5C80,0x10000000),(0x7B0000,0x106780,0x100E0000)]
def va2off(va):
    for off,size,base in SEGS:
        if base<=va<base+size: return off+(va-base)
    return None
def off2va(off):
    for o,size,base in SEGS:
        if o<=off<o+size: return base+(off-o)
    return None
def read(va,n): 
    o=va2off(va); return DATA[o:o+n] if o is not None else b''
def u32(va): return struct.unpack('>I',read(va,4))[0]
def u64(va): return struct.unpack('>Q',read(va,8))[0]
def strings(minlen=5):
    out={}
    for m in re.finditer(rb'[ -~]{%d,}'%minlen, DATA):
        va=off2va(m.start())
        if va is not None: out[va]=m.group().decode('latin1')
    return out
def disasm(va,n=64):
    o=va2off(va)
    p=subprocess.run(['llvm-objdump','-d','--triple=powerpc64','--start-address=%d'%va,
                      '--stop-address=%d'%(va+n*4),ELF],capture_output=True,text=True)
    return p.stdout

def build_const_index(text_va=0x10240, text_end=0x656478):
    """Map every 32-bit constant the code materializes with lis/addi|ori to its code sites."""
    idx={}
    o=va2off(text_va); n=(text_end-text_va)//4
    words=struct.unpack('>%dI'%n, DATA[o:o+n*4])
    pend={}
    for i,w in enumerate(words):
        op=w>>26
        va=text_va+i*4
        if op==15:                      # addis
            rD=(w>>21)&31; rA=(w>>16)&31; simm=w&0xFFFF
            if rA==0: pend[rD]=(simm,va)
            else: pend.pop(rD,None)
        elif op==14:                    # addi
            rD=(w>>21)&31; rA=(w>>16)&31; simm=w&0xFFFF
            if rA in pend and rA==rD:
                hi,hva=pend.pop(rD)
                lo=simm-0x10000 if simm&0x8000 else simm
                idx.setdefault(((hi<<16)+lo)&0xFFFFFFFF,[]).append(hva)
            else: pend.pop(rD,None)
        elif op==24:                    # ori
            rS=(w>>21)&31; rA=(w>>16)&31; uimm=w&0xFFFF
            if rS in pend and rS==rA:
                hi,hva=pend.pop(rS)
                idx.setdefault(((hi<<16)|uimm)&0xFFFFFFFF,[]).append(hva)
            else: pend.pop(rA,None)
        else:
            rD=(w>>21)&31
            if op in (31,63,32,36,58,62,34,38,40,44,48,52,54,50):
                pend.pop(rD,None)
    return idx

TOC_BASE=0x6C6988
def build_toc_index(text_va=0x10240, text_end=0x656478):
    """disp(r2) -> [code VAs].  PPC64 r2-relative loads reach the 4-byte pointer table."""
    o=va2off(text_va); n=(text_end-text_va)//4
    words=struct.unpack('>%dI'%n, DATA[o:o+n*4])
    idx={}
    for i,w in enumerate(words):
        op=w>>26
        if op in (32,58,40,34):          # lwz / ld / lhz / lbz
            rA=(w>>16)&31
            if rA==2:
                d=w&0xFFFF
                if d&0x8000: d-=0x10000
                idx.setdefault(d,[]).append(text_va+i*4)
    return idx

def toc_refs(target_va, tocidx):
    """Every code site that loads a TOC entry whose stored value is target_va."""
    out=[]
    pat=struct.pack('>I',target_va)
    p=va2off(0x6BE988); end=va2off(0x6D4408)
    while True:
        h=DATA.find(pat,p,end)
        if h<0: break
        tva=off2va(h); d=tva-TOC_BASE
        out.extend(tocidx.get(d,[]))
        p=h+1
    return sorted(set(out))
