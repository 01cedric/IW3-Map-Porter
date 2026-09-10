"""Bounded IW3 PC TechniqueSet member replay for FX ownership corridors.

This reads owned shader bytecode to advance the source block-4 cursor. It is
not a PC-to-RSX compiler. Unsupported pointer/declaration layouts fail closed.
"""
import struct
from .backend.v4_ffio import decode_pc_pointer


def replay_technique_members(zone, technique, member_start, physical_limit):
    physical=technique.root_offset+0x94
    cursor=member_start
    def take(size,alignment=1):
        nonlocal physical,cursor
        if size<0 or physical+size>physical_limit:
            raise ValueError('PC TechniqueSet member exceeds physical corridor')
        data=zone[physical:physical+size]
        physical+=size;cursor=(cursor+alignment-1)&~(alignment-1);cursor+=size
        return data
    def kind(raw):
        value=decode_pc_pointer(raw).kind
        if value not in ('null','packed','following'):
            raise ValueError(f'Unsupported PC TechniqueSet member pointer: {value}')
        return value
    def string(raw):
        if kind(raw)!='following':return
        end=zone.find(b'\0',physical,min(physical_limit,physical+1024))
        if end<0:raise ValueError('Unterminated PC TechniqueSet member name')
        take(end-physical+1)
    def word(data,offset=0):return struct.unpack_from('<I',data,offset)[0]
    string(word(zone,technique.root_offset))
    if kind(technique.remapped_pointer)=='following':
        raise ValueError('Owned remapped PC TechniqueSet is unsupported in corridor')
    for raw in technique.technique_pointers:
        if kind(raw)!='following':continue
        root=take(8,4);count=struct.unpack_from('<H',root,6)[0]
        if not 1<=count<=64:raise ValueError('Invalid PC MaterialTechnique pass count')
        table=take(count*20)
        for i in range(count):
            entry=table[i*20:(i+1)*20]
            if kind(word(entry))=='following':
                raise ValueError('Inline PC vertex declaration is unsupported in corridor')
            for offset,stage in ((4,0xfffe),(8,0xffff)):
                if kind(word(entry,offset))!='following':continue
                shader=take(16,4)
                string(word(shader))
                if kind(word(shader,8))=='following':
                    words=struct.unpack_from('<H',shader,12)[0]
                    if words<2:raise ValueError('Invalid PC shader program length')
                    program=take(words*4,4)
                    version=word(program)
                    if version>>16!=stage or program[-4:]!=b'\xff\xff\x00\x00':
                        raise ValueError('Invalid PC shader program header/terminator')
            argc=sum(entry[12:15]);argkind=kind(word(entry,16))
            if argkind=='following':
                args=take(argc*8,4)
                for j in range(argc):
                    typ=struct.unpack_from('<H',args,j*8)[0]
                    if typ>7:raise ValueError('Invalid PC shader argument type')
                    if typ in (1,7) and kind(word(args,j*8+4))=='following':take(16,4)
            elif argc and argkind=='null':raise ValueError('Missing PC shader arguments')
        string(word(root))
    if physical!=physical_limit:
        raise ValueError(f'PC TechniqueSet physical replay ends at 0x{physical:X}, expected 0x{physical_limit:X}')
    return cursor
