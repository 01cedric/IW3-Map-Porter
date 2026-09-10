#!/usr/bin/env python3
from __future__ import annotations

from pathlib import Path
import sys

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))

from cod4porter.zone_writer import RelocatingZoneWriter,TEMP_BLOCK


def main()->None:
    w=RelocatingZoneWriter()
    w.push_block(TEMP_BLOCK)
    w.reserve_in_block(0x20,'parent asset root')
    parent_before=w.current_location

    starts=[]
    for size in (0x30,0x10):
        w.push_block(TEMP_BLOCK)
        starts.append(w.current_location)
        w.reserve_in_block(size,'nested sibling asset')
        w.pop_block()
        assert w.current_location==parent_before

    assert starts[0]==starts[1]
    w.reserve_in_block(0x08,'parent member after nested assets')
    assert w.current_location.offset==0x28
    w.pop_block()

    out=w.build()
    assert out.block_sizes[TEMP_BLOCK]==0x50
    print({'passed':True,'sibling_start':starts[0].offset,'parent_end':0x28,'temp_highwater':out.block_sizes[TEMP_BLOCK]})


if __name__=='__main__':main()
