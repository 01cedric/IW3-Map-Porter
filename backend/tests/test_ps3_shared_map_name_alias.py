"""GfxWorld, ClipMap, MapEnts and GameWorldMp share the ComWorld's BSP-path XString.

Retail PS3 mp_shipment writes exactly one map-name string into block 4 - the ComWorld's
inline ``maps/mp/mp_shipment.d3dbsp`` - and every other map asset carries a packed alias to
it:

    ComWorld     +0x00 = FOLLOWING       -> 'maps/mp/mp_shipment.d3dbsp'   (inline, owner)
    GfxWorld     +0x00 = 0x8051D721      -> the same block-4 offset
    GfxWorld     +0x04 = FOLLOWING       -> 'mp_shipment'                  (baseName, inline)
    GameWorldMp  +0x00 = 0x8051D721
    ClipMap      +0x00 = 0x8051D721

The porter used to alias GfxWorld.baseName instead, so all four names resolved to the short
``mp_shipment``.  The zone still loaded - nothing about the stream changes - but
``CM_LoadMap`` resolves the ClipMap by the full BSP path, so the game rejected the map with

    Couldn't find the bsp for this map.  Please build the fast file
    associated with maps/mp/mp_getaway.d3dbsp and try again.

which is exactly what mp_getaway did on hardware once the load-blocking defects were gone.
"""

from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from cod4porter.assets.gfxworld import GfxWorldNode  # noqa: E402
from cod4porter.graph import nested_symbol  # noqa: E402

ASSEMBLER = (ROOT / 'cod4porter' / 'assembler.py').read_text(encoding='utf-8')
GFXWORLD = (ROOT / 'cod4porter' / 'assets' / 'gfxworld.py').read_text(encoding='utf-8')
WORLD_CORE = (ROOT / 'cod4porter' / 'world_core.py').read_text(encoding='utf-8')

# --- the node exposes the alias target, and defaults to the old local behaviour --------
assert 'shared_map_name_symbol' in {f for f in GfxWorldNode.__dataclass_fields__}
assert GfxWorldNode.__dataclass_fields__['shared_map_name_symbol'].default is None
assert "w.register_pointer_relocation(rp,self.shared_map_name_symbol or name," in GFXWORLD
# ...and the short baseName is still written inline behind the root.
assert "w.write_latin1z(q.base_name,'GfxWorld baseName')" in GFXWORLD
assert "w32(b,4,FOLLOWING)" in GFXWORLD

# --- the ComWorld owns the shared string ----------------------------------------------
assert "name_loc=w.define_symbol(self.symbol+'::name');w.write_latin1z(self.source.name,'ComWorld name')" in WORLD_CORE
assert nested_symbol('comworld:x', 'name') == 'comworld:x::name'

# --- and the assembler points every consumer at it ------------------------------------
assert "shared_world_name=nested_symbol(com_sym,'name')" in ASSEMBLER
assert "shared_world_name=nested_symbol(gfx_sym,'name')" not in ASSEMBLER
assert "shared_map_name_symbol=nested_symbol(com_sym,'name')" in ASSEMBLER
# the plane alias is unrelated and still comes from the GfxWorld
assert "shared_world_planes=nested_symbol(gfx_sym,'planes')" in ASSEMBLER
# the two consumers keep taking the shared symbol
assert 'shared_name_symbol=shared_world_name,' in ASSEMBLER
assert 'GameWorldMpNode(core.gfxworld.base_name,shared_world_name,' in ASSEMBLER

# --- the name the engine looks the ClipMap up by ---------------------------------------
def bsp_path(map_name: str) -> str:
    return 'maps/mp/%s.d3dbsp' % map_name


assert bsp_path('mp_shipment') == 'maps/mp/mp_shipment.d3dbsp'
assert bsp_path('mp_getaway') == 'maps/mp/mp_getaway.d3dbsp'
assert bsp_path('mp_getaway') != 'mp_getaway'

print({'skipped': False, 'shared_string_owner': 'ComWorld', 'aliases': 4})
