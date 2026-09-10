from __future__ import annotations

import struct
import sys
import tempfile
from pathlib import Path

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))

from cod4porter.backend.v4_ffio import decode_ps3_pointer, encode_pc_packed
from cod4porter.assets.basic import LightDefNode,RawFileNode,StringTableNode
from cod4porter.graph import write_graph
from cod4porter.graph import AssetType, nested_symbol
from cod4porter.main_ff import save_main_fastfile
from cod4porter.plan import DispositionKind,PortPlan,SourceDisposition
from cod4porter.readback import inspect_main_fastfile
from cod4porter.source_assets import (
    SourceStringTable,
    _parse_rawfile_at,
    packed_xstring_evidence_from_tables,
)
from cod4porter.world_core import ComWorldNode, parse_comworld
from cod4porter.zone_writer import FOLLOWING, INSERT


def _pc_getaway_comworld_fixture() -> tuple[bytes, str]:
    name='maps/mp/mp_getaway.d3dbsp'
    count=102
    root=bytearray(0x10+len(name)+1+count*0x44+len('light_point_linear')+1)
    struct.pack_into('<IiiI',root,0,0xFFFFFFFF,1,count,0xFFFFFFFF)
    cursor=0x10
    root[cursor:cursor+len(name)+1]=name.encode('latin-1')+b'\0';cursor+=len(name)+1
    for index in range(count):
        light=cursor+index*0x44
        root[light]=2
        if index==2:raw=0xFFFFFFFF
        elif index>=3:raw=encode_pc_packed(4,0x40968C)
        else:raw=0
        struct.pack_into('<I',root,light+0x40,raw)
    cursor+=count*0x44
    root[cursor:]=b'light_point_linear\0'
    return bytes(root),name


def test_comworld_reuses_one_block4_xstring() -> None:
    source,name=_pc_getaway_comworld_fixture()
    com=parse_comworld(source,name,102)
    assert com.packed_def_name_count==99
    assert com.resolved_packed_def_name_count==99
    assert [x.def_name for x in com.primary_lights[2:]]==['light_point_linear']*100

    result=write_graph([ComWorldNode(com,'com:getaway')])
    row=result.context.results['comworld:com:getaway']
    root=row['root_physical'];light_base=root+0x10+len(name)+1
    words=[struct.unpack_from('>I',result.zone.zone_bytes,light_base+i*0x44+0x40)[0] for i in range(102)]
    assert words[:2]==[0,0]
    assert words[2]==FOLLOWING
    assert len(set(words[3:]))==1
    target=decode_ps3_pointer(words[3],result.zone.block_sizes)
    assert target.kind=='packed' and target.block==4
    assert all(decode_ps3_pointer(x,result.zone.block_sizes)==target for x in words[3:])
    assert row['packed_def_name_relocations']==99
    assert result.zone.zone_bytes[light_base+102*0x44:light_base+102*0x44+19]==b'light_point_linear\0'


def test_lightdef_external_attenuation_shell_and_alignment() -> None:
    node=LightDefNode(
        'light_point_linear',0x62,1,None,'lightdef:getaway',
        attenuation_external_image_name='falloff_linear',
        attenuation_external_pointer_kind='insert',
    )
    result=write_graph([node])
    row=result.context.results['lightdef:lightdef:getaway'];z=result.zone.zone_bytes
    root=row['root_physical'];shell=row['attenuation_external_root_physical'];name=row['attenuation_external_name_physical']
    assert struct.unpack_from('>I',z,root+4)[0]==INSERT
    assert shell==root+0x10+len('light_point_linear')+1  # logical-only alignment adds no FastFile bytes
    assert row['attenuation_external_root_location'].block==0
    assert row['attenuation_external_root_location'].offset==0x10
    assert z[shell:shell+0x30]==b'\0'*0x30
    assert struct.unpack_from('>I',z,shell+0x30)[0]==FOLLOWING
    assert name==shell+0x34
    assert z[name:name+16]==b',falloff_linear\0'
    assert result.zone.block_sizes[0]==0x44  # parent root + nested image root high-water
    alias_symbol=nested_symbol(node.symbol,'attenuation.insertAlias')
    alias=result.zone.symbols[alias_symbol]
    assert alias.block==4 and alias.offset%4==0
    assert result.zone.block_sizes[4]>=alias.offset+4  # persistent INSERT alias cell


def test_rawfile_766_inherits_exact_mapstable_xstring_identity() -> None:
    packed=0x4048EBF1
    table=SourceStringTable(
        505,0,'mp/mapstable.csv',1,1,('mp_getaway',),1,1,0,0,(packed,),
    )
    evidence=packed_xstring_evidence_from_tables((table,))
    assert evidence=={packed:'mp_getaway'}
    fallback=SourceStringTable(505,0,'x.csv',1,1,('__runtime_fallback',),1,0,0,0,(0x40000101,))
    object.__setattr__(fallback,'runtime_fallback_raws',(0x40000101,))
    assert packed_xstring_evidence_from_tables((fallback,))=={}

    zone=bytearray(13)
    struct.pack_into('<III',zone,0,packed,0,0xFFFFFFFF)
    zone[12]=0
    raw=_parse_rawfile_at(bytes(zone),0,766,evidence,True)
    assert raw.name=='mp_getaway'
    assert not raw.name_runtime_fallback
    assert raw.payload==b'' and raw.physical_end==13

    table_node=StringTableNode('mp/mapstable.csv',1,1,('mp_getaway',),'stringtable:505')
    raw_node=RawFileNode('mp_getaway',b'','rawfile:766',table_node.cell_symbol(0))
    result=write_graph([table_node,raw_node])
    rr=result.context.results['rawfile:rawfile:766'];name_word=struct.unpack_from('>I',result.zone.zone_bytes,rr['root_physical'])[0]
    target=decode_ps3_pointer(name_word,result.zone.block_sizes)
    assert target.kind=='packed' and target.block==4
    symbol=result.zone.symbols[table_node.cell_symbol(0)]
    assert (symbol.block,symbol.offset)==(target.block,target.offset)
    assert rr['name_physical']==-1


def test_combined_independent_readback() -> None:
    source,name=_pc_getaway_comworld_fixture()
    com_node=ComWorldNode(parse_comworld(source,name,102),'com:getaway')
    light_node=LightDefNode('light_point_linear',0x62,1,None,'lightdef:getaway','falloff_linear','insert')
    table_node=StringTableNode('mp/mapstable.csv',1,1,('mp_getaway',),'stringtable:505')
    raw_node=RawFileNode('mp_getaway',b'','rawfile:766',table_node.cell_symbol(0))
    nodes=(com_node,light_node,table_node,raw_node)
    source_types=(0x0C,0x11,0x20,0x1F)
    dispositions=tuple(
        SourceDisposition(index,source_types[index],DispositionKind.EMITTED,node.symbol,node.type,'C10 combined readback fixture')
        for index,node in enumerate(nodes)
    )
    plan=PortPlan(nodes,(),dispositions,'mp_getaway')
    build=plan.build()
    with tempfile.TemporaryDirectory() as directory:
        path=Path(directory)/'mp_getaway.ff';save_main_fastfile(path,build,verify=True)
        report=inspect_main_fastfile(path,plan,build)
    assert report.passed,report
    bytype={row['type']:row for row in report.family_checks}
    assert bytype[AssetType.COMWORLD.name]['proof']['packed_def_names']==99
    assert bytype[AssetType.LIGHTDEF.name]['proof']['attenuation_external']
    assert bytype[AssetType.RAWFILE.name]['proof']['shared_name']


def main() -> None:
    test_comworld_reuses_one_block4_xstring()
    test_lightdef_external_attenuation_shell_and_alignment()
    test_rawfile_766_inherits_exact_mapstable_xstring_identity()
    test_combined_independent_readback()
    print({'passed':True,'comworld_b4_aliases':99,'lightdef_external_shell_bytes':0x34,'rawfile_766':'mp_getaway'})


if __name__=='__main__':
    main()
