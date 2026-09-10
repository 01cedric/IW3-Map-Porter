from __future__ import annotations

from dataclasses import replace
from pathlib import Path
import runpy
import struct

from cod4porter.assets.clipmap import ClipMapNode, DYNDEF_SIZE, POSE_SIZE, CLIENT_SIZE, COLLISION_SIZE
from cod4porter.graph import write_graph
from cod4porter.pc_clipmap import DynEnt

ns = runpy.run_path(str(Path(__file__).with_name("test_pc_clipmap.py")))
clip = ns["clip"]

dyn = DynEnt(
    list_kind="model",
    index=0,
    type=0,
    quaternion=(0.0, 0.0, 0.0, 1.0),
    origin=(10.0, 20.0, 30.0),
    xmodel_raw=0,
    brush_model=0,
    physics_brush_model=0,
    destroy_fx_raw=0,
    destroy_pieces_raw=0,
    phys_preset_raw=0,
    health=100,
    center_mass=(0.0, 0.0, 0.0),
    moments=(1.0, 1.0, 1.0),
    products=(0.0, 0.0, 0.0),
    contents=1,
)
node = ClipMapNode(replace(clip, dyn_models=(dyn,)), "clipmap.dyn.test")
graph = write_graph([node])
result = graph.context.results["clipmap:clipmap.dyn.test"]
zone = graph.zone.zone_bytes
root = result["root_physical"]
dyn_root = result["dyn_model_physical"]

assert struct.unpack_from(">H", zone, root + 0xF4)[0] == 1
assert struct.unpack_from(">H", zone, root + 0xF6)[0] == 0
assert struct.unpack_from(">i", zone, dyn_root)[0] == 0
assert struct.unpack_from(">fff", zone, dyn_root + 0x14) == (10.0, 20.0, 30.0)
assert struct.unpack_from(">i", zone, dyn_root + 0x34)[0] == 100
assert len(zone[dyn_root:dyn_root + DYNDEF_SIZE]) == DYNDEF_SIZE

runtime = dict(result["runtime"])
assert set(runtime) == {"modelPose", "modelClient", "modelCollision"}
assert graph.zone.block_sizes[1] >= POSE_SIZE + CLIENT_SIZE + COLLISION_SIZE
assert all("DynEnt" not in dep.description for dep in node.dependencies)

print({
    "passed": True,
    "dyn_models": 1,
    "dyn_brushes": 0,
    "dyn_root": hex(dyn_root),
    "runtime_entries": sorted(runtime),
    "runtime_block_bytes": graph.zone.block_sizes[1],
})
