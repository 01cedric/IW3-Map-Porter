"""Validate linked PS3 world references before offering a successful preview.

These are bounds and asset-identity checks, not GPU execution or game startup.
"""
import struct


def validate_aabb_trees(memory, world, zone_ranges):
    """Check the linked records and relative edges consumed by SPU traversal."""
    def read(pointer, count, stride, label):
        size=count*stride
        if not size:
            return b''
        if not pointer or not any(base <= pointer and pointer+size <= base+length
                                  for base,length in zone_ranges):
            raise RuntimeError(f'GfxWorld {label}: array outside declared zone memory')
        return memory.read(pointer,size)

    cell_count=memory.u32(world+0xfc)
    cells=read(memory.u32(world+0x110),cell_count,0x38,'cells')
    model_count=memory.u32(world+0x250)
    total=parents=values=0
    for ci in range(cell_count):
        count,pointer=struct.unpack_from('>II',cells,ci*0x38+0x18)
        records=read(pointer,count,0x28,f'cell {ci} AABB trees')
        total+=count
        for ni in range(count):
            p=ni*0x28
            children=struct.unpack_from('>H',records,p+0x18)[0]
            offset=struct.unpack_from('>i',records,p+0x24)[0]
            if children:
                if offset <= 0 or offset % 0x28:
                    raise RuntimeError(f'GfxWorld cell {ci} AABB {ni}: child offset {offset} '
                                       'does not address a forward 40-byte PS3 record')
                if ni+offset//0x28+children > count:
                    raise RuntimeError(f'GfxWorld cell {ci} AABB {ni}: children exceed cell tree array')
                parents+=1
            sm_count,sm_pointer=struct.unpack_from('>HI',records,p+0x1e)
            indices=read(sm_pointer,sm_count,2,f'cell {ci} AABB {ni} static-model indices')
            if any(v[0] >= model_count for v in struct.iter_unpack('>H',indices)):
                raise RuntimeError(f'GfxWorld cell {ci} AABB {ni}: static-model index out of range')
            values+=sm_count
    return dict(cells=cell_count,nodes=total,parents=parents,static_model_indices=values)


def validate_world(memory, world, inventory, zone_ranges):
    def array(pointer, count, stride, label):
        size = count * stride
        if not size:
            return b''
        if not pointer or not any(base <= pointer and pointer + size <= base + length
                                  for base, length in zone_ranges):
            raise RuntimeError(f'GfxWorld {label}: {count} records at 0x{pointer:08x} '
                               'extend outside declared zone memory')
        return memory.read(pointer, size)

    vertices = memory.u32(world + 0x34)
    index_count = memory.u32(world + 0x10)
    surface_count = memory.u32(world + 0x1c)
    model_count = memory.u32(world + 0x250)
    layer_bytes = memory.u32(world + 0x44)
    array(memory.u32(world + 0x38), vertices, 16, 'vertices')
    array(memory.u32(world + 0x48), layer_bytes, 1, 'vertex layers')
    raw_indices = array(memory.u32(world + 0x14), index_count, 2, 'indices')
    indices = struct.unpack('>' + str(index_count) + 'H', raw_indices)
    surfaces = array(memory.u32(world + 0x29c), surface_count, 52, 'surfaces')
    models = array(memory.u32(world + 0x2a4), model_count, 44, 'static models')
    material_headers = {h for (t, _), h in inventory.items() if t == 4}
    model_headers = {h for (t, _), h in inventory.items() if t == 3}
    for i in range(surface_count):
        layer, first_vertex, minimum, span, triangles, first_index, material = struct.unpack_from(
            '>IIIHHII', surfaces, i * 52)
        if first_index + triangles * 3 > index_count:
            raise RuntimeError(f'GfxWorld surface {i}: triangle range exceeds index buffer')
        values = indices[first_index:first_index + triangles * 3]
        if values and (min(values) < minimum or max(values) >= minimum + span
                       or first_vertex + max(values) >= vertices):
            raise RuntimeError(f'GfxWorld surface {i}: vertex index exceeds its declared range')
        if layer_bytes and values and layer + (minimum + span) * 28 > layer_bytes:
            raise RuntimeError(f'GfxWorld surface {i}: vertex layers exceed buffer')
        if material not in material_headers:
            raise RuntimeError(f'GfxWorld surface {i}: material is not registered')
    for i in range(model_count):
        if struct.unpack_from('>I', models, i * 44 + 32)[0] not in model_headers:
            raise RuntimeError(f'GfxWorld static model {i}: XModel is not registered')
    aabb=validate_aabb_trees(memory,world,zone_ranges)
    return dict(passed=True, surfaces=surface_count, vertices=vertices,
                indices=index_count, static_models=model_count,
                aabb_trees=aabb,
                scope='linked world buffers, AABB traversal and registered references', gpu_executed=False)
