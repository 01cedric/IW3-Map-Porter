from __future__ import annotations

from pathlib import Path
import sys

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))

from cod4porter.assets.basic import ExternalFxNode
from cod4porter.backend.v4_ffio import FOLLOWING,decode_ps3_pointer,parse_xasset_list
from cod4porter.graph import header_symbol,write_graph


assets=(ExternalFxNode('fx/a','fx.a'),ExternalFxNode('fx/b','fx.b'))
result=write_graph(assets,('one',None,'three'))
zone=result.zone.zone_bytes
parsed=parse_xasset_list(zone,'ps3')

# Raw XAssetList bytes are physically present after the 0x24-byte zone header,
# but consume no logical B4 space. B4 begins with three ScriptString pointer cells.
assert parsed.asset_pool_offset==0x24+0x10+12+len(b'one\0')+len(b'three\0')
header_base=(12+len(b'one\0')+len(b'three\0')+3)&~3
assert result.zone.symbols[header_symbol('fx.a')].offset==header_base+4
for asset,node in zip(parsed.assets,assets):
    logical=header_base+asset.index*8+4
    assert result.zone.symbols[header_symbol(node.symbol)].offset==logical
    decoded=decode_ps3_pointer(result.zone.symbols[header_symbol(node.symbol)].encode(),result.zone.block_sizes)
    assert decoded.block==4 and decoded.offset==logical
    assert asset.serialized_pointer==FOLLOWING

# B4: 12 pointer bytes + ten XString bytes + two logical align bytes +
# 16 XAssetHeader bytes + two six-byte FX names. The raw list root is excluded.
assert result.zone.block_sizes[4]==52
print({'passed':True,'b4':result.zone.block_sizes[4],'first_header_pointer':result.zone.symbols[header_symbol('fx.a')].offset})
